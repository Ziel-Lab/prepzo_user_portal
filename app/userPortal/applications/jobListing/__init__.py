from flask import Blueprint

job_listing_bp = Blueprint("job_listing", __name__)

from . import routes  # noqa: E402 