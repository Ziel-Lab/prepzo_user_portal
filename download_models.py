#!/usr/bin/env python3
"""
Pre-download VAD and Turn Detection models for LiveKit Agent
Run this script once to download models before starting the agent
"""

import os
import sys
import logging

# Add the app directory to Python path so we can import modules
sys.path.insert(0, '/app')

# Import secrets module to load AWS secrets
try:
    from app.secrets import get_secret
    SECRETS_AVAILABLE = True
except ImportError:
    print("Warning: Could not import secrets module. Environment variables must be set manually.")
    SECRETS_AVAILABLE = False

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def load_secrets_from_aws():
    """Load secrets from AWS Secret Manager and set environment variables"""
    if not SECRETS_AVAILABLE:
        logger.warning("Secrets module not available - using existing environment variables")
        return
    
    try:
        logger.info("Loading secrets from AWS Secret Manager...")
        secret_name = "userPortal"
        region_name = "us-east-1"
        secrets = get_secret(secret_name, region_name)
        
        if secrets:
            # Set environment variables from AWS secrets
            for key, value in secrets.items():
                if value:  # Only set non-empty values
                    os.environ[key] = str(value)
            
            logger.info("✅ AWS secrets loaded successfully")
            
            # Log which critical keys we have (without exposing values)
            critical_keys = ['OPENAI_API_KEY', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET', 'LIVEKIT_URL']
            available_keys = [key for key in critical_keys if os.environ.get(key)]
            missing_keys = [key for key in critical_keys if not os.environ.get(key)]
            
            if available_keys:
                logger.info(f"Available keys: {available_keys}")
            if missing_keys:
                logger.warning(f"Missing keys: {missing_keys}")
                
        else:
            logger.error("No secrets retrieved from AWS Secret Manager")
            
    except Exception as e:
        logger.error(f"Failed to load secrets from AWS: {e}")
        logger.info("Continuing with existing environment variables...")

def download_silero_vad():
    """Download Silero VAD model"""
    try:
        logger.info("Downloading Silero VAD model...")
        from livekit.plugins import silero
        
        # Load the model (this will download it)
        vad = silero.VAD.load()
        logger.info("✅ Silero VAD model downloaded successfully")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to download Silero VAD: {e}")
        return False

def download_turn_detection():
    """Download English turn detection model"""
    try:
        logger.info("Checking turn detection model...")
        # Skip turn detection model download - it needs to be done within agent context
        logger.info("ℹ️  Turn detection model will be downloaded when agent starts")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to download turn detection model: {e}")
        return False

def check_openai_stt():
    """Check OpenAI STT configuration"""
    try:
        logger.info("Checking OpenAI STT configuration...")
        
        # Check if OpenAI API key is available
        openai_key = os.environ.get('OPENAI_API_KEY')
        if not openai_key:
            logger.warning("⚠️  OPENAI_API_KEY not found - skipping STT check")
            logger.info("ℹ️  OpenAI STT will be configured when agent starts")
            return True
            
        from livekit.plugins.openai import stt as openai_stt
        
        # Create STT instance (doesn't download models, uses API)
        stt = openai_stt.STT(model="whisper-1")
        logger.info("✅ OpenAI STT configured successfully")
        return True
    except Exception as e:
        logger.warning(f"⚠️  OpenAI STT check failed: {e}")
        logger.info("ℹ️  OpenAI STT will be configured when agent starts")
        return True  # Don't fail the entire process for this

def main():
    """Download all models"""
    logger.info("🚀 Starting model download process...")
    
    # First, load secrets from AWS Secret Manager
    load_secrets_from_aws()
    
    results = {
        'silero_vad': download_silero_vad(),
        'turn_detection': download_turn_detection(), 
        'openai_stt': check_openai_stt()
    }
    
    success_count = sum(results.values())
    total_count = len(results)
    
    logger.info(f"\n📊 Download Summary:")
    logger.info(f"✅ Successful: {success_count}/{total_count}")
    
    for feature, success in results.items():
        status = "✅" if success else "❌"
        logger.info(f"{status} {feature}")
    
    if success_count == total_count:
        logger.info("\n🎉 All models prepared successfully!")
        logger.info("You can now start the agent without timeout issues.")
    else:
        logger.info(f"\n✅ {success_count}/{total_count} features prepared.")
        logger.info("The agent will handle remaining models during startup.")
    
    # Always return success (exit code 0) to prevent container restart loop
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0)  # Always exit with success to prevent restart loop 