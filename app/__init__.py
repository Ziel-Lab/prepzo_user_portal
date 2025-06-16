from flask import Flask, request, jsonify
from flask_cors import CORS
import logging
import sys
from .main import main_bp
from .userPortal.documents import upload_bp
from .userPortal.careerTools.resumeAnalyze import resume_analyze_bp
from .userPortal.careerTools.coverLetter import cover_letter_bp
from .userPortal.careerTools.linkedinOptimizer import linkedin_optimizer_bp
from .userPortal.subscription import subscription_bp
from .auth import auth_bp
from .extensions import init_supabase
from .secrets import get_secret
import re

def create_app():
    app = Flask(__name__)

    secret_name = "userPortal"
    region_name = "us-east-1"
    secrets = get_secret(secret_name, region_name)
    if secrets:
        for key, value in secrets.items():
            app.config[key] = value
    
     # Centralized CORS Configuration - Now handled by custom middleware
    CORS(app,
         supports_credentials=True)

    # Logging setup
    app.logger.handlers.clear()
    app.logger.setLevel(logging.INFO) 
    stream_handler = logging.StreamHandler(sys.stdout) 
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(formatter)
    app.logger.addHandler(stream_handler)

    init_supabase(app)  # Initializes Supabase client with app config

    @app.after_request
    def after_request_func(response):
        # Allow requests from all Vercel preview deployments, localhost, and custom domains
        allowed_origins_regex = [
            r"https://prepzo-client-.*\.vercel\.app",
            r"http://localhost:.*",
            "https://prepzo.ai",
            "https://www.prepzo.ai",
            "https://dashboard.prepzo.ai",
        ]
        
        origin = request.headers.get('Origin')
        if origin:
            for pattern in allowed_origins_regex:
                if re.fullmatch(pattern, origin):
                    response.headers['Access-Control-Allow-Origin'] = origin
                    break
        
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS, PUT, PATCH, DELETE'
        
        # Handle preflight (OPTIONS) requests
        if request.method == 'OPTIONS':
            response.status_code = 200 # OK
        
        return response

    @app.before_request
    def log_request_info():
        app.logger.info(f"Incoming Request: {request.method} {request.path}")
        if request.data and request.content_type != 'multipart/form-data': 
            try:
                if request.content_type == 'application/json':
                    body_preview = str(request.get_json(silent=True) or request.data.decode('utf-8', errors='replace'))[:500]
                    app.logger.info(f"Request Body (JSON): {body_preview}")
                elif request.form:
                     app.logger.info(f"Request Form Data: {request.form}")
                else:
                    body_preview = str(request.data[:500])
                    app.logger.info(f"Request Body (Preview): {body_preview}")
            except Exception as e:
                app.logger.warning(f"Could not parse/log request body: {e}")

    @app.after_request
    def log_response_info(response):
        app.logger.info(f"Outgoing Response: {request.method} {request.path} - Status {response.status_code}")
        # The custom @app.after_request handles CORS headers now, so we can remove the log here.
        return response

    app.register_blueprint(main_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(resume_analyze_bp)
    app.register_blueprint(cover_letter_bp)
    app.register_blueprint(linkedin_optimizer_bp)
    app.register_blueprint(subscription_bp)
    app.register_blueprint(auth_bp)
    return app