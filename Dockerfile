FROM python:3.12


WORKDIR /app

COPY requirements.txt .


RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# Define environment variables (if any)
# ENV FLASK_APP run.py
# ENV FLASK_RUN_HOST 0.0.0.0
# If you are using Flask development server and want it to be accessible
# and run in debug mode, you might set these, but for production, 
# you'd typically use a production-grade WSGI server like Gunicorn.

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "run:app"] 