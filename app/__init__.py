from flask import Flask, request
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

def create_app():
    app = Flask(__name__)

    secret_name = "userPortal"
    region_name = "us-east-1"
    secrets = get_secret(secret_name, region_name)
    if secrets:
        for key, value in secrets.items():
            app.config[key] = value
    
    # Centralized CORS Configuration
    CORS(app,
         origins=["https://prepzo-client-git-dev-prepzo.vercel.app"],
         supports_credentials=True,
         allow_headers=["Content-Type", "Authorization"],
         methods=["GET", "POST", "OPTIONS", "PUT", "PATCH", "DELETE"])

    # Logging setup
    app.logger.handlers.clear()
    app.logger.setLevel(logging.INFO) 
    stream_handler = logging.StreamHandler(sys.stdout) 
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(formatter)
    app.logger.addHandler(stream_handler)

    init_supabase(app)  # Initializes Supabase client with app config

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
        return response

    app.register_blueprint(main_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(resume_analyze_bp)
    app.register_blueprint(cover_letter_bp)
    app.register_blueprint(linkedin_optimizer_bp)
    app.register_blueprint(subscription_bp)
    app.register_blueprint(auth_bp)
    return app