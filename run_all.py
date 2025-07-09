#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Run Both Services - Flask App and LiveKit Agent
This script starts both the web application and the LiveKit agent together
Usage: python run_all.py
"""

import os
import sys
import time
import signal
import subprocess
import multiprocessing
from threading import Thread
import atexit

# Fix Windows console encoding for Unicode characters
if sys.platform == "win32":
    try:
        # Set console to UTF-8 mode
        os.system('chcp 65001 >nul 2>&1')
    except:
        pass

# Ensure we're in the right directory
if not os.path.exists('run.py') or not os.path.exists('start_agent.py'):
    print("❌ Error: Please run this script from the project root directory")
    print("   Usage: python run_all.py")
    sys.exit(1)

# Global process references for cleanup
flask_process = None
agent_process = None

def signal_handler(signum, frame):
    """Handle Ctrl+C and other signals"""
    print("\n🛑 Shutting down services...")
    cleanup_processes()
    sys.exit(0)

def cleanup_processes():
    """Clean up all processes"""
    global flask_process, agent_process
    
    if flask_process and flask_process.poll() is None:
        print("🔄 Stopping Flask app...")
        flask_process.terminate()
        try:
            flask_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            flask_process.kill()
    
    if agent_process and agent_process.poll() is None:
        print("🔄 Stopping LiveKit agent...")
        agent_process.terminate()
        try:
            agent_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent_process.kill()
    
    print("✅ All processes stopped")

def run_flask_app():
    """Run the Flask application"""
    global flask_process
    
    print("Starting Flask Web Application...")
    sys.stdout.flush()
    try:
        flask_process = subprocess.Popen(
            [sys.executable, "run.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=0  # Unbuffered
        )
        
        # Stream Flask output
        for line in iter(flask_process.stdout.readline, ''):
            if line.strip():
                print(f"[FLASK] {line.strip()}")
                sys.stdout.flush()
        
    except Exception as e:
        print(f"Error running Flask app: {e}")
        sys.stdout.flush()
        return False
    
    return True

def run_livekit_agent():
    """Run the LiveKit agent"""
    global agent_process
    
    # Wait a bit for Flask to start first
    print("Waiting 5 seconds for Flask to fully start...")
    sys.stdout.flush()
    time.sleep(5)
    
    print("Starting LiveKit Agent...")
    sys.stdout.flush()
    try:
        # Get command line arguments (dev, start, etc.)
        agent_mode = "start"  # Default to production mode
        if len(sys.argv) > 1:
            agent_mode = sys.argv[1]
        
        print(f"Agent mode: {agent_mode}")
        sys.stdout.flush()
        
        agent_process = subprocess.Popen(
            [sys.executable, "start_agent.py", agent_mode],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=0  # Unbuffered
        )
        
        # Stream Agent output
        for line in iter(agent_process.stdout.readline, ''):
            if line.strip():
                print(f"[AGENT] {line.strip()}")
                sys.stdout.flush()
        
    except Exception as e:
        print(f"Error running LiveKit agent: {e}")
        sys.stdout.flush()
        return False
    
    return True

def monitor_processes():
    """Monitor both processes and restart if needed"""
    global flask_process, agent_process
    
    while True:
        time.sleep(10)  # Check every 10 seconds
        
        # Check Flask process
        if flask_process and flask_process.poll() is not None:
            print("⚠️ Flask process stopped unexpectedly")
            # Optionally restart or exit
            break
            
        # Check Agent process
        if agent_process and agent_process.poll() is not None:
            print("⚠️ Agent process stopped unexpectedly")
            # Optionally restart or exit
            break

def main():
    """Main function to orchestrate both services"""
    print("Prepzo User Portal - Starting All Services")
    print("=" * 50)
    print("Web App: http://localhost:5000")
    print("LiveKit Agent: Running in background")
    print("=" * 50)
    print("Press Ctrl+C to stop all services")
    print()
    sys.stdout.flush()  # Force output to show immediately
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    atexit.register(cleanup_processes)
    
    # Start both services in separate threads
    flask_thread = Thread(target=run_flask_app, name="FlaskThread", daemon=True)
    agent_thread = Thread(target=run_livekit_agent, name="AgentThread", daemon=True)
    
    try:
        # Start Flask first
        print("Starting Flask thread...")
        sys.stdout.flush()
        flask_thread.start()
        print("Flask app thread started")
        sys.stdout.flush()
        
        # Wait a moment, then start agent
        print("Waiting 2 seconds before starting agent...")
        sys.stdout.flush()
        time.sleep(2)
        print("Starting agent thread...")
        sys.stdout.flush()
        agent_thread.start()
        print("LiveKit agent thread started")
        sys.stdout.flush()
        
        # Monitor processes
        monitor_thread = Thread(target=monitor_processes, name="MonitorThread", daemon=True)
        monitor_thread.start()
        
        # Keep main thread alive
        while True:
            if not flask_thread.is_alive() and not agent_thread.is_alive():
                print("⚠️ Both services have stopped")
                break
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n🛑 Received interrupt signal")
    except Exception as e:
        print(f"❌ Error in main loop: {e}")
    finally:
        cleanup_processes()

if __name__ == "__main__":
    main() 