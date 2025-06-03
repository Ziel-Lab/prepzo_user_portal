# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
# Assumes requirements.txt is in the same directory as the Dockerfile
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container at /app
COPY . .

# Make port 5000 available to the world outside this container
# Change this if your application runs on a different port
EXPOSE 5000

# Define environment variables (if any)
# ENV FLASK_APP run.py
# ENV FLASK_RUN_HOST 0.0.0.0
# If you are using Flask development server and want it to be accessible
# and run in debug mode, you might set these, but for production, 
# you'd typically use a production-grade WSGI server like Gunicorn.

# Command to run the application
# Replace with your actual run command if different
# For a production setup, you might use Gunicorn, e.g.:
# CMD ["gunicorn", "--bind", "0.0.0.0:5000", "your_wsgi_application_module:app_instance_name"]
# For a simple development server run via run.py:
CMD ["python", "run.py"] 