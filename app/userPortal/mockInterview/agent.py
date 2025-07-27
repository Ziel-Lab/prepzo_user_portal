"""
LiveKit Agent with OpenAI Realtime API for Mock Interviews
Following official LiveKit documentation for production configuration
"""

import os
import asyncio
import logging
import json
import time
from typing import Dict, Any, Optional
from dataclasses import dataclass

from livekit import agents
from livekit.agents import (
    Agent, 
    AgentSession, 
    JobContext, 
    function_tool, 
    RunContext, 
    llm
)
from livekit.plugins.openai import realtime

# Removed TranscriptionHandler import - using direct live transcription instead

# Configure logging for production
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


@dataclass
class InterviewContext:
    """Interview session context data"""
    session_id: str  # mock_interview.id
    attempt_id: str  # mock_interview_attempts.id
    attempt_number: int  # 1, 2, or 3
    user_id: str
    user_display_name: str
    resume_text: str
    job_description: str
    company_name: str
    position: str
    interview_type: str  # 'technical', 'behavioral', 'system_design', etc.
    difficulty_level: str  # 'entry', 'mid', 'senior'
    custom_instructions: str = ""
    company_details: str = ""
    linkedin_profile: str = ""
    agent_prompt: str = ""  # Pre-prepared agent instructions
    status_prep: str = "PENDING"  # PENDING, DONE
    room_name: str = ""
    title: str = ""
    attempt_status: str = "pending"  # pending, active, completed, cancelled

class MockInterviewAgent(Agent):
    """Production-level mock interview agent using OpenAI Realtime API"""
    
    def __init__(self, interview_context: InterviewContext) -> None:
        self.interview_context = interview_context
        
        # Get instructions from agent_prompt column only (will raise ValueError if missing)
        instructions = self._build_interview_instructions()
        
        # Store configuration for AgentSession (avoid overriding read-only properties)
        self._interview_instructions = instructions
        self.voice = "alloy"  # OpenAI voice option
        self.temperature = 0.7
        
        # Initialize Agent with instructions from database only
        super().__init__(instructions=instructions)
        
        # Interview state tracking
        self.questions_asked = []
        self.current_stage = "introduction"  # introduction -> main_questions -> wrap_up -> feedback -> ending
        self.start_time = None
        self.max_duration_minutes = 45  # Maximum interview duration
        self.target_duration_minutes = 30  # Target interview duration
        
        # No longer use TranscriptionHandler - causes table mismatch errors
        # Live transcription will be handled directly
        
        # Schedule time-based checks
        self._schedule_time_checks()
    
    def _build_interview_instructions(self) -> str:
        """Get interview instructions from agent_prompt column only"""
        
        # ONLY use agent_prompt from database - no dynamic instruction building
        if hasattr(self.interview_context, 'agent_prompt') and self.interview_context.agent_prompt and self.interview_context.agent_prompt.strip():
            logger.info(f"Using agent_prompt from database for session {self.interview_context.session_id}")
            
            # Add essential ending instructions to existing prompt if not already present
            ending_instructions = """
            
            INTERVIEW ENDING GUIDELINES:
            - Aim for 20-30 minute interviews unless candidate needs more time
            - End when you've gathered sufficient information about the candidate's abilities
            - Use the end_interview function when: you've asked enough questions, reached natural conclusion, or time limit approached
            - Provide constructive summary when ending
            - Common ending reasons: "questions_complete", "time_complete", "natural_conclusion"
            """
            
            # Only add ending instructions if not already present
            agent_prompt = self.interview_context.agent_prompt.strip()
            if "end_interview" not in agent_prompt.lower():
                agent_prompt += ending_instructions
            
            return agent_prompt
        
        # If no agent_prompt, refuse to start the interview
        error_msg = f"No agent_prompt found for session {self.interview_context.session_id}. Interview cannot start without proper agent instructions."
        logger.error(error_msg)
        raise ValueError(error_msg)
    
    def _schedule_time_checks(self):
        """Schedule periodic time checks for interview duration management"""
        async def time_check_task():
            try:
                # Wait for interview to actually start
                while not self.start_time:
                    await asyncio.sleep(5)
                
                # Check every 5 minutes
                while self.current_stage not in ["ending", "completed"]:
                    await asyncio.sleep(300)  # 5 minutes
                    
                    if not self.start_time:
                        continue
                        
                    elapsed_minutes = (asyncio.get_event_loop().time() - self.start_time) / 60
                    
                    # Target duration warning (25 minutes)
                    if elapsed_minutes >= 25 and self.current_stage not in ["wrap_up", "ending"]:
                        await self.session.generate_reply(
                            instructions="Gently let the candidate know we're approaching the end of our time and start wrapping up with final questions."
                        )
                        self.current_stage = "wrap_up"
                        logger.info("Interview moved to wrap-up stage due to time")
                    
                    # Hard time limit (45 minutes)
                    elif elapsed_minutes >= self.max_duration_minutes:
                        await self.end_interview(
                            context=None,
                            reason="time_limit_reached",
                            summary="We've reached our maximum interview time. Thank you for your responses today."
                        )
                        break
                        
            except Exception as e:
                logger.error(f"Error in time check task: {e}")
        
        # Start the time check task
        asyncio.create_task(time_check_task())
    
    
    async def on_enter(self) -> None:
        """Called when agent enters the session"""
        self.start_time = asyncio.get_event_loop().time()
        logger.info(f"Mock interview agent entered session {self.interview_context.session_id} (Attempt {self.interview_context.attempt_number})")
        
        # Mark attempt as active
        asyncio.create_task(self.update_attempt_status('active'))
        
        # Debug: Log available methods on this agent
        agent_methods = [method for method in dir(self) if callable(getattr(self, method)) and not method.startswith('_')]
        logger.info(f"Available agent methods: {agent_methods}")
        
        # Save system message for interview start
        # asyncio.create_task(self.transcription_handler.save_system_message(
        #     f"Interview attempt {self.interview_context.attempt_number} started for {self.interview_context.position} position at {self.interview_context.company_name}",
        #     metadata={
        #         "event": "interview_start",
        #         "interview_type": self.interview_context.interview_type,
        #         "position": self.interview_context.position,
        #         "company": self.interview_context.company_name,
        #         "attempt_number": self.interview_context.attempt_number
        #     }
        # ))
        
        # Generate initial greeting based on agent_prompt instructions only
        greeting_instructions = "Begin the interview with an appropriate greeting and introduction as specified in your instructions. Start the interview professionally."
        
        await self.session.generate_reply(instructions=greeting_instructions)
        
        # Save the greeting as an interviewer message and update live transcription
        greeting_text = f"Interview attempt {self.interview_context.attempt_number} started"
        # asyncio.create_task(self.transcription_handler.save_interviewer_message(
        #     greeting_text,
        #     message_type="greeting",
        #     metadata={"stage": "introduction", "attempt_number": self.interview_context.attempt_number}
        # ))
        
        # Update live transcription with greeting
        # asyncio.create_task(self.transcription_handler.update_live_transcription(
        #     "interviewer", 
        #     "AI Interviewer", 
        #     greeting_text, 
        #     "introduction"
        # ))
        
        logger.info(f"Started interview attempt {self.interview_context.attempt_number} for session {self.interview_context.session_id}")
        logger.info(f"Live transcription initialized for attempt: {self.interview_context.attempt_id}")
    
    async def on_exit(self) -> None:
        """Called when agent exits the session"""
        if self.start_time and self.current_stage != "ending":
            # Only mark as completed if not already handled by end_interview
            duration = asyncio.get_event_loop().time() - self.start_time
            duration_minutes = duration / 60
            logger.info(f"Mock interview attempt {self.interview_context.attempt_number} completed. Duration: {duration:.1f} seconds")
            
            # Mark attempt as completed and save duration
            asyncio.create_task(self.update_attempt_completion(duration_minutes))
            
            # Save system message for interview end (non-blocking)
            # asyncio.create_task(self.transcription_handler.save_system_message(
            #     f"Interview attempt {self.interview_context.attempt_number} completed. Duration: {duration:.1f} seconds",
            #     metadata={
            #         "event": "interview_end",
            #         "duration_seconds": duration,
            #         "duration_minutes": duration_minutes,
            #         "questions_asked": len(self.questions_asked),
            #         "final_stage": self.current_stage,
            #         "attempt_number": self.interview_context.attempt_number,
            #         "ended_by": "system"
            #     }
            # ))
        elif self.current_stage == "ending":
            logger.info(f"Interview was ended by agent, skipping duplicate completion marking")
        else:
            logger.info("Interview exit without proper start time")

    async def update_attempt_status(self, status: str):
        """Update the status of the current attempt"""
        try:
            from ...extensions import get_admin_client
            admin_client = get_admin_client()
            
            update_data = {
                'status': status,
                'started_at': 'now()' if status == 'active' else None
            }
            
            result = admin_client.table('mock_interview_attempts')\
                .update(update_data)\
                .eq('id', self.interview_context.attempt_id)\
                .execute()
            
            if result.data:
                logger.info(f"Updated attempt {self.interview_context.attempt_id} status to {status}")
            else:
                logger.warning(f"Failed to update attempt status for {self.interview_context.attempt_id}")
                
        except Exception as e:
            logger.error(f"Error updating attempt status: {e}")

    async def update_attempt_completion(self, duration_minutes: float):
        """Mark attempt as completed and save duration"""
        try:
            from ...extensions import get_admin_client
            admin_client = get_admin_client()
            
            update_data = {
                'status': 'completed',
                'completed_at': 'now()',
                'actual_duration_minutes': int(duration_minutes)
            }
            
            result = admin_client.table('mock_interview_attempts')\
                .update(update_data)\
                .eq('id', self.interview_context.attempt_id)\
                .execute()
            
            if result.data:
                logger.info(f"Marked attempt {self.interview_context.attempt_id} as completed")
            else:
                logger.warning(f"Failed to mark attempt as completed for {self.interview_context.attempt_id}")
                
        except Exception as e:
            logger.error(f"Error marking attempt as completed: {e}")
    
    @function_tool()
    async def move_to_next_stage(self, context: RunContext, stage: str):
        """Move interview to next stage
        
        Args:
            stage: The stage to move to (introduction, main_questions, wrap_up, feedback)
        """
        previous_stage = self.current_stage
        valid_stages = ["introduction", "main_questions", "wrap_up", "feedback"]
        
        if stage not in valid_stages:
            return f"Invalid stage. Valid stages are: {', '.join(valid_stages)}"
        
        self.current_stage = stage
        
        # Save stage transition to transcription
        # await self.save_stage_transition(previous_stage, stage)
        
        logger.info(f"Interview moved from {previous_stage} to {stage} stage")
        return f"Interview stage changed to: {stage}"
    
    @function_tool()
    async def record_question_asked(self, context: RunContext, question: str, category: str):
        """Record a question that was asked
        
        Args:
            question: The question that was asked
            category: The category of the question (technical, behavioral, etc.)
        """
        question_data = {
            'question': question,
            'category': category,
            'timestamp': asyncio.get_event_loop().time() - (self.start_time or 0)
        }
        
        self.questions_asked.append(question_data)
        logger.info(f"Question recorded: {category} - {question[:50]}...")
        
        # Save question to transcription database
        # await self.transcription_handler.save_interviewer_message(
        #     question,
        #     message_type="question",
        #     metadata={
        #         "category": category,
        #         "question_number": len(self.questions_asked),
        #         "stage": self.current_stage,
        #         "interview_time": question_data['timestamp']
        #     }
        # )
        
        return f"Question recorded in {category} category"
    
    @function_tool()
    async def get_interview_progress(self, context: RunContext):
        """Get current interview progress and statistics
        
        Returns:
            Dictionary with progress information
        """
        progress = {
            'current_stage': self.current_stage,
            'questions_asked': len(self.questions_asked),
            'interview_duration': (asyncio.get_event_loop().time() - (self.start_time or 0)) if self.start_time else 0,
            'questions_by_category': {}
        }
        
        # Count questions by category
        for q in self.questions_asked:
            category = q.get('category', 'general')
            progress['questions_by_category'][category] = progress['questions_by_category'].get(category, 0) + 1
        
        return json.dumps(progress)

    @function_tool()
    async def end_interview(self, context: RunContext, reason: str, summary: str):
        """End the interview session
        
        Args:
            reason: Why the interview is ending (e.g., "time_complete", "questions_complete", "natural_conclusion")
            summary: Brief summary of the interview performance
        """
        try:
            logger.info(f"Agent initiated interview end - Reason: {reason}")
            
            # Mark interview as ending
            self.current_stage = "ending"
            
            # Calculate duration
            duration = asyncio.get_event_loop().time() - (self.start_time or 0)
            duration_minutes = duration / 60
            
            # Generate ending message using provided summary only
            ending_message = f"""Thank you for completing this interview. 
            
            {summary}
            
            I'll now end our session. Your responses have been recorded and you'll receive detailed feedback shortly."""
            
            # Send ending message to user
            await self.session.generate_reply(instructions=f"Say this ending message to the candidate: {ending_message}")
            
            # Send structured notification to frontend via text stream
            try:
                if hasattr(self.session, 'room') and hasattr(self.session.room, 'localParticipant'):
                    end_notification = {
                        "type": "interview_end",
                        "reason": reason,
                        "summary": summary,
                        "attempt_number": self.interview_context.attempt_number,
                        "session_id": self.interview_context.session_id,
                        "attempt_id": self.interview_context.attempt_id,
                        "duration_minutes": duration_minutes,
                        "next_steps": "feedback_ready" if self.interview_context.attempt_number == 3 else "attempt_available"
                    }
                    
                    # Send via text stream
                    await self.session.room.local_participant.send_text(
                        json.dumps(end_notification),
                        topic="interview_control"
                    )
                    logger.info("Sent interview end notification to frontend")
                    
            except Exception as e:
                logger.error(f"Error sending text stream notification: {e}")
            
            # Save ending system message
            # asyncio.create_task(self.transcription_handler.save_system_message(
            #     f"Interview ended by agent. Reason: {reason}. Summary: {summary}",
            #     metadata={
            #         "event": "interview_end_by_agent",
            #         "reason": reason,
            #         "summary": summary,
            #         "duration_seconds": duration,
            #         "duration_minutes": duration_minutes,
            #         "attempt_number": self.interview_context.attempt_number
            #     }
            # ))
            
            # Wait a moment for messages to be sent
            await asyncio.sleep(3)
            
            # Mark attempt as completed
            await self.update_attempt_completion(duration_minutes)
            
            # Disconnect from room (this will trigger on_exit)
            if hasattr(self.session, 'room'):
                await self.session.room.disconnect()
                
            return f"Interview ended successfully. Reason: {reason}"
            
        except Exception as e:
            logger.error(f"Error ending interview: {str(e)}")
            return f"Error ending interview: {str(e)}"


    
    async def save_stage_transition(self, from_stage: str, to_stage: str):
        """Save stage transition as system message"""
        try:
            # asyncio.create_task(self.transcription_handler.save_system_message(
            #     f"Interview stage changed from '{from_stage}' to '{to_stage}'",
            #     metadata={
            #         "event": "stage_transition",
            #         "from_stage": from_stage,
            #         "to_stage": to_stage,
            #         "timestamp": asyncio.get_event_loop().time() - (self.start_time or 0)
            #     }
            # ))
            pass # No longer using TranscriptionHandler for stage transitions
        except Exception as e:
            logger.error(f"Error saving stage transition: {str(e)}")


# Removed duplicate transcription handlers - session.py handles this correctly


async def save_live_transcription(attempt_id: str, speaker_type: str, speaker_name: str, content: str):
    """Save message directly to live transcription in mock_interview_attempts table"""
    try:
        from ...extensions import get_admin_client
        admin_client = get_admin_client()
        
        # Get current live transcription
        response = admin_client.table('mock_interview_attempts')\
            .select('live_transcription')\
            .eq('id', attempt_id)\
            .execute()
        
        if not response.data:
            logger.warning(f"No attempt found with id {attempt_id}")
            return
            
        current_transcription = response.data[0].get('live_transcription') or {"conversation": []}
        
        # Add new conversation entry
        from datetime import datetime
        conversation_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "speaker_type": speaker_type,
            "speaker_name": speaker_name,
            "message": content,
            "sequence": len(current_transcription.get("conversation", [])) + 1
        }
        
        current_transcription["conversation"].append(conversation_entry)
        
        # Update the attempt with new live transcription
        update_response = admin_client.table('mock_interview_attempts')\
            .update({'live_transcription': current_transcription})\
            .eq('id', attempt_id)\
            .execute()
        
        if update_response.data:
            logger.info(f"Saved live transcription for attempt {attempt_id}: {speaker_type} - {content[:50]}...")
        else:
            logger.warning(f"Failed to update live transcription for attempt {attempt_id}")
            
    except Exception as e:
        logger.error(f"Error saving live transcription: {e}")


async def update_interview_status_prep(session_id: str, status_prep: str, agent_prompt: str = None) -> bool:
    """Update the status_prep field for an interview session
    
    Args:
        session_id: The interview session ID
        status_prep: The new status ('PENDING' or 'DONE')
        agent_prompt: Optional agent prompt to set when status is DONE
        
    Returns:
        True if update was successful, False otherwise
    """
    try:
        # Import here to avoid circular imports
        from ...extensions import get_admin_client
        admin_client = get_admin_client()
        
        update_data = {'status_prep': status_prep}
        if agent_prompt:
            update_data['agent_prompt'] = agent_prompt
        
        result = admin_client.table('mock_interview')\
            .update(update_data)\
            .eq('id', session_id)\
            .execute()
        
        if result.data:
            logger.info(f"Updated interview session {session_id} status_prep to {status_prep}")
            return True
        else:
            logger.warning(f"No rows updated for session_id {session_id}")
            return False
                        
    except Exception as e:
        logger.error(f"Error updating interview status_prep: {e}")
        return False

async def check_interview_session_ready(session_id: str) -> bool:
    """Check if an interview session is ready to start (status_prep = DONE)
    
    Args:
        session_id: The interview session ID
        
    Returns:
        True if session is ready (status_prep = DONE), False otherwise
    """
    try:           
        # Import here to avoid circular imports
        from ...extensions import get_admin_client
        admin_client = get_admin_client()
        
        result = admin_client.table('mock_interview')\
            .select('status_prep')\
            .eq('id', session_id)\
            .execute()
        
        if result.data:
            status_prep = result.data[0].get('status_prep', 'PENDING')
            return status_prep == 'DONE'
        else:
            logger.warning(f"No interview session found for session_id {session_id}")
            return False
                        
    except Exception as e:
        logger.error(f"Error checking interview session readiness: {e}")
        return False

async def get_interview_session_from_metadata(session_id: str) -> Optional[InterviewContext]:
    """Get interview context from database by session ID - now creates or gets current attempt"""
    try:
        # Import here to avoid circular imports
        from ...extensions import get_admin_client
        admin_client = get_admin_client()
        
        # Query mock_interview table (main session)
        result = admin_client.table('mock_interview')\
            .select('*')\
            .eq('id', session_id)\
            .execute()
        
        if not result.data:
            logger.warning(f"No interview session found for session_id {session_id}")
            return None
            
        session_data = result.data[0]
        
        # Check if status_prep is DONE before proceeding
        status_prep = session_data.get('status_prep', 'PENDING')
        if status_prep != 'DONE':
            logger.warning(f"Interview session {session_id} is not ready (status_prep: {status_prep}). Agent will not connect.")
            return None
        
        # Get current attempt or create new one
        attempt_data = await get_or_create_current_attempt(session_id, admin_client)
        if not attempt_data:
            logger.error(f"Could not get or create attempt for session {session_id}")
            return None
        
        return _create_interview_context_from_session_and_attempt(session_data, attempt_data)
        
    except Exception as e:
        logger.error(f"Error getting interview context from metadata: {e}")
        return None

async def get_interview_session_context(room_name: str) -> Optional[InterviewContext]:
    """Get interview context from database based on room name - now works with attempts"""
    try:
        # Import here to avoid circular imports
        from ...extensions import get_admin_client
        admin_client = get_admin_client()
        
        # First, try to find the attempt by room_name
        attempt_result = admin_client.table('mock_interview_attempts')\
            .select('*, mock_interview(*)')\
            .eq('room_name', room_name)\
            .execute()
        
        if attempt_result.data:
            attempt_data = attempt_result.data[0]
            session_data = attempt_data['mock_interview']
            
            # Check if status_prep is DONE
            if session_data.get('status_prep', 'PENDING') != 'DONE':
                logger.warning(f"Interview session is not ready. Agent will not connect.")
                return None
            
            return _create_interview_context_from_session_and_attempt(session_data, attempt_data)
        
        # Fallback: try to extract session ID from room name and create attempt
        import re
        uuid_pattern = r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})'
        uuid_matches = re.findall(uuid_pattern, room_name)
        
        if uuid_matches:
            potential_session_id = uuid_matches[-1]
            logger.info(f"Extracted potential session_id from room name: {potential_session_id}")
            
            # Try to find session and create/get attempt
            session_result = admin_client.table('mock_interview')\
                .select('*')\
                .eq('id', potential_session_id)\
                .execute()
                
            if session_result.data:
                session_data = session_result.data[0]
                
                if session_data.get('status_prep', 'PENDING') != 'DONE':
                    logger.warning(f"Interview session {potential_session_id} is not ready. Agent will not connect.")
                    return None
                
                # Create or get current attempt
                attempt_data = await get_or_create_current_attempt(potential_session_id, admin_client, room_name)
                if attempt_data:
                    return _create_interview_context_from_session_and_attempt(session_data, attempt_data)
        
        logger.warning(f"No interview session found for room_name {room_name}")
        return None
            
    except Exception as e:
        logger.error(f"Error getting interview context by room name: {e}")
        return None

async def get_or_create_current_attempt(session_id: str, admin_client, room_name: str = None) -> Optional[dict]:
    """Get current attempt or create a new one if under the 3-attempt limit"""
    try:
        # Get existing attempts for this session
        attempts_result = admin_client.table('mock_interview_attempts')\
            .select('*')\
            .eq('mock_interview_id', session_id)\
            .order('attempt_number', desc=False)\
            .execute()
        
        existing_attempts = attempts_result.data or []
        
        # Check for active attempt
        for attempt in existing_attempts:
            if attempt['status'] in ['pending', 'active']:
                logger.info(f"Found existing active attempt {attempt['attempt_number']} for session {session_id}")
                return attempt
        
        # If no active attempt, create new one if under limit
        next_attempt_number = len(existing_attempts) + 1
        
        if next_attempt_number > 3:
            logger.warning(f"Session {session_id} has already reached maximum 3 attempts")
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
        
        insert_result = admin_client.table('mock_interview_attempts')\
            .insert(new_attempt)\
            .execute()
        
        if insert_result.data:
            logger.info(f"Created new attempt {next_attempt_number} for session {session_id}")
            return insert_result.data[0]
        else:
            logger.error(f"Failed to create new attempt for session {session_id}")
            return None
            
    except Exception as e:
        logger.error(f"Error getting or creating attempt: {e}")
        return None

def _create_interview_context_from_session_and_attempt(session_data: dict, attempt_data: dict) -> InterviewContext:
    """Create InterviewContext from session and attempt data"""
    user_display_name = (
        session_data.get('display_name') or 
        session_data.get('user_display_name') or 
        'Candidate'
    )
    
    return InterviewContext(
        session_id=session_data.get('id', ''),
        attempt_id=attempt_data.get('id', ''),
        attempt_number=attempt_data.get('attempt_number', 1),
        user_id=session_data.get('user_id', ''),
        user_display_name=user_display_name,
        resume_text=session_data.get('resume_text', ''),
        job_description=session_data.get('job_description', ''),
        company_name=session_data.get('company_name', 'Company'),
        position=session_data.get('position', 'Software Engineer'),
        interview_type=session_data.get('interview_type', 'behavioral'),
        difficulty_level=session_data.get('difficulty_level', 'mid'),
        custom_instructions=session_data.get('custom_instructions', ''),
        company_details=session_data.get('company_details', ''),
        linkedin_profile=session_data.get('linkedin_profile', ''),
        agent_prompt=session_data.get('agent_prompt', ''),
        status_prep=session_data.get('status_prep', 'PENDING'),
        room_name=attempt_data.get('room_name', ''),
        title=session_data.get('title', ''),
        attempt_status=attempt_data.get('status', 'pending')
    )


# Note: LiveKit agents handle room disconnection and shutdown automatically


# LiveKit job entry point
async def entrypoint(ctx: JobContext):
    """Main entry point for LiveKit agent jobs"""
    logger.info(f"Mock interview agent job started for room: {ctx.room.name}")
    
    # Extract interview context from job metadata or room name
    interview_context = None
    
    # Try to get context from job metadata first
    # Check various ways metadata might be accessible in current LiveKit agents API
    metadata = None
    session_id = None
    
    # Try different metadata access patterns
    try:
        # Method 1: ctx.job.metadata (legacy)
        if hasattr(ctx, 'job') and hasattr(ctx.job, 'metadata'):
            metadata = ctx.job.metadata
            logger.info(f"Found metadata via ctx.job.metadata: {type(metadata)}")
        
        # Method 2: Direct ctx metadata
        elif hasattr(ctx, 'metadata'):
            metadata = ctx.metadata
            logger.info(f"Found metadata via ctx.metadata: {type(metadata)}")
            
        # Method 3: Job context metadata
        elif hasattr(ctx, 'job') and hasattr(ctx.job, 'data'):
            metadata = getattr(ctx.job.data, 'metadata', None)
            logger.info(f"Found metadata via ctx.job.data.metadata: {type(metadata)}")
            
        if metadata is not None:
            # Handle metadata as either string (JSON) or dict
            if isinstance(metadata, str) and metadata.strip():
                # If metadata is a non-empty JSON string, parse it
                logger.info(f"Job metadata is string: {metadata[:100]}...")
                metadata_dict = json.loads(metadata)
                session_id = metadata_dict.get('session_id')
            elif isinstance(metadata, dict):
                # If metadata is already a dict, use it directly
                logger.info(f"Job metadata is dict: {list(metadata.keys())}")
                session_id = metadata.get('session_id')
            elif isinstance(metadata, str) and not metadata.strip():
                logger.warning("Metadata is empty string")
            else:
                logger.warning(f"Unexpected metadata type: {type(metadata)}")
            
            if session_id:
                logger.info(f"Found session_id in job metadata: {session_id}")
                interview_context = await get_interview_session_from_metadata(session_id)
        else:
            logger.warning("No metadata found in job context")
                
    except Exception as e:
        logger.error(f"Error processing job metadata: {e}")
        logger.error(f"Raw metadata: {metadata}")
    
    # Fallback: try to get context from room name
    if not interview_context:
        interview_context = await get_interview_session_context(ctx.room.name)
    
    # Final fallback: create default context
    if not interview_context:
        logger.warning("No interview context found or session not ready. Agent will not start.")
        return
    
    # Double-check that the session is ready
    if interview_context.status_prep != "DONE":
        logger.warning(f"Interview session {interview_context.session_id} status_prep is {interview_context.status_prep}, not DONE. Agent will not start.")
        return
    
    # Validate that agent_prompt exists before creating agent
    if not hasattr(interview_context, 'agent_prompt') or not interview_context.agent_prompt or not interview_context.agent_prompt.strip():
        logger.error(f"No agent_prompt found for session {interview_context.session_id}. Agent cannot start without proper instructions.")
        return
    
    logger.info(f"Agent prompt found for session {interview_context.session_id}. Prompt length: {len(interview_context.agent_prompt)} characters")
    logger.debug(f"Agent prompt preview: {interview_context.agent_prompt[:200]}...")
    
    # Create the interview agent (will use agent_prompt from database only)
    try:
        assistant = MockInterviewAgent(interview_context)
    except ValueError as e:
        logger.error(f"Failed to create interview agent: {e}")
        return
    
    # Create AgentSession with OpenAI Realtime API (v1.0+ pattern)
    session = AgentSession(
        llm=realtime.RealtimeModel(
            voice=assistant.voice,
            temperature=assistant.temperature
        )
    )
    
    # Add event handlers for conversation capture
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
            
            # Get text content from the item
            content = None
            if hasattr(item, 'text_content'):
                if callable(item.text_content):
                    content = item.text_content()
                else:
                    content = item.text_content
            elif hasattr(item, 'content'):
                content = item.content
            elif hasattr(item, 'text'):
                content = item.text
            
            if not content or not content.strip():
                return
            
            # Log the conversation item
            if item_role == "user":
                logger.info(f"[{interview_context.room_name}] user: {content}")
                # Save user message to live transcription
                asyncio.create_task(save_live_transcription(
                    interview_context.attempt_id,
                    "candidate",
                    interview_context.user_display_name,
                    content.strip()
                ))
            elif item_role == "assistant":
                logger.info(f"[{interview_context.room_name}] assistant: {content}")
                # Save assistant message to live transcription
                asyncio.create_task(save_live_transcription(
                    interview_context.attempt_id,
                    "interviewer", 
                    "AI Interviewer",
                    content.strip()
                ))
                
        except Exception as e:
            logger.error(f"Error in conversation_item_added handler: {e}")

    # Connect to the room and start the agent
    await ctx.connect()
    
    # Start the session with the agent
    await session.start(room=ctx.room, agent=assistant)
    
    logger.info("Mock interview session started successfully")

def main():
    """Main function to run the mock interview agent"""

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
    
    # Run the agent with correct configuration
    try:
        worker_options = agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            port=8081  # Debug port
        )
        agents.cli.run_app(worker_options)
        return True
    except Exception as e:
        logger.error(f"Error running agent: {e}")
        return False

# Main execution for standalone running
if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)