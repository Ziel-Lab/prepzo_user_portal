import asyncio
import logging
from ...extensions import supabase

logger = logging.getLogger(__name__)

async def get_interview_config(room_name: str):
    """
    Get interview configuration from database
    
    Args:
        room_name (str): The room name
        
    Returns:
        dict: Interview configuration
    """
    try:
        if not supabase:
            logger.warning("Supabase not available, using default interview config")
            return get_default_interview_config()
        
        # Query interview session from database
        response = supabase.table('mock_interview').select('*').eq('room_name', room_name).execute()
        
        if response.data and len(response.data) > 0:
            session_data = response.data[0]
            
            interview_config = {
                'session_id': session_data.get('id'),
                'interview_type': session_data.get('interview_type', 'behavioral'),
                'difficulty_level': session_data.get('difficulty_level', 'medium'),
                'position': session_data.get('position', 'Software Engineer'),
                'duration_minutes': session_data.get('duration_minutes', 30),
                'custom_instructions': session_data.get('custom_instructions', ''),
                'room_name': room_name
            }
            
            # Get enhanced context if available
            if session_data.get('resume_url') or session_data.get('job_description'):
                interview_config['interview_context'] = {
                    'resume_text': await get_document_text(session_data.get('resume_url')),
                    'job_description': session_data.get('job_description')
                }
            
            logger.info(f"Retrieved interview config for room {room_name}")
            return interview_config
        else:
            logger.warning(f"No interview session found for room {room_name}, using default config")
            return get_default_interview_config()
            
    except Exception as e:
        logger.error(f"Error getting interview config: {str(e)}")
        return get_default_interview_config()

def get_default_interview_config():
    """Get default interview configuration"""
    return {
        'interview_type': 'behavioral',
        'difficulty_level': 'medium',
        'position': 'Software Engineer',
        'duration_minutes': 30,
        'custom_instructions': '',
        'room_name': 'default'
    }

async def get_document_text(document_url: str):
    """Get text content from document URL"""
    if not document_url:
        return None
    
    try:
        from .api import get_document_content
        return await get_document_content(document_url)
    except Exception as e:
        logger.error(f"Error getting document text: {str(e)}")
        return None

async def store_interview_results(room_name: str, interview_results: dict):
    """
    Store interview results in database - now works with attempts
    
    DISABLED: This function conflicts with agent.py and routes.py updates
    causing race conditions. Status and duration are now handled by:
    - agent.py: save_live_transcription (real-time during interview)
    - routes.py: save_attempt_transcription (frontend calls)
    
    Args:
        room_name (str): The room name
        interview_results (dict): The interview results
    """
    logger.info(f"store_interview_results called for {room_name} but DISABLED to prevent conflicts")
    return  # DISABLED - preventing race conditions with agent.py updates
    
    try:
        if not supabase:
            logger.warning("Supabase not available, cannot store interview results")
            return
        
        # Find the attempt by room_name
        response = supabase.table('mock_interview_attempts')\
            .select('*')\
            .eq('room_name', room_name)\
            .execute()
        
        if not response.data:
            logger.warning(f"No attempt found for room {room_name}")
            return
            
        attempt_data = response.data[0]
        attempt_id = attempt_data.get('id')
        
        # Update the attempt with results
        update_data = {
            'transcript': interview_results.get('transcript', []),
            'feedback': interview_results.get('feedback', {}),
            'evaluation_score': interview_results.get('score'),
            'status': 'completed'
        }
        
        # Only update duration if not already set
        if not attempt_data.get('actual_duration_minutes') and interview_results.get('duration'):
            update_data['actual_duration_minutes'] = interview_results.get('duration', 0) / 60
            update_data['completed_at'] = 'now()'
        
        result = supabase.table('mock_interview_attempts')\
            .update(update_data)\
            .eq('id', attempt_id)\
            .execute()
        
        if result.data:
            logger.info(f"Successfully stored interview results for attempt {attempt_id}")
        else:
            logger.error(f"Failed to store interview results for attempt {attempt_id}")
            
    except Exception as e:
        logger.error(f"Error storing interview results: {str(e)}")


async def create_new_attempt(session_id: str, room_name: str = None) -> dict:
    """
    Create a new attempt for a session
    
    Args:
        session_id (str): The mock_interview session ID
        room_name (str): Optional custom room name
        
    Returns:
        dict: The created attempt data or None if failed
    """
    try:
        if not supabase:
            logger.warning("Supabase not available, cannot create attempt")
            return None
        
        # Check existing attempts
        attempts_response = supabase.table('mock_interview_attempts')\
            .select('attempt_number')\
            .eq('mock_interview_id', session_id)\
            .order('attempt_number', desc=True)\
            .execute()
        
        existing_attempts = attempts_response.data or []
        next_attempt_number = len(existing_attempts) + 1
        
        if next_attempt_number > 3:
            logger.warning(f"Session {session_id} already has maximum 3 attempts")
            return None
        
        # Generate room name if not provided
        if not room_name:
            room_name = f"interview_{session_id}_attempt_{next_attempt_number}"
        
        # Create new attempt
        new_attempt = {
            'mock_interview_id': session_id,
            'attempt_number': next_attempt_number,
            'room_name': room_name,
            'status': 'pending'
        }
        
        insert_result = supabase.table('mock_interview_attempts')\
            .insert(new_attempt)\
            .execute()
        
        if insert_result.data:
            logger.info(f"Created new attempt {next_attempt_number} for session {session_id}")
            return insert_result.data[0]
        else:
            logger.error(f"Failed to create new attempt for session {session_id}")
            return None
            
    except Exception as e:
        logger.error(f"Error creating new attempt: {e}")
        return None


async def get_session_attempts(session_id: str) -> list:
    """
    Get all attempts for a session
    
    Args:
        session_id (str): The mock_interview session ID
        
    Returns:
        list: List of attempts for the session
    """
    try:
        if not supabase:
            logger.warning("Supabase not available, cannot get attempts")
            return []
        
        response = supabase.table('mock_interview_attempts')\
            .select('*')\
            .eq('mock_interview_id', session_id)\
            .order('attempt_number', desc=False)\
            .execute()
        
        return response.data or []
        
    except Exception as e:
        logger.error(f"Error getting session attempts: {e}")
        return []


async def get_attempt_by_room_name(room_name: str) -> dict:
    """
    Get attempt data by room name
    
    Args:
        room_name (str): The LiveKit room name
        
    Returns:
        dict: Attempt data with session info or None if not found
    """
    try:
        if not supabase:
            logger.warning("Supabase not available, cannot get attempt")
            return None
        
        response = supabase.table('mock_interview_attempts')\
            .select('*, mock_interview(*)')\
            .eq('room_name', room_name)\
            .execute()
        
        if response.data:
            return response.data[0]
        else:
            logger.warning(f"No attempt found for room {room_name}")
            return None
            
    except Exception as e:
        logger.error(f"Error getting attempt by room name: {e}")
        return None

# Removed orphaned transcription functions - no longer called by any event handlers
# Transcription is now handled correctly via MockInterviewAgent.transcription_handler
# These functions were orphaned - no event handlers call them anymore


# Removed update_attempt_live_transcription - now handled directly in agent.py via save_live_transcription 