#!/bin/bash
set -e

# Start Gunicorn for Flask app in the background
gunicorn --bind 0.0.0.0:5000 --timeout 120 --access-logfile - --error-logfile - run:app &
GUNICORN_PID=$!

# Wait a few seconds to ensure Flask app is up
sleep 5

# Start the LiveKit agent in the background
python start_agent.py start &
AGENT_PID=$!

# Handle shutdown
trap 'echo "Stopping..."; kill $GUNICORN_PID $AGENT_PID; wait $GUNICORN_PID $AGENT_PID' SIGTERM SIGINT

# Wait for both processes
wait $GUNICORN_PID $AGENT_PID