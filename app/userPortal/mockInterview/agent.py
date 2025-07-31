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
        # Convert to string first if it's not already
        text_str = str(text)
        
        # Handle Windows-specific encoding issues
        # Replace problematic Unicode characters that cause charmap codec errors
        problematic_chars = {
            '\u2018': "'",  # Left single quotation mark
            '\u2019': "'",  # Right single quotation mark  
            '\u201c': '"',  # Left double quotation mark
            '\u201d': '"',  # Right double quotation mark
            '\u2013': '-',  # En dash
            '\u2014': '-',  # Em dash
            '\u2026': '...', # Horizontal ellipsis
            '\u00a0': ' ',  # Non-breaking space
            '\u0081': '',   # High control character
            '\u008d': '',   # High control character
            '\u008f': '',   # High control character
            '\u0090': '',   # High control character
            '\u009d': '',   # High control character
        }
        
        # Replace problematic characters
        for unicode_char, replacement in problematic_chars.items():
            text_str = text_str.replace(unicode_char, replacement)
        
        # Remove any remaining high control characters (0x80-0x9F)
        text_str = ''.join(char for char in text_str if ord(char) < 0x80 or ord(char) > 0x9F)
        
        # Ensure text is properly encoded as UTF-8
        return text_str.encode('utf-8', errors='replace').decode('utf-8')
        
    except Exception as e:
        logger.warning(f"Error encoding text: {e}. Using fallback.")
        # Fallback: remove all non-ASCII characters
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
        self._ctx = None  # Will be set in entrypoint
        
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
    
    async def on_exit(self) -> None:
        """Called when agent exits the session"""
        logger.info(f"Simple agent exited session {self.session_id}")
        
        # Cancel timeout task
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        
        # BACKUP: If agent is exiting without calling end_interview, call it now
        # This prevents stuck sessions when agent disconnects naturally
        try:
            # Check if interview was already marked as completed
            from ...extensions import get_admin_client
            admin_client = get_admin_client()
            
            # Retry logic for database query
            max_retries = 3
            retry_delay = 1
            
            for attempt in range(max_retries):
                try:
                    response = admin_client.table('mock_interview_attempts')\
                        .select('status')\
                        .eq('id', self.attempt_id)\
                        .execute()
                    break  # Success, exit retry loop
                except Exception as db_error:
                    if attempt < max_retries - 1:  # Not the last attempt
                        logger.warning(f"Database status check attempt {attempt + 1} failed: {str(db_error)[:100]}... Retrying in {retry_delay}s")
                        import time
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        # Last attempt failed, re-raise the error
                        logger.error(f"All {max_retries} database status check attempts failed for attempt {self.attempt_id}")
                        raise db_error
            
            if response.data:
                current_status = response.data[0].get('status', 'pending')
                logger.info(f"Agent exiting - current attempt status: {current_status}")
                
                if current_status not in ['completed', 'PROCESSED']:
                    logger.warning(f"⚠️  Agent exiting but interview not completed! Status: {current_status}")
                    logger.warning(f"⚠️  This indicates the interview was not properly ended by calling end_interview()")
                    logger.warning(f"⚠️  Interview will remain in '{current_status}' status - manual intervention may be needed")
                    # DO NOT auto-complete here - agent should have called end_interview() if interview was truly complete
                else:
                    logger.info("✅ Interview already completed, clean exit")
            else:
                logger.warning(f"Could not find attempt {self.attempt_id} during exit")
                
        except Exception as exit_completion_error:
            logger.error(f"Error during exit completion check: {exit_completion_error}")
            # Continue with normal exit
    
    async def _timeout_handler(self) -> None:
        """14-minute timeout backup to prevent stuck sessions"""
        try:
            # Wait 14 minutes (840 seconds)
            await asyncio.sleep(840)
            
            logger.info("14-minute timeout reached - auto-ending interview")
            # Mark as completed due to timeout
            await mark_interview_completed(self.attempt_id, "timeout - session duration limit reached")
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
            
            # Step 1: Mark interview as completed in database
            await mark_interview_completed(self.attempt_id, reason)
            
            # Step 2: Quick goodbye message  
            await context.session.generate_reply(
                instructions=safe_encode_text("Thank you for the interview. The session will end shortly.")
            )
            
            # Step 3: Wait 1 second for message delivery
            await asyncio.sleep(1)
            
            # Step 4: Find THE user participant and call RPC (1 agent + 1 user scenario)
            try:
                # Get all remote participants (should be just 1 user in mock interview)
                all_remote_participants = list(self._ctx.room.remote_participants.values())
                logger.info(f"Found {len(all_remote_participants)} remote participants in room")
                
                # Filter to find the user (non-agent participant)
                user_participants = [p for p in all_remote_participants 
                                   if p.kind != "agent" and not p.identity.startswith('agent')]
                
                logger.info(f"Found {len(user_participants)} user participants for RPC")
                
                if user_participants:
                    # In 1-agent-1-user scenario, there should be exactly 1 user
                    user_participant = user_participants[0]
                    user_identity = user_participant.identity
                    
                    logger.info(f"Sending RPC 'forceEndInterview' to user: {user_identity}")
                    
                    # Call frontend RPC method 'forceEndInterview'
                    rpc_payload = {
                        'reason': safe_encode_text(reason),
                        'timestamp': datetime.utcnow().isoformat(),
                        'session_id': self.session_id,
                        'attempt_id': self.attempt_id
                    }
                    logger.info(f"📤 Sending RPC to {user_identity} with payload: {rpc_payload}")
                    
                    try:
                        response = await self._ctx.room.local_participant.perform_rpc(
                            destination_identity=user_identity,
                            method='forceEndInterview',  # Frontend must register this method
                            payload=json.dumps(rpc_payload),
                            response_timeout=10.0  # Increased to 10 seconds
                        )
                        logger.info(f"✅ RPC sent successfully to {user_identity}, response: {response}")
                    except asyncio.TimeoutError:
                        logger.error(f"⏰ RPC TIMEOUT - user {user_identity} did not respond within 10 seconds")
                        logger.error(f"This means frontend likely didn't register 'forceEndInterview' RPC method")
                        logger.error(f"🔧 WORKAROUND: Will still disconnect agent to prevent stuck session")
                    except Exception as rpc_call_error:
                        logger.error(f"❌ RPC call failed: {rpc_call_error}")
                        logger.error(f"Check if frontend properly registered RPC handler before room.connect()")
                        logger.error(f"🔧 WORKAROUND: Will still disconnect agent to prevent stuck session")
                        # Continue with disconnect even if RPC fails
                else:
                    logger.warning("⚠️ No user participants found for RPC call - user may have already left")
            
            except Exception as rpc_error:
                logger.error(f"Error in RPC section: {rpc_error}")
            
            # Step 5: Always disconnect agent after RPC (or attempt)
            try:
                # Cancel timeout task since we're ending
                if self._timeout_task and not self._timeout_task.done():
                    self._timeout_task.cancel()
                
                # Give frontend a moment to process RPC
                await asyncio.sleep(2)
                
                # Disconnect the agent from the room (using stored JobContext)
                await self._ctx.room.disconnect()
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
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            # Fallback: force disconnect (using stored JobContext)
            try:
                if self._ctx and hasattr(self._ctx, 'room'):
                    await self._ctx.room.disconnect()
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
    """Save message directly to live transcription in mock_interview_attempts table WITHOUT changing status"""
    try:
        from ...extensions import get_admin_client
        admin_client = get_admin_client()
        
        # Get current live transcription and status
        response = admin_client.table('mock_interview_attempts')\
            .select('live_transcription, status')\
            .eq('id', attempt_id)\
            .execute()
        
        if not response.data:
            logger.warning(f"No attempt found with id {attempt_id}")
            return
        
        attempt_data = response.data[0]
        current_transcription = attempt_data.get('live_transcription') or {"conversation": []}
        current_status = attempt_data.get('status', 'pending')
        
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
        
        # Update ONLY the transcription, do NOT change status here
        # Status should only be changed by the end_interview function
        update_data = {
            'live_transcription': current_transcription,
            'updated_at': 'now()'
        }
        
        logger.info(f"Updating attempt {attempt_id} with live transcription only (status remains: {current_status})")
        
        update_response = admin_client.table('mock_interview_attempts')\
            .update(update_data)\
            .eq('id', attempt_id)\
            .execute()
        
        if update_response.data:
            logger.info(f"Saved live transcription for attempt {attempt_id}: {speaker_type} - {content[:50]}...")
        else:
            logger.error(f"Failed to update live transcription for attempt {attempt_id} - no data returned from update")
            logger.error(f"Update data attempted: {update_data}")
            
    except Exception as e:
        logger.error(f"Error saving live transcription: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")


async def mark_interview_completed(attempt_id: str, reason: str = "Interview completed"):
    """Mark the interview attempt as completed with proper status and duration calculation"""
    try:
        from ...extensions import get_admin_client
        admin_client = get_admin_client()
        
        # Get current attempt data including started_at for duration calculation
        response = admin_client.table('mock_interview_attempts')\
            .select('status, started_at')\
            .eq('id', attempt_id)\
            .execute()
        
        if not response.data:
            logger.warning(f"No attempt found with id {attempt_id}")
            return
        
        attempt_data = response.data[0]
        current_status = attempt_data.get('status', 'pending')
        started_at = attempt_data.get('started_at')
        
        logger.info(f"Marking interview attempt {attempt_id} as completed. Current status: {current_status}")
        
        # Only update status if not already in final state
        if current_status in ['completed', 'PROCESSED']:
            logger.info(f"Attempt {attempt_id} already in final state ({current_status}), skipping completion")
            return
        
        # Calculate actual duration in minutes
        actual_duration_minutes = 0
        if started_at:
            try:
                from datetime import datetime
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
        
        # Update status to completed
        update_data = {
            'status': 'completed',
            'completed_at': 'now()',
            'actual_duration_minutes': actual_duration_minutes,
            'updated_at': 'now()'
        }
        
        logger.info(f"Marking attempt {attempt_id} as completed (duration: {actual_duration_minutes} minutes, reason: {reason})")
        
        update_response = admin_client.table('mock_interview_attempts')\
            .update(update_data)\
            .eq('id', attempt_id)\
            .execute()
        
        if update_response.data:
            logger.info(f"Successfully marked attempt {attempt_id} as completed")
        else:
            logger.error(f"Failed to mark attempt {attempt_id} as completed - no data returned from update")
            
    except Exception as e:
        logger.error(f"Error marking interview completed: {e}")
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
        # Store JobContext for RPC calls (following official example pattern)
        assistant._ctx = ctx
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
                port=8081,  # Debug port
                # No agent_name = automatic dispatch enabled (1 agent per room)
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