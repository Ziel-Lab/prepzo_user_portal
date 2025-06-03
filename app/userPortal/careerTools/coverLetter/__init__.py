from flask import Blueprint

cover_letter_bp = Blueprint("cover_letter", __name__)

from . import routes  
