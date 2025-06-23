from flask import jsonify, g, current_app
from app.userPortal.subscription.helpers import require_authentication, get_last_day_of_month
from . import auth_bp
from app import extensions
from datetime import date

@auth_bp.route('/me', methods=['GET'])
@require_authentication
def get_user_profile():
    """
    Returns the profile information of the currently authenticated user.
    This provides a secure way for the frontend to get user details.
    Subscription and usage records are handled by the /subscription/status endpoint.
    """
    user = g.user
    
    # Extract relevant, safe-to-share user information
    profile_data = {
        'id': user.id,
        'email': user.email,
        'full_name': user.user_metadata.get('full_name'),
        'avatar_url': user.user_metadata.get('avatar_url') or user.user_metadata.get('picture'),
    }
    
    return jsonify(profile_data), 200 