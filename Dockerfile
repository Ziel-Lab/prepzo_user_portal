FROM python:3.12


WORKDIR /app

COPY requirements.txt .


RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get update && \
    apt-get install -y wget && \
    wget https://github.com/jgm/pandoc/releases/download/3.2/pandoc-3.2-1-amd64.deb && \
    dpkg -i pandoc-3.2-1-amd64.deb && \
    rm pandoc-3.2-1-amd64.deb

RUN apt-get update && \
    apt-get install -y texlive-latex-base texlive-fonts-recommended texlive-fonts-extra texlive-latex-extra && \
    apt-get clean

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
