#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Wrapper script to start the mock interview agent with proper Flask context
Run this from the project root: python start_agent.py
"""

import os
import sys

# Fix Windows console encoding for Unicode characters
if sys.platform == "win32":
    try:
        # Try to set UTF-8 encoding for Windows console
        import codecs
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.detach())
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.detach())
    except:
        # If that fails, just continue without UTF-8 (emojis might not display)
        pass

# Ensure we're in the right directory
if not os.path.exists('run.py'):
    print("Error: Please run this script from the project root directory")
    print("   Usage: python start_agent.py")
    sys.exit(1)

# Import the Flask app to get AWS secrets
from run import app

# Import the agent
from app.userPortal.mockInterview.agent import main

if __name__ == "__main__":
    print("Starting Mock Interview Agent with Flask context...")
    
    # Load AWS secrets from Flask config FIRST
    with app.app_context():
        livekit_url = app.config.get('LIVEKIT_URL', '')
        livekit_api_key = app.config.get('LIVEKIT_API_KEY', '')
        livekit_api_secret = app.config.get('LIVEKIT_API_SECRET', '')
        openai_api_key = app.config.get('OPENAI_API_KEY', '')
        
        print("AWS secrets loaded from Secret Manager")
        print(f"LIVEKIT_URL: {livekit_url[:50]}..." if livekit_url else "LIVEKIT_URL: NOT SET")
    
    # NOW set environment variables OUTSIDE the Flask context (so they persist)
    os.environ['LIVEKIT_URL'] = livekit_url
    os.environ['LIVEKIT_API_KEY'] = livekit_api_key
    os.environ['LIVEKIT_API_SECRET'] = livekit_api_secret
    os.environ['OPENAI_API_KEY'] = openai_api_key
    
    # Verify they're set
    print(f"Environment variable LIVEKIT_URL is now: {os.environ.get('LIVEKIT_URL', 'NOT SET')[:50]}...")
    print(f"Environment variable LIVEKIT_API_KEY is now: {'SET' if os.environ.get('LIVEKIT_API_KEY') else 'NOT SET'}")
    print(f"Environment variable LIVEKIT_API_SECRET is now: {'SET' if os.environ.get('LIVEKIT_API_SECRET') else 'NOT SET'}")
    print(f"Environment variable OPENAI_API_KEY is now: {'SET' if os.environ.get('OPENAI_API_KEY') else 'NOT SET'}")
    
    # Handle command line arguments (pass through to agent)
    # For production, ensure we use 'start' mode
    agent_mode = "start"  # Default to production
    if len(sys.argv) > 1 and sys.argv[1] in ['dev', 'console']:
        agent_mode = sys.argv[1]
    
    # Override sys.argv for the agent to pick up the mode
    sys.argv = ['agent.py', agent_mode]
    print(f"Running agent in {agent_mode} mode")
    
    # Now run the agent - environment variables are set and will persist
    main() 