from flask import request, jsonify, current_app, g
from . import mock_interview_bp
from ..subscription.helpers import require_authentication, get_user_display_name
from ...extensions import supabase, get_user_client, get_admin_client
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
from datetime import datetime

logger = logging.getLogger(__name__)



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
    if status in ['completed']:
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
        
        # EMERGENCY DEBUG - This will definitely show up
        logger.info("EMERGENCY DEBUG - RAW REQUEST DATA:")
        logger.info(str(data))
        
        # Check user's session limits before creating
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
                'mock_interview_session_lifetime_count': 0,
                'mock_interview_attempt_lifetime_count': 0
            }
            admin_client.table('feature_usage').insert(default_usage).execute()
            usage_data = default_usage
        else:
            usage_data = usage_result.data[0]
        
        plan_id = usage_data.get('plan_id', 1)
        
        # Get plan limits
        plan_result = admin_client.table('subscription_plans')\
            .select('mock_interview_session')\
            .eq('id', plan_id)\
            .execute()
        
        if not plan_result.data:
            session_limit = 0 if plan_id == 1 else 3
        else:
            session_limit = plan_result.data[0].get('mock_interview_session', 0)
        
        # Count current sessions
        sessions_result = admin_client.table('mock_interview')\
            .select('id')\
            .eq('user_id', user_id)\
            .execute()
        
        current_sessions = len(sessions_result.data) if sessions_result.data else 0
        
        # Check limits
        if plan_id == 1:  # Free plan
            return jsonify({
                'error': 'Mock interviews require a Pro or Premium subscription',
                'plan_required': 'Pro',
                'current_plan': 'Free'
            }), 403
        
        if session_limit and current_sessions >= session_limit:
            plan_name = 'Pro' if plan_id == 2 else 'Premium'
            return jsonify({
                'error': f'Session limit reached ({current_sessions}/{session_limit})',
                'plan_required': 'Premium' if plan_id == 2 else None,
                'current_plan': plan_name
            }), 403
        
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
        
        # Handle company data - frontend sends both 'company' and 'company_name' fields  
        # Store the company URL directly in company_name column
        
        # EMERGENCY DEBUG - Check all company fields
        logger.info("EMERGENCY DEBUG - COMPANY FIELDS:")
        logger.info(f"data.get('company_name'): '{data.get('company_name', 'NOT_FOUND')}'")
        logger.info(f"data.get('company'): '{data.get('company', 'NOT_FOUND')}'") 
        logger.info(f"data.get('companyUrl'): '{data.get('companyUrl', 'NOT_FOUND')}'")
        logger.info(f"data.get('company_url'): '{data.get('company_url', 'NOT_FOUND')}'")
        
        company_url = data.get('company_url', '') or data.get('company_name', '') or data.get('company', '') or data.get('companyUrl', '')
        company_name = company_url  # Store URL directly as company_name
        
        logger.info(f"FINAL COMPANY VALUES - company_url: '{company_url}', company_name: '{company_name}'")
        

        
        # Handle both description (NewSessionModal) and custom_instructions formats
        custom_instructions = data.get('description') or data.get('custom_instructions', '')
        
        # Normalize interview type (handle both hyphen and underscore formats)
        normalized_interview_type = interview_type.replace('-', '_')
        
        # Allow any interview type - no validation needed
        # Use normalized type for consistency
        interview_type = normalized_interview_type
        
        # Handle resume content with proper priority logic
        # Priority order: document_id > explicit resume_url > auto-fetch most recent
        # This ensures user selections are always respected over automatic substitution
        
        if resume_document_id:
            # Priority 1: User selected a specific document by ID
            extracted_resume_text, error = get_resume_content_by_document_id(g.user.id, resume_document_id)
            if error:
                return jsonify({
                    'error': f'Failed to extract resume content: {error}'
                }), 400
            resume_text = extracted_resume_text
            
            # Get the exact document URL as stored in the database (exactly what user selected)
            try:
                admin_client = get_admin_client()
                doc_result = admin_client.table('user_documents')\
                    .select('document_url')\
                    .eq('id', resume_document_id)\
                    .eq('uid', g.user.id)\
                    .execute()
                if doc_result.data:
                    # Use the exact URL from the database without any modifications
                    resume_url = doc_result.data[0]['document_url']
            except Exception as e:
                logger.warning(f"Failed to get document URL for resume_document_id {resume_document_id}: {e}")
        
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
        
        elif resume_url:
            # Priority 2: User explicitly provided a resume URL - respect their choice
            # Extract text from the provided URL if resume_text is not already provided
            if not resume_text:
                try:
                    resume_text = extract_resume_text_from_url(resume_url) or "Resume content extraction in progress..."
                except Exception as e:
                    logger.warning(f"Could not extract resume text from provided URL: {e}")
                    resume_text = "Resume uploaded - content extraction in progress..."
        
        elif not resume_text and not resume_url:
            # Priority 3: No resume data provided at all - only then auto-fetch most recent
            try:
                # Direct database query for most recent resume
                try:
                    admin_client = get_admin_client()
                except RuntimeError as e:
                    logger.error(f"Admin client not available: {e}")
                    return jsonify({'error': 'Server configuration error'}), 500
                    
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
                    
                    # Try to extract text from resume (optional - can fail)
                    try:
                        resume_text = extract_resume_text_from_url(resume_url) or "Resume content extraction in progress..."
                    except Exception as e:
                        logger.warning(f"Could not extract resume text: {e}")
                        resume_text = "Resume uploaded - content extraction in progress..."
                else:
                    logger.warning(f"No resume documents found for user {g.user.id}")
                    return jsonify({
                        'error': 'No resume found. Please upload a resume before creating an interview session.'
                    }), 400
                    
            except Exception as e:
                logger.error(f"Error fetching resume: {e}")
                return jsonify({
                    'error': 'Failed to fetch resume. Please try again.'
                }), 400
       
        
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
                
                # Debug logging for database update
                logger.info(f"About to update database - company_name: '{update_data.get('company_name')}', company_url: '{company_url}'")
                logger.info("EMERGENCY DEBUG - FULL UPDATE DATA:")
                logger.info(f"update_data['company_name'] = '{update_data.get('company_name')}'")
                logger.info(f"company_name variable = '{company_name}'")
                logger.info(f"company_url variable = '{company_url}'")
                
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
                            logger.info(f"Attempting to update company_name with exact value: {company_name}")
                            admin_client.table('mock_interview').update({'company_name': company_name}).eq('id', session_id).execute()
                            logger.info("Successfully updated company_name")
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
                            logger.info(f"Fallback: attempting company_name update with: {company_name}")
                            admin_client.table('mock_interview').update({'company_name': company_name}).eq('id', session_id).execute()
                            logger.info("Successfully updated company_name despite other field failures")
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
        
        # Check existing attempts for this session
        attempts_result = admin_client.table('mock_interview_attempts')\
            .select('attempt_number, status')\
            .eq('mock_interview_id', session_id)\
            .order('attempt_number', desc=True)\
            .execute()
        
        existing_attempts = attempts_result.data if attempts_result.data else []
        
        # Check if we can create a new attempt (max 3 attempts per session)
        if len(existing_attempts) >= 3:
            return jsonify({
                'error': 'Maximum attempts reached for this session (3/3)',
                'attempts_used': len(existing_attempts),
                'max_attempts': 3
            }), 403
        
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
            return jsonify({'error': 'Failed to generate room token'}), 500
        
        # Update session status and attempt status
        admin_client.table('mock_interview')\
            .update({'status': 'active', 'updated_at': 'now()'})\
            .eq('id', session_id)\
            .execute()
        
        logger.info(f"Setting started_at timestamp for attempt {attempt_result.data[0]['id']}")
        attempt_update_result = admin_client.table('mock_interview_attempts')\
            .update({'status': 'active', 'started_at': 'now()'})\
            .eq('id', attempt_result.data[0]['id'])\
            .execute()
        
        if attempt_update_result.data:
            logger.info(f"Successfully set started_at for attempt {attempt_result.data[0]['id']}")
        else:
            logger.error(f"Failed to set started_at for attempt {attempt_result.data[0]['id']}")
        
        # Update attempt counter in feature_usage
        try:
            # First get current count
            usage_result = admin_client.table('feature_usage')\
                .select('mock_interview_attempt_lifetime_count')\
                .eq('user_id', g.user.id)\
                .execute()
            
            if usage_result.data:
                current_attempts = usage_result.data[0].get('mock_interview_attempt_lifetime_count', 0)
                admin_client.table('feature_usage')\
                    .update({'mock_interview_attempt_lifetime_count': current_attempts + 1})\
                    .eq('user_id', g.user.id)\
                    .execute()
        except Exception as counter_error:
            logger.warning(f"Failed to update attempt counter: {counter_error}")
        
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
        
        # NO AGENT DISPATCH - Let LiveKit handle this automatically
        # Agents will be auto-dispatched by LiveKit when users join rooms
        logger.info(f"Room {attempt_room_name} created - agent will auto-join via LiveKit")
        
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
            .select('plan_id, mock_interview_session_lifetime_count, mock_interview_attempt_lifetime_count')\
            .eq('user_id', user_id)\
            .execute()
        
        if not usage_result.data:
            # Create default feature usage record if doesn't exist
            default_usage = {
                'user_id': user_id,
                'plan_id': 1,  # Default to free plan
                'mock_interview_session_lifetime_count': 0,
                'mock_interview_attempt_lifetime_count': 0
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
            plan_limits = {'mock_interview_session': 0, 'mock_interview_attempts': 3}
        else:
            plan_limits = plan_result.data[0]
        
        # Count current sessions for this user
        sessions_result = admin_client.table('mock_interview')\
            .select('id')\
            .eq('user_id', user_id)\
            .execute()
        
        current_sessions = len(sessions_result.data) if sessions_result.data else 0
        
        # Prepare response
        response_data = {
            'plan_id': plan_id,
            'session_limit': plan_limits.get('mock_interview_session', 0),
            'sessions_used': current_sessions,
            'sessions_remaining': max(0, (plan_limits.get('mock_interview_session', 0) or 999999) - current_sessions),
            'attempts_per_session': plan_limits.get('mock_interview_attempts', 3),
            'is_unlimited_sessions': plan_limits.get('mock_interview_session') is None or plan_limits.get('mock_interview_session') == 0,
            'can_create_session': (
                plan_limits.get('mock_interview_session') is None or  # Unlimited
                plan_limits.get('mock_interview_session') == 0 or     # Free plan (no sessions)
                current_sessions < plan_limits.get('mock_interview_session', 0)
            ),
            'plan_name': 'Free' if plan_id == 1 else ('Pro' if plan_id == 2 else 'Premium')
        }
        
        # Special handling for free plan (plan_id 1)
        if plan_id == 1:
            response_data['can_create_session'] = False
            response_data['session_limit'] = 0
            response_data['sessions_remaining'] = 0
        
        return jsonify(response_data), 200
        
    except Exception as e:
        logger.error(f"Error getting user mock interview limits: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/configure', methods=['POST'])
@require_authentication
def configure_interview():
    """Configure interview parameters (prompt is generated by n8n)"""
    try:
        data = request.get_json()
        interview_type = data.get('interview_type', 'behavioral')
        difficulty_level = data.get('difficulty_level', 'medium')
        position = data.get('position', 'Software Engineer')
        custom_instructions = data.get('custom_instructions', '')
        
        # Return configuration parameters (prompt is generated by n8n)
        return jsonify({
            'interview_type': interview_type,
            'difficulty_level': difficulty_level,
            'position': position,
            'custom_instructions': custom_instructions,
            'note': 'Agent prompt is generated by n8n workflow'
        }), 200
        
    except Exception as e:
        logger.error(f"Error configuring interview: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/sessions', methods=['GET'])
@require_authentication
def get_user_sessions():
    """Get user's interview sessions"""
    try:
        # Use admin client with explicit user filtering for security due to user client session issues
        try:
            admin_client = get_admin_client()
        except RuntimeError as e:
            logger.error(f"Admin client not available: {e}")
            return jsonify({'error': 'Server configuration error'}), 500
            
        # Explicitly filter by user_id for security (equivalent to RLS)
        result = admin_client.table('mock_interview')\
            .select('*')\
            .eq('user_id', g.user.id)\
            .order('created_at', desc=True)\
            .execute()
        
        # Add display status information to each session
        sessions_with_display = []
        for session in (result.data or []):
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
            sessions_with_display.append(session_with_display)
        
        return jsonify({
            'sessions': sessions_with_display
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching user sessions: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/session/<session_id>/end', methods=['POST'])
@require_authentication
def end_interview_session(session_id):
    """
    DEPRECATED: Use /attempt/<attempt_id>/complete instead
    End an interview session and provide feedback - Legacy endpoint for backward compatibility
    """
    try:
        data = request.get_json()
        attempt_id = data.get('attempt_id')
        
        if not attempt_id:
            return jsonify({
                'error': 'This endpoint is deprecated. Please use /attempt/<attempt_id>/complete instead.',
                'deprecated': True,
                'new_endpoint': '/attempt/<attempt_id>/complete'
            }), 400
        
        # Redirect to new attempt completion endpoint
        feedback = data.get('feedback', {})
        transcript = data.get('transcript', {})
        actual_duration_minutes = data.get('actual_duration_minutes', 0)
        
        admin_client = get_admin_client()
        
        # Update attempt with completion data (same logic as complete_attempt)
        update_data = {
            'status': 'completed',
            'completed_at': 'now()',
            'actual_duration_minutes': actual_duration_minutes,
            'updated_at': 'now()'
        }
        
        if transcript:
            update_data['transcript'] = json.dumps(transcript)
        if feedback:
            update_data['feedback'] = json.dumps(feedback)
        
        result = admin_client.table('mock_interview_attempts')\
            .update(update_data)\
            .eq('id', attempt_id)\
            .execute()
        
        if result.data:
            return jsonify({
                'status': 'completed',
                'message': 'Attempt completed successfully (via legacy endpoint)',
                'deprecated_warning': 'This endpoint is deprecated. Use /attempt/<attempt_id>/complete',
                'attempt': result.data[0]
            }), 200
        else:
            return jsonify({'error': 'Attempt not found'}), 404
            
    except Exception as e:
        logger.error(f"Error ending interview session (legacy): {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/interview-types', methods=['GET'])
def get_interview_types():
    """Get suggested interview types (all custom types are allowed)"""
    suggested_types = {
        'behavioral': 'Behavioral Interview',
        'technical': 'Technical Interview', 
        'system_design': 'System Design Interview',
        'case_study': 'Case Study Interview',
        'hr_round': 'HR Round',
        'panel_interview': 'Panel Interview',
        'final_round': 'Final Round',
        'screening': 'Screening Interview'
    }
    
    return jsonify({
        'suggested_types': suggested_types,
        'note': 'Any custom interview type is allowed - these are just suggestions',
        'custom_allowed': True
    }), 200



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
            'status': 'completed',
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

@mock_interview_bp.route('/user-documents', methods=['GET'])
@require_authentication
def get_user_documents():
    """Get user's documents for interview preparation"""
    try:
        # Use admin client with explicit user filtering to avoid RLS issues
        admin_client = get_admin_client()
        result = admin_client.table('user_documents')\
            .select('id, uid, document_name, document_type, document_url, created_at')\
            .eq('uid', g.user.id)\
            .order('created_at', desc=True)\
            .execute()
        
        # Additional security check - ensure all returned documents belong to this user
        user_documents = [doc for doc in (result.data or []) if doc.get('uid') == g.user.id]
        
        logger.info(f"Found {len(user_documents)} documents for user {g.user.id[:8]}***")
        
        return jsonify({
            'documents': user_documents
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching user documents: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/session/<session_id>/results', methods=['GET'])
@require_authentication
def get_interview_results(session_id):
    """Get detailed interview results including SWOT analysis"""
    try:
        # Get session with all results
        user_client = get_user_client()
        result = user_client.table('mock_interview')\
            .select('*')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not result.data:
            return jsonify({'error': 'Session not found'}), 404
        
        session = result.data[0]
        
        # Compute display status for session
        display_info = get_display_status(
            session.get('status', 'created'),
            session.get('status_prep', 'PENDING')
        )
        
        # Return comprehensive results
        return jsonify({
            'session_id': session_id,
            'interview_type': session.get('interview_type'),
            'position': session.get('position'),
            'company_name': session.get('company_name'),
            'status': session.get('status'),
            'display_status': display_info['display_status'],
            'display_text': display_info['display_text'],
            'is_ready_to_join': display_info['is_ready_to_join'],
            'color_class': display_info['color_class'],
            'status_prep': session.get('status_prep', 'PENDING'),
            'duration_planned': session.get('duration_minutes'),
            'duration_actual': session.get('duration_actual'),
            'created_at': session.get('created_at'),
            'ended_at': session.get('ended_at'),
            'transcript': session.get('transcript'),
            'swot_analysis': session.get('swot_analysis'),
            'participant_data': session.get('participant_data'),
            'interview_context': {
                'job_description': session.get('job_description'),
                'company_details': session.get('company_details'),
                'linkedin_profile': session.get('linkedin_profile')
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching interview results: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/session/<session_id>/swot-analysis', methods=['GET'])
@require_authentication
def get_swot_analysis(session_id):
    """Get just the SWOT analysis for a session"""
    try:
        user_client = get_user_client()
        result = user_client.table('mock_interview')\
            .select('swot_analysis, status, position, company_name')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not result.data:
            return jsonify({'error': 'Session not found'}), 404
        
        session = result.data[0]
        
        if session.get('status') != 'completed':
            return jsonify({'error': 'Interview not yet completed'}), 400
        
        return jsonify({
            'session_id': session_id,
            'position': session.get('position'),
            'company_name': session.get('company_name'),
            'swot_analysis': session.get('swot_analysis')
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching SWOT analysis: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/session/<session_id>/start-agent', methods=['POST'])
@require_authentication
def start_interview_agent(session_id):
    """DISABLED: Agent dispatch is handled in join route only."""
    
    return jsonify({
        'status': 'disabled',
        'message': 'This endpoint is disabled. Agent dispatch happens automatically in join route.',
        'session_id': session_id
    }), 410  # 410 Gone - resource is no longer available

@mock_interview_bp.route('/document/<int:document_id>/resume-content', methods=['GET'])
@require_authentication
def get_resume_content(document_id):
    """Extract and return resume text content from a user's document"""
    try:
        resume_text, error = get_resume_content_by_document_id(g.user.id, document_id)
        
        if error:
            return jsonify({'error': error}), 404
        
        return jsonify({
            'document_id': document_id,
            'resume_text': resume_text,
            'character_count': len(resume_text) if resume_text else 0
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching resume content: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/user-documents/resumes', methods=['GET'])
@require_authentication
def get_user_resume_documents():
    """Get user's resume documents specifically"""
    try:
        # Use admin client with explicit user filtering to avoid RLS issues
        admin_client = get_admin_client()
        result = admin_client.table('user_documents')\
            .select('id, uid, document_name, document_type, document_url, created_at')\
            .eq('uid', g.user.id)\
            .in_('document_type', ['resume', 'Resume', 'CV', 'cv'])\
            .order('created_at', desc=True)\
            .execute()
        
        # Additional security check - ensure all returned documents belong to this user
        user_resumes = [doc for doc in (result.data or []) if doc.get('uid') == g.user.id]
        
        logger.info(f"Found {len(user_resumes)} resume documents for user {g.user.id[:8]}***")
        
        return jsonify({
            'resume_documents': user_resumes
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching resume documents: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500 

@mock_interview_bp.route('/user-documents/cover-letters', methods=['GET'])
@require_authentication
def get_user_cover_letter_documents():
    """Get user's cover letter documents specifically"""
    try:
        # Use admin client with explicit user filtering to avoid RLS issues
        admin_client = get_admin_client()
        result = admin_client.table('user_documents')\
            .select('id, uid, document_name, document_type, document_url, created_at')\
            .eq('uid', g.user.id)\
            .in_('document_type', ['cover_letter', 'Cover Letter', 'coverletter', 'Cover_Letter'])\
            .order('created_at', desc=True)\
            .execute()
        
        # Additional security check - ensure all returned documents belong to this user
        user_cover_letters = [doc for doc in (result.data or []) if doc.get('uid') == g.user.id]
        
        logger.info(f"Found {len(user_cover_letters)} cover letter documents for user {g.user.id[:8]}***")
        
        return jsonify({
            'cover_letter_documents': user_cover_letters
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching cover letter documents: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/session/<session_id>/attempts', methods=['GET'])
@require_authentication
def get_session_attempts(session_id):
    """Get all attempts for a specific interview session with complete details"""
    try:
        admin_client = get_admin_client()
        
        logger.info(f"Fetching attempts for session {session_id} for user {g.user.id[:8]}***")
        
        # Verify session belongs to user
        session_result = admin_client.table('mock_interview')\
            .select('id, title, interview_type, position, company_name')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not session_result.data:
            logger.warning(f"Session {session_id} not found for user {g.user.id[:8]}***")
            return jsonify({'error': 'Session not found'}), 404
        
        session_info = session_result.data[0]
        logger.info(f"Session found: {session_info['title']}")
        
        # Get all attempts for this session
        attempts_result = admin_client.table('mock_interview_attempts')\
            .select('*')\
            .eq('mock_interview_id', session_id)\
            .order('attempt_number', desc=False)\
            .execute()
        
        raw_attempts = attempts_result.data if attempts_result.data else []
        logger.info(f"Found {len(raw_attempts)} raw attempts for session {session_id}")
        
        # Process and enhance attempts for frontend compatibility
        processed_attempts = []
        for attempt in raw_attempts:
            # Status is already uppercase (PROCESSED, COMPLETED, ACTIVE, PENDING, etc.)
            status = attempt.get('status', 'PENDING')
            
            # Enhanced attempt object for frontend
            processed_attempt = {
                **attempt,
                'status': status,
                'has_feedback': bool(attempt.get('feedback')),
                'has_transcript': bool(attempt.get('transcript')),
                'has_live_transcription': bool(attempt.get('live_transcription')),
                'is_completed': status in ['COMPLETED', 'PROCESSED'],
                'is_processed': status == 'PROCESSED',
                'can_view_feedback': status in ['COMPLETED', 'PROCESSED'] and bool(attempt.get('feedback')),
                'duration_display': f"{attempt.get('actual_duration_minutes', 0)} min" if attempt.get('actual_duration_minutes') else 'N/A',
                'score_display': f"{attempt.get('evaluation_score', 0)}/100" if attempt.get('evaluation_score') is not None else 'Pending'
            }
            
            processed_attempts.append(processed_attempt)
            logger.info(f"Attempt {attempt.get('attempt_number')}: status={status}, has_feedback: {processed_attempt['has_feedback']}")
        
        # Format response with complete information
        response_data = {
            'session_id': session_id,
            'session_info': session_info,
            'attempts': processed_attempts,
            'total_attempts': len(processed_attempts),
            'max_attempts': 3,
            'remaining_attempts': max(0, 3 - len(processed_attempts))
        }
        
        logger.info(f"Returning {len(processed_attempts)} processed attempts for session {session_id}")
        return jsonify(response_data), 200
        
    except Exception as e:
        logger.error(f"Error getting session attempts: {str(e)}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/attempt/<attempt_id>', methods=['GET'])
@require_authentication
def get_attempt_details(attempt_id):
    """Get detailed information for a specific attempt including transcripts and feedback"""
    try:
        admin_client = get_admin_client()
        
        logger.info(f"Fetching attempt details for attempt {attempt_id} by user {g.user.id[:8]}***")
        
        # Get attempt with session info
        attempt_result = admin_client.table('mock_interview_attempts')\
            .select('*, mock_interview!inner(user_id, title, interview_type, position, company_name)')\
            .eq('id', attempt_id)\
            .execute()
        
        if not attempt_result.data:
            logger.warning(f"Attempt {attempt_id} not found")
            return jsonify({'error': 'Attempt not found'}), 404
        
        attempt = attempt_result.data[0]
        
        # Verify user owns this session
        if attempt['mock_interview']['user_id'] != g.user.id:
            logger.warning(f"Access denied for attempt {attempt_id} - user {g.user.id[:8]}*** != {attempt['mock_interview']['user_id'][:8]}***")
            return jsonify({'error': 'Access denied'}), 403
        
        # Status is already uppercase (PROCESSED, COMPLETED, ACTIVE, PENDING, etc.)
        status = attempt.get('status', 'PENDING')
        
        # Parse feedback JSON if it exists
        feedback_data = None
        if attempt.get('feedback'):
            try:
                if isinstance(attempt['feedback'], str):
                    feedback_data = json.loads(attempt['feedback'])
                else:
                    feedback_data = attempt['feedback']
            except json.JSONDecodeError:
                logger.warning(f"Invalid feedback JSON for attempt {attempt_id}")
                feedback_data = None
        
        # Parse transcript JSON if it exists
        transcript_data = None
        if attempt.get('transcript'):
            try:
                if isinstance(attempt['transcript'], str):
                    transcript_data = json.loads(attempt['transcript'])
                else:
                    transcript_data = attempt['transcript']
            except json.JSONDecodeError:
                logger.warning(f"Invalid transcript JSON for attempt {attempt_id}")
                transcript_data = None
        
        # Enhanced attempt object with parsed data
        enhanced_attempt = {
            **attempt,
            'status': status,
            'feedback': feedback_data,  # Parsed JSON
            'transcript': transcript_data,  # Parsed JSON
            'evaluation_score': attempt.get('evaluation_score'),
            'actual_duration_minutes': attempt.get('actual_duration_minutes', 0)
        }
        
        response_data = {
            'attempt': enhanced_attempt,
            'has_transcript': bool(transcript_data),
            'has_live_transcription': bool(attempt.get('live_transcription')),
            'has_feedback': bool(feedback_data),
            'is_completed': status in ['COMPLETED', 'PROCESSED'],
            'is_processed': status == 'PROCESSED',
            'can_view_feedback': status in ['COMPLETED', 'PROCESSED'] and bool(feedback_data)
        }
        
        logger.info(f"Attempt {attempt_id}: status={status}, has_feedback={bool(feedback_data)}, has_transcript={bool(transcript_data)}")
        
        return jsonify(response_data), 200
        
    except Exception as e:
        logger.error(f"Error getting attempt details: {str(e)}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/attempt/<attempt_id>/save-transcription', methods=['POST'])
@require_authentication
def save_attempt_transcription(attempt_id):
    """Save live transcription data for an active attempt and mark as completed"""
    try:
        logger.info(f"save_attempt_transcription called for attempt {attempt_id}")
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
        
        logger.info(f"Attempt lookup result for {attempt_id}: found={bool(attempt_result.data)}")
        
        if not attempt_result.data:
            return jsonify({'error': 'Attempt not found'}), 404
        
        attempt = attempt_result.data[0]
        current_status = attempt.get('status', 'unknown')
        
        logger.info(f"Current status for attempt {attempt_id}: {current_status}")
        
        if attempt['mock_interview']['user_id'] != g.user.id:
            return jsonify({'error': 'Access denied'}), 403
        
        if attempt['status'] == 'completed':
            logger.info(f"Attempt {attempt_id} already completed, skipping update")
            return jsonify({'error': 'Attempt already completed'}), 400
        
        # Calculate actual duration in minutes
        actual_duration_minutes = 0
        if attempt.get('started_at'):
            try:
                from datetime import datetime
                logger.info(f"Calculating duration for attempt {attempt_id}: started_at='{attempt.get('started_at')}'")
                started_at = datetime.fromisoformat(attempt['started_at'].replace('Z', '+00:00'))
                current_time = datetime.utcnow().replace(tzinfo=started_at.tzinfo)
                duration_seconds = (current_time - started_at).total_seconds()
                actual_duration_minutes = max(0, int(duration_seconds / 60))  # Convert to minutes, minimum 0
                logger.info(f"Duration calculation for attempt {attempt_id}: {duration_seconds} seconds = {actual_duration_minutes} minutes")
            except Exception as duration_error:
                logger.warning(f"Error calculating duration for attempt {attempt_id}: {duration_error}")
                actual_duration_minutes = 0
        else:
            logger.warning(f"No started_at timestamp for attempt {attempt_id}, cannot calculate duration")
        
        # Update live transcription AND mark as completed with duration
        update_data = {
            'live_transcription': json.dumps(live_transcription),
            'status': 'completed',
            'completed_at': 'now()',
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
            logger.info(f"Status updated from '{current_status}' to '{new_status}' for attempt {attempt_id}")
            logger.info(f"Saved live transcription and marked attempt {attempt_id} as completed (duration: {actual_duration_minutes} minutes)")
            return jsonify({
                'message': 'Live transcription saved and attempt completed successfully',
                'attempt_id': attempt_id,
                'status': 'completed',
                'actual_duration_minutes': actual_duration_minutes,
                'transcription_length': len(str(live_transcription))
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

@mock_interview_bp.route('/attempt/<attempt_id>/complete', methods=['POST'])
@require_authentication
def complete_attempt(attempt_id):
    """Complete an attempt and save final transcript and metadata"""
    try:
        data = request.get_json()
        transcript = data.get('transcript', {})
        live_transcription = data.get('live_transcription', {})
        actual_duration_minutes = data.get('actual_duration_minutes', 0)
        
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
        
        if attempt['status'] in ['completed', 'PROCESSED']:
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
        
        # Update attempt with completion data
        update_data = {
            'status': 'completed',
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
        
        update_result = admin_client.table('mock_interview_attempts')\
            .update(update_data)\
            .eq('id', attempt_id)\
            .execute()
        
        if update_result.data:
            completed_attempt = update_result.data[0]
            logger.info(f"Completed attempt {attempt_id} with {actual_duration_minutes} minutes duration")
            
            return jsonify({
                'message': 'Attempt completed successfully',
                'attempt': completed_attempt,
                'status': 'completed',
                'actual_duration_minutes': actual_duration_minutes,
                'processing_message': 'Your interview will be analyzed and feedback will be available soon.'
            }), 200
        else:
            return jsonify({'error': 'Failed to complete attempt'}), 500
        
    except Exception as e:
        logger.error(f"Error completing attempt: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500



@mock_interview_bp.route('/user/attempts', methods=['GET'])
@require_authentication
def get_user_all_attempts():
    """Get all attempts across all sessions for the authenticated user"""
    try:
        limit = request.args.get('limit', 20, type=int)
        limit = min(limit, 100)  # Cap at 100
        
        admin_client = get_admin_client()
        
        # Get user's attempts with session info
        attempts_result = admin_client.table('mock_interview_attempts')\
            .select('*, mock_interview!inner(user_id, title, interview_type, position, company_name, created_at)')\
            .order('created_at', desc=True)\
            .limit(limit)\
            .execute()
        
        if not attempts_result.data:
            return jsonify({
                'attempts': [],
                'total_count': 0
            }), 200
        
        # Filter attempts for this user only (additional security)
        user_attempts = [
            attempt for attempt in attempts_result.data 
            if attempt['mock_interview']['user_id'] == g.user.id
        ]
        
        # Format the response
        formatted_attempts = []
        for attempt in user_attempts:
            formatted_attempt = {
                **attempt,
                'session_title': attempt['mock_interview']['title'],
                'session_info': {
                    'interview_type': attempt['mock_interview']['interview_type'],
                    'position': attempt['mock_interview']['position'],
                    'company_name': attempt['mock_interview']['company_name'],
                    'created_at': attempt['mock_interview']['created_at']
                },
                'has_feedback': bool(attempt.get('feedback')),
                'is_processed': attempt.get('status') == 'PROCESSED'
            }
            formatted_attempts.append(formatted_attempt)
        
        return jsonify({
            'attempts': formatted_attempts,
            'total_count': len(formatted_attempts),
            'limit': limit
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting user attempts: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500


@mock_interview_bp.route('/session/<session_id>/transcriptions', methods=['GET'])
@require_authentication
def get_session_transcriptions(session_id):
    """Get real-time transcriptions for a specific interview session"""
    try:
        # Verify user has access to this session
        user_client = get_user_client()
        session_result = user_client.table('mock_interview')\
            .select('id')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not session_result.data:
            return jsonify({'error': 'Session not found or access denied'}), 404
        
        # Get transcriptions for this session
        try:
            admin_client = get_admin_client()
            transcriptions_result = admin_client.table('interview_transcriptions')\
                .select('*')\
                .eq('session_id', session_id)\
                .order('sequence_number', desc=False)\
                .execute()
            
            transcriptions = transcriptions_result.data if transcriptions_result.data else []
            
            # Get session summary
            summary = get_session_summary(session_id)
            
            return jsonify({
                'session_id': session_id,
                'transcriptions': transcriptions,
                'summary': summary,
                'total_count': len(transcriptions)
            }), 200
            
        except Exception as e:
            logger.error(f"Error fetching transcriptions: {str(e)}")
            return jsonify({'error': 'Failed to fetch transcriptions'}), 500
            
    except Exception as e:
        logger.error(f"Error in get_session_transcriptions: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500


@mock_interview_bp.route('/transcriptions/recent', methods=['GET'])
@require_authentication
def get_user_recent_transcriptions():
    """Get recent transcriptions for the authenticated user across all sessions"""
    try:
        limit = request.args.get('limit', 50, type=int)
        limit = min(limit, 100)  # Cap at 100 for performance
        
        transcriptions = get_user_transcriptions(g.user.id, limit=limit)
        
        return jsonify({
            'user_id': g.user.id,
            'transcriptions': transcriptions,
            'count': len(transcriptions),
            'limit': limit
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching user transcriptions: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500


@mock_interview_bp.route('/session/<session_id>/transcriptions/summary', methods=['GET'])
@require_authentication
def get_session_transcription_summary(session_id):
    """Get transcription summary and statistics for a session"""
    try:
        # Verify user has access to this session
        user_client = get_user_client()
        session_result = user_client.table('mock_interview')\
            .select('id, interview_type, position, company_name, created_at')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not session_result.data:
            return jsonify({'error': 'Session not found or access denied'}), 404
        
        session = session_result.data[0]
        summary = get_session_summary(session_id)
        
        return jsonify({
            'session_id': session_id,
            'session_info': session,
            'transcription_summary': summary
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching transcription summary: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500


@mock_interview_bp.route('/session/<session_id>/transcriptions/export', methods=['GET'])
@require_authentication
def export_session_transcriptions(session_id):
    """Export session transcriptions in various formats"""
    try:
        # Verify user has access to this session
        user_client = get_user_client()
        session_result = user_client.table('mock_interview')\
            .select('*')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not session_result.data:
            return jsonify({'error': 'Session not found or access denied'}), 404
        
        session = session_result.data[0]
        export_format = request.args.get('format', 'json').lower()
        
        # Get transcriptions
        try:
            admin_client = get_admin_client()
            transcriptions_result = admin_client.table('interview_transcriptions')\
                .select('speaker_type, speaker_name, message_text, message_type, wall_clock_time, sequence_number')\
                .eq('session_id', session_id)\
                .order('sequence_number', desc=False)\
                .execute()
            
            transcriptions = transcriptions_result.data if transcriptions_result.data else []
            
            if export_format == 'text':
                # Export as plain text conversation
                text_output = f"Interview Transcription\n"
                text_output += f"Session: {session['title']}\n"
                text_output += f"Position: {session['position']} at {session['company_name']}\n"
                text_output += f"Type: {session['interview_type']}\n"
                text_output += f"Date: {session['created_at']}\n"
                text_output += "=" * 50 + "\n\n"
                
                for t in transcriptions:
                    timestamp = t['wall_clock_time'][:19] if t['wall_clock_time'] else 'Unknown'
                    speaker = t['speaker_name'] or t['speaker_type'].title()
                    text_output += f"[{timestamp}] {speaker}: {t['message_text']}\n\n"
                
                from flask import make_response
                response = make_response(text_output)
                response.headers['Content-Type'] = 'text/plain'
                response.headers['Content-Disposition'] = f'attachment; filename="interview_{session_id}_transcript.txt"'
                return response
                
            else:
                # Default JSON export
                return jsonify({
                    'session_info': session,
                    'transcriptions': transcriptions,
                    'export_format': 'json',
                    'exported_at': datetime.utcnow().isoformat()
                }), 200
                
        except Exception as e:
            logger.error(f"Error exporting transcriptions: {str(e)}")
            return jsonify({'error': 'Failed to export transcriptions'}), 500
            
    except Exception as e:
        logger.error(f"Error in export_session_transcriptions: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500


@mock_interview_bp.route('/session/<session_id>/prep-status', methods=['GET'])
@require_authentication 
def get_session_prep_status(session_id):
    """Get the preparation status of an interview session"""
    try:
        admin_client = get_admin_client()
        result = admin_client.table('mock_interview')\
            .select('status, status_prep, agent_prompt')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not result.data:
            return jsonify({'error': 'Session not found'}), 404
        
        session = result.data[0]
        
        # Compute display status
        display_info = get_display_status(
            session.get('status', 'created'),
            session.get('status_prep', 'PENDING')
        )
        
        return jsonify({
            'session_id': session_id,
            'status': session.get('status', 'created'),
            'status_prep': session.get('status_prep', 'PENDING'),
            'display_status': display_info['display_status'],
            'display_text': display_info['display_text'],
            'is_ready_to_join': display_info['is_ready_to_join'],
            'color_class': display_info['color_class'],
            'agent_prompt_ready': bool(session.get('agent_prompt', '').strip()),
            'ready_for_agent': session.get('status_prep') == 'DONE'
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting session prep status: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500


@mock_interview_bp.route('/session/<session_id>/update-prep', methods=['POST'])
@require_authentication
def update_session_prep(session_id):
    """Update session preparation status and agent prompt (for testing/manual override)"""
    try:
        data = request.get_json()
        status_prep = data.get('status_prep')
        agent_prompt = data.get('agent_prompt', '')
        
        if status_prep not in ['PENDING', 'DONE']:
            return jsonify({'error': 'Invalid status_prep. Must be PENDING or DONE'}), 400
        
        admin_client = get_admin_client()
        
        # Verify session exists and user owns it
        result = admin_client.table('mock_interview')\
            .select('id')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not result.data:
            return jsonify({'error': 'Session not found'}), 404
        
        # Update the session
        update_data = {'status_prep': status_prep}
        if agent_prompt:
            update_data['agent_prompt'] = agent_prompt
        
        result = admin_client.table('mock_interview')\
            .update(update_data)\
            .eq('id', session_id)\
            .execute()
        
        if result.data:
            logger.info(f"Updated session {session_id} prep status to {status_prep}")
            
            # Get the updated session with all data
            updated_session_result = admin_client.table('mock_interview')\
                .select('*')\
                .eq('id', session_id)\
                .execute()
            
            if updated_session_result.data:
                session = updated_session_result.data[0]
                
                # Compute display status for the updated session
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
                    'session_id': session_id,
                    'status_prep': status_prep,
                    'agent_prompt_updated': bool(agent_prompt),
                    'message': f'Session preparation status updated to {status_prep}',
                    'session': session_with_display
                }), 200
            else:
                return jsonify({
                    'session_id': session_id,
                    'status_prep': status_prep,
                    'agent_prompt_updated': bool(agent_prompt),
                    'message': f'Session preparation status updated to {status_prep}'
                }), 200
        else:
            return jsonify({'error': 'Failed to update session'}), 500
            
    except Exception as e:
        logger.error(f"Error updating session prep: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500 

@mock_interview_bp.route('/session/<session_id>/status', methods=['GET'])
@require_authentication
def get_session_status(session_id):
    """Lightweight endpoint for polling session status - optimized for frontend real-time updates"""
    try:
        admin_client = get_admin_client()
        result = admin_client.table('mock_interview')\
            .select('status, status_prep, created_at')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not result.data:
            return jsonify({'error': 'Session not found'}), 404
        
        session = result.data[0]
        
        # Compute lightweight display status
        display_info = get_display_status(
            session.get('status', 'created'),
            session.get('status_prep', 'PENDING')
        )
        
        return jsonify({
            'session_id': session_id,
            'status': session.get('status'),
            'status_prep': session.get('status_prep', 'PENDING'),
            'display_status': display_info['display_status'],
            'display_text': display_info['display_text'],
            'is_ready_to_join': display_info['is_ready_to_join'],
            'color_class': display_info['color_class'],
            'last_updated': session.get('created_at')  # Could be updated_at if available
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting session status: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500 

@mock_interview_bp.route('/debug/attempts', methods=['GET'])
@require_authentication
def debug_user_attempts():
    """Debug endpoint to check user's attempts and their statuses"""
    try:
        admin_client = get_admin_client()
        user_id = g.user.id
        
        # Get all user sessions
        sessions_result = admin_client.table('mock_interview')\
            .select('id, title, status, created_at')\
            .eq('user_id', user_id)\
            .execute()
        
        sessions = sessions_result.data if sessions_result.data else []
        
        # Get all attempts for user's sessions
        session_ids = [session['id'] for session in sessions]
        
        debug_info = {
            'user_id': user_id[:8] + '***',
            'total_sessions': len(sessions),
            'session_ids': session_ids,
            'sessions': sessions,
            'attempts_by_session': {},
            'all_attempts': []
        }
        
        if session_ids:
            attempts_result = admin_client.table('mock_interview_attempts')\
                .select('*')\
                .in_('mock_interview_id', session_ids)\
                .execute()
            
            all_attempts = attempts_result.data if attempts_result.data else []
            debug_info['total_attempts'] = len(all_attempts)
            debug_info['all_attempts'] = all_attempts
            
            # Group attempts by session
            for attempt in all_attempts:
                session_id = attempt['mock_interview_id']
                if session_id not in debug_info['attempts_by_session']:
                    debug_info['attempts_by_session'][session_id] = []
                debug_info['attempts_by_session'][session_id].append({
                    'id': attempt['id'],
                    'attempt_number': attempt['attempt_number'],
                    'status': attempt['status'],
                    'has_feedback': bool(attempt.get('feedback')),
                    'has_transcript': bool(attempt.get('transcript')),
                    'evaluation_score': attempt.get('evaluation_score'),
                    'started_at': attempt.get('started_at'),
                    'completed_at': attempt.get('completed_at')
                })
        else:
            debug_info['total_attempts'] = 0
        
        return jsonify({
            'debug_data': debug_info,
            'summary': {
                'sessions_found': len(sessions),
                'attempts_found': debug_info.get('total_attempts', 0),
                'sessions_with_attempts': len([s for s in session_ids if s in debug_info.get('attempts_by_session', {})]),
                'status_breakdown': {}
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error in debug attempts endpoint: {str(e)}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/document/<int:document_id>/cover-letter-content', methods=['GET'])
@require_authentication
def get_cover_letter_content(document_id):
    """Extract and return cover letter text content from a user's document"""
    try:
        cover_letter_text, error = get_cover_letter_content_by_document_id(g.user.id, document_id)
        
        if error:
            return jsonify({'error': error}), 404
        
        return jsonify({
            'document_id': document_id,
            'cover_letter_text': cover_letter_text,
            'character_count': len(cover_letter_text) if cover_letter_text else 0
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching cover letter content: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/debug/user-documents', methods=['GET'])
@require_authentication
def debug_user_documents():
    """Debug endpoint to verify user document fetching works correctly"""
    try:
        admin_client = get_admin_client()
        user_id = g.user.id
        
        # Test all document queries
        all_docs_result = admin_client.table('user_documents')\
            .select('id, uid, document_name, document_type, document_url, created_at')\
            .eq('uid', user_id)\
            .order('created_at', desc=True)\
            .execute()
        
        resume_docs_result = admin_client.table('user_documents')\
            .select('id, uid, document_name, document_type, document_url, created_at')\
            .eq('uid', user_id)\
            .in_('document_type', ['resume', 'Resume', 'CV', 'cv'])\
            .order('created_at', desc=True)\
            .execute()
        
        cover_letter_docs_result = admin_client.table('user_documents')\
            .select('id, uid, document_name, document_type, document_url, created_at')\
            .eq('uid', user_id)\
            .in_('document_type', ['cover_letter', 'Cover Letter', 'coverletter', 'Cover_Letter'])\
            .order('created_at', desc=True)\
            .execute()
        
        debug_data = {
            'user_id': user_id[:8] + '***',
            'all_documents': {
                'count': len(all_docs_result.data or []),
                'documents': all_docs_result.data or [],
                'uids_present': [doc.get('uid', 'MISSING') for doc in (all_docs_result.data or [])]
            },
            'resume_documents': {
                'count': len(resume_docs_result.data or []),
                'documents': resume_docs_result.data or [],
                'uids_present': [doc.get('uid', 'MISSING') for doc in (resume_docs_result.data or [])]
            },
            'cover_letter_documents': {
                'count': len(cover_letter_docs_result.data or []),
                'documents': cover_letter_docs_result.data or [],
                'uids_present': [doc.get('uid', 'MISSING') for doc in (cover_letter_docs_result.data or [])]
            },
            'query_info': {
                'using_admin_client': True,
                'filtering_by_uid': user_id,
                'uid_in_select': True
            }
        }
        
        return jsonify({
            'debug_data': debug_data,
            'status': 'success',
            'message': 'Document queries executed successfully'
        }), 200
        
    except Exception as e:
        logger.error(f"Error in debug user documents: {str(e)}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return jsonify({'error': 'Internal server error', 'details': str(e)}), 500

@mock_interview_bp.route('/debug/session/<session_id>/content', methods=['GET'])
@require_authentication
def debug_session_content(session_id):
    """Debug endpoint to check session content including cover letter data"""
    try:
        admin_client = get_admin_client()
        
        # Get session with all content fields
        session_result = admin_client.table('mock_interview')\
            .select('id, title, resume_text, resume_url, resume_document_id, cover_letter_text, cover_letter_url, interview_context, created_at')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not session_result.data:
            return jsonify({'error': 'Session not found'}), 404
        
        session = session_result.data[0]
        
        # Parse interview_context if it's JSON
        interview_context = session.get('interview_context', {})
        if isinstance(interview_context, str):
            try:
                interview_context = json.loads(interview_context)
            except json.JSONDecodeError:
                interview_context = {}
        
        debug_data = {
            'session_id': session_id,
            'title': session.get('title'),
            'created_at': session.get('created_at'),
            'resume_data': {
                'resume_text_length': len(session.get('resume_text', '') or ''),
                'resume_url': session.get('resume_url'),
                'resume_document_id': session.get('resume_document_id'),
                'has_resume_text': bool(session.get('resume_text')),
                'has_resume_url': bool(session.get('resume_url'))
            },
            'cover_letter_data': {
                'cover_letter_text_length': len(session.get('cover_letter_text', '') or ''),
                'cover_letter_url': session.get('cover_letter_url'),
                'has_cover_letter_text': bool(session.get('cover_letter_text')),
                'has_cover_letter_url': bool(session.get('cover_letter_url'))
            },
            'interview_context': {
                'has_context': bool(interview_context),
                'context_keys': list(interview_context.keys()) if interview_context else [],
                'cover_letter_in_context': 'cover_letter_text' in interview_context if interview_context else False,
                'resume_in_context': 'resume_text' in interview_context if interview_context else False
            }
        }
        
        return jsonify({
            'debug_data': debug_data,
            'raw_session_data': session
        }), 200
        
    except Exception as e:
        logger.error(f"Error in debug session content endpoint: {str(e)}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return jsonify({'error': 'Internal server error'}), 500