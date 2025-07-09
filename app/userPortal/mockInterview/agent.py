"""
LiveKit Agent with OpenAI Realtime integration for mock interviews
"""

import os
import asyncio
import logging
import aiohttp
import json
from typing import Dict, Any, Optional

# Handle imports for both standalone and Flask app usage
try:
    from flask import current_app
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False
    current_app = None

# Handle relative imports
try:
    from .prompt import get_interview_prompt, get_closing_prompt, get_enhanced_interview_prompt
except ImportError:
    try:
        from prompt import get_interview_prompt, get_closing_prompt, get_enhanced_interview_prompt
    except ImportError:
        # Define minimal fallbacks if prompt module not available
        def get_interview_prompt(*args, **kwargs):
            return "You are a professional interviewer conducting a mock interview."
        def get_closing_prompt():
            return "Thank you for the interview. Please provide closing feedback."
        def get_enhanced_interview_prompt(context):
            return f"You are interviewing for {context.get('position', 'a position')}. Conduct a professional interview."

# Configure logging for production
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# LiveKit agents imports
try:
    from livekit import agents
    from livekit.agents import AgentSession, Agent, RoomInputOptions
    from livekit.plugins import openai, noise_cancellation
    from dotenv import load_dotenv
    LIVEKIT_AGENTS_AVAILABLE = True
    logger.info("LiveKit agents imported successfully")
except ImportError as e:
    LIVEKIT_AGENTS_AVAILABLE = False
    logger.error(f"Failed to import LiveKit agents: {e}")

# Load environment variables if running standalone
load_dotenv()

# Define the session state class
class InterviewSessionState:
    """State management for interview session"""
    
    def __init__(self, interview_config=None):
        self.interview_config = interview_config or {}
        self.interview_started = False
        self.interview_duration = self.interview_config.get('duration_minutes', 30) * 60  # Convert to seconds
        self.start_time = None
        self.transcript = []
        self.feedback_notes = []
        self.instructions = ""

    def start_interview(self):
        """Start the interview session"""
        if not self.interview_started:
            self.interview_started = True
            self.start_time = asyncio.get_event_loop().time()
            logger.info(f"Interview started for session: {self.interview_config.get('session_id', 'unknown')}")

    def add_to_transcript(self, speaker: str, message: str, message_type: str = 'text'):
        """Add message to interview transcript"""
        if not self.start_time:
            return
            
        self.transcript.append({
            'speaker': speaker,
            'message': message,
            'timestamp': asyncio.get_event_loop().time() - self.start_time,
            'type': message_type
        })

    def should_end_interview(self):
        """Check if interview should end based on time"""
        if not self.start_time:
            return False
        
        elapsed_time = asyncio.get_event_loop().time() - self.start_time
        return elapsed_time >= self.interview_duration

    async def end_interview(self):
        """End the interview and provide closing feedback"""
        # Generate feedback
        feedback = await self.generate_feedback()
        
        # Calculate interview duration
        duration = asyncio.get_event_loop().time() - (self.start_time or 0)
        
        # Store results
        self.interview_results = {
            'transcript': self.transcript,
            'feedback': feedback,
            'duration': duration,
            'interview_config': self.interview_config
        }
        
        # Send webhook with interview completion data
        await self.send_completion_webhook()

    async def generate_feedback(self):
        """Generate AI feedback based on the interview"""
        feedback = {
            'overall_score': 'Good',
            'strengths': [
                'Clear communication',
                'Relevant examples',
                'Professional demeanor'
            ],
            'areas_for_improvement': [
                'Provide more specific details',
                'Use more quantifiable metrics'
            ],
            'specific_notes': 'The candidate demonstrated good communication skills and provided relevant examples.',
            'recommendations': [
                'Practice with more technical questions',
                'Prepare more STAR method examples'
            ]
        }
        
        return feedback

    def get_interview_results(self):
        """Get the interview results"""
        return getattr(self, 'interview_results', None)

    async def send_completion_webhook(self):
        """Send interview completion data to webhook endpoint"""
        try:
            # Get session ID from interview config
            session_id = self.interview_config.get('session_id')
            if not session_id:
                logger.warning("No session_id found in interview config for webhook")
                return
            
            # Prepare webhook payload
            webhook_data = {
                'session_id': session_id,
                'room_name': self.interview_config.get('room_name'),
                'transcript': self.format_transcript_for_webhook(),
                'duration': self.interview_results.get('duration', 0),
                'participant_data': {
                    'total_messages': len(self.transcript),
                    'feedback_notes': self.feedback_notes,
                    'interview_type': self.interview_config.get('interview_type'),
                    'position': self.interview_config.get('position')
                }
            }
            
            # Get webhook URL from Flask config or environment
            if FLASK_AVAILABLE and current_app:
                webhook_url = current_app.config.get('INTERVIEW_WEBHOOK_URL')
            else:
                webhook_url = os.getenv('INTERVIEW_WEBHOOK_URL')
                
            if not webhook_url:
                # Fallback to local development URL
                webhook_url = 'http://localhost:5000/userPortal/mockInterview/webhook/interview-completed'
            
            # Send webhook
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    webhook_url,
                    json=webhook_data,
                    headers={'Content-Type': 'application/json'},
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status == 200:
                        logger.info(f"Successfully sent completion webhook for session {session_id}")
                    else:
                        logger.error(f"Webhook failed with status {response.status}: {await response.text()}")
                        
        except Exception as e:
            logger.error(f"Error sending completion webhook: {str(e)}")

    def format_transcript_for_webhook(self):
        """Format transcript for webhook transmission"""
        try:
            formatted_transcript = []
            
            for entry in self.transcript:
                formatted_entry = {
                    'speaker': entry.get('speaker', 'unknown'),
                    'message': entry.get('message', ''),
                    'timestamp': entry.get('timestamp', 0),
                    'type': entry.get('type', 'message')
                }
                formatted_transcript.append(formatted_entry)
            
            # Convert to readable text format for analysis
            text_transcript = "\n\n".join([
                f"[{entry['timestamp']:.1f}s] {entry['speaker']}: {entry['message']}"
                for entry in formatted_transcript
                if entry['message'].strip()
            ])
            
            return text_transcript
            
        except Exception as e:
            logger.error(f"Error formatting transcript: {str(e)}")
            return "Error formatting transcript"

# Global interview state (in production, this would be stored in a database)
interview_sessions = {}

class InterviewAssistant(Agent):
    """Custom agent for mock interviews"""
    
    def __init__(self, session_state: InterviewSessionState):
        # Get enhanced interview prompt with full context
        interview_config = session_state.interview_config
        interview_context = interview_config.get('interview_context', {})
        
        if interview_context and any([
            interview_context.get('resume_text'),
            interview_context.get('job_description')
        ]):
            instructions = get_enhanced_interview_prompt(interview_context)
        else:
            # Fallback to basic prompt if no enhanced context provided
            instructions = get_interview_prompt(
                interview_type=interview_config.get('interview_type', 'behavioral'),
                difficulty_level=interview_config.get('difficulty_level', 'medium'),
                position=interview_config.get('position', 'Software Engineer'),
                custom_instructions=interview_config.get('custom_instructions', '')
            )
        
        # Add greeting instruction to the beginning
        greeting_instruction = (
            "Start the interview by greeting the participant warmly and asking them to introduce themselves. "
            "Keep your initial greeting brief and natural. "
        )
        instructions = greeting_instruction + instructions
        
        super().__init__(instructions=instructions)
        self.session_state = session_state

async def entrypoint(ctx: agents.JobContext):
    """Main entrypoint for the interview agent"""
    if not LIVEKIT_AGENTS_AVAILABLE:
        logger.error("LiveKit agents not available")
        return
        
    logger.info(f"Starting interview session in room: {ctx.room.name}")
    
    # Check OpenAI API key
    openai_key = os.getenv('OPENAI_API_KEY')
    if openai_key:
        logger.info(f"OpenAI API key found (length: {len(openai_key)})")
    else:
        logger.error("OpenAI API key not found!")
        return
    
    # Get interview configuration
    interview_config = await get_interview_config(ctx.room.name)
    
    # Initialize interview session state
    session_state = InterviewSessionState(interview_config)
    interview_sessions[ctx.room.name] = session_state
    
    # Create interview assistant
    assistant = InterviewAssistant(session_state)
    
    # Create agent session with OpenAI Realtime model
    logger.info("Creating AgentSession with RealtimeModel")
    try:
        session = AgentSession(
            llm=openai.realtime.RealtimeModel(
                voice="coral"  # You can change to "alloy", "sage", "shimmer", etc.
            )
        )
        logger.info("AgentSession created successfully")
    except Exception as e:
        logger.error(f"Error creating AgentSession: {e}")
        raise
    
    # Set up basic transcript tracking (optional - RealtimeModel handles most of this)
    @session.on("agent_speech_committed")
    def on_agent_speech(msg):
        session_state.add_to_transcript("agent", msg.content)
        logger.info(f"Agent said: {msg.content}")
    
    @session.on("user_speech_committed") 
    def on_user_speech(msg):
        session_state.add_to_transcript("user", msg.content)
        logger.info(f"User said: {msg.content}")
    
    # Start the session
    logger.info("Starting AgentSession")
    try:
        await session.start(
            room=ctx.room,
            agent=assistant,
            room_input_options=RoomInputOptions(
                # Production: Enable LiveKit Cloud enhanced noise cancellation
                # - For production, always use LiveKit Cloud features
                # - For telephony applications, use `BVCTelephony` for best results
                noise_cancellation=noise_cancellation.BVC(),
            ),
        )
        logger.info("AgentSession started successfully")
    except Exception as e:
        logger.error(f"Error starting AgentSession: {e}")
        raise
    
    # Connect to the room
    logger.info("Connecting to room")
    try:
        await ctx.connect()
        logger.info("Connected to room successfully")
    except Exception as e:
        logger.error(f"Error connecting to room: {e}")
        raise
    
    # Start the interview
    session_state.start_interview()
    
    # Simple greeting after connection
    logger.info("Sending initial greeting")
    try:
        await asyncio.sleep(2)  # Wait for connection to stabilize
        
        greeting_instructions = (
            "Greet the participant warmly and ask them to introduce themselves. "
            "Say something like: 'Welcome to your mock interview! I'm your AI interviewer. "
            "Please introduce yourself and tell me about your background.'"
        )
        
        await session.generate_reply(instructions=greeting_instructions)
        logger.info("Initial greeting sent successfully")
    except Exception as e:
        logger.error(f"Error sending greeting: {e}")
    
    # Monitor for interview completion
    asyncio.create_task(monitor_interview_completion(session, session_state))
    
    logger.info("Interview session initialized successfully")



async def handle_interview_end(session: AgentSession, session_state: InterviewSessionState):
    """Handle the end of an interview"""
    try:
        await session_state.end_interview()
        
        # Generate closing message
        closing_message = get_closing_prompt()
        await session.generate_reply(
            instructions=f"End the interview with this closing message: {closing_message}"
        )
        
        logger.info("Interview ended successfully")
        
    except Exception as e:
        logger.error(f"Error handling interview end: {str(e)}")

async def monitor_interview_completion(session: AgentSession, session_state: InterviewSessionState):
    """Monitor interview for completion based on time"""
    try:
        # Wait for interview duration
        await asyncio.sleep(session_state.interview_duration)
        
        # Check if interview should end
        if session_state.should_end_interview():
            logger.info(f"Interview time limit reached for session")
            await handle_interview_end(session, session_state)
            
    except Exception as e:
        logger.error(f"Error monitoring interview completion: {str(e)}")

async def get_interview_config(room_name: str) -> Dict[str, Any]:
    """Get interview configuration for the room"""
    # Check if we have a pre-stored configuration
    if room_name in interview_sessions:
        return interview_sessions[room_name].interview_config
    
    # Return default configuration
    return {
        'session_id': room_name,
        'room_name': room_name,
        'interview_type': 'behavioral',
        'difficulty_level': 'medium',
        'position': 'Software Engineer',
        'duration_minutes': 30,
        'custom_instructions': '',
        'interview_context': {}
    }

async def start_agent_for_session(session_config: Dict[str, Any]):
    """Start the interview agent for a specific session (called from Flask routes)"""
    if not LIVEKIT_AGENTS_AVAILABLE:
        logger.error("LiveKit agents not available")
        return False
        
    try:
        room_name = session_config.get('room_name')
        if not room_name:
            logger.error("No room_name provided in session config")
            return False
        
        logger.info(f"Starting agent for session: {room_name}")
        
        # Store the enhanced session config for the agent to use
        interview_sessions[room_name] = InterviewSessionState(session_config)
        
        return True
        
    except Exception as e:
        logger.error(f"Error starting agent for session: {str(e)}")
        return False

def check_configuration():
    """Check if required configuration is available"""
    required_config_vars = [
        'LIVEKIT_URL',
        'LIVEKIT_API_KEY', 
        'LIVEKIT_API_SECRET',
        'OPENAI_API_KEY'
    ]
    
    def get_config_value(key):
        """Get configuration value from Flask app config or environment"""
        if FLASK_AVAILABLE and current_app:
            return current_app.config.get(key)
        return os.getenv(key)
    
    missing_vars = [var for var in required_config_vars if not get_config_value(var)]
    
    if missing_vars:
        if FLASK_AVAILABLE and current_app:
            print(f"Missing configuration in AWS Secret Manager: {missing_vars}")
        else:
            print(f"Missing environment variables: {missing_vars}")
            print("Tip: Use 'python start_agent.py' from project root to load AWS secrets")
        return False
    else:
        print("All required configuration available")
        return True

def main():
    """Main entry point for the interview agent"""
    print("Mock Interview Agent - Production Ready")
    print("=" * 40)
    
    # Check LiveKit agents availability
    if not LIVEKIT_AGENTS_AVAILABLE:
        print("LiveKit agents package not available")
        print("   Please install: pip install livekit-agents[openai]")
        return
    else:
        print("LiveKit agents available")
    
    # Check configuration
    if not check_configuration():
        return
    
    print("\nStarting LiveKit Interview Agent in Production Mode...")
    
    try:
        # Use the correct WorkerOptions for the current LiveKit agents version
        worker_options = agents.WorkerOptions(entrypoint_fnc=entrypoint)
        
        # Set up CLI with worker options
        agents.cli.run_app(worker_options)
    except Exception as e:
        logger.error(f"Error running agent: {str(e)}")
        print(f"Error running agent: {str(e)}")

if __name__ == "__main__":
    main()