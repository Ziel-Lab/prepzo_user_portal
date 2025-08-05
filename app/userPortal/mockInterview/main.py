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
    from .agent import entrypoint, main as agent_main
    LIVEKIT_AGENTS_AVAILABLE = True
except ImportError:
    try:
        from agent import entrypoint, main as agent_main
        LIVEKIT_AGENTS_AVAILABLE = True
    except ImportError:
        entrypoint = None
        agent_main = None
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
        
        # DISABLED: Preventing duplicate agent workers 
        # Only agent.py should run the worker to prevent 2 agents per room
        logger.info("main.py is disabled - use agent.py directly to start the agent worker")
        logger.info("To start agent: python -m app.userPortal.mockInterview.agent")
        
        # If you need to run from main.py, call agent_main directly (no WorkerOptions)
        if agent_main:
            logger.info("Calling agent_main() directly without creating duplicate worker")
            agent_main()
        else:
            logger.error("No agent_main function available")
    except Exception as e:
        logger.error(f"Interview agent application error: {str(e)}")
        logger.error(traceback.format_exc())
        raise

if __name__ == "__main__":
    main() 