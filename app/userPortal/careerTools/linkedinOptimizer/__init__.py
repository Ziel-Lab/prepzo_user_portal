from flask import Blueprint

linkedin_optimizer_bp = Blueprint("linkedin_optimizer", __name__)

from . import routes  