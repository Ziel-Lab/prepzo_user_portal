from flask import request, jsonify, current_app, g
from . import mock_interview_bp
from ..subscription.helpers import require_authentication, get_user_display_name
from ...extensions import supabase, get_user_client, get_admin_client
from ...pagination import MockInterviewPagination
import uuid
import asyncio
import openai
import requests
import PyPDF2
import io
import json
from .api import create_interview_room, get_room_token
from .transcription import get_user_transcriptions, get_session_summary, TranscriptionHandler
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)


def execute_with_retry(query_func, operation_name="database query", max_retries=3):
    """
    Execute a database query with exponential backoff retry logic
    
    Args:
        query_func: Function that returns the query builder (should end with .execute())
        operation_name: Description for logging
        max_retries: Maximum number of retry attempts
    
    Returns:
        Query result
    """
    retry_delay = 1
    
    for attempt in range(max_retries):
        try:
            return query_func().execute()
        except Exception as db_error:
            if attempt < max_retries - 1:  # Not the last attempt
                logger.warning(f"{operation_name} attempt {attempt + 1} failed: {str(db_error)[:100]}... Retrying in {retry_delay}s")
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            else:
                # Last attempt failed, re-raise the error
                logger.error(f"All {max_retries} {operation_name} attempts failed")
                raise db_error



def cleanup_failed_attempt(attempt_id: str, reason: str = "Agent dispatch failed") -> bool:
    """
    Delete failed attempts from database so they don't count as real attempts
    Only handles 'failed' status - system/agent failures that shouldn't count against user
    """
    try:
        admin_client = get_admin_client()
        
        # First verify the attempt exists and has 'failed' status
        check_result = admin_client.table('mock_interview_attempts')\
            .select('id, status, attempt_number, mock_interview_id')\
            .eq('id', attempt_id)\
            .execute()
        
        if not check_result.data:
            logger.warning(f"Attempt {attempt_id} not found for cleanup")
            return False
        
        attempt_data = check_result.data[0]
        current_status = attempt_data.get('status')
        
        # Only cleanup 'failed' status attempts (system failures)
        if current_status != 'failed':
            logger.info(f"Attempt {attempt_id} has status '{current_status}' - not cleaning up (only 'failed' attempts are cleaned)")
            return False
        
        attempt_number = attempt_data.get('attempt_number')
        session_id = attempt_data.get('mock_interview_id')
        
        logger.info(f"Cleaning up failed attempt {attempt_id} (attempt #{attempt_number}) - reason: {reason}")
        
        # Delete the failed attempt from database
        logger.info(f"Attempting to delete attempt {attempt_id} from database...")
        try:
            # Try method 1: Standard delete
            delete_result = admin_client.table('mock_interview_attempts')\
                .delete()\
                .eq('id', attempt_id)\
                .execute()
        except Exception as e1:
            logger.warning(f"Standard delete failed: {e1}")
            try:
                # Try method 2: Delete with count
                delete_result = admin_client.table('mock_interview_attempts')\
                    .delete(count='exact')\
                    .eq('id', attempt_id)\
                    .execute()
            except Exception as e2:
                logger.error(f"All delete methods failed: {e1}, {e2}")
                raise e2
        
        logger.info(f"Delete operation result for {attempt_id}: {delete_result}")
        
        # Verify deletion was successful
        verify_result = admin_client.table('mock_interview_attempts')\
            .select('id')\
            .eq('id', attempt_id)\
            .execute()
        
        logger.info(f"Verification check for {attempt_id}: found {len(verify_result.data)} records")
        
        if len(verify_result.data) == 0:
            logger.info(f"Successfully deleted failed attempt {attempt_id} - this attempt won't count against user's limit")
            
            # Update attempt numbers for remaining attempts in this session
            remaining_attempts_result = admin_client.table('mock_interview_attempts')\
                .select('id, attempt_number')\
                .eq('mock_interview_id', session_id)\
                .order('attempt_number', desc=False)\
                .execute()
            
            if remaining_attempts_result.data:
                # Renumber remaining attempts to fill the gap
                for i, remaining_attempt in enumerate(remaining_attempts_result.data, 1):
                    if remaining_attempt['attempt_number'] != i:
                        admin_client.table('mock_interview_attempts')\
                            .update({'attempt_number': i})\
                            .eq('id', remaining_attempt['id'])\
                            .execute()
                        logger.info(f"Renumbered attempt {remaining_attempt['id']} to #{i}")
            
            return True
        else:
            logger.error(f"Failed to delete attempt {attempt_id} - record still exists in database")
            logger.error(f"Remaining record: {verify_result.data}")
            return False
            
    except Exception as e:
        logger.error(f"Error cleaning up failed attempt {attempt_id}: {e}")
        return False


def get_display_status(status, status_prep):
    """
    Compute user-friendly display status based on status and status_prep
    
    Args:
        status: Current session status (created, active, completed, etc.)
        status_prep: Preparation status (PENDING, DONE)
    
    Returns:
        dict with display_status and is_ready_to_join
    """
    # Handle completed and failed states first
    if status in ['COMPLETED']:
        return {
            'display_status': 'completed',
            'display_text': 'Completed',
            'is_ready_to_join': False,
            'color_class': 'success'
        }
    
    if status in ['failed', 'cancelled']:
        return {
            'display_status': 'failed',
            'display_text': 'Failed' if status == 'failed' else 'Cancelled',
            'is_ready_to_join': False,
            'color_class': 'error'
        }
    
    if status in ['active', 'in-progress']:
        return {
            'display_status': 'active',
            'display_text': 'In Progress',
            'is_ready_to_join': True,
            'color_class': 'active'
        }
    
    # For created/scheduled sessions, check preparation status
    if status in ['created', 'scheduled', 'agent_ready', 'agent_dispatched']:
        if status_prep == 'DONE':
            return {
                'display_status': 'ready',
                'display_text': 'Ready to Start',
                'is_ready_to_join': True,
                'color_class': 'ready'
            }
        else:
            return {
                'display_status': 'preparing',
                'display_text': 'Preparing Interview...',
                'is_ready_to_join': False,
                'color_class': 'preparing'
            }
    
    # Default fallback
    return {
        'display_status': 'unknown',
        'display_text': 'Unknown Status',
        'is_ready_to_join': False,
        'color_class': 'default'
    }

def extract_resume_text_from_url(file_url):
    """Extract text content from resume file URL"""
    try:
        # Download the file
        response = requests.get(file_url, timeout=30)
        response.raise_for_status()
        
        # Get file content
        file_content = response.content
        content_type = response.headers.get('content-type', '').lower()
        
        # Extract text based on file type
        if 'pdf' in content_type:
            return extract_pdf_text(file_content)
        elif 'text' in content_type:
            return file_content.decode('utf-8')
        else:
            logger.warning(f"Unsupported file type for resume extraction: {content_type}")
            return "Unable to extract text from this file type. Please provide resume text manually."
            
    except Exception as e:
        logger.error(f"Error extracting resume text from URL: {str(e)}")
        return f"Error extracting resume content: {str(e)}"

def extract_pdf_text(pdf_content):
    """Extract text from PDF content"""
    try:
        pdf_file = io.BytesIO(pdf_content)
        pdf_reader = PyPDF2.PdfReader(pdf_file)
        
        text = ""
        for page_num in range(len(pdf_reader.pages)):
            page = pdf_reader.pages[page_num]
            text += page.extract_text() + "\n"
        
        return text.strip()
        
    except Exception as e:
        return f"Error extracting PDF content: {str(e)}"

def get_resume_content_by_document_id(user_id, document_id):
    """Get resume content by document ID"""
    try:
        # Get document details using admin client (we pass user_id for filtering)
        try:
            admin_client = get_admin_client()
        except RuntimeError as e:
            return None, f"Server configuration error: {e}"
            
        result = admin_client.table('user_documents')\
            .select('document_url, document_name, document_type')\
            .eq('id', document_id)\
            .eq('uid', user_id)\
            .execute()
        
        if not result.data:
            return None, "Document not found"
        
        document = result.data[0]
        document_url = document['document_url']
        
        # Extract text content
        resume_text = extract_resume_text_from_url(document_url)
        
        return resume_text, None
        
    except Exception as e:
        logger.error(f"Error getting resume content: {str(e)}")
        return None, str(e)

def get_cover_letter_content_by_document_id(user_id, document_id):
    """Get cover letter content by document ID"""
    try:
        # Get document details using admin client (we pass user_id for filtering)
        try:
            admin_client = get_admin_client()
        except RuntimeError as e:
            return None, f"Server configuration error: {e}"
            
        result = admin_client.table('user_documents')\
            .select('document_url, document_name, document_type')\
            .eq('id', document_id)\
            .eq('uid', user_id)\
            .execute()
        
        if not result.data:
            return None, "Document not found"
        
        document = result.data[0]
        document_url = document['document_url']
        
        # Extract text content (reuse the same extraction logic as resume)
        cover_letter_text = extract_resume_text_from_url(document_url)
        
        return cover_letter_text, None
        
    except Exception as e:
        logger.error(f"Error getting cover letter content: {str(e)}")
        return None, str(e)







@mock_interview_bp.route('/create-session', methods=['POST'])
@require_authentication
def create_interview_session():
    """Create a new mock interview session with enhanced context"""
    try:
        data = request.get_json()
        

        
        # Get user's plan information
        admin_client = get_admin_client()
        user_id = g.user.id
        
        # Get user's current plan and usage
        usage_result = admin_client.table('feature_usage')\
            .select('plan_id, mock_interview_session_lifetime_count')\
            .eq('user_id', user_id)\
            .execute()
        
        if not usage_result.data:
            # Create default feature usage record
            default_usage = {
                'user_id': user_id,
                'plan_id': 1,  # Default to free plan
                'mock_interview_session_lifetime_count': 0
            }
            admin_client.table('feature_usage').insert(default_usage).execute()
            usage_data = default_usage
            current_sessions = 0
        else:
            usage_data = usage_result.data[0]
            current_sessions = usage_data.get('mock_interview_session_lifetime_count', 0)
        
        plan_id = usage_data.get('plan_id', 1)
        
        # Get plan limits
        plan_result = admin_client.table('subscription_plans')\
            .select('mock_interview_session')\
            .eq('id', plan_id)\
            .execute()
        
        if plan_result.data:
            session_limit = plan_result.data[0].get('mock_interview_session', 0)
        else:
            # Exact limits for each plan_id if plan not found in database
            if plan_id == 1:
                session_limit = 0  # Free plan: 0 sessions
            elif plan_id == 2:
                session_limit = 3  # Pro plan: 3 sessions
            elif plan_id == 3:
                session_limit = 999999999  # Premium plan: unlimited sessions
            else:
                session_limit = 0  # Unknown plan: no sessions
        
        # Check if plan has unlimited sessions (only >1000 sessions counts as unlimited)
        is_unlimited = session_limit is not None and session_limit > 1000
        
        # No special handling - use exact database values for all plans
        
        # Allow all users to create sessions regardless of plan limits
        
        # Basic interview parameters - extract from request data
        interview_type = data.get('type') or data.get('interview_type', 'behavioral')
        difficulty_level = data.get('difficulty_level', 'medium')
        position = data.get('role') or data.get('position', 'Software Engineer')
        duration_minutes = 15  # Always 15 minutes regardless of input
               
        # Enhanced context data from form - handle both frontend formats
        title = data.get('title', 'Mock Interview Session')
        resume_text = data.get('resume_text', '')
        # Handle both resumeUrl (NewSessionModal) and resume_url formats
        resume_url = data.get('resumeUrl') or data.get('resume_url', '')
        resume_document_id = data.get('resume_document_id')
        
        # Cover letter handling (optional)
        cover_letter_text = data.get('cover_letter_text', '')
        cover_letter_url = data.get('coverLetterUrl') or data.get('cover_letter_url', '')
        cover_letter_document_id = data.get('cover_letter_document_id')
        
        # Handle both jobDescription (NewSessionModal) and job_description formats  
        job_description = data.get('jobDescription') or data.get('job_description', '')
        
        
        company_url = data.get('company_url', '') or data.get('company_name', '') or data.get('company', '') or data.get('companyUrl', '')
        company_name = company_url  # Store URL directly as company_name
        

        

        
        # Handle both description (NewSessionModal) and custom_instructions formats
        custom_instructions = data.get('description') or data.get('custom_instructions', '')
        
        # Normalize interview type (handle both hyphen and underscore formats)
        normalized_interview_type = interview_type.replace('-', '_')
        
        # Allow any interview type - no validation needed
        # Use normalized type for consistency
        interview_type = normalized_interview_type
        
        # Handle document processing and text extraction
        # Priority order: document_id > explicit URL > provided text > auto-fetch most recent
        
        if resume_document_id:
            # Priority 1: User selected a specific document by ID
            extracted_resume_text, error = get_resume_content_by_document_id(g.user.id, resume_document_id)
            if error:
                return jsonify({
                    'error': f'Failed to extract resume content: {error}'
                }), 400
            resume_text = extracted_resume_text
            
            # Get the exact document URL as stored in the database
            try:
                admin_client = get_admin_client()
                doc_result = admin_client.table('user_documents')\
                    .select('document_url')\
                    .eq('id', resume_document_id)\
                    .eq('uid', g.user.id)\
                    .execute()
                if doc_result.data:
                    resume_url = doc_result.data[0]['document_url']
            except Exception as e:
                logger.warning(f"Failed to get document URL for resume_document_id {resume_document_id}: {e}")
        
        elif resume_url and not resume_text:
            # Priority 2: User provided resume URL but no text - extract it
            try:
                resume_text = extract_resume_text_from_url(resume_url) or "Resume content extraction in progress..."
            except Exception as e:
                logger.warning(f"Could not extract resume text from provided URL: {e}")
                resume_text = "Resume uploaded - content extraction in progress..."
        
        elif not resume_text and not resume_url:
            # Priority 3: No resume data provided at all - auto-fetch most recent
            try:
                admin_client = get_admin_client()
                resume_result = admin_client.table('user_documents')\
                    .select('id, document_name, document_url')\
                    .eq('uid', g.user.id)\
                    .in_('document_type', ['resume', 'Resume', 'CV', 'cv'])\
                    .order('created_at', desc=True)\
                    .limit(1)\
                    .execute()
                
                if resume_result.data:
                    resume_doc = resume_result.data[0]
                    resume_url = resume_doc['document_url']
                    resume_document_id = resume_doc['id']
                    
                    # Extract text from resume
                    try:
                        resume_text = extract_resume_text_from_url(resume_url) or "Resume content extraction in progress..."
                    except Exception as e:
                        logger.warning(f"Could not extract resume text: {e}")
                        resume_text = "Resume uploaded - content extraction in progress..."
                else:
                    return jsonify({
                        'error': 'No resume found. Please upload a resume before creating an interview session.'
                    }), 400
                    
            except Exception as e:
                logger.error(f"Error fetching resume: {e}")
                return jsonify({
                    'error': 'Failed to fetch resume. Please try again.'
                }), 400
        
        # Handle cover letter content (optional)
        if cover_letter_document_id:
            # User selected a specific cover letter document by ID
            extracted_cover_letter_text, error = get_cover_letter_content_by_document_id(g.user.id, cover_letter_document_id)
            if error:
                logger.warning(f"Failed to extract cover letter content: {error}")
                cover_letter_text = "Cover letter extraction failed"
            else:
                cover_letter_text = extracted_cover_letter_text
            
            # Get the cover letter document URL
            try:
                admin_client = get_admin_client()
                cover_doc_result = admin_client.table('user_documents')\
                    .select('document_url')\
                    .eq('id', cover_letter_document_id)\
                    .eq('uid', g.user.id)\
                    .execute()
                if cover_doc_result.data:
                    cover_letter_url = cover_doc_result.data[0]['document_url']
            except Exception as e:
                logger.warning(f"Failed to get document URL for cover_letter_document_id {cover_letter_document_id}: {e}")
                
        elif cover_letter_url and not cover_letter_text:
            # User provided cover letter URL but no text - extract it
            try:
                cover_letter_text = extract_resume_text_from_url(cover_letter_url) or "Cover letter content extraction in progress..."
            except Exception as e:
                logger.warning(f"Could not extract cover letter text from provided URL: {e}")
                cover_letter_text = "Cover letter uploaded - content extraction in progress..."
       
        
        if not job_description or len(job_description.strip()) < 10:
            logger.error(f"Job description validation failed: '{job_description}'")
            return jsonify({
                'error': 'Job description is required and must be at least 10 characters'
            }), 400
        
        # Get user's display name
        user_display_name = get_user_display_name(g.user)
        
        # Generate unique session ID
        session_id = str(uuid.uuid4())
        # Try different room name format to avoid URL validation
        room_name = f"session-{session_id}"  # Use hyphen instead of underscore
        
        # Create LiveKit room
        room_response = asyncio.run(create_interview_room(room_name))
        if not room_response:
            logger.error("Failed to create LiveKit room")
            return jsonify({'error': 'Failed to create interview room'}), 500
        
        # Prepare interview context for AI agent (ensure JSON serializable)
        interview_context = {
            'resume_text': resume_text or '',
            'resume_url': resume_url or '',
            'cover_letter_text': cover_letter_text or '',
            'cover_letter_url': cover_letter_url or '',
            'job_description': job_description or '',
            'company_name': company_name or '',
            'company_url': company_url or '',
            'position': position or '',
            'interview_type': interview_type or '',
            'difficulty_level': difficulty_level or '',
            'custom_instructions': custom_instructions or ''
        }
        
        # Store session in database (matching mock_interview table schema)
        session_data = {
            'id': session_id,
            'user_id': g.user.id,
            'title': title or 'Mock Interview Session',
            'interview_type': interview_type,
            'difficulty_level': difficulty_level,
            'position': position,
            'company_name': company_name or '',  # Store the company URL in company_name column
            'duration_minutes': duration_minutes,  # Always 15 minutes
            'resume_url': resume_url if resume_url else None,  # Ensure null if empty
            'resume_document_id': resume_document_id if resume_document_id else None,
            'cover_letter_url': cover_letter_url if cover_letter_url else None,  # New column
            'cover_letter_text': cover_letter_text if cover_letter_text else None,  # New column
            'job_description': job_description,
            'custom_instructions': custom_instructions or '',
            'room_name': room_name,
            'status': 'created',
            'resume_text': resume_text or '',
            'display_name': user_display_name,
            'status_prep': 'PENDING'  # NEW: Triggers n8n webhook workflow
        }
        
       
        # Try to get admin client with comprehensive error handling
        admin_client = None
        try:
            admin_client = get_admin_client()
        except RuntimeError as e:
            logger.error(f"Admin client not available via function: {e}")
            # Try fallback via app extensions
            try:
                admin_client = current_app.extensions.get('supabase_admin')
            except Exception as fallback_e:
                logger.error(f"Fallback also failed: {fallback_e}")
        except Exception as e:
            logger.error(f"Unexpected error getting admin client: {e}")
        
        if admin_client is None:
            logger.error("No admin client available - server configuration error")
            return jsonify({'error': 'Server configuration error - database not available'}), 500
            
        try:
            # Simple essential insert approach
            essential_data = {
                'id': session_id,
                'user_id': str(g.user.id),
                'room_name': room_name,
                'status_prep': 'PENDING'
            }
            result = admin_client.table('mock_interview').insert(essential_data).execute()
            
            # If successful, add all other fields via update
            if result.data:
                update_data = {
                    'title': title or 'Mock Interview Session',
                    'interview_type': interview_type,
                    'difficulty_level': difficulty_level,
                    'position': position,
                    'company_name': company_name or '',
                    'duration_minutes': duration_minutes,  # Always 15 minutes
                    'resume_document_id': resume_document_id if resume_document_id else None,
                    'cover_letter_text': cover_letter_text if cover_letter_text else None,
                    'job_description': job_description,
                    'custom_instructions': custom_instructions or '',
                    'status': 'created',
                    'resume_text': resume_text or '',
                    'display_name': user_display_name,
                    'interview_context': interview_context
                }
        
                
                try:
                    result = admin_client.table('mock_interview').update(update_data).eq('id', session_id).execute()
                except Exception as main_update_error:
                    logger.error(f"Main update failed: {main_update_error}")
                    logger.error(f"Update data that failed: {update_data}")
                    # Continue with individual field updates
                    
                    # Now try to add resume_url and cover_letter_url separately (using exact URLs from documents)
                    if resume_url:
                        try:
                            admin_client.table('mock_interview').update({'resume_url': resume_url}).eq('id', session_id).execute()
                        except Exception as resume_error:
                            logger.warning(f"Failed to update resume_url (proceeding anyway): {resume_error}")
                            logger.warning(f"Resume URL that failed: {resume_url}")
                            # Session still works without resume_url
                    
                    if cover_letter_url:
                        try:
                            admin_client.table('mock_interview').update({'cover_letter_url': cover_letter_url}).eq('id', session_id).execute()
                        except Exception as cover_letter_error:
                            logger.warning(f"Failed to update cover_letter_url (proceeding anyway): {cover_letter_error}")
                            logger.warning(f"Cover letter URL that failed: {cover_letter_url}")
                            # Session still works without cover_letter_url
                    
                    # Update company_name separately to ensure it's saved
                    if company_name:
                        try:

                            admin_client.table('mock_interview').update({'company_name': company_name}).eq('id', session_id).execute()
                        except Exception as company_error:
                            logger.error(f"Failed to update company_name: {company_error}")
                            logger.error(f"Company name that failed: {company_name}")
                    else:
                        logger.warning("company_name is empty, skipping company_name update")

                            
                except Exception as update_error:
                    logger.warning(f"Failed to update additional fields: {update_error}")
                    # Try to add URLs even if other fields failed (using exact document URLs)
                    if resume_url:
                        try:
                            admin_client.table('mock_interview').update({'resume_url': resume_url}).eq('id', session_id).execute()
                        except Exception as resume_error:
                            logger.warning(f"Failed to update resume_url in fallback: {resume_error}")
                            logger.warning(f"Problematic resume URL: {resume_url}")
                    
                    if cover_letter_url:
                        try:
                            admin_client.table('mock_interview').update({'cover_letter_url': cover_letter_url}).eq('id', session_id).execute()
                        except Exception as cover_letter_error:
                            logger.warning(f"Failed to update cover_letter_url in fallback: {cover_letter_error}")
                            logger.warning(f"Problematic cover letter URL: {cover_letter_url}")
                    
                    # Fallback: Update company_name separately
                    if company_name:
                        try:

                            admin_client.table('mock_interview').update({'company_name': company_name}).eq('id', session_id).execute()

                        except Exception as company_error:
                            logger.error(f"Failed to update company_name in fallback: {company_error}")
                            logger.error(f"Problematic company name: {company_name}")
                    else:
                        logger.warning("company_name is empty in fallback, skipping company_name update")

                    # Continue anyway, basic session was created
            
            # Final success check
            if result.data:
                # Update feature usage counter for session creation
                try:
                    admin_client.table('feature_usage')\
                        .update({
                            'mock_interview_session_lifetime_count': current_sessions + 1,
                            'updated_at': 'now()'
                        })\
                        .eq('user_id', user_id)\
                        .execute()
                except Exception as counter_error:
                    logger.warning(f"Failed to update session counter: {counter_error}")
                    # Don't fail the session creation for counter issues
                return jsonify({
                    'session_id': session_id,
                    'room_name': room_name,
                    'interview_type': interview_type,
                    'position': position,
                    'company_name': company_name,  # Contains the company URL
                    'company_url': company_url,    # Same as company_name for frontend compatibility
                    'duration_minutes': duration_minutes,  # Always 15 minutes
                    'status': 'created',
                    'context_loaded': bool(resume_text or resume_url),
                    'resume_auto_extracted': bool(resume_document_id),
                    'resume_character_count': len(resume_text) if resume_text else 0,
                    'cover_letter_included': bool(cover_letter_text or cover_letter_url),
                    'cover_letter_auto_extracted': bool(cover_letter_document_id),
                    'cover_letter_character_count': len(cover_letter_text) if cover_letter_text else 0
                }), 201
            else:
                logger.error(f"Failed to create session in database: {result}")
                return jsonify({'error': 'Failed to create session'}), 500
                    
        except Exception as insert_error:
            logger.error(f"Database insert process failed: {insert_error}")
            raise Exception(f"Cannot insert into mock_interview table: {insert_error}")
            
    except Exception as e:
        import traceback
        logger.error(f"Error creating interview session: {str(e)}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/join/<session_id>', methods=['GET'])
@require_authentication
def join_interview_session(session_id):
    """Get room credentials to join an interview session - creates new attempt if needed"""
    try:
        # Verify session belongs to user using admin client with explicit user filtering
        try:
            admin_client = get_admin_client()
        except RuntimeError as e:
            logger.error(f"Admin client not available: {e}")
            return jsonify({'error': 'Server configuration error'}), 500
            
        result = admin_client.table('mock_interview')\
            .select('*')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not result.data:
            return jsonify({'error': 'Session not found'}), 404
        
        session = result.data[0]
        
        # Check existing attempts for this session with full details for recovery
        attempts_result = admin_client.table('mock_interview_attempts')\
            .select('id, attempt_number, status, room_name, started_at, updated_at')\
            .eq('mock_interview_id', session_id)\
            .order('attempt_number', desc=True)\
            .execute()
        
        existing_attempts = attempts_result.data if attempts_result.data else []
        
        # Check for stuck attempts and orphaned sessions - improved logic
        from datetime import datetime, timedelta
        current_time = datetime.utcnow()
        
        for attempt in existing_attempts:
            if attempt['status'] == 'active':
                should_recover = False
                recovery_reason = ""
                
                if attempt.get('started_at'):
                    try:
                        started_at = datetime.fromisoformat(attempt['started_at'].replace('Z', '+00:00'))
                        # Reduced timeout to 10 minutes for better UX
                        if current_time - started_at.replace(tzinfo=None) > timedelta(minutes=10):
                            should_recover = True
                            recovery_reason = f"timeout (>10min) - started at {started_at}"
                    except Exception as date_error:
                        logger.warning(f"Invalid started_at date for attempt {attempt['id']}: {date_error}")
                        should_recover = True
                        recovery_reason = "invalid started_at timestamp"
                else:
                    # No started_at means the session was created but never properly started
                    should_recover = True
                    recovery_reason = "no started_at timestamp"
                
                # Additional check: verify if LiveKit room actually exists
                if not should_recover and attempt.get('room_name'):
                    try:
                        from .api import get_room_participants
                        participants = asyncio.run(get_room_participants(attempt['room_name']))
                        # If room has no participants, it's effectively dead
                        if len(participants) == 0:
                            should_recover = True
                            recovery_reason = "room exists but has no participants"
                    except Exception as room_check_error:
                        logger.warning(f"Could not check room status for {attempt['room_name']}: {room_check_error}")
                        # If we can't check the room, assume it's dead after 5 minutes
                        if attempt.get('started_at'):
                            try:
                                started_at = datetime.fromisoformat(attempt['started_at'].replace('Z', '+00:00'))
                                if current_time - started_at.replace(tzinfo=None) > timedelta(minutes=5):
                                    should_recover = True
                                    recovery_reason = "room check failed and >5min elapsed"
                            except:
                                should_recover = True
                                recovery_reason = "room check failed and invalid timestamp"
                
                if should_recover:
                    logger.warning(f"Recovering stuck attempt {attempt['id']} - reason: {recovery_reason}")
                    
                    try:
                        # Mark stuck attempt as failed
                        admin_client.table('mock_interview_attempts')\
                            .update({
                                'status': 'failed',
                                'completed_at': 'now()',
                                'updated_at': 'now()'
                            })\
                            .eq('id', attempt['id'])\
                            .execute()
                        
                        # Try to cleanup the stuck room
                        if attempt.get('room_name'):
                            try:
                                from .api import delete_room
                                cleanup_success = asyncio.run(delete_room(attempt['room_name']))
                                if cleanup_success:
                                    logger.info(f"Cleaned up stuck room: {attempt['room_name']}")
                            except Exception as cleanup_error:
                                logger.warning(f"Failed to cleanup stuck room {attempt['room_name']}: {cleanup_error}")
                        
                        # Cleanup the failed attempt so it doesn't count against user's limit
                        cleanup_success = cleanup_failed_attempt(attempt['id'], f"Stuck session recovery - {recovery_reason}")
                        if cleanup_success:
                            logger.info(f"Cleaned up stuck attempt {attempt['id']} - removed from database")
                            # Remove from local list since it's deleted
                            existing_attempts.remove(attempt)
                        else:
                            # Update local attempt status if cleanup failed
                            attempt['status'] = 'failed'
                            logger.warning(f"Could not cleanup stuck attempt {attempt['id']} - marked as failed")
                        
                        logger.info(f"Successfully recovered stuck attempt {attempt['id']}")
                        
                    except Exception as recovery_error:
                        logger.error(f"Error recovering stuck attempt {attempt['id']}: {recovery_error}")
        
        # Refresh attempts list after recovery
        completed_or_failed_attempts = [a for a in existing_attempts if a['status'] in ['COMPLETED', 'failed']]
        active_attempts = [a for a in existing_attempts if a['status'] == 'active']
        
        # Check if we can create a new attempt (max 3 attempts per session)
        total_attempts = len(existing_attempts)
        if total_attempts >= 3:
            return jsonify({
                'error': 'Maximum attempts reached for this session (3/3)',
                'attempts_used': total_attempts,
                'max_attempts': 3,
                'completed_attempts': len(completed_or_failed_attempts),
                'active_attempts': len(active_attempts)
            }), 403
        
        # Check if there's already an active attempt
        if active_attempts:
            return jsonify({
                'error': 'There is already an active interview session for this mock interview',
                'active_attempt': active_attempts[0],
                'suggestion': 'Please wait for the current session to complete or contact support'
            }), 409
        
        next_attempt_number = len(existing_attempts) + 1
        
        # Create new attempt
        attempt_room_name = f"interview_{session_id}_attempt_{next_attempt_number}"
        new_attempt = {
            'mock_interview_id': session_id,
            'attempt_number': next_attempt_number,
            'room_name': attempt_room_name,
            'status': 'pending'
        }
        
        attempt_result = admin_client.table('mock_interview_attempts')\
            .insert(new_attempt)\
            .execute()
        
        if not attempt_result.data:
            return jsonify({'error': 'Failed to create interview attempt'}), 500
        
        # Generate room token for the attempt room
        token = asyncio.run(get_room_token(attempt_room_name, g.user.id))
        if not token:
            # Mark the attempt as failed since room token generation failed
            attempt_id = attempt_result.data[0]['id']
            admin_client.table('mock_interview_attempts')\
                .update({
                    'status': 'failed',
                    'completed_at': 'now()',
                    'updated_at': 'now()'
                })\
                .eq('id', attempt_id)\
                .execute()
            
            logger.error(f"Failed to generate room token for attempt {attempt_id} - marked as failed")
            
            # Cleanup the failed attempt so it doesn't count against user's limit
            cleanup_success = cleanup_failed_attempt(attempt_id, "Room token generation failed - agent dispatch failure")
            if cleanup_success:
                logger.info(f"Cleaned up failed attempt {attempt_id} - user can retry without penalty")
            else:
                logger.warning(f"Failed to cleanup attempt {attempt_id} - user may need manual assistance")
            
            return jsonify({'error': 'Failed to generate room token'}), 500
        
        # Update session status and attempt status
        admin_client.table('mock_interview')\
            .update({'status': 'active', 'updated_at': 'now()'})\
            .eq('id', session_id)\
            .execute()
        

        attempt_update_result = admin_client.table('mock_interview_attempts')\
            .update({'status': 'active', 'started_at': 'now()'})\
            .eq('id', attempt_result.data[0]['id'])\
            .execute()
        
        if attempt_update_result.data:
            logger.info(f"Successfully set started_at for attempt {attempt_result.data[0]['id']}")
        else:
            logger.error(f"Failed to set started_at for attempt {attempt_result.data[0]['id']}")
        
        # Note: Attempt counter tracking removed since column doesn't exist in database
        
        # Update session object with new status and compute display status
        session['status'] = 'active'
        display_info = get_display_status(
            session.get('status', 'created'),
            session.get('status_prep', 'PENDING')
        )
        
        session_with_display = {
            **session,
            'display_status': display_info['display_status'],
            'display_text': display_info['display_text'],
            'is_ready_to_join': display_info['is_ready_to_join'],
            'color_class': display_info['color_class']
        }
        
        
        return jsonify({
            'token': token,
            'room_name': attempt_room_name,
            'livekit_url': current_app.config.get('LIVEKIT_URL'),
            'session': session_with_display,
            'attempt': {
                'id': attempt_result.data[0]['id'],
                'attempt_number': next_attempt_number,
                'total_attempts': len(existing_attempts) + 1,
                'max_attempts': 3
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error joining interview session: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/session/<session_id>', methods=['GET'])
@require_authentication
def get_session_data(session_id):
    """Get session data with attempts summary"""
    try:
        # Verify session belongs to user using admin client with explicit user filtering
        try:
            admin_client = get_admin_client()
        except RuntimeError as e:
            logger.error(f"Admin client not available: {e}")
            return jsonify({'error': 'Server configuration error'}), 500
            
        result = admin_client.table('mock_interview')\
            .select('*')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not result.data:
            return jsonify({'error': 'Session not found'}), 404
        
        session = result.data[0]
        
        # Get attempts summary for this session
        attempts_result = admin_client.table('mock_interview_attempts')\
            .select('id, attempt_number, status, started_at, completed_at, actual_duration_minutes, evaluation_score')\
            .eq('mock_interview_id', session_id)\
            .order('attempt_number', desc=False)\
            .execute()
        
        attempts = attempts_result.data if attempts_result.data else []
        
        # Compute user-friendly display status
        display_info = get_display_status(
            session.get('status', 'created'),
            session.get('status_prep', 'PENDING')
        )
        
        # Add display status information to session
        session_with_display = {
            **session,
            'display_status': display_info['display_status'],
            'display_text': display_info['display_text'],
            'is_ready_to_join': display_info['is_ready_to_join'],
            'color_class': display_info['color_class'],
            'attempts_summary': {
                'total_attempts': len(attempts),
                'max_attempts': 3,
                'remaining_attempts': max(0, 3 - len(attempts)),
                'attempts': attempts
            }
        }
        
        return jsonify({
            'session': session_with_display
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching session data: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500


@mock_interview_bp.route('/user-limits', methods=['GET'])
@require_authentication
def get_user_mock_interview_limits():
    """Get user's mock interview limits and current usage based on their subscription plan"""
    try:
        admin_client = get_admin_client()
        user_id = g.user.id
        
        # Get user's current plan and usage from feature_usage table
        usage_result = admin_client.table('feature_usage')\
            .select('plan_id, mock_interview_session_lifetime_count')\
            .eq('user_id', user_id)\
            .execute()
        
        if not usage_result.data:
            # Create default feature usage record if doesn't exist
            default_usage = {
                'user_id': user_id,
                'plan_id': 1,  # Default to free plan
                'mock_interview_session_lifetime_count': 0
            }
            admin_client.table('feature_usage').insert(default_usage).execute()
            usage_data = default_usage
        else:
            usage_data = usage_result.data[0]
        
        plan_id = usage_data.get('plan_id', 1)
        
        # Get plan limits from subscription_plans table
        plan_result = admin_client.table('subscription_plans')\
            .select('mock_interview_session, mock_interview_attempts')\
            .eq('id', plan_id)\
            .execute()
        
        if not plan_result.data:
            logger.warning(f"No subscription plan found for plan_id {plan_id}")
            # Exact limits for each plan_id if plan not found in database
            if plan_id == 1:
                plan_limits = {'mock_interview_session': 0, 'mock_interview_attempts': 0}  # Free plan
            elif plan_id == 2:
                plan_limits = {'mock_interview_session': 3, 'mock_interview_attempts': 3}  # Pro plan
            elif plan_id == 3:
                plan_limits = {'mock_interview_session': 999999999, 'mock_interview_attempts': 3}  # Premium plan
            else:
                plan_limits = {'mock_interview_session': 0, 'mock_interview_attempts': 0}  # Unknown plan
        else:
            plan_limits = plan_result.data[0]
        
        # Count current sessions for this user
        sessions_result = admin_client.table('mock_interview')\
            .select('id')\
            .eq('user_id', user_id)\
            .execute()
        
        current_sessions = len(sessions_result.data) if sessions_result.data else 0
        
        # Determine if plan has unlimited sessions (only >1000 sessions counts as unlimited)
        session_limit = plan_limits.get('mock_interview_session', 0)
        is_unlimited = session_limit is not None and session_limit > 1000
        
        # Prepare response
        response_data = {
            'plan_id': plan_id,
            'session_limit': session_limit if not is_unlimited else None,
            'sessions_used': current_sessions,
            'sessions_remaining': 999999999 if is_unlimited else max(0, session_limit - current_sessions),
            'attempts_per_session': plan_limits.get('mock_interview_attempts', 3),
            'is_unlimited_sessions': is_unlimited,
            'can_create_session': True,  # Always allow session creation
            'plan_name': 'Free' if plan_id == 1 else ('Pro' if plan_id == 2 else 'Premium')
        }
        
        # Always allow session creation - button always appears
        response_data['can_create_session'] = True
        
        return jsonify(response_data), 200
        
    except Exception as e:
        logger.error(f"Error getting user mock interview limits: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500



@mock_interview_bp.route('/sessions', methods=['GET'])
@require_authentication
def get_user_sessions():
    """Get user's interview sessions with cursor pagination - FIXED"""
    try:
        admin_client = get_admin_client()
        
        # Get paginated sessions using fixed pagination
        sessions, pagination_metadata = MockInterviewPagination.paginate_user_sessions(
            admin_client, 
            g.user.id, 
            request.args
        )
        
        # Add display status to each session
        for session in sessions:
            try:
                display_info = get_display_status(
                    session.get('status', 'created'),
                    session.get('status_prep', 'PENDING')
                )
                
                session['display_status'] = display_info['display_status']
                session['display_text'] = display_info['display_text']
                session['is_ready_to_join'] = display_info['is_ready_to_join']
                session['color_class'] = display_info['color_class']
                
            except Exception:
                # Fallback to basic status
                session['display_status'] = 'unknown'
                session['display_text'] = 'Unknown Status'
                session['is_ready_to_join'] = False
                session['color_class'] = 'default'
        
        return jsonify({
            'sessions': sessions,
            'pagination': pagination_metadata
        }), 200
        
    except Exception as e:
        logger.error(f"Sessions API error: {str(e)}")
        return jsonify({'error': 'Unable to load sessions'}), 500

@mock_interview_bp.route('/sessions/mobile', methods=['GET'])
@require_authentication
def get_user_sessions_mobile():
    """Mobile optimized endpoint with cursor pagination - FIXED"""
    try:
        admin_client = get_admin_client()
        
        # Get paginated sessions using fixed pagination
        sessions, pagination_metadata = MockInterviewPagination.paginate_user_sessions(
            admin_client, 
            g.user.id, 
            request.args
        )
        
        # Create mobile-optimized session data
        mobile_sessions = []
        for session in sessions:
            try:
                display_info = get_display_status(
                    session.get('status', 'created'),
                    session.get('status_prep', 'PENDING')
                )
                
                mobile_session = {
                    'id': session['id'],
                    'title': session.get('title', 'Interview Session'),
                    'position': session.get('position', 'Position'),
                    'company_name': session.get('company_name', ''),
                    'interview_type': session.get('interview_type', 'behavioral'),
                    'created_at': session['created_at'],
                    'display_status': display_info['display_status'],
                    'display_text': display_info['display_text'],
                    'is_ready_to_join': display_info['is_ready_to_join'],
                    'color_class': display_info['color_class']
                }
                mobile_sessions.append(mobile_session)
                
            except Exception:
                # Basic fallback for mobile
                mobile_sessions.append({
                    'id': session['id'],
                    'title': session.get('title', 'Interview Session'),
                    'position': session.get('position', 'Position'),
                    'created_at': session['created_at'],
                    'display_status': 'unknown',
                    'is_ready_to_join': False
                })
        
        return jsonify({
            'sessions': mobile_sessions,
            'pagination': pagination_metadata
        }), 200
        
    except Exception as e:
        logger.error(f"Mobile sessions API error: {str(e)}")
        return jsonify({'error': 'Unable to load sessions'}), 500

@mock_interview_bp.route('/sessions/verify', methods=['GET'])
@require_authentication
def verify_pagination():
    """Quick verification that pagination is working - REMOVE AFTER TESTING"""
    try:
        admin_client = get_admin_client()
        
        # Count total sessions
        count_result = admin_client.table('mock_interview')\
            .select('id', count='exact')\
            .eq('user_id', g.user.id)\
            .execute()
        
        total_count = getattr(count_result, 'count', 0)
        
        # Get first page with limit 5 for testing
        sessions, pagination = MockInterviewPagination.paginate_user_sessions(
            admin_client, g.user.id, {'limit': '5'}
        )
        
        return jsonify({
            'verification': {
                'total_sessions_in_db': total_count,
                'first_5_sessions_count': len(sessions),
                'has_more': pagination.get('has_more', False),
                'next_cursor_exists': 'next_cursor' in pagination,
                'pagination_working': total_count > 5 and pagination.get('has_more', False),
                'status': 'WORKING' if (total_count > 5 and pagination.get('has_more', False)) else 'CHECK_NEEDED'
            },
            'pagination_metadata': pagination
        }), 200
        
    except Exception as e:
        return jsonify({'verification_error': str(e)}), 500


@mock_interview_bp.route('/webhook/interview-completed', methods=['POST'])
def interview_completed_webhook():
    """Webhook endpoint to receive interview completion data"""
    try:
        data = request.get_json()
        
        # Extract webhook data
        session_id = data.get('session_id')
        room_name = data.get('room_name')
        transcript = data.get('transcript', '')
        duration = data.get('duration', 0)
        participant_data = data.get('participant_data', {})
        
        if not session_id:
            return jsonify({'error': 'session_id is required'}), 400
        
        # Verify session exists
        admin_client = get_admin_client()
        result = admin_client.table('mock_interview')\
            .select('*')\
            .eq('id', session_id)\
            .execute()
        
        if not result.data:
            return jsonify({'error': 'Session not found'}), 404
        
        session = result.data[0]
        
        # Process transcript and generate SWOT analysis
        swot_analysis = asyncio.run(generate_swot_analysis(session, transcript))
        
        # Update session with completion data
        update_data = {
            'status': 'COMPLETED',
            'ended_at': 'now()',
            'updated_at': 'now()',
            'transcript': transcript,
            'duration_actual': duration,
            'swot_analysis': swot_analysis,
            'participant_data': participant_data
        }
        
        result = admin_client.table('mock_interview')\
            .update(update_data)\
            .eq('id', session_id)\
            .execute()
        
        if result.data:
            logger.info(f"Interview session {session_id} completed successfully")
            return jsonify({
                'status': 'success',
                'session_id': session_id,
                'swot_analysis': swot_analysis
            }), 200
        else:
            logger.error(f"Failed to update session {session_id}")
            return jsonify({'error': 'Failed to update session'}), 500
            
    except Exception as e:
        logger.error(f"Error processing interview completion webhook: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/session/<session_id>/attempts', methods=['GET'])
@require_authentication
def get_session_attempts(session_id):
    """Get all attempts for a specific interview session with complete details"""
    try:
        admin_client = get_admin_client()
        
     
        
        # Verify session belongs to user with retry logic
        session_result = execute_with_retry(
            lambda: admin_client.table('mock_interview')\
                .select('id, title, interview_type, position, company_name')\
                .eq('id', session_id)\
                .eq('user_id', g.user.id),
            f"session verification for {session_id}"
        )
        
        if not session_result.data:
            logger.warning(f"Session {session_id} not found for user {g.user.id[:8]}***")
            return jsonify({'error': 'Session not found'}), 404
        
        session_info = session_result.data[0]

        
        # Get all attempts for this session with retry logic
        attempts_result = execute_with_retry(
            lambda: admin_client.table('mock_interview_attempts')\
                .select('id, attempt_number, room_name, status, actual_duration_minutes, feedback, completed_at, created_at')\
                .eq('mock_interview_id', session_id)\
                .order('attempt_number', desc=False),
            f"attempts query for session {session_id}"
        )
        
        raw_attempts = attempts_result.data if attempts_result.data else []

        
        # Process and enhance attempts for frontend compatibility
        processed_attempts = []
        for attempt in raw_attempts:
            # Status is already uppercase (COMPLETED, ACTIVE, PENDING, etc.)
            status = attempt.get('status', 'PENDING')
            
            # Enhanced attempt object for frontend
            processed_attempt = {
                **attempt,
                'status': status,
                'has_feedback': bool(attempt.get('feedback')),
                'is_completed': status in ['COMPLETED'],
                'is_processed': status in ['PROCESSED'] and bool(attempt.get('feedback')),
                'can_view_feedback': status in ['PROCESSED'] and bool(attempt.get('feedback')),
                'duration_display': f"{attempt.get('actual_duration_minutes', 0)} min" if attempt.get('actual_duration_minutes') else 'N/A',
                'completed_at_display': attempt.get('completed_at') if attempt.get('completed_at') else 'N/A',
                'created_at_display': attempt.get('created_at') if attempt.get('created_at') else 'N/A',
                'actual_duration_minutes_display': f"{attempt.get('actual_duration_minutes', 0)} min" if attempt.get('actual_duration_minutes') else 'N/A'

            }
            
            processed_attempts.append(processed_attempt)

        
        # Format response with complete information
        response_data = {
            'session_id': session_id,
            'session_info': session_info,
            'attempts': processed_attempts,
            'total_attempts': len(processed_attempts),
            'max_attempts': 3,
            'remaining_attempts': max(0, 3 - len(processed_attempts))
        }
        

        return jsonify(response_data), 200
        
    except Exception as e:
        logger.error(f"Error getting session attempts: {str(e)}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return jsonify({'error': 'Internal server error'}), 500


@mock_interview_bp.route('/attempt/<attempt_id>', methods=['GET'])
@require_authentication
def get_attempt_data(attempt_id):
    """Get attempt data by attempt ID"""
    try:
        admin_client = get_admin_client()
        
        # Get attempt data with session info
        attempt_result = admin_client.table('mock_interview_attempts')\
            .select('*, mock_interview!inner(user_id, title, interview_type, position, company_name, created_at)')\
            .eq('id', attempt_id)\
            .execute()
        
        if not attempt_result.data:
            return jsonify({'error': 'Attempt not found'}), 404
        
        attempt = attempt_result.data[0]
        
        # Verify user owns this attempt
        if attempt['mock_interview']['user_id'] != g.user.id:
            return jsonify({'error': 'Access denied'}), 403
        
        # Enhance attempt data for frontend
        enhanced_attempt = {
            **attempt,
            'has_feedback': bool(attempt.get('feedback')),
            'has_transcript': bool(attempt.get('transcript')),
            'has_live_transcription': bool(attempt.get('live_transcription')),
            'is_completed': attempt.get('status') in ['COMPLETED'],
            'is_processed': attempt.get('status') in ['PROCESSED'] and bool(attempt.get('feedback')),
            'can_view_feedback': attempt.get('status') in ['PROCESSED'] and bool(attempt.get('feedback')),
            'duration_display': f"{attempt.get('actual_duration_minutes', 0)} min" if attempt.get('actual_duration_minutes') else 'N/A',
            'score_display': f"{attempt.get('evaluation_score', 0)}/100" if attempt.get('evaluation_score') is not None else 'Pending',
            'completed_at_display': attempt.get('completed_at') if attempt.get('completed_at') else 'N/A',
            'created_at_display': attempt.get('created_at') if attempt.get('created_at') else 'N/A',
            'actual_duration_minutes_display': f"{attempt.get('actual_duration_minutes', 0)} min" if attempt.get('actual_duration_minutes') else 'N/A'
        }
        
        return jsonify({
            'attempt': enhanced_attempt,
            'session_info': attempt['mock_interview']
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting attempt data: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/attempt/<attempt_id>/save-transcription', methods=['POST'])
@require_authentication
def save_attempt_transcription(attempt_id):
    """Save live transcription data for an active attempt (does NOT mark as completed)"""
    try:
        data = request.get_json()
        live_transcription = data.get('live_transcription', {})
        
        if not live_transcription:
            return jsonify({'error': 'Live transcription data is required'}), 400
        
        admin_client = get_admin_client()
        
        # Verify attempt exists and user owns it - also get started_at for duration calculation
        attempt_result = admin_client.table('mock_interview_attempts')\
            .select('id, status, started_at, mock_interview!inner(user_id)')\
            .eq('id', attempt_id)\
            .execute()
        

        
        if not attempt_result.data:
            return jsonify({'error': 'Attempt not found'}), 404
        
        attempt = attempt_result.data[0]
        current_status = attempt.get('status', 'unknown')
              
        if attempt['mock_interview']['user_id'] != g.user.id:
            return jsonify({'error': 'Access denied'}), 403
        
        
        # Calculate actual duration in minutes
        actual_duration_minutes = 0
        if attempt.get('started_at'):
            try:
                from datetime import datetime

                started_at = datetime.fromisoformat(attempt['started_at'].replace('Z', '+00:00'))
                current_time = datetime.utcnow().replace(tzinfo=started_at.tzinfo)
                duration_seconds = (current_time - started_at).total_seconds()
                actual_duration_minutes = max(0, int(duration_seconds / 60))  # Convert to minutes, minimum 0

            except Exception as duration_error:
                logger.warning(f"Error calculating duration for attempt {attempt_id}: {duration_error}")
                actual_duration_minutes = 0
        else:
            logger.warning(f"No started_at timestamp for attempt {attempt_id}, cannot calculate duration")
        
        # Update live transcription ONLY - do NOT mark as completed here
        # Status should only be updated to 'COMPLETED' by explicit completion calls
        update_data = {
            'live_transcription': json.dumps(live_transcription),
            'actual_duration_minutes': actual_duration_minutes,
            'updated_at': 'now()'
        }
        
        logger.info(f"Updating attempt {attempt_id} with data keys: {list(update_data.keys())}")
        
        update_result = admin_client.table('mock_interview_attempts')\
            .update(update_data)\
            .eq('id', attempt_id)\
            .execute()
        
        logger.info(f"Update result for attempt {attempt_id}: success={bool(update_result.data)}, data_length={len(update_result.data) if update_result.data else 0}")
        
        if update_result.data:
            updated_attempt = update_result.data[0]
            new_status = updated_attempt.get('status', 'unknown')
            logger.info(f"Saved live transcription for attempt {attempt_id} (duration: {actual_duration_minutes} minutes)")
            logger.info(f"Status remains '{new_status}' - interview completion must be handled separately")
            return jsonify({
                'message': 'Live transcription saved successfully',
                'attempt_id': attempt_id,
                'status': new_status,
                'actual_duration_minutes': actual_duration_minutes,
                'transcription_length': len(str(live_transcription)),
                'note': 'Interview status not changed - use /complete endpoint to mark as completed'
            }), 200
        else:
            logger.error(f"Failed to save transcription for attempt {attempt_id} - no data returned from update")
            logger.error(f"Update data attempted: {update_data}")
            return jsonify({'error': 'Failed to save transcription'}), 500
        
    except Exception as e:
        logger.error(f"Error saving attempt transcription: {str(e)}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/attempt/<attempt_id>/subscribe', methods=['GET'])
@require_authentication
def subscribe_to_attempt_updates(attempt_id):
    """Get subscription configuration for real-time attempt updates"""
    try:
        admin_client = get_admin_client()
        
        # Verify attempt exists and user owns it
        attempt_result = admin_client.table('mock_interview_attempts')\
            .select('id, mock_interview!inner(user_id)')\
            .eq('id', attempt_id)\
            .execute()
        
        if not attempt_result.data:
            return jsonify({'error': 'Attempt not found'}), 404
        
        attempt = attempt_result.data[0]
        
        if attempt['mock_interview']['user_id'] != g.user.id:
            return jsonify({'error': 'Access denied'}), 403
        
        # Return subscription configuration for frontend
        return jsonify({
            'subscription_config': {
                'channel_name': f'attempt-updates-{attempt_id}',
                'table': 'mock_interview_attempts',
                'filter': f'id=eq.{attempt_id}',
                'events': ['UPDATE'],
                'watch_columns': ['status', 'feedback', 'evaluation_score'],
                'target_status': 'COMPLETED',
                'attempt_id': attempt_id
            },
            'supabase_config': {
                'url': current_app.config.get('SUPABASE_URL'),
                'anon_key': current_app.config.get('SUPABASE_ANON_KEY')
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error setting up attempt subscription: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/attempts/realtime-token', methods=['POST'])
@require_authentication  
def get_realtime_token():
    """Get a token for real-time subscriptions (if needed for authenticated channels)"""
    try:
        # For now, return the user's JWT token for authenticated real-time channels
        # In production, you might want to generate a specific real-time token
        
        return jsonify({
            'token': request.headers.get('Authorization', '').replace('Bearer ', ''),
            'user_id': g.user.id,
            'expires_in': 3600  # 1 hour
        }), 200
        
    except Exception as e:
        logger.error(f"Error generating realtime token: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

def extract_score_from_feedback(feedback_json):
    """Extract numeric score from feedback JSON - enhanced version"""
    try:
        if isinstance(feedback_json, str):
            feedback_data = json.loads(feedback_json)
        else:
            feedback_data = feedback_json
            
        # Try multiple possible score field names and formats
        score_fields = ['Score', 'score', 'overall_score', 'rating', 'evaluation_score']
        
        for field in score_fields:
            if field in feedback_data:
                score_value = feedback_data[field]
                
                # Handle string format like "7/10", "8.5/10"
                if isinstance(score_value, str):
                    if '/' in score_value:
                        score_parts = score_value.split('/')
                        if score_parts and score_parts[0].strip():
                            try:
                                return float(score_parts[0].strip())
                            except ValueError:
                                continue
                    else:
                        # Try direct conversion for strings like "7", "8.5"
                        try:
                            return float(score_value.strip())
                        except ValueError:
                            continue
                
                # Handle numeric values directly
                elif isinstance(score_value, (int, float)):
                    return float(score_value)
        
        # If no direct score field, look for nested scoring
        for key, value in feedback_data.items():
            if isinstance(value, dict) and 'score' in value:
                try:
                    return float(value['score'])
                except (ValueError, TypeError):
                    continue
                    
        return None
    except Exception as e:
        logger.warning(f"Error extracting score from feedback: {str(e)}")
        return None

@mock_interview_bp.route('/attempt/<attempt_id>/process-feedback', methods=['POST'])
@require_authentication
def process_feedback(attempt_id):
    """Process and store feedback with extracted score"""
    try:
        data = request.get_json()
        feedback = data.get('feedback', {})
        
        if not feedback:
            return jsonify({'error': 'Feedback data is required'}), 400
        
        admin_client = get_admin_client()
        
        # Verify attempt exists and user owns it
        attempt_result = admin_client.table('mock_interview_attempts')\
            .select('id, status, mock_interview!inner(user_id)')\
            .eq('id', attempt_id)\
            .execute()
        
        if not attempt_result.data:
            return jsonify({'error': 'Attempt not found'}), 404
        
        attempt = attempt_result.data[0]
        
        if attempt['mock_interview']['user_id'] != g.user.id:
            return jsonify({'error': 'Access denied'}), 403
        
        # Extract score from feedback
        evaluation_score = extract_score_from_feedback(feedback)
        
        # Update attempt with feedback and extracted score - keep as completed
        update_data = {
            'feedback': json.dumps(feedback),
            'updated_at': 'now()'
        }
        
        # Add evaluation score if successfully extracted
        if evaluation_score is not None:
            update_data['evaluation_score'] = evaluation_score
            logger.info(f"Extracted score {evaluation_score} from feedback for attempt {attempt_id}")
        else:
            logger.warning(f"Could not extract score from feedback for attempt {attempt_id}")
        
        update_result = admin_client.table('mock_interview_attempts')\
            .update(update_data)\
            .eq('id', attempt_id)\
            .execute()
        
        if update_result.data:
            processed_attempt = update_result.data[0]
            logger.info(f"Added feedback for attempt {attempt_id} with score {evaluation_score}")
            
            return jsonify({
                'message': 'Feedback added successfully',
                'attempt': processed_attempt,
                'evaluation_score': evaluation_score,
                'status': processed_attempt.get('status', 'COMPLETED')
            }), 200
        else:
            return jsonify({'error': 'Failed to add feedback'}), 500
        
    except Exception as e:
        logger.error(f"Error processing feedback: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/attempt/<attempt_id>/complete', methods=['POST'])
@require_authentication
def complete_attempt(attempt_id):
    """Complete an attempt and save final transcript and metadata - USER-INITIATED SUCCESSFUL COMPLETIONS ONLY"""
    try:
        data = request.get_json()
        transcript = data.get('transcript', {})
        live_transcription = data.get('live_transcription', {})
        actual_duration_minutes = data.get('actual_duration_minutes', 0)
        feedback = data.get('feedback', {})  # Optional feedback in completion
        
        admin_client = get_admin_client()
        
        # Verify attempt exists and user owns it - also get started_at for duration calculation
        attempt_result = admin_client.table('mock_interview_attempts')\
            .select('id, status, started_at, mock_interview!inner(user_id)')\
            .eq('id', attempt_id)\
            .execute()
        
        if not attempt_result.data:
            return jsonify({'error': 'Attempt not found'}), 404
        
        attempt = attempt_result.data[0]
        
        if attempt['mock_interview']['user_id'] != g.user.id:
            return jsonify({'error': 'Access denied'}), 403
        
        if attempt['status'] in ['COMPLETED', 'failed', 'error']:
            return jsonify({'error': f'Attempt already in final state: {attempt["status"]}'}), 400
        
        # Calculate actual duration if not provided
        if actual_duration_minutes <= 0 and attempt.get('started_at'):
            try:
                from datetime import datetime
                started_at = datetime.fromisoformat(attempt['started_at'].replace('Z', '+00:00'))
                current_time = datetime.utcnow().replace(tzinfo=started_at.tzinfo)
                duration_seconds = (current_time - started_at).total_seconds()
                actual_duration_minutes = max(0, int(duration_seconds / 60))  # Convert to minutes, minimum 0
                logger.info(f"Calculated duration for attempt {attempt_id}: {actual_duration_minutes} minutes")
            except Exception as duration_error:
                logger.warning(f"Error calculating duration for attempt {attempt_id}: {duration_error}")
                actual_duration_minutes = 0
        
        # Determine final status based on duration - successful completion flow
        final_status = 'COMPLETED'
        
        # If session ended within 3 minutes, mark as error
        if actual_duration_minutes < 3:
            final_status = 'error'
            logger.warning(f"Session ended within 3 minutes ({actual_duration_minutes}min) - marking as error")
            # Note: This is a successful user-initiated completion, but duration too short
        
        # Update attempt with completion data
        update_data = {
            'status': final_status,
            'completed_at': 'now()',
            'actual_duration_minutes': actual_duration_minutes,
            'updated_at': 'now()'
        }
        
        # Add transcript data if provided
        if transcript:
            update_data['transcript'] = json.dumps(transcript)
        
        # Add live transcription if provided
        if live_transcription:
            update_data['live_transcription'] = json.dumps(live_transcription)
        
        # Handle feedback if provided in completion 
        if feedback and final_status != 'error':  # Don't process feedback for error sessions
            evaluation_score = extract_score_from_feedback(feedback)
            update_data['feedback'] = json.dumps(feedback)
            # Keep status as 'COMPLETED' - no PROCESSED status
            
            if evaluation_score is not None:
                update_data['evaluation_score'] = evaluation_score
                logger.info(f"Extracted score {evaluation_score} from feedback for attempt {attempt_id}")
        
        update_result = admin_client.table('mock_interview_attempts')\
            .update(update_data)\
            .eq('id', attempt_id)\
            .execute()
        
        if update_result.data:
            completed_attempt = update_result.data[0]
            status = update_data['status']  # Use the actual status we set
            logger.info(f"Completed attempt {attempt_id} with {actual_duration_minutes} minutes duration, status: {status}")
            
            response_data = {
                'message': 'Interview session completed successfully' if status != 'error' else 'Session ended too early',
                'attempt': completed_attempt,
                'status': status,
                'actual_duration_minutes': actual_duration_minutes,
                'completion_type': 'user_initiated'  # This is from frontend completion
            }
            
            if status == 'error':
                response_data['error_reason'] = 'Session duration was less than 3 minutes'
                response_data['processing_message'] = 'Interview ended too early to provide meaningful feedback.'
            elif feedback and status == 'COMPLETED':
                response_data['evaluation_score'] = update_data.get('evaluation_score')
                response_data['processing_message'] = 'Interview completed successfully with feedback!'
            else:
                response_data['processing_message'] = 'Your interview was completed successfully! Analysis will be available soon.'
            
            return jsonify(response_data), 200
        else:
            return jsonify({'error': 'Failed to complete attempt'}), 500
        
    except Exception as e:
        logger.error(f"Error completing attempt: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

# New backend endpoint
@mock_interview_bp.route('/sessions/count', methods=['GET'])
@require_authentication
def get_user_sessions_count():
    """Get total count of user's sessions - lightweight endpoint"""
    try:
        admin_client = get_admin_client()
        
        count_result = admin_client.table('mock_interview')\
            .select('id', count='exact')\
            .eq('user_id', g.user.id)\
            .execute()
        
        total_count = getattr(count_result, 'count', 0)
        
        return jsonify({
            'total_sessions': total_count,
            'user_id': g.user.id[:8] + '***'  # For debugging
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting session count: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/sessions/stats', methods=['GET'])
@require_authentication
def get_user_sessions_stats():
    """Get comprehensive stats for user's mock interview sessions"""
    try:
        admin_client = get_admin_client()
        user_id = g.user.id
        
        # Get total sessions count
        sessions_count_result = admin_client.table('mock_interview')\
            .select('id', count='exact')\
            .eq('user_id', user_id)\
            .execute()
        
        total_sessions = getattr(sessions_count_result, 'count', 0)
        
        # Get all attempts for this user with feedback and duration data
        attempts_result = admin_client.table('mock_interview_attempts')\
            .select('id, status, feedback, evaluation_score, actual_duration_minutes, mock_interview_id')\
            .execute()
        
        # Filter attempts for this user's sessions
        user_sessions_result = admin_client.table('mock_interview')\
            .select('id')\
            .eq('user_id', user_id)\
            .execute()
        
        user_session_ids = [s['id'] for s in user_sessions_result.data] if user_sessions_result.data else []
        
        attempts = [a for a in (attempts_result.data or []) if a.get('mock_interview_id') in user_session_ids]
        
        # Filter PROCESSED attempts (these have feedback JSON with scores)
        processed_attempts = [a for a in attempts if a.get('status') == 'PROCESSED']
        
        # Debug logging
        logger.info(f"Stats query found {len(attempts)} total attempts for user, {len(processed_attempts)} PROCESSED attempts")
        statuses = [a.get('status') for a in attempts]
        logger.info(f"Attempt statuses found: {set(statuses)}")
        if processed_attempts:
            logger.info(f"PROCESSED attempt IDs: {[a.get('id') for a in processed_attempts]}")
        else:
            logger.warning("No PROCESSED attempts found - checking for other status variations")
            # Check for lowercase processed or other variations
            other_processed = [a for a in attempts if str(a.get('status', '')).lower() in ['processed']]
            if other_processed:
                logger.warning(f"Found {len(other_processed)} attempts with status variations: {[a.get('status') for a in other_processed]}")
                processed_attempts = other_processed  # Use these instead
        
        # Count sessions where all 3 attempts are processed
        attempts_by_session = {}
        for attempt in attempts:
            session_id = attempt.get('mock_interview_id')
            if session_id not in attempts_by_session:
                attempts_by_session[session_id] = []
            attempts_by_session[session_id].append(attempt)
        
        # Only count sessions with all 3 attempts processed
        completed_sessions = []
        for session_id, session_attempts in attempts_by_session.items():
            if (len(session_attempts) == 3 and 
                all(a.get('status') == 'PROCESSED' for a in session_attempts)):
                completed_sessions.append(session_id)
        
        completed_sessions_count = len(completed_sessions)
        
        # Log session completion details
        logger.info(f"Found {len(attempts_by_session)} total sessions")
        for session_id, session_attempts in attempts_by_session.items():
            logger.info(f"Session {session_id}: {len(session_attempts)} attempts, "
                      f"statuses: {[a.get('status') for a in session_attempts]}")
        logger.info(f"Completed sessions (all 3 attempts PROCESSED): {completed_sessions_count}")
        
        # Calculate average score from feedback JSON
        scores = []
        total_duration_minutes = 0
        
        for attempt in processed_attempts:
            attempt_id = attempt.get('id', 'unknown')
            logger.info(f"Processing attempt {attempt_id} - status: {attempt.get('status')}, has_feedback: {bool(attempt.get('feedback'))}, has_eval_score: {bool(attempt.get('evaluation_score'))}")
            
            # Extract score from feedback JSON using enhanced function
            if attempt.get('feedback'):
                try:
                    extracted_score = extract_score_from_feedback(attempt['feedback'])
                    if extracted_score is not None:
                        scores.append(extracted_score)
                        logger.info(f"Extracted score {extracted_score} from feedback for attempt {attempt_id}")
                    else:
                        logger.warning(f"Could not extract score from feedback for attempt {attempt_id}")
                        # Log the feedback structure for debugging
                        if isinstance(attempt['feedback'], str):
                            try:
                                feedback_data = json.loads(attempt['feedback'])
                                logger.warning(f"Feedback keys for attempt {attempt_id}: {list(feedback_data.keys())}")
                            except:
                                logger.warning(f"Invalid JSON in feedback for attempt {attempt_id}")
                        else:
                            logger.warning(f"Feedback keys for attempt {attempt_id}: {list(attempt['feedback'].keys()) if isinstance(attempt['feedback'], dict) else 'not a dict'}")
                except Exception as e:
                    logger.warning(f"Error parsing feedback for attempt {attempt_id}: {e}")
            
            # Fallback to evaluation_score if no feedback score
            elif attempt.get('evaluation_score'):
                eval_score = attempt['evaluation_score']
                if eval_score <= 10:
                    scores.append(eval_score)
                    logger.info(f"Used evaluation_score {eval_score} for attempt {attempt_id}")
                else:
                    # Convert percentage to rating out of 10
                    converted_score = (eval_score / 100) * 10
                    scores.append(converted_score)
                    logger.info(f"Converted evaluation_score {eval_score}% to {converted_score}/10 for attempt {attempt_id}")
            else:
                logger.warning(f"No score available for attempt {attempt_id}")
            
            # Add duration
            if attempt.get('actual_duration_minutes'):
                total_duration_minutes += attempt['actual_duration_minutes']
        
        # Calculate average score
        avg_score = 0
        if scores:
            avg_score = sum(scores) / len(scores)
            avg_score = round(avg_score, 1)  # Round to 1 decimal place
        
        # Convert total time to hours and minutes
        total_hours = total_duration_minutes // 60
        remaining_minutes = total_duration_minutes % 60
        
        # Prepare response
        stats = {
            'total_sessions': total_sessions,
            'completed_sessions': completed_sessions_count,
            'avg_score': avg_score,
            'avg_score_display': f"{avg_score}/10" if scores else "0/10",
            'total_time_minutes': total_duration_minutes,
            'total_time_hours': total_hours,
            'total_time_display': f"{total_hours}h {remaining_minutes}m",
            'total_attempts': len(attempts),
            'completed_attempts': len(processed_attempts),
            'scores_count': len(scores)
        }
        
        logger.info(f"Stats for user {user_id[:8]}***: {stats}")
        
        return jsonify({
            'stats': stats,
            'debug': {
                'total_attempts_found': len(attempts),
                'processed_attempts_found': len(processed_attempts),
                'scores_extracted': len(scores),
                'sample_scores': scores[:5] if scores else []
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting user session stats: {str(e)}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return jsonify({'error': 'Internal server error'}), 500


@mock_interview_bp.route('/cleanup-stuck-sessions', methods=['POST'])
@require_authentication
def cleanup_stuck_sessions():
    """Manual cleanup endpoint for stuck sessions - useful for debugging and user support"""
    try:
        admin_client = get_admin_client()
        user_id = g.user.id
        
        # Get all active attempts for this user
        active_attempts_result = admin_client.table('mock_interview_attempts')\
            .select('id, mock_interview_id, room_name, started_at, updated_at, mock_interview!inner(user_id)')\
            .eq('status', 'active')\
            .eq('mock_interview.user_id', user_id)\
            .execute()
        
        if not active_attempts_result.data:
            return jsonify({
                'message': 'No stuck sessions found',
                'cleaned_attempts': 0
            }), 200
        
        cleaned_count = 0
        from datetime import datetime, timedelta
        current_time = datetime.utcnow()
        
        for attempt in active_attempts_result.data:
            attempt_id = attempt['id']
            room_name = attempt.get('room_name')
            started_at = attempt.get('started_at')
            
            logger.info(f"Force cleaning stuck attempt {attempt_id}")
            
            try:
                # Mark as failed
                admin_client.table('mock_interview_attempts')\
                    .update({
                        'status': 'failed',
                        'completed_at': 'now()',
                        'updated_at': 'now()'
                    })\
                    .eq('id', attempt_id)\
                    .execute()
                
                # Clean up room if it exists
                if room_name:
                    try:
                        from .api import delete_room
                        asyncio.run(delete_room(room_name))
                        logger.info(f"Cleaned up room: {room_name}")
                    except Exception as room_error:
                        logger.warning(f"Failed to clean room {room_name}: {room_error}")
                
                cleaned_count += 1
                logger.info(f"Successfully cleaned stuck attempt {attempt_id}")
                
            except Exception as cleanup_error:
                logger.error(f"Failed to clean attempt {attempt_id}: {cleanup_error}")
        
        return jsonify({
            'message': f'Cleaned up {cleaned_count} stuck sessions',
            'cleaned_attempts': cleaned_count
        }), 200
        
    except Exception as e:
        logger.error(f"Error in cleanup endpoint: {e}")
        return jsonify({'error': 'Failed to cleanup stuck sessions'}), 500


@mock_interview_bp.route('/cleanup-failed-attempts', methods=['POST'])
@require_authentication
def cleanup_failed_attempts():
    """Cleanup failed attempts for this user - removes them from database so they don't count as attempts"""
    try:
        admin_client = get_admin_client()
        user_id = g.user.id
        
        # Get all failed attempts for this user
        failed_attempts_result = admin_client.table('mock_interview_attempts')\
            .select('id, attempt_number, mock_interview_id, created_at, mock_interview!inner(user_id, title)')\
            .eq('status', 'failed')\
            .eq('mock_interview.user_id', user_id)\
            .execute()
        
        if not failed_attempts_result.data:
            return jsonify({
                'message': 'No failed attempts found to cleanup',
                'cleaned_attempts': 0
            }), 200
        
        cleaned_count = 0
        cleanup_details = []
        
        for failed_attempt in failed_attempts_result.data:
            attempt_id = failed_attempt['id']
            session_title = failed_attempt['mock_interview']['title']
            attempt_number = failed_attempt['attempt_number']
            
            logger.info(f"Cleaning up failed attempt {attempt_id} from session '{session_title}'")
            
            try:
                cleanup_success = cleanup_failed_attempt(attempt_id, "Manual cleanup by user request")
                if cleanup_success:
                    cleaned_count += 1
                    cleanup_details.append({
                        'attempt_id': attempt_id,
                        'session_title': session_title,
                        'attempt_number': attempt_number,
                        'status': 'cleaned'
                    })
                    logger.info(f"Successfully cleaned failed attempt {attempt_id}")
                else:
                    cleanup_details.append({
                        'attempt_id': attempt_id,
                        'session_title': session_title,
                        'attempt_number': attempt_number,
                        'status': 'failed_to_clean'
                    })
                    logger.warning(f"Failed to clean attempt {attempt_id}")
                
            except Exception as cleanup_error:
                logger.error(f"Error cleaning failed attempt {attempt_id}: {cleanup_error}")
                cleanup_details.append({
                    'attempt_id': attempt_id,
                    'session_title': session_title,
                    'attempt_number': attempt_number,
                    'status': 'error',
                    'error': str(cleanup_error)
                })
        
        return jsonify({
            'message': f'Processed {len(failed_attempts_result.data)} failed attempts, cleaned {cleaned_count}',
            'total_failed_found': len(failed_attempts_result.data),
            'cleaned_attempts': cleaned_count,
            'cleanup_details': cleanup_details,
            'note': 'Cleaned attempts are permanently removed and won\'t count against your attempt limit'
        }), 200
        
    except Exception as e:
        logger.error(f"Error in failed attempts cleanup endpoint: {e}")
        return jsonify({'error': 'Failed to cleanup failed attempts'}), 500