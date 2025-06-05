FROM python:3.12


WORKDIR /app

COPY requirements.txt .


RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# WARNING: The following lines make AWS credentials available as environment variables inside the container.
# This is generally not recommended for production. For AWS environments like EC2, it's more secure to use
# IAM roles assigned to the instance, which the AWS SDK can use automatically without needing explicit credentials.
# These ARGs are for passing credentials during the build process, which bakes them into the image.
ARG AWS_ACCESS_KEY_ID
ARG AWS_SECRET_ACCESS_KEY
ARG AWS_REGION

ENV AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}
ENV AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}
ENV AWS_REGION=${AWS_REGION}

EXPOSE 5000

# Define environment variables (if any)
# ENV FLASK_APP run.py
# ENV FLASK_RUN_HOST 0.0.0.0
# If you are using Flask development server and want it to be accessible
# and run in debug mode, you might set these, but for production, 
# you'd typically use a production-grade WSGI server like Gunicorn.

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "run:app"] 