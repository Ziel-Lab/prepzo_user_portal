from flask import Flask, request
from flask_cors import CORS
import logging
import sys
import os
from dotenv import load_dotenv
from .main import main_bp
from .userPortal.documents import upload_bp
from .userPortal.careerTools.resumeAnalyze import resume_analyze_bp
from .userPortal.careerTools.coverLetter import cover_letter_bp
from .userPortal.careerTools.linkedinOptimizer import linkedin_optimizer_bp
from .userPortal.subscription import subscription_bp
from .extensions import init_supabase

load_dotenv()

def create_app():
    app = Flask(__name__)

    # Load config from .env
    app.config['SUPABASE_URL'] = os.getenv('SUPABASE_URL')
    app.config['SUPABASE_SERVICE_ROLE_KEY'] = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
    app.config['STRIPE_SECRET_API_KEY'] = os.getenv('STRIPE_SECRET_API_KEY')
    app.config['STRIPE_WEBHOOK_SECRET'] = os.getenv('STRIPE_WEBHOOK_SECRET')
    app.config['STRIPE_PAID_PLAN_PRICE_ID'] = os.getenv('STRIPE_PAID_PLAN_PRICE_ID')
    app.config['FRONTEND_URL'] = os.getenv('FRONTEND_URL')
    app.config['SUPABASE_JWT_SECRET'] = os.getenv('SUPABASE_JWT_SECRET')

    # Xano API URLs
    app.config['XANO_API_URL_RESUME_ANALYZE'] = os.getenv('XANO_API_URL_RESUME_ANALYZE')
    app.config['XANO_API_URL_RESUME_ROAST'] = os.getenv('XANO_API_URL_RESUME_ROAST')
    app.config['XANO_API_URL_COVER_LETTER'] = os.getenv('XANO_API_URL_COVER_LETTER')
    app.config['XANO_API_URL_LINKEDIN_OPTIMIZER'] = os.getenv('XANO_API_URL_LINKEDIN_OPTIMIZER')

    # Centralized CORS Configuration
    # This will handle CORS for all routes in the application.
    # It allows credentials and restricts origins to your FRONTEND_URL in production.
    cors_config = {
        "supports_credentials": True,
        "allow_headers": ["Content-Type", "Authorization"],
        "expose_headers": ["Content-Type", "Authorization"]
    }
    if app.config.get('FRONTEND_URL'):
        CORS(app, resources={r"/*": {"origins": app.config['FRONTEND_URL']}}, **cors_config)
    else:
        # Fallback for local development if FRONTEND_URL is not set
        # Using a wildcard for headers in local dev can help with debugging.
        CORS(app, resources={r"/*": {"origins": "*"}}, allow_headers="*", supports_credentials=True)

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
    return app