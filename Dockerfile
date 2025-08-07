FROM python:3.12


WORKDIR /app

COPY requirements.txt .

# Install more complete LaTeX stack (larger, but feature-rich)
RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-latex-base \
    texlive-latex-recommended \
    texlive-fonts-recommended \
    texlive-latex-extra \
    texlive-fonts-extra \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt
# Copy only the download script first
COPY download_models.py .

# Download models during build (requires environment variables at build time)
# Comment out the next line if you prefer runtime download via entrypoint.sh
# RUN python download_models.py

COPY . .


EXPOSE 5000

# Define environment variables (if any)
# ENV FLASK_APP run.py
# ENV FLASK_RUN_HOST 0.0.0.0
# If you are using Flask development server and want it to be accessible
# and run in debug mode, you might set these, but for production, 
# you'd typically use a production-grade WSGI server like Gunicorn.


RUN chmod +x entrypoint.sh
CMD ["./entrypoint.sh"] 
