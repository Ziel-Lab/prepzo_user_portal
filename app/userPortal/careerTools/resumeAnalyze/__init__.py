from flask import Blueprint

resume_analyze_bp = Blueprint("resume_analyze", __name__)

from . import routes 
