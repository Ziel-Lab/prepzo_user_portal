from flask import request, jsonify, current_app, g
from . import mock_interview_bp
from ..subscription.helpers import require_authentication
from ...extensions import supabase, livekit_client
import uuid
import asyncio
import openai
import requests
import PyPDF2
import io
from .api import create_interview_room, get_room_token
from .prompt import get_interview_prompt, INTERVIEW_TYPES
import logging

logger = logging.getLogger(__name__)

async def extract_resume_text_from_url(file_url):
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
        logger.error(f"Error extracting PDF text: {str(e)}")
        return f"Error extracting PDF content: {str(e)}"

async def get_resume_content_by_document_id(user_id, document_id):
    """Get resume content by document ID"""
    try:
        # Get document details
        result = supabase.table('user_documents')\
            .select('document_url, document_name, document_type')\
            .eq('id', document_id)\
            .eq('uid', user_id)\
            .execute()
        
        if not result.data:
            return None, "Document not found"
        
        document = result.data[0]
        document_url = document['document_url']
        
        # Extract text content
        resume_text = await extract_resume_text_from_url(document_url)
        
        return resume_text, None
        
    except Exception as e:
        logger.error(f"Error getting resume content: {str(e)}")
        return None, str(e)

async def get_most_recent_resume(user_id):
    """Get the most recent resume document and extract its content"""
    try:
        # Get the most recent resume document
        result = supabase.table('user_documents')\
            .select('id, document_url, document_name, document_type')\
            .eq('uid', user_id)\
            .eq('document_type', 'resume')\
            .order('created_at', desc=True)\
            .limit(1)\
            .execute()
        
        if not result.data:
            return None, "No resume documents found"
        
        document = result.data[0]
        document_id = document['id']
        document_url = document['document_url']
        
        # Extract text content
        resume_text = await extract_resume_text_from_url(document_url)
        
        return {
            'document_id': document_id,
            'resume_text': resume_text,
            'resume_file_url': document_url,
            'document_name': document['document_name']
        }, None
        
    except Exception as e:
        logger.error(f"Error getting most recent resume: {str(e)}")
        return None, str(e)

async def generate_swot_analysis(session, transcript):
    """Generate SWOT analysis from interview transcript using OpenAI"""
    try:
        openai_api_key = current_app.config.get('OPENAI_API_KEY')
        if not openai_api_key:
            logger.error("OPENAI_API_KEY not found in configuration")
            return None
        
        # Set up OpenAI client
        client = openai.OpenAI(api_key=openai_api_key)
        
        # Build context for SWOT analysis
        position = session.get('position', 'Software Engineer')
        company_name = session.get('company_name', 'the company')
        interview_type = session.get('interview_type', 'behavioral')
        job_description = session.get('job_description', '')
        resume_text = session.get('resume_text', '')
        
        swot_prompt = f"""
Analyze the following mock interview transcript and generate a comprehensive SWOT analysis for the candidate.

CONTEXT:
- Position: {position} at {company_name}
- Interview Type: {interview_type}
- Job Description: {job_description}
- Candidate's Resume: {resume_text}

INTERVIEW TRANSCRIPT:
{transcript}

Please provide a detailed SWOT analysis in the following JSON format:

{{
    "strengths": [
        "Specific strength 1 with example from interview",
        "Specific strength 2 with example from interview",
        "..."
    ],
    "weaknesses": [
        "Area for improvement 1 with specific example",
        "Area for improvement 2 with specific example", 
        "..."
    ],
    "opportunities": [
        "Growth opportunity 1 based on their background",
        "Skill development area that could benefit their career",
        "..."
    ],
    "threats": [
        "Potential challenge or gap relative to the role",
        "Competitive disadvantage they should address",
        "..."
    ],
    "overall_score": 85,
    "overall_feedback": "Summary of overall performance with specific examples",
    "key_recommendations": [
        "Specific actionable recommendation 1",
        "Specific actionable recommendation 2",
        "..."
    ],
    "interviewer_notes": "Additional insights from the interview that would help HR/hiring managers"
}}

Focus on:
1. Specific examples from the interview transcript
2. How their responses align with the job requirements
3. Communication skills and presentation
4. Technical knowledge (if applicable)
5. Cultural fit and soft skills
6. Areas where they excelled or struggled
7. Actionable advice for improvement

Be constructive, specific, and reference actual parts of the interview.
"""
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an expert interview analyst and career coach. Provide detailed, constructive, and actionable feedback based on interview performance."},
                {"role": "user", "content": swot_prompt}
            ],
            temperature=0.3,
            max_tokens=2000
        )
        
        # Parse JSON response
        import json
        swot_analysis = json.loads(response.choices[0].message.content)
        
        logger.info(f"Generated SWOT analysis for session {session.get('id')}")
        return swot_analysis
        
    except Exception as e:
        logger.error(f"Error generating SWOT analysis: {str(e)}")
        return {
            "error": "Failed to generate SWOT analysis",
            "message": str(e),
            "strengths": ["Unable to analyze - please review transcript manually"],
            "weaknesses": ["Analysis generation failed"],
            "opportunities": ["Manual review recommended"],
            "threats": ["Technical error in analysis"],
            "overall_score": 0,
            "overall_feedback": "SWOT analysis could not be generated due to technical error",
            "key_recommendations": ["Please contact support for manual analysis"],
            "interviewer_notes": f"Error: {str(e)}"
        }

@mock_interview_bp.route('/create-session', methods=['POST'])
@require_authentication
def create_interview_session():
    """Create a new mock interview session with enhanced context"""
    try:
        data = request.get_json()
        
        # Basic interview parameters
        interview_type = data.get('interview_type', 'behavioral')
        difficulty_level = data.get('difficulty_level', 'medium')
        position = data.get('position', 'Software Engineer')
        duration_minutes = data.get('duration_minutes', 30)
        
        # Enhanced context data
        resume_text = data.get('resume_text', '')
        resume_file_url = data.get('resume_file_url', '')
        resume_document_id = data.get('resume_document_id')  # New: document ID option
        job_description = data.get('job_description', '')
        company_name = data.get('company_name', '')
        company_details = data.get('company_details', '')
        linkedin_profile = data.get('linkedin_profile', '')
        custom_instructions = data.get('custom_instructions', '')
        
        # Validate interview type
        if interview_type not in INTERVIEW_TYPES:
            return jsonify({
                'error': 'Invalid interview type',
                'valid_types': list(INTERVIEW_TYPES.keys())
            }), 400
        
        # Handle resume content - fetch from document if document_id provided, or auto-fetch most recent
        if resume_document_id:
            extracted_resume_text, error = asyncio.run(get_resume_content_by_document_id(g.user.id, resume_document_id))
            if error:
                return jsonify({
                    'error': f'Failed to extract resume content: {error}'
                }), 400
            resume_text = extracted_resume_text
            
            # Also get the document URL for reference
            doc_result = supabase.table('user_documents')\
                .select('document_url')\
                .eq('id', resume_document_id)\
                .eq('uid', g.user.id)\
                .execute()
            if doc_result.data:
                resume_file_url = doc_result.data[0]['document_url']
        
        # If no resume data provided, automatically fetch the most recent resume
        elif not resume_text and not resume_file_url:
            recent_resume, error = asyncio.run(get_most_recent_resume(g.user.id))
            if error:
                return jsonify({
                    'error': f'No resume provided and failed to fetch recent resume: {error}'
                }), 400
            
            if recent_resume:
                resume_text = recent_resume['resume_text']
                resume_file_url = recent_resume['resume_file_url']
                resume_document_id = recent_resume['document_id']
            else:
                return jsonify({
                    'error': 'No resume provided and no resume documents found. Please upload a resume first.'
                }), 400
        
        # Final validation for required fields
        if not job_description:
            return jsonify({
                'error': 'Job description is required'
            }), 400
        
        # Generate unique session ID
        session_id = str(uuid.uuid4())
        room_name = f"interview_{session_id}"
        
        # Create LiveKit room
        room_response = asyncio.run(create_interview_room(room_name))
        if not room_response:
            return jsonify({'error': 'Failed to create interview room'}), 500
        
        # Prepare interview context for AI agent
        interview_context = {
            'resume_text': resume_text,
            'resume_file_url': resume_file_url,
            'job_description': job_description,
            'company_name': company_name,
            'company_details': company_details,
            'linkedin_profile': linkedin_profile,
            'position': position,
            'interview_type': interview_type,
            'difficulty_level': difficulty_level,
            'custom_instructions': custom_instructions
        }
        
        # Store enhanced session in database
        session_data = {
            'id': session_id,
            'user_id': g.user.id,
            'interview_type': interview_type,
            'difficulty_level': difficulty_level,
            'position': position,
            'duration_minutes': duration_minutes,
            'room_name': room_name,
            'status': 'created',
            'interview_context': interview_context,
            'resume_text': resume_text,
            'resume_file_url': resume_file_url,
            'resume_document_id': resume_document_id,
            'job_description': job_description,
            'company_name': company_name,
            'company_details': company_details,
            'linkedin_profile': linkedin_profile,
            'custom_instructions': custom_instructions,
            'created_at': 'now()',
            'updated_at': 'now()'
        }
        
        result = supabase.table('mock_interview_sessions').insert(session_data).execute()
        
        if result.data:
            return jsonify({
                'session_id': session_id,
                'room_name': room_name,
                'interview_type': interview_type,
                'position': position,
                'company_name': company_name,
                'status': 'created',
                'context_loaded': bool(resume_text or resume_file_url),
                'resume_auto_extracted': bool(resume_document_id),
                'resume_character_count': len(resume_text) if resume_text else 0
            }), 201
        else:
            logger.error(f"Failed to create session in database: {result}")
            return jsonify({'error': 'Failed to create session'}), 500
            
    except Exception as e:
        logger.error(f"Error creating interview session: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/join/<session_id>', methods=['GET'])
@require_authentication
def join_interview_session(session_id):
    """Get room credentials to join an interview session"""
    try:
        # Verify session belongs to user
        result = supabase.table('mock_interview_sessions')\
            .select('*')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not result.data:
            return jsonify({'error': 'Session not found'}), 404
        
        session = result.data[0]
        room_name = session['room_name']
        
        # Generate room token
        token = asyncio.run(get_room_token(room_name, g.user.id))
        if not token:
            return jsonify({'error': 'Failed to generate room token'}), 500
        
        # Update session status
        supabase.table('mock_interview_sessions')\
            .update({'status': 'active', 'updated_at': 'now()'})\
            .eq('id', session_id)\
            .execute()
        
        return jsonify({
            'token': token,
            'room_name': room_name,
            'livekit_url': current_app.config.get('LIVEKIT_URL'),
            'session': session
        }), 200
        
    except Exception as e:
        logger.error(f"Error joining interview session: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/configure', methods=['POST'])
@require_authentication
def configure_interview():
    """Configure interview parameters and get prompt"""
    try:
        data = request.get_json()
        interview_type = data.get('interview_type', 'behavioral')
        difficulty_level = data.get('difficulty_level', 'medium')
        position = data.get('position', 'Software Engineer')
        custom_instructions = data.get('custom_instructions', '')
        
        # Get interview prompt
        prompt = get_interview_prompt(
            interview_type=interview_type,
            difficulty_level=difficulty_level,
            position=position,
            custom_instructions=custom_instructions
        )
        
        return jsonify({
            'prompt': prompt,
            'interview_type': interview_type,
            'difficulty_level': difficulty_level,
            'position': position
        }), 200
        
    except Exception as e:
        logger.error(f"Error configuring interview: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/sessions', methods=['GET'])
@require_authentication
def get_user_sessions():
    """Get user's interview sessions"""
    try:
        result = supabase.table('mock_interview_sessions')\
            .select('*')\
            .eq('user_id', g.user.id)\
            .order('created_at', desc=True)\
            .execute()
        
        return jsonify({
            'sessions': result.data or []
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching user sessions: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/session/<session_id>/end', methods=['POST'])
@require_authentication
def end_interview_session(session_id):
    """End an interview session and provide feedback"""
    try:
        data = request.get_json()
        feedback = data.get('feedback', {})
        transcript = data.get('transcript', '')
        
        # Update session
        update_data = {
            'status': 'completed',
            'ended_at': 'now()',
            'updated_at': 'now()',
            'transcript': transcript,
            'feedback': feedback
        }
        
        result = supabase.table('mock_interview_sessions')\
            .update(update_data)\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if result.data:
            return jsonify({
                'status': 'completed',
                'feedback': feedback
            }), 200
        else:
            return jsonify({'error': 'Session not found'}), 404
            
    except Exception as e:
        logger.error(f"Error ending interview session: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/interview-types', methods=['GET'])
def get_interview_types():
    """Get available interview types"""
    return jsonify({
        'interview_types': INTERVIEW_TYPES
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
        result = supabase.table('mock_interview_sessions')\
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
        
        result = supabase.table('mock_interview_sessions')\
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
        # Get user documents from the documents table
        result = supabase.table('user_documents')\
            .select('id, document_name, document_type, document_url, created_at')\
            .eq('uid', g.user.id)\
            .order('created_at', desc=True)\
            .execute()
        
        return jsonify({
            'documents': result.data or []
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
        result = supabase.table('mock_interview_sessions')\
            .select('*')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not result.data:
            return jsonify({'error': 'Session not found'}), 404
        
        session = result.data[0]
        
        # Return comprehensive results
        return jsonify({
            'session_id': session_id,
            'interview_type': session.get('interview_type'),
            'position': session.get('position'),
            'company_name': session.get('company_name'),
            'status': session.get('status'),
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
        result = supabase.table('mock_interview_sessions')\
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
    """Mark session as ready for agent (agent runs as separate process)"""
    try:
        # Get session data
        result = supabase.table('mock_interview_sessions')\
            .select('*')\
            .eq('id', session_id)\
            .eq('user_id', g.user.id)\
            .execute()
        
        if not result.data:
            return jsonify({'error': 'Session not found'}), 404
        
        session = result.data[0]
        
        if session.get('status') != 'active':
            return jsonify({'error': 'Session must be active to start agent'}), 400
        
        # Update session status to indicate agent should join
        # The actual LiveKit agent runs as a separate process and will
        # detect rooms and join them automatically
        supabase.table('mock_interview_sessions')\
            .update({
                'status': 'agent_ready', 
                'updated_at': 'now()'
            })\
            .eq('id', session_id)\
            .execute()
        
        logger.info(f"Session {session_id} marked as ready for agent. Agent process should detect and join room: {session['room_name']}")
        
        return jsonify({
            'status': 'agent_ready',
            'session_id': session_id,
            'room_name': session['room_name'],
            'message': 'Session is ready for AI interviewer. The agent process will join automatically.'
        }), 200
        
    except Exception as e:
        logger.error(f"Error marking session for agent: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@mock_interview_bp.route('/document/<int:document_id>/resume-content', methods=['GET'])
@require_authentication
def get_resume_content(document_id):
    """Extract and return resume text content from a user's document"""
    try:
        resume_text, error = asyncio.run(get_resume_content_by_document_id(g.user.id, document_id))
        
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
        # Get user documents filtered for resumes
        result = supabase.table('user_documents')\
            .select('id, document_name, document_type, document_url, created_at')\
            .eq('uid', g.user.id)\
            .eq('document_type', 'resume')\
            .order('created_at', desc=True)\
            .execute()
        
        return jsonify({
            'resume_documents': result.data or []
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching resume documents: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500 