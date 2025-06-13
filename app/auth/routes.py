from flask import jsonify, g
from app.userPortal.subscription.helpers import require_authentication
from . import auth_bp

@auth_bp.route('/me', methods=['GET'])
@require_authentication
def get_user_profile():
    """
    Returns the profile information of the currently authenticated user.
    This provides a secure way for the frontend to get user details
    without directly querying the database.
    """
    user = g.user
    
    # Extract relevant, safe-to-share user information
    profile_data = {
        'id': user.id,
        'email': user.email,
        'full_name': user.user_metadata.get('full_name'),
        'avatar_url': user.user_metadata.get('avatar_url'),
    }
    
    return jsonify(profile_data), 200 