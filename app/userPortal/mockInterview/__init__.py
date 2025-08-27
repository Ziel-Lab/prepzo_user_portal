from flask import Blueprint

mock_interview_bp = Blueprint('mock_interview', url_prefix='/mockInterview')

from . import routes 