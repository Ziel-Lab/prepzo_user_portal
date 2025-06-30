from flask import Blueprint

subscription_bp = Blueprint("subscription", __name__, url_prefix="/subscription")

from . import routes  
