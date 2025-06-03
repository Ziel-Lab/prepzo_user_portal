from flask import Flask
from .main import main_bp
from .userPortal.documents import upload_bp
from .userPortal.careerTools.resumeAnalyze import resume_analyze_bp
from .userPortal.careerTools.coverLetter import cover_letter_bp
from .userPortal.careerTools.linkedinOptimizer import linkedin_optimizer_bp
from .extensions import init_supabase

def create_app():
    app = Flask(__name__)

    init_supabase()  # Initializes Supabase client

    app.register_blueprint(main_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(resume_analyze_bp)
    app.register_blueprint(cover_letter_bp)
    app.register_blueprint(linkedin_optimizer_bp)
    return app
