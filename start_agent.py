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
    
    # Push Flask app context to access AWS secrets
    with app.app_context():
        # Set environment variables from Flask config for the agent
        os.environ['LIVEKIT_URL'] = app.config.get('LIVEKIT_URL', '')
        os.environ['LIVEKIT_API_KEY'] = app.config.get('LIVEKIT_API_KEY', '')
        os.environ['LIVEKIT_API_SECRET'] = app.config.get('LIVEKIT_API_SECRET', '')
        os.environ['OPENAI_API_KEY'] = app.config.get('OPENAI_API_KEY', '')
        
        print("AWS secrets loaded from Secret Manager")
        
        # Handle command line arguments (pass through to agent)
        # For production, ensure we use 'start' mode
        agent_mode = "start"  # Default to production
        if len(sys.argv) > 1 and sys.argv[1] in ['dev', 'console']:
            agent_mode = sys.argv[1]
        
        # Override sys.argv for the agent to pick up the mode
        sys.argv = ['agent.py', agent_mode]
        print(f"Running agent in {agent_mode} mode")
        
        # Now run the agent
        main() 