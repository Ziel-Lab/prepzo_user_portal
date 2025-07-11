from flask import Blueprint, jsonify, g
from app import extensions # To access extensions.supabase
from app.userPortal.subscription.helpers import require_authentication

main_bp = Blueprint("main", __name__)

@main_bp.route("/health")
def health():
    return jsonify({"status": "ok", "app": "prepzo-user-portal is healthy"})

@main_bp.route("/")
def index():
    return jsonify({"message": "Prepzo User Portal API"})

@main_bp.route('/api/check-auth', methods=['GET', 'OPTIONS'])
@require_authentication  
def check_auth_status():
    """
    Checks if the user is authenticated and returns basic authentication status.
    This endpoint is expected by the frontend for authentication verification.
    """
    user = g.user
    
    # Return basic authentication status and user info
    auth_data = {
        'authenticated': True,
        'user': {
            'id': user.id,
            'email': user.email,
            'full_name': user.user_metadata.get('full_name'),
            'avatar_url': user.user_metadata.get('avatar_url') or user.user_metadata.get('picture'),
        }
    }
    
    return jsonify(auth_data), 200

# REMOVED: Unprotected /test-supabase endpoint - was a critical security vulnerability
# Database connectivity should be tested through internal monitoring, not public endpoints
