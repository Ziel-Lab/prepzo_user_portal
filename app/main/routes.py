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

@main_bp.route('/test-supabase')
def test_supabase_connection():
    """
    A simple, unauthenticated endpoint to test the Supabase connection.
    """
    if not extensions.supabase:
        return jsonify({"error": "Supabase client is not initialized."}), 500

    try:
        # Attempt to read from a public or known table like subscription_plans
        # We select 'id' because we know it exists, which confirms the connection.
        response = extensions.supabase.table('subscription_plans').select('id').limit(1).execute()
        
        # The 'postgrest-py' library might return an object with a 'data' attribute
        # or it might be the data itself if using a different version or configuration.
        # We check for the data attribute first.
        data_to_return = response.data if hasattr(response, 'data') else response

        return jsonify({
            "message": "Successfully connected to Supabase and fetched data.",
            "data": data_to_return
        }), 200

    except Exception as e:
        # Catch any exception and return it, which will give us the exact error.
        return jsonify({
            "error": "Failed to connect to Supabase or fetch data.",
            "details": str(e)
        }), 500
