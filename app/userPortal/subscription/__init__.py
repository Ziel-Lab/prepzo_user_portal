from flask import Blueprint

subscription_bp = Blueprint('subscription', __name__, url_prefix='/subscription')

# Import routes to register them with the blueprint
from . import routes  # noqa: F401, E402
