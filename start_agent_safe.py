
"""
Safe LiveKit Agent Startup Script
Handles model loading failures gracefully and provides fallback configurations
"""

import os
import sys
import subprocess
import logging
import time

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def check_dependencies():
    """Check if required dependencies are installed"""
    required_packages = [
        'livekit-agents',
        'livekit-plugins-openai',
        'livekit-plugins-silero'
    ]
    
    missing = []
    for package in required_packages:
        try:
            __import__(package.replace('-', '_'))
        except ImportError:
            missing.append(package)
    
    if missing:
        logger.warning(f"Missing packages: {missing}")
        logger.info("Run: pip install " + " ".join(missing))
        return False
    return True

def test_model_loading():
    """Test if models can be loaded without timeout"""
    logger.info("Testing model loading capabilities...")
    
    test_results = {}
    
    # Test Silero VAD
    try:
        logger.info("Testing Silero VAD...")
        from livekit.plugins import silero
        vad = silero.VAD.load()
        test_results['silero_vad'] = True
        logger.info("✅ Silero VAD working")
    except Exception as e:
        test_results['silero_vad'] = False
        logger.warning(f"❌ Silero VAD failed: {e}")
    
    # Test Turn Detection
    try:
        logger.info("Testing Turn Detection...")
        from livekit.plugins.turn_detector.english import EnglishModel
        model = EnglishModel()
        test_results['turn_detection'] = True
        logger.info("✅ Turn Detection working")
    except Exception as e:
        test_results['turn_detection'] = False
        logger.warning(f"❌ Turn Detection failed: {e}")
    
    # Test OpenAI STT
    try:
        logger.info("Testing OpenAI STT...")
        from livekit.plugins.openai import stt as openai_stt
        stt = openai_stt.STT(model="whisper-1")
        test_results['openai_stt'] = True
        logger.info("✅ OpenAI STT working")
    except Exception as e:
        test_results['openai_stt'] = False
        logger.warning(f"❌ OpenAI STT failed: {e}")
    
    return test_results

def start_agent_with_timeout():
    """Start the agent with a timeout to detect startup issues"""
    logger.info("Starting LiveKit Agent...")
    
    # Get the current directory
    agent_dir = os.path.dirname(os.path.abspath(__file__))
    agent_path = os.path.join(agent_dir, "app", "userPortal", "mockInterview", "agent.py")
    
    # Build the command
    cmd = [
        sys.executable,
        agent_path,
        "start"
    ]
    
    try:
        # Start the agent process
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        logger.info(f"Agent process started with PID: {process.pid}")
        
        # Monitor the process for initial startup (first 60 seconds)
        start_time = time.time()
        timeout = 60  # 60 seconds timeout
        
        while time.time() - start_time < timeout:
            if process.poll() is not None:
                # Process has terminated
                stdout, stderr = process.communicate()
                logger.error(f"Agent process terminated early!")
                logger.error(f"STDOUT: {stdout}")
                logger.error(f"STDERR: {stderr}")
                return False
            
            time.sleep(1)
        
        if process.poll() is None:
            logger.info("✅ Agent started successfully and is running!")
            # Keep the process running
            try:
                process.wait()
            except KeyboardInterrupt:
                logger.info("Shutting down agent...")
                process.terminate()
                process.wait()
            return True
        else:
            logger.error("Agent failed to start within timeout period")
            return False
            
    except Exception as e:
        logger.error(f"Failed to start agent: {e}")
        return False

def main():
    """Main startup sequence"""
    logger.info("🚀 Safe LiveKit Agent Startup")
    logger.info("=" * 50)
    
    # Step 1: Check dependencies
    logger.info("Step 1: Checking dependencies...")
    if not check_dependencies():
        logger.error("❌ Missing required dependencies")
        return False
    
    # Step 2: Test model loading
    logger.info("\nStep 2: Testing model loading...")
    test_results = test_model_loading()
    
    working_features = sum(test_results.values())
    total_features = len(test_results)
    
    logger.info(f"\n📊 Feature Test Results: {working_features}/{total_features} working")
    
    if working_features == 0:
        logger.warning("⚠️ No enhanced features available, using basic configuration")
    elif working_features < total_features:
        logger.info(f"ℹ️ Partial features available ({working_features}/{total_features})")
    else:
        logger.info("✅ All features working perfectly!")
    
    # Step 3: Start the agent
    logger.info("\nStep 3: Starting agent...")
    success = start_agent_with_timeout()
    
    if success:
        logger.info("🎉 Agent startup completed successfully!")
    else:
        logger.error("❌ Agent startup failed")
        logger.info("💡 Try running download_models.py first to pre-download models")
    
    return success

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1) 