"""
Main entry point for the Mock Interview Agent application
"""
import asyncio
import logging
import traceback
import os
from dotenv import load_dotenv

# Handle imports for both standalone and package usage
try:
    from .agent import initialize_interview_session, LIVEKIT_AGENTS_AVAILABLE
except ImportError:
    try:
        from agent import initialize_interview_session, LIVEKIT_AGENTS_AVAILABLE
    except ImportError:
        initialize_interview_session = None
        LIVEKIT_AGENTS_AVAILABLE = False

# Initialize application
load_dotenv()

# Setup logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("interview-agent")

def main():
    """
    Main application entry point
    
    Initializes and runs the LiveKit interview agent application
    """
    try:
        logger.info("Starting Mock Interview Agent application")
        
        if not LIVEKIT_AGENTS_AVAILABLE:
            logger.error("LiveKit agents not available. Please install: pip install livekit-agents livekit-plugins-openai")
            return
        
        # Import LiveKit CLI after confirming availability
        from livekit.agents import cli, WorkerOptions
        
        # Validate critical settings
        required_env_vars = [
            'LIVEKIT_URL',
            'LIVEKIT_API_KEY', 
            'LIVEKIT_API_SECRET',
            'OPENAI_API_KEY'
        ]
        
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            logger.error(f"Missing required environment variables: {missing_vars}")
            logger.warning("Interview agent started with missing critical configuration")
        
        # Run the agent application
        cli.run_app(
            WorkerOptions(
                entrypoint_fnc=initialize_interview_session,
            ),
        )
    except Exception as e:
        logger.error(f"Interview agent application error: {str(e)}")
        logger.error(traceback.format_exc())
        raise

if __name__ == "__main__":
    main() 