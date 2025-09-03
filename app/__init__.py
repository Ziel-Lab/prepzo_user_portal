# app/__init__.py  (or whichever module you use as the app factory)
from flask import Flask, request, jsonify, make_response
import logging
import sys
from dotenv import load_dotenv
from .main import main_bp
from .userPortal.documents import upload_bp
from .userPortal.careerTools.resumeAnalyze import resume_analyze_bp
from .userPortal.careerTools.coverLetter import cover_letter_bp
from .userPortal.careerTools.linkedinOptimizer import linkedin_optimizer_bp
from .userPortal.subscription import subscription_bp
from .userPortal.mockInterview import mock_interview_bp
from .auth import auth_bp
from .extensions import init_supabase, init_livekit
from .secrets import get_secret
import re
from .userPortal.applications.jobListing import job_listing_bp
from .userPortal.profile import profile_bp

# Load local .env (optional)
load_dotenv()

def create_app():
    app = Flask(__name__)

    # Reject extremely large request bodies early (e.g. huge file uploads)
    app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 MB

    # Load secrets from AWS Secrets Manager
    secret_name = "userPortal-dev"
    region_name = "us-east-1"
    secrets = get_secret(secret_name, region_name)
    if secrets:
        for key, value in secrets.items():
            app.config[key] = value
        app.logger.info("Configuration loaded from AWS Secrets Manager")
    else:
        app.logger.error("FATAL: Could not load configuration from AWS Secrets Manager")

    # Logging setup
    app.logger.handlers.clear()
    app.logger.setLevel(logging.DEBUG)
    stream_handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(formatter)
    app.logger.addHandler(stream_handler)

    # Initialize external clients
    init_supabase(app)
    init_livekit(app)

    # Validate Supabase hook configuration from AWS Secrets Manager
    from .auth.routes import validate_hook_configuration
    if not validate_hook_configuration(app):
        app.logger.error(
            "FATAL: Supabase hook configuration validation failed - app may not start properly"
        )
        # Optionally raise here in production:
        # raise RuntimeError("Invalid Supabase hook configuration")

    # Compile allowed origin regex patterns once
    allowed_origins_patterns = [
        r"https://prepzo-client-.*\.vercel\.app",
        r"https://prepzo-client-git-dev-prepzo\.vercel\.app",
        r"http://localhost:\d+",
        r"http://127\.0\.0\.1:\d+",
        r"https://.*\.ngrok-free\.app",
        r"https://prepzo\.ai",
        r"https://www\.prepzo\.ai",
        r"https://dashboard\.prepzo\.ai",
    ]
    # Precompile for slightly faster matching and to catch bad patterns early
    compiled_origin_regex = []
    for p in allowed_origins_patterns:
        try:
            compiled_origin_regex.append(re.compile(p))
        except re.error as e:
            app.logger.warning(f"Invalid CORS regex pattern '{p}': {e}")

    # --- Unified before_request: OPTIONS bypass + request logging ---
    @app.before_request
    def before_request_handler():
        origin = request.headers.get("Origin")
        app.logger.debug(f"[CORS DEBUG] Incoming request: {request.method} {request.path} | Origin: {origin}")

        # If preflight OPTIONS, short-circuit here so auth decorators don't reject it.
        if request.method == 'OPTIONS':
            # Return an empty response here; it will still pass through after_request to attach CORS headers.
            return make_response('', 200)

        # Logging body info for non-OPTIONS requests (safe, lightweight)
        try:
            if request.data and request.content_type != 'multipart/form-data':
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

    # --- Unified after_request: apply CORS headers + logging ---
    @app.after_request
    def after_request_func(response):
        """
        Attach CORS headers for allowed origins matched by regex.
        Also set Vary: Origin and log what matched.
        """
        origin = request.headers.get('Origin')
        matched = None

        if origin:
            for cre in compiled_origin_regex:
                try:
                    if cre.fullmatch(origin):
                        response.headers['Access-Control-Allow-Origin'] = origin
                        matched = cre.pattern
                        break
                except Exception as e:
                    # This should not happen for compiled patterns, but be defensive.
                    app.logger.warning(f"[CORS DEBUG] pattern match error for {cre.pattern}: {e}")

        # Required for credentialed requests
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Allow-Headers'] = (
            'Content-Type, Authorization, X-Requested-With, ngrok-skip-browser-warning'
        )
        response.headers['Access-Control-Allow-Methods'] = (
            'GET, POST, OPTIONS, PUT, PATCH, DELETE'
        )

        # Indicate response varies by Origin (important for caches)
        response.headers['Vary'] = 'Origin'

        app.logger.debug(
            f"[CORS DEBUG] Origin={origin} matched_pattern={matched} | Response status={response.status_code}"
        )

        # If we somehow return earlier for OPTIONS, ensure it's an empty OK with headers.
        if request.method == 'OPTIONS':
            # make_response will ensure the headers we set are kept.
            return make_response(('', 200, dict(response.headers)))

        app.logger.info(f"Outgoing Response: {request.method} {request.path} - Status {response.status_code}")
        return response

    # --- Register blueprints ---
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(subscription_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(resume_analyze_bp)
    app.register_blueprint(cover_letter_bp)
    app.register_blueprint(linkedin_optimizer_bp)
    app.register_blueprint(job_listing_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(mock_interview_bp, url_prefix='/mockInterview')

    # Fallback route for auth hook without /auth prefix (compatibility)
    @app.route('/custom-access-token-hook', methods=['POST'])
    def custom_access_token_hook_fallback():
        """
        Fallback route for custom access token hook without /auth prefix
        Routes to the auth blueprint implementation
        """
        from .auth.routes import custom_access_token_hook
        return custom_access_token_hook()

    return app
