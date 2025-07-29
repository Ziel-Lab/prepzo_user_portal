#!/usr/bin/env python3
"""
Pre-download VAD and Turn Detection models for LiveKit Agent
Run this script once to download models before starting the agent
"""

import os
import sys
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
        logger.info("Downloading English turn detection model...")
        from livekit.plugins.turn_detector.english import EnglishModel
        
        # Load the model (this will download it)
        model = EnglishModel()
        logger.info("✅ English turn detection model downloaded successfully")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to download turn detection model: {e}")
        return False

def check_openai_stt():
    """Check OpenAI STT configuration"""
    try:
        logger.info("Checking OpenAI STT configuration...")
        from livekit.plugins.openai import stt as openai_stt
        
        # Create STT instance (doesn't download models, uses API)
        stt = openai_stt.STT(model="whisper-1")
        logger.info("✅ OpenAI STT configured successfully")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to configure OpenAI STT: {e}")
        return False

def main():
    """Download all models"""
    logger.info("🚀 Starting model download process...")
    
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
        logger.info("\n🎉 All models downloaded successfully!")
        logger.info("You can now start the agent without timeout issues.")
    else:
        logger.warning(f"\n⚠️  {total_count - success_count} features failed to download.")
        logger.info("The agent will still work but may use basic conversation features.")
    
    return success_count == total_count

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1) 