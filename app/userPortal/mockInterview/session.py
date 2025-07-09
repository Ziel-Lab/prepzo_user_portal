import asyncio
import traceback
import logging
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomInputOptions,
    ConversationItemAddedEvent, 
    AgentStateChangedEvent,
)
from livekit.plugins import deepgram, silero, google, openai, noise_cancellation
from .agent import InterviewAgent
from ...extensions import supabase
from flask import current_app

logger = logging.getLogger(__name__)

async def initialize_interview_session(ctx: JobContext):
    """
    Initialize and run the interview agent session
    
    Args:
        ctx (JobContext): The job context from LiveKit
    """
    try:
        logger.info(f"Interview Agent JobContext received for room {ctx.room.name}")

        # Add shutdown hook
        async def _shutdown_hook():
            logger.info(f"Interview agent for room {ctx.room.name} is shutting down.")
            # Store interview results if available
            if hasattr(interview_agent, 'interview_results') and interview_agent.interview_results:
                await store_interview_results(ctx.room.name, interview_agent.interview_results)

        ctx.add_shutdown_callback(_shutdown_hook)
        logger.info(f"Shutdown hook registered for interview room {ctx.room.name}")

        logger.info("Initializing interview session components")
        
        # Initialize voice and LLM plugins
        vad_plugin = await asyncio.to_thread(silero.VAD.load)
        logger.info("VAD model initialized")
        
        # Initialize TTS plugin - use OpenAI for interviews
        tts_plugin = openai.TTS(
            model='tts-1',
            voice='nova'
        )
        logger.info("TTS client initialized with OpenAI TTS")
        
        # Initialize STT plugin
        stt_plugin = deepgram.STT(
            model="nova-2-conversationalai",
            keywords=[("interview", 1.5), ("experience", 1.2), ("skills", 1.2)],
            language="en-US",
            endpointing_ms=25,
            no_delay=True,
            numerals=True
        )
        logger.info("STT client initialized")
        
        # Initialize LLM plugin - use Google Gemini for interviews
        llm_plugin = google.LLM(
            model="gemini-1.5-flash",
            temperature=0.7
        )
        logger.info("LLM client initialized")
        
        # Connect to the room
        await ctx.connect()
        logger.info(f"Connected to interview room {ctx.room.name}")

        # Get interview configuration from room metadata or database
        interview_config = await get_interview_config(ctx.room.name)
        
        # Initialize the interview agent
        interview_agent = InterviewAgent(interview_config=interview_config)

        # Create the agent session
        session = AgentSession(
            vad=vad_plugin,
            stt=stt_plugin,
            llm=llm_plugin,
            tts=tts_plugin,
        )
        logger.info("Interview AgentSession created")
        
        # Register event handlers for session
        _register_interview_event_handlers(session, ctx.room.name)

        # Start the session
        logger.info(f"Starting Interview AgentSession in room {ctx.room.name}")
        input_options = RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC()
        )
        await session.start(room=ctx.room, agent=interview_agent, room_input_options=input_options)
        logger.info(f"Interview AgentSession started for room {ctx.room.name}")

    except Exception as e:
        logger.error(f"Error in interview agent session: {str(e)}")
        logger.error(traceback.format_exc())
        raise

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
        response = supabase.table('mock_interview_sessions').select('*').eq('room_name', room_name).execute()
        
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
    Store interview results in database
    
    Args:
        room_name (str): The room name
        interview_results (dict): The interview results
    """
    try:
        if not supabase:
            logger.warning("Supabase not available, cannot store interview results")
            return
        
        # Update the interview session with results
        update_data = {
            'transcript': interview_results.get('transcript', []),
            'feedback': interview_results.get('feedback', {}),
            'duration_seconds': interview_results.get('duration', 0),
            'completed_at': 'now()',
            'status': 'completed'
        }
        
        response = supabase.table('mock_interview_sessions').update(update_data).eq('room_name', room_name).execute()
        
        if response.data:
            logger.info(f"Successfully stored interview results for room {room_name}")
        else:
            logger.error(f"Failed to store interview results for room {room_name}")
            
    except Exception as e:
        logger.error(f"Error storing interview results: {str(e)}")

def _register_interview_event_handlers(session: AgentSession, room_name: str):
    """
    Register event handlers for the interview session
    
    Args:
        session (AgentSession): The agent session
        room_name (str): The room name
    """
    @session.on("conversation_item_added")
    def on_conversation_item_added(event: ConversationItemAddedEvent):
        if not hasattr(event, 'item'):
            logger.warning("'conversation_item_added' event missing 'item' attribute")
            return
            
        item = event.item
        role_str = ""
        item_role = getattr(item, 'role', None)
        
        if item_role == "user":
            role_str = "user"
        elif item_role == "assistant":
            if hasattr(item, 'tool_calls') and item.tool_calls: 
                return  # Skip tool calls
            role_str = "assistant"
        else:
            return  # Ignore other roles

        content = getattr(item, 'text_content', None)
        if content:
            logger.info(f"[{room_name}] {role_str}: {content}")

    @session.on("agent_state_changed")
    def on_agent_state_changed(event: AgentStateChangedEvent):
        logger.info(f"[{room_name}] Agent state changed to: {event.state}") 