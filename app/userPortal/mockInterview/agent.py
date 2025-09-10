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
        
        # Strict RPC tracking
        self._rpc_sent_successfully = False
        self._rpc_max_retries = 5
        self._rpc_retry_delay = 1.0  # seconds
        self._force_disconnect_timeout = 15.0  # seconds
        
        # Use the agent prompt directly from database (it already contains comprehensive end_interview instructions)
        instructions = safe_encode_text(agent_prompt.strip())
        
        # Initialize Agent with instructions from database only
        super().__init__(instructions=instructions)
        
        logger.info(f"Simple agent created for session {session_id}")
    
    async def _send_strict_rpc(self, reason: str, rpc_type: str = "end_interview") -> bool:
        """
        Strictly send RPC with retries and confirmation requirement
        Returns True only if RPC was successfully sent and acknowledged
        """
        if self._rpc_sent_successfully:
            logger.info("RPC already sent successfully, skipping duplicate")
            return True
            
        if not self._ctx or not self._ctx.room:
            logger.error("No room context available for strict RPC")
            return False
        
        # Find user participants
        all_remote_participants = list(self._ctx.room.remote_participants.values())
        user_participants = [p for p in all_remote_participants 
                           if p.kind != "agent" and not p.identity.startswith('agent')]
        
        if not user_participants:
            logger.warning("No user participants found for strict RPC")
            return False
        
        user_participant = user_participants[0]
        user_identity = user_participant.identity
        
        # Prepare RPC payload
        rpc_payload = {
            'reason': safe_encode_text(reason),
            'timestamp': datetime.utcnow().isoformat(),
            'session_id': self.session_id,
            'attempt_id': self.attempt_id,
            'rpc_type': rpc_type,
            'requires_confirmation': True
        }
        
        logger.info(f"Starting strict RPC to {user_identity} with {self._rpc_max_retries} max retries")
        
        # Retry loop
        for attempt in range(self._rpc_max_retries):
            try:
                # Check connection state before each attempt
                if hasattr(self._ctx.room, 'connection_state'):
                    connection_state = str(self._ctx.room.connection_state)
                    if connection_state not in ['CONNECTED', 'RECONNECTING']:
                        logger.warning(f"RPC attempt {attempt + 1} skipped - bad connection state: {connection_state}")
                        if attempt < self._rpc_max_retries - 1:
                            await asyncio.sleep(self._rpc_retry_delay)
                            continue
                        else:
                            return False
                
                # Check participant connection
                if hasattr(user_participant, 'connection_quality') and user_participant.connection_quality == 'LOST':
                    logger.warning(f"RPC attempt {attempt + 1} skipped - user connection lost")
                    if attempt < self._rpc_max_retries - 1:
                        await asyncio.sleep(self._rpc_retry_delay)
                        continue
                    else:
                        return False
                
                logger.info(f"RPC attempt {attempt + 1}/{self._rpc_max_retries} to {user_identity}")
                
                # Send RPC with confirmation requirement
                response = await self._ctx.room.local_participant.perform_rpc(
                    destination_identity=user_identity,
                    method=rpc_type,
                    payload=json.dumps(rpc_payload),
                    response_timeout=8.0  # Longer timeout for strict mode
                )
                
                # Parse response to check for confirmation
                try:
                    response_data = json.loads(response)
                    if response_data.get('status') == 'success':
                        logger.info(f"Strict RPC confirmed by frontend on attempt {attempt + 1}")
                        self._rpc_sent_successfully = True
                        return True
                    else:
                        logger.warning(f"Frontend returned error: {response_data.get('message', 'Unknown error')}")
                except (json.JSONDecodeError, TypeError):
                    # Empty or invalid response, but RPC was sent
                    logger.warning(f"RPC sent but received invalid response on attempt {attempt + 1}")
                
            except asyncio.TimeoutError:
                logger.warning(f"RPC timeout on attempt {attempt + 1}")
            except Exception as rpc_error:
                logger.error(f"RPC attempt {attempt + 1} failed: {rpc_error}")
                
                # Log specific error types
                if "engine is closed" in str(rpc_error):
                    logger.error("LiveKit engine closed - cannot retry RPC")
                    return False
                elif "connection error" in str(rpc_error):
                    logger.warning("Connection error - will retry if attempts remaining")
            
            # Wait before retry (except on last attempt)
            if attempt < self._rpc_max_retries - 1:
                delay = self._rpc_retry_delay * (attempt + 1)  # Exponential backoff
                logger.info(f"Waiting {delay}s before RPC retry...")
                await asyncio.sleep(delay)
        
        logger.error(f"All {self._rpc_max_retries} RPC attempts failed")
        return False

    async def _cleanup_failed_room(self) -> None:
        """Cleanup room on LiveKit server after connection failure"""
        try:
            if not self.session_id:
                return
                
            # Import here to avoid circular imports
            from .api import delete_room
            
            # Construct room name from session and attempt IDs
            room_name = f"interview_{self.session_id}_attempt_{self.attempt_id.split('_')[-1] if '_' in self.attempt_id else '1'}"
            
            logger.info(f"Attempting to cleanup failed room: {room_name}")
            success = await delete_room(room_name)
            if success:
                logger.info(f"Successfully cleaned up room: {room_name}")
            else:
                logger.warning(f"Room cleanup may have failed for: {room_name}")
                
        except Exception as e:
            logger.error(f"Error in room cleanup: {e}")
    
    async def on_enter(self) -> None:
        """Called when agent enters the session"""
        logger.info(f"Simple agent entered session {self.session_id}")
        
        # Start 12-minute timeout as backup
        self._timeout_task = asyncio.create_task(self._timeout_handler())

    async def _wait_for_rpc_opportunity(self) -> None:
        """
        Wait for connection to become available for RPC
        Used during force disconnect timeout period
        """
        check_interval = 0.5  # Check every 500ms
        while True:
            if not self._ctx or not self._ctx.room:
                break
                
            # Check if connection is good for RPC
            if hasattr(self._ctx.room, 'connection_state'):
                connection_state = str(self._ctx.room.connection_state)
                if connection_state in ['CONNECTED', 'RECONNECTING']:
                    # Check for user participants
                    user_participants = [p for p in self._ctx.room.remote_participants.values() 
                                       if p.kind != "agent" and not p.identity.startswith('agent')]
                    if user_participants:
                        logger.info("RPC opportunity detected during force disconnect timeout")
                        return
            
            await asyncio.sleep(check_interval)
    
    async def on_exit(self) -> None:
        """Called when agent exits the session - STRICT MODE: Must notify frontend before exit"""
        logger.info(f"Agent exited session {self.session_id} - STRICT MODE ENABLED")
        
        # Cancel timeout task
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        
        # Always ensure interview is completed in database
        try:
            from ...extensions import get_admin_client
            admin_client = get_admin_client()
            
            response = admin_client.table('mock_interview_attempts')\
                .select('status')\
                .eq('id', self.attempt_id)\
                .execute()
            
            if response.data:
                current_status = response.data[0].get('status', 'pending')
                
                if current_status not in ['COMPLETED', 'failed', 'error']:
                    logger.info(f"Force-failing unfinished interview (status: {current_status})")
                    await mark_interview_failed(self.attempt_id, "agent disconnected - force failed")
                else:
                    logger.info(f"Interview already in final state: {current_status}")
                
        except Exception as e:
            logger.error(f"Error in exit completion: {e}")
            # CRITICAL: Always try to mark as failed to prevent stuck sessions
            # Use direct database update as final fallback
            try:
                await mark_interview_failed(self.attempt_id, "agent exit error - force failed")
            except Exception as fallback_error:
                logger.error(f"Fallback completion also failed: {fallback_error}")
                # Last resort: direct database update without helper function
                try:
                    from ...extensions import get_admin_client
                    admin_client = get_admin_client()
                    admin_client.table('mock_interview_attempts')\
                        .update({
                            'status': 'failed',
                            'completed_at': 'now()',
                            'updated_at': 'now()'
                        })\
                        .eq('id', self.attempt_id)\
                        .execute()
                    logger.warning(f"Used last resort database update for attempt {self.attempt_id}")
                except Exception as last_resort_error:
                    logger.critical(f"CRITICAL: All cleanup methods failed for attempt {self.attempt_id}: {last_resort_error}")
                    # This should trigger alerts in production
        
        # STRICT MODE: Must send RPC before allowing exit
        if not self._rpc_sent_successfully:
            logger.warning("Exit triggered but RPC not yet sent - attempting strict RPC on exit")
            
            # Try to send strict RPC on exit
            rpc_success = await self._send_strict_rpc("agent_disconnected - on_exit")
            
            if not rpc_success:
                logger.warning("Exit RPC failed, attempting force disconnect after timeout")
                
                # Give additional time for any pending operations
                try:
                    await asyncio.wait_for(
                        self._wait_for_rpc_opportunity(),
                        timeout=self._force_disconnect_timeout
                    )
                    # Try one final RPC attempt
                    rpc_success = await self._send_strict_rpc("agent_disconnected - on_exit - final attempt")
                except asyncio.TimeoutError:
                    logger.error(f"Force disconnect timeout ({self._force_disconnect_timeout}s) reached on exit")
            
            # Log final RPC status
            if rpc_success:
                logger.info("Exit RPC sent successfully")
            else:
                logger.error("Exit completed but RPC was never confirmed")
        else:
            logger.info("RPC already sent successfully, skipping exit RPC")
        
        # Always disconnect from room after RPC attempt
        if self._ctx and self._ctx.room:
            try:
                logger.info("Proceeding with room disconnect on exit")
                await self._ctx.room.disconnect()
                logger.info("Exit disconnect completed")
            except Exception as disconnect_error:
                logger.error(f"Error in exit disconnect: {disconnect_error}")
                try:
                    await self._cleanup_failed_room()
                except Exception as cleanup_error:
                    logger.error(f"Exit room cleanup failed: {cleanup_error}")
    
    async def _timeout_handler(self) -> None:
        """Timeout handler with connection monitoring to prevent stuck sessions"""
        try:
            # Wait 12 minutes (720 seconds) - reduced from 14 minutes for better UX
            timeout_duration = 720
            check_interval = 30  # Check connection every 30 seconds
            elapsed_time = 0
            
            while elapsed_time < timeout_duration:
                await asyncio.sleep(check_interval)
                elapsed_time += check_interval
                
                # Check connection health every 30 seconds
                if self._ctx and self._ctx.room:
                    try:
                        if hasattr(self._ctx.room, 'connection_state'):
                            connection_state = str(self._ctx.room.connection_state)
                            if connection_state in ['DISCONNECTED', 'FAILED']:
                                logger.warning(f"Connection lost (state: {connection_state}) - ending interview early")
                                await mark_interview_failed(self.attempt_id, f"connection lost - {connection_state}")
                                await self.end_interview(f"connection lost - {connection_state}")
                                return
                            elif connection_state == 'RECONNECTING':
                                logger.info(f"Connection reconnecting at {elapsed_time}s mark")
                    except Exception as check_error:
                        logger.warning(f"Connection check failed: {check_error}")
                
                # Log progress every 2 minutes
                if elapsed_time % 120 == 0:
                    remaining_minutes = (timeout_duration - elapsed_time) // 60
                    logger.info(f"Interview progress: {elapsed_time//60} minutes elapsed, {remaining_minutes} minutes remaining")
            
            # If we reach here, timeout was reached
            logger.info("12-minute timeout reached - auto-ending interview")
            await mark_interview_failed(self.attempt_id, "timeout - session duration limit reached")
            await self.end_interview("timeout - session duration limit reached")
            
        except asyncio.CancelledError:
            logger.info("Timeout handler cancelled - session ended naturally")
        except Exception as e:
            logger.error(f"Error in timeout handler: {e}")
            # Force failure on any error to prevent stuck sessions
            try:
                await mark_interview_failed(self.attempt_id, f"timeout handler error - {str(e)}")
            except:
                pass
    
    @function_tool()
    async def end_interview(self, context: RunContext = None, reason: str = "Interview completed"):
        """End the interview - STRICT MODE: Must notify frontend before disconnecting"""
        try:
            logger.info(f"Ending interview with strict RPC: {reason}")
            
            # Step 1: Mark interview as successfully completed in database
            await mark_interview_successful(self.attempt_id, reason)
            
            # Step 2: Quick goodbye message (if context available)
            if context and hasattr(context, 'session'):
                try:
                    await context.session.generate_reply(
                        instructions=safe_encode_text("Thank you for the interview. The session will end shortly.")
                    )
                    # Step 3: Wait 1 second for message delivery
                    await asyncio.sleep(1)
                except Exception as message_error:
                    logger.warning(f"Could not send goodbye message: {message_error}")
            else:
                logger.info("No context available for goodbye message, proceeding with RPC")
            
            # Step 4: Cancel timeout task
            if self._timeout_task and not self._timeout_task.done():
                self._timeout_task.cancel()
            
            # Step 5: STRICT MODE - Send RPC with confirmation before disconnecting
            logger.info("Starting strict RPC sequence for interview end")
            rpc_success = await self._send_strict_rpc(reason)
            
            if not rpc_success:
                logger.warning("Strict RPC failed during interview end, attempting force disconnect")
                
                # Give additional time for any pending operations
                try:
                    await asyncio.wait_for(
                        self._wait_for_rpc_opportunity(),
                        timeout=self._force_disconnect_timeout
                    )
                    # Try one final RPC attempt
                    rpc_success = await self._send_strict_rpc(reason + " - final attempt")
                except asyncio.TimeoutError:
                    logger.error(f"Force disconnect timeout ({self._force_disconnect_timeout}s) reached during interview end")
            
            # Step 6: Actually disconnect from room
            if self._ctx and self._ctx.room:
                try:
                    # Give frontend brief moment to process RPC
                    await asyncio.sleep(1.5)
                    
                    logger.info("Proceeding with room disconnect after strict RPC")
                    await self._ctx.room.disconnect()
                    logger.info("Successfully disconnected from room")
                except Exception as disconnect_error:
                    logger.error(f"Error during room disconnect: {disconnect_error}")
                    # Try room cleanup
                    try:
                        await self._cleanup_failed_room()
                    except Exception as cleanup_error:
                        logger.error(f"Room cleanup also failed: {cleanup_error}")
            
            # Step 7: Log final RPC status
            if rpc_success:
                logger.info("Interview ended successfully with strict RPC confirmation")
            else:
                logger.error("Interview ended but strict RPC was never confirmed")
        
        except Exception as e:
            logger.error(f"Error ending interview: {e}")
            # Fallback: try one more strict RPC, then force disconnect
            try:
                logger.warning("Attempting emergency strict RPC due to interview end error")
                emergency_success = await self._send_strict_rpc(f"emergency end - {str(e)}")
                
                if not emergency_success:
                    logger.error("Emergency strict RPC also failed")
                
                # Force disconnect regardless
                if self._ctx and hasattr(self._ctx, 'room'):
                    await self._ctx.room.disconnect()
                    logger.info("Emergency disconnect completed")
                elif context and hasattr(context, 'session') and hasattr(context.session, 'close'):
                    await context.session.close()
                    logger.info("Emergency session close completed")
                    
            except Exception as emergency_error:
                logger.error(f"Emergency disconnect failed: {emergency_error}")
                # Final attempt at cleanup
                try:
                    await self._cleanup_failed_room()
                except:
                    pass


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


async def mark_interview_failed(attempt_id: str, reason: str = "Interview failed"):
    """Mark the interview attempt as FAILED - only for failures and errors"""
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
        
        logger.info(f"Marking interview attempt {attempt_id} as FAILED. Current status: {current_status}, reason: {reason}")
        
        # Only update status if not already in final state
        if current_status in ['COMPLETED', 'failed', 'error']:
            logger.info(f"Attempt {attempt_id} already in final state ({current_status}), skipping failure marking")
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
        
        # Determine failure type - agent failures are ALWAYS 'failed', regardless of duration
        final_status = 'failed'
        
        # Agent disconnections, timeouts, and system failures should ALWAYS be 'failed'
        # The 3-minute rule only applies to user-initiated completions, not system failures
        logger.warning(f"System/agent failure detected - marking as failed: {reason}")
        
        # Update status based on conditions
        update_data = {
            'status': final_status,
            'completed_at': 'now()',
            'actual_duration_minutes': actual_duration_minutes,
            'updated_at': 'now()'
        }
        
        logger.info(f"Marking attempt {attempt_id} as {final_status} (duration: {actual_duration_minutes} minutes, reason: {reason})")
        
        update_response = admin_client.table('mock_interview_attempts')\
            .update(update_data)\
            .eq('id', attempt_id)\
            .execute()
        
        if update_response.data:
            logger.info(f"Successfully marked attempt {attempt_id} as {final_status}")
            
            # For certain failure types (early agent failures), consider cleanup
            if "agent disconnected" in reason.lower() or "dispatch" in reason.lower() or "timeout" in reason.lower():
                logger.info(f"Considering cleanup for failed attempt {attempt_id} due to agent/system failure")
                # Note: Cleanup logic would be called from routes.py where database access is easier
                # This is just marking - the routes.py handles cleanup decisions
        else:
            logger.error(f"Failed to mark attempt {attempt_id} as {final_status} - no data returned from update")
            
    except Exception as e:
        logger.error(f"Error marking interview as failed: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")


async def mark_interview_successful(attempt_id: str, reason: str = "Interview completed successfully"):
    """Mark the interview attempt as successfully COMPLETED - only for normal successful completions"""
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
        
        logger.info(f"Marking interview attempt {attempt_id} as SUCCESSFULLY COMPLETED. Current status: {current_status}")
        
        # Only update status if not already in final state
        if current_status in ['COMPLETED', 'failed', 'error']:
            logger.info(f"Attempt {attempt_id} already in final state ({current_status}), skipping successful completion")
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
        
        # For successful completions, ensure minimum duration was met
        if actual_duration_minutes < 3:
            logger.warning(f"Successful completion attempted but duration too short ({actual_duration_minutes}min) - marking as error instead")
            # For user-initiated completions that are too short, mark as 'error' directly
            from ...extensions import get_admin_client
            admin_client = get_admin_client()
            
            update_data = {
                'status': 'error',
                'completed_at': 'now()',
                'actual_duration_minutes': actual_duration_minutes,
                'updated_at': 'now()'
            }
            
            update_response = admin_client.table('mock_interview_attempts')\
                .update(update_data)\
                .eq('id', attempt_id)\
                .execute()
            
            if update_response.data:
                logger.info(f"Successfully marked attempt {attempt_id} as error (too short: {actual_duration_minutes} minutes)")
            else:
                logger.error(f"Failed to mark attempt {attempt_id} as error")
            return
        
        # Mark as COMPLETED for successful sessions
        final_status = 'COMPLETED'
        
        # Update status - successful completion
        update_data = {
            'status': final_status,
            'completed_at': 'now()',
            'actual_duration_minutes': actual_duration_minutes,
            'updated_at': 'now()'
        }
        
        logger.info(f"Marking attempt {attempt_id} as SUCCESSFULLY COMPLETED (duration: {actual_duration_minutes} minutes, reason: {reason})")
        
        update_response = admin_client.table('mock_interview_attempts')\
            .update(update_data)\
            .eq('id', attempt_id)\
            .execute()
        
        if update_response.data:
            logger.info(f"Successfully marked attempt {attempt_id} as COMPLETED")
        else:
            logger.error(f"Failed to mark attempt {attempt_id} as completed - no data returned from update")
            
    except Exception as e:
        logger.error(f"Error marking interview as successfully completed: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")


# LiveKit job entry point
async def entrypoint(ctx: JobContext):
    """Simplified entry point for LiveKit agent jobs with dispatch timeout"""
    logger.info(f"SIMPLE AGENT STARTUP: Starting for room: {ctx.room.name}")
    
    # Get agent prompt from database
    agent_prompt, session_id, attempt_id = await get_agent_prompt_from_db(ctx.room.name)
    
    if not agent_prompt or not session_id or not attempt_id:
        logger.error("CRITICAL: Could not get agent_prompt from database. Agent will not start.")
        # Emergency cleanup if we can't get basic data
        try:
            from .api import delete_room
            await delete_room(ctx.room.name)
            logger.info(f"Cleaned up room {ctx.room.name} after dispatch failure")
        except:
            pass
        return
    
    # Start dispatch timeout - if agent doesn't connect in 10 seconds, cleanup
    dispatch_timeout_task = asyncio.create_task(_agent_dispatch_timeout(ctx.room.name, attempt_id, 10.0))
    
    logger.info(f"Got agent_prompt for session {session_id}. Prompt length: {len(agent_prompt)} characters")
    
    # Create the simplified agent
    try:
        assistant = SimpleMockInterviewAgent(agent_prompt, session_id, attempt_id)
        # Store JobContext for RPC calls (following official example pattern)
        assistant._ctx = ctx
    except Exception as e:
        logger.error(f"Failed to create simple agent: {e}")
        # Cleanup on agent creation failure
        if not dispatch_timeout_task.done():
            dispatch_timeout_task.cancel()
        await _emergency_dispatch_cleanup(ctx.room.name, attempt_id, f"agent creation failed: {e}")
        return
    
    # Create AgentSession with OpenAI Realtime API
    session_config = {
        'llm': realtime.RealtimeModel(
            voice="alloy",
            temperature=0.7
        )
    }
    
    # Add noise cancellation
    try:
        from livekit.plugins import noise_cancellation
        session_config['room_input_options'] = {
            'noise_cancellation': noise_cancellation.BVC()  # Background voice cancellation
        }
        logger.info("Noise cancellation (BVC) enabled")
    except Exception as nc_error:
        logger.warning(f"Failed to load noise cancellation: {nc_error}")
    
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
    try:
        logger.info("Connecting to LiveKit room...")
        await ctx.connect()
        logger.info(f"Connected to room: {ctx.room.name}")
        
        # Start the session with the agent
        logger.info("Starting simple agent session...")
        await session.start(room=ctx.room, agent=assistant)
        
        # Cancel dispatch timeout - agent connected successfully
        if not dispatch_timeout_task.done():
            dispatch_timeout_task.cancel()
            logger.info("Agent connected successfully - dispatch timeout cancelled")
        
        logger.info("Simple mock interview session started successfully!")
        logger.info(f"Simple agent ready for session: {session_id}")
        
    except Exception as connection_error:
        logger.error(f"Failed to connect agent to room: {connection_error}")
        # Cleanup on connection failure
        if not dispatch_timeout_task.done():
            dispatch_timeout_task.cancel()
        await _emergency_dispatch_cleanup(ctx.room.name, attempt_id, f"connection failed: {connection_error}")
        return


async def _agent_dispatch_timeout(room_name: str, attempt_id: str, timeout_seconds: float):
    """Emergency timeout handler - NEVER leave status as 'active' if agent fails to connect"""
    try:
        await asyncio.sleep(timeout_seconds)
        
        # If we reach here, agent failed to connect in time
        logger.critical(f"AGENT DISPATCH TIMEOUT ({timeout_seconds}s) for room {room_name}")
        
        # 1. IMMEDIATELY mark attempt as failed in database
        try:
            await mark_interview_failed(attempt_id, "agent dispatch timeout - never connected")
            logger.info(f"Marked attempt {attempt_id} as FAILED due to dispatch timeout")
        except Exception as db_error:
            logger.error(f"Failed to mark attempt as failed: {db_error}")
        
        # 2. DESTROY the room
        try:
            from .api import delete_room
            await delete_room(room_name)
            logger.info(f"Destroyed room {room_name} after dispatch timeout")
        except Exception as room_error:
            logger.error(f"Failed to destroy room {room_name}: {room_error}")
        
        # 3. Cleanup the failed attempt (so it doesn't count against user)
        try:
            from .routes import cleanup_failed_attempt
            cleanup_failed_attempt(attempt_id, "agent dispatch timeout")
            logger.info(f"Cleaned up failed attempt {attempt_id}")
        except Exception as cleanup_error:
            logger.error(f"Failed to cleanup attempt {attempt_id}: {cleanup_error}")
        
        logger.critical(f"Dispatch timeout cleanup completed for {room_name}")
        
    except asyncio.CancelledError:
        # Normal case - agent connected successfully
        logger.info(f"Dispatch timeout cancelled for {room_name} - agent connected successfully")
    except Exception as e:
        logger.critical(f"Dispatch timeout handler failed: {e}")


async def _emergency_dispatch_cleanup(room_name: str, attempt_id: str, reason: str):
    """Emergency cleanup for any dispatch failure - NEVER leave status as 'active'"""
    logger.critical(f"EMERGENCY DISPATCH CLEANUP: {room_name} - {reason}")
    
    # 1. Mark as failed in database
    try:
        await mark_interview_failed(attempt_id, f"dispatch failure: {reason}")
        logger.info(f"Marked attempt {attempt_id} as FAILED")
    except Exception as db_error:
        logger.error(f"Failed to mark attempt as failed: {db_error}")
    
    # 2. Destroy room
    try:
        from .api import delete_room
        await delete_room(room_name)
        logger.info(f"Destroyed room {room_name}")
    except Exception as room_error:
        logger.error(f"Failed to destroy room: {room_error}")
    
    # 3. Cleanup attempt
    try:
        from .routes import cleanup_failed_attempt
        cleanup_failed_attempt(attempt_id, f"emergency dispatch cleanup: {reason}")
        logger.info(f"Cleaned up attempt {attempt_id}")
    except Exception as cleanup_error:
        logger.error(f"Failed to cleanup attempt: {cleanup_error}")


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
                drain_timeout=30,  # Reduced from default 30 minutes to 30 seconds
                # No agent_name = automatic dispatch enabled (1 agent per room)
                # LiveKit handles concurrent rooms automatically
            )
            logger.info("Starting Simple LiveKit agent worker with 30s drain timeout...")
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