from flask import Blueprint

mock_interview_bp = Blueprint('mock_interview', __name__)

from . import routes 