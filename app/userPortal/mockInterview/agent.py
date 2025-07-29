"""
Simplified LiveKit Agent for Mock Interviews
Only gets agent_prompt from database + transcription
"""

import os
import asyncio
import logging
import json
import time
from typing import Dict, Any, Optional
from datetime import datetime

# Fix Windows encoding issues with Unicode characters in job descriptions
os.environ['PYTHONIOENCODING'] = 'utf-8'

def safe_encode_text(text: str) -> str:
    """Safely encode text to handle Unicode characters on Windows"""
    if not text:
        return ""
    try:
        # Ensure text is properly encoded as UTF-8 and handle any problematic characters
        return text.encode('utf-8', errors='replace').decode('utf-8')
    except Exception as e:
        logger.warning(f"Error encoding text: {e}. Using fallback.")
        # Fallback: remove non-ASCII characters
        return ''.join(char if ord(char) < 128 else '?' for char in str(text))

from livekit import agents
from livekit.agents import (
    Agent, 
    AgentSession, 
    JobContext, 
    RunContext, 
    llm,
    function_tool
)
from livekit.plugins.openai import realtime
from livekit.plugins import silero
from livekit.plugins.turn_detector.english import EnglishModel
from livekit.plugins.openai import stt as openai_stt

# Configure logging for production with UTF-8 support
try:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        encoding='utf-8'  # Explicit UTF-8 encoding for log files
    )
except Exception:
    # Fallback for older Python versions
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
logger = logging.getLogger(__name__)


class SimpleMockInterviewAgent(Agent):
    """Simplified mock interview agent - only agent_prompt + transcription + simple ending"""
    
    def __init__(self, agent_prompt: str, session_id: str, attempt_id: str) -> None:
        self.session_id = session_id
        self.attempt_id = attempt_id
        self._session_start_time = datetime.utcnow()
        self._timeout_task = None
        
        # Use the agent prompt directly
        instructions = safe_encode_text(agent_prompt.strip())
        
        # Initialize Agent with instructions from database only
        super().__init__(instructions=instructions)
        
        logger.info(f"Simple agent created for session {session_id}")
    
    async def on_enter(self) -> None:
        """Called when agent enters the session"""
        logger.info(f"Simple agent entered session {self.session_id}")
        
        # Start 14-minute timeout as backup
        self._timeout_task = asyncio.create_task(self._timeout_handler())
        
        # Generate initial greeting
        await self.session.generate_reply(
            instructions=safe_encode_text("Begin the interview with an appropriate greeting and introduction as specified in your instructions.")
        )
    
    async def on_exit(self) -> None:
        """Called when agent exits the session"""
        logger.info(f"Simple agent exited session {self.session_id}")
        
        # Cancel timeout task
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
    
    async def _timeout_handler(self) -> None:
        """14-minute timeout backup to prevent stuck sessions"""
        try:
            # Wait 14 minutes (840 seconds)
            await asyncio.sleep(840)
            
            logger.info("14-minute timeout reached - auto-ending interview")
            await self.end_interview("timeout - session duration limit reached")
            
        except asyncio.CancelledError:
            logger.info("Timeout handler cancelled - session ended naturally")
        except Exception as e:
            logger.error(f"Error in timeout handler: {e}")
    
    @function_tool()
    async def end_interview(self, context: RunContext, reason: str = "Interview completed"):
        """End the interview - brief notification then RPC call to frontend"""
        try:
            logger.info(f"Ending interview with reason: {reason}")
            
            # Step 1: Quick goodbye message
            await context.session.generate_reply(
                instructions=safe_encode_text("Thank you for the interview. The session will end shortly.")
            )
            
            # Step 2: Wait 1 second for message delivery
            await asyncio.sleep(1)
            
            # Step 3: Find user participant and call RPC
            try:
                user_participants = [p for p in context.room.remote_participants 
                                   if not p.identity.startswith('agent')]
                
                if user_participants:
                    user_identity = user_participants[0].identity
                    
                    # Call frontend RPC method 'forceEndInterview'
                    await context.room.local_participant.perform_rpc(
                        destination_identity=user_identity,
                        method='forceEndInterview',  # Exact frontend method name
                        payload=json.dumps({
                            'reason': safe_encode_text(reason),
                            'timestamp': datetime.utcnow().isoformat()
                        })
                    )
                    logger.info(f"RPC forceEndInterview sent successfully to {user_identity}")
                else:
                    logger.warning("No user participants found for RPC call")
            
            except Exception as rpc_error:
                logger.error(f"Error sending RPC: {rpc_error}")
            
            # Step 4: Always disconnect agent after RPC (or attempt)
            try:
                # Cancel timeout task since we're ending
                if self._timeout_task and not self._timeout_task.done():
                    self._timeout_task.cancel()
                
                # Give frontend a moment to process RPC
                await asyncio.sleep(2)
                
                # Disconnect the agent from the room
                await context.room.disconnect()
                logger.info("Agent disconnected from room successfully")
                
            except Exception as disconnect_error:
                logger.error(f"Error disconnecting from room: {disconnect_error}")
                # Try alternative disconnect method
                try:
                    if hasattr(context.session, 'close'):
                        await context.session.close()
                        logger.info("Agent session closed successfully")
                except Exception as close_error:
                    logger.error(f"Error closing session: {close_error}")
        
        except Exception as e:
            logger.error(f"Error ending interview: {e}")
            # Fallback: force disconnect
            try:
                if hasattr(context, 'room'):
                    await context.room.disconnect()
                elif hasattr(context, 'session') and hasattr(context.session, 'close'):
                    await context.session.close()
                logger.info("Fallback disconnect successful")
            except Exception as fallback_error:
                logger.error(f"Fallback disconnect failed: {fallback_error}")


async def get_agent_prompt_from_db(room_name: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Get agent_prompt from database based on room name"""
    try:
        logger.info(f"Looking up agent_prompt for room: {room_name}")
        
        # Import here to avoid circular imports
        from ...extensions import get_admin_client
        admin_client = get_admin_client()
        
        # First, try to find the attempt by room_name
        attempt_result = admin_client.table('mock_interview_attempts')\
            .select('id, mock_interview_id, mock_interview(id, agent_prompt, status_prep)')\
            .eq('room_name', room_name)\
            .execute()
        
        if attempt_result.data:
            attempt_data = attempt_result.data[0]
            session_data = attempt_data['mock_interview']
            
            # Check if status_prep is DONE
            if session_data.get('status_prep', 'PENDING') != 'DONE':
                logger.error(f"Session not ready (status_prep: {session_data.get('status_prep')})")
                return None, None, None
            
            agent_prompt = session_data.get('agent_prompt', '')
            if not agent_prompt or not agent_prompt.strip():
                logger.error("No agent_prompt found in database")
                return None, None, None
            
            session_id = session_data.get('id', '')
            attempt_id = attempt_data.get('id', '')
            
            logger.info(f"Found agent_prompt for session {session_id}")
            return safe_encode_text(agent_prompt), session_id, attempt_id
        
        # Fallback: try to extract session ID from room name
        import re
        uuid_pattern = r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})'
        uuid_matches = re.findall(uuid_pattern, room_name)
        
        if uuid_matches:
            potential_session_id = uuid_matches[-1]
            
            # Try to find session directly
            session_result = admin_client.table('mock_interview')\
                .select('id, agent_prompt, status_prep')\
                .eq('id', potential_session_id)\
                .execute()
                
            if session_result.data:
                session_data = session_result.data[0]
                
                if session_data.get('status_prep', 'PENDING') != 'DONE':
                    logger.error(f"Session not ready (status_prep: {session_data.get('status_prep')})")
                    return None, None, None
                
                agent_prompt = session_data.get('agent_prompt', '')
                if not agent_prompt or not agent_prompt.strip():
                    logger.error("No agent_prompt found in database")
                    return None, None, None
                
                # Create a simple attempt ID (not saving to DB since this is simplified)
                attempt_id = f"simple_attempt_{int(time.time())}"
                
                logger.info(f"Found agent_prompt for session {potential_session_id}")
                return safe_encode_text(agent_prompt), potential_session_id, attempt_id
        
        logger.error(f"No session found for room_name {room_name}")
        return None, None, None
            
    except Exception as e:
        logger.error(f"Error getting agent_prompt: {e}")
        return None, None, None


async def save_live_transcription(attempt_id: str, speaker_type: str, speaker_name: str, content: str):
    """Save message directly to live transcription in mock_interview_attempts table and mark as completed"""
    try:
        from ...extensions import get_admin_client
        admin_client = get_admin_client()
        
        # Get current live transcription, status, and started_at for duration calculation
        response = admin_client.table('mock_interview_attempts')\
            .select('live_transcription, status, started_at')\
            .eq('id', attempt_id)\
            .execute()
        
        if not response.data:
            logger.warning(f"No attempt found with id {attempt_id}")
            return
        
        attempt_data = response.data[0]
        current_transcription = attempt_data.get('live_transcription') or {"conversation": []}
        current_status = attempt_data.get('status', 'pending')
        started_at = attempt_data.get('started_at')
        
        logger.info(f"Current status for attempt {attempt_id}: {current_status}")
        
        # Add new conversation entry with safe encoding
        from datetime import datetime
        conversation_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "speaker_type": safe_encode_text(speaker_type),
            "speaker_name": safe_encode_text(speaker_name),
            "message": safe_encode_text(content),
            "sequence": len(current_transcription.get("conversation", [])) + 1
        }
        
        current_transcription["conversation"].append(conversation_entry)
        
        # Update the attempt with new live transcription AND mark as completed
        update_data = {
            'live_transcription': current_transcription,
            'updated_at': 'now()'
        }
        
        # Only update status if not already in final state (completed or PROCESSED)
        if current_status not in ['completed', 'PROCESSED']:
            # Calculate actual duration in minutes
            actual_duration_minutes = 0
            if started_at:
                try:
                    logger.info(f"Calculating duration for attempt {attempt_id}: started_at='{started_at}'")
                    started_at_dt = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                    current_time = datetime.utcnow().replace(tzinfo=started_at_dt.tzinfo)
                    duration_seconds = (current_time - started_at_dt).total_seconds()
                    actual_duration_minutes = max(0, int(duration_seconds / 60))  # Convert to minutes, minimum 0
                    logger.info(f"Duration calculation for attempt {attempt_id}: {duration_seconds} seconds = {actual_duration_minutes} minutes")
                except Exception as duration_error:
                    logger.warning(f"Error calculating duration for attempt {attempt_id}: {duration_error}")
                    actual_duration_minutes = 0
            else:
                logger.warning(f"No started_at timestamp for attempt {attempt_id}, cannot calculate duration")
            
            update_data['status'] = 'completed'
            update_data['completed_at'] = 'now()'
            update_data['actual_duration_minutes'] = actual_duration_minutes
            logger.info(f"Marking attempt {attempt_id} as completed with live transcription (duration: {actual_duration_minutes} minutes)")
        else:
            logger.info(f"Attempt {attempt_id} already in final state ({current_status}), only updating transcription")
        
        logger.info(f"Updating attempt {attempt_id} with data: {list(update_data.keys())}")
        
        update_response = admin_client.table('mock_interview_attempts')\
            .update(update_data)\
            .eq('id', attempt_id)\
            .execute()
        
        logger.info(f"Update response for attempt {attempt_id}: success={bool(update_response.data)}, data_length={len(update_response.data) if update_response.data else 0}")
        
        if update_response.data:
            updated_attempt = update_response.data[0]
            new_status = updated_attempt.get('status', 'unknown')
            status_msg = "and marked as completed" if current_status != 'completed' else ""
            duration_msg = f" (duration: {update_data.get('actual_duration_minutes', 'unknown')} minutes)" if current_status != 'completed' else ""
            logger.info(f"Saved live transcription for attempt {attempt_id} {status_msg}{duration_msg}: {speaker_type} - {content[:50]}...")
            logger.info(f"Status updated from '{current_status}' to '{new_status}' for attempt {attempt_id}")
        else:
            logger.error(f"Failed to update live transcription for attempt {attempt_id} - no data returned from update")
            logger.error(f"Update data attempted: {update_data}")
            
    except Exception as e:
        logger.error(f"Error saving live transcription: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")


# LiveKit job entry point
async def entrypoint(ctx: JobContext):
    """Simplified entry point for LiveKit agent jobs"""
    logger.info(f"SIMPLE AGENT STARTUP: Starting for room: {ctx.room.name}")
    
    # Get agent prompt from database
    agent_prompt, session_id, attempt_id = await get_agent_prompt_from_db(ctx.room.name)
    
    if not agent_prompt or not session_id or not attempt_id:
        logger.error("CRITICAL: Could not get agent_prompt from database. Agent will not start.")
        return
    
    logger.info(f"Got agent_prompt for session {session_id}. Prompt length: {len(agent_prompt)} characters")
    
    # Create the simplified agent
    try:
        assistant = SimpleMockInterviewAgent(agent_prompt, session_id, attempt_id)
    except Exception as e:
        logger.error(f"Failed to create simple agent: {e}")
        return
    
    # Create AgentSession with OpenAI Realtime API
    session_config = {
        'llm': realtime.RealtimeModel(
            voice="alloy",
            temperature=0.7
        )
    }
    
    # Try to add VAD with fallback to basic configuration
    try:
        vad = silero.VAD.load(
            min_silence_duration=0.8,
            activation_threshold=0.6,
            min_speech_duration=0.1
        )
        session_config['vad'] = vad
        logger.info("VAD loaded successfully")
    except Exception as vad_error:
        logger.warning(f"Failed to load VAD: {vad_error}")
    
    # Create session
    logger.info(f"Creating simple AgentSession with config: {list(session_config.keys())}")
    session = AgentSession(**session_config)
    logger.info("Simple AgentSession created successfully")
    
    # Add event handlers for transcription ONLY
    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        """Capture conversation items and save to live transcription"""
        try:
            if not hasattr(event, 'item'):
                return
                
            item = event.item
            item_role = getattr(item, 'role', None)
            
            # Skip tool calls
            if item_role == "assistant" and hasattr(item, 'tool_calls') and item.tool_calls:
                return
            
            # Get text content from the item with safe encoding
            content = None
            if hasattr(item, 'text_content'):
                if callable(item.text_content):
                    content = safe_encode_text(str(item.text_content()))
                else:
                    content = safe_encode_text(str(item.text_content))
            elif hasattr(item, 'content'):
                content = safe_encode_text(str(item.content))
            elif hasattr(item, 'text'):
                content = safe_encode_text(str(item.text))
            
            if not content or not content.strip():
                return
            
            # Log and save transcription
            if item_role == "user":
                logger.info(f"[{ctx.room.name}] user: {content}")
                asyncio.create_task(save_live_transcription(
                    attempt_id,
                    "candidate",
                    "User",
                    content.strip()
                ))
            elif item_role == "assistant":
                logger.info(f"[{ctx.room.name}] assistant: {content}")
                asyncio.create_task(save_live_transcription(
                    attempt_id,
                    "interviewer", 
                    "AI Interviewer",
                    content.strip()
                ))
                
        except Exception as e:
            logger.error(f"Error in conversation_item_added handler: {e}")

    # Connect to the room and start the agent
    logger.info("Connecting to LiveKit room...")
    await ctx.connect()
    logger.info(f"Connected to room: {ctx.room.name}")
    
    # Start the session with the agent
    logger.info("Starting simple agent session...")
    await session.start(room=ctx.room, agent=assistant)
    
    logger.info("Simple mock interview session started successfully!")
    logger.info(f"Simple agent ready for session: {session_id}")

def main():
    """Main function to run the simplified mock interview agent"""
    try:
        logger.info("Starting Simple Mock Interview Agent")

        required_env_vars = [
            'OPENAI_API_KEY',
            'LIVEKIT_API_KEY', 
            'LIVEKIT_API_SECRET',
            'LIVEKIT_URL'
        ]
        
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            logger.error(f"Missing required environment variables: {missing_vars}")
            return False
        
        logger.info("All required environment variables are set")
        
        # Run the agent with correct configuration
        try:
            logger.info("Creating WorkerOptions...")
            worker_options = agents.WorkerOptions(
                entrypoint_fnc=entrypoint,
                port=8081  # Debug port
            )
            logger.info("Starting Simple LiveKit agent worker...")
            agents.cli.run_app(worker_options)
            return True
        except Exception as e:
            logger.error(f"Error running agent: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            return False
            
    except UnicodeDecodeError as e:
        print(f"UNICODE ERROR: {e}")
        print("This is likely caused by special characters in job descriptions or company names.")
        print("The safe_encode_text function should handle this, but startup failed.")
        return False
    except Exception as e:
        print(f"STARTUP ERROR: {e}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        return False

# Main execution for standalone running
if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)