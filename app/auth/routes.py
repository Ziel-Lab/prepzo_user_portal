from flask import jsonify, g, current_app, request
from app.userPortal.subscription.helpers import require_authentication
from . import auth_bp
from app import extensions
from datetime import date
import os
import hmac
import hashlib
import json

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

@auth_bp.route('/token-info', methods=['GET'])
@require_authentication
def get_token_info():
    """
    Returns information about the current JWT token and custom claims.
    Useful for debugging and understanding token refresh behavior.
    """
    user = g.user
    user_claims = getattr(g, 'user_claims', {})
    
    token_info = {
        'user_id': user.id,
        'email': user.email,
        'token_version': user_claims.get('token_version', 'legacy'),
        'subscription_plan_id': user_claims.get('subscription_plan_id'),
        'display_name': user_claims.get('display_name'),
        'issued_at': user_claims.get('issued_at'),
        'refresh_enabled': user_claims.get('refresh_enabled', False),
        'has_custom_claims': len(user_claims) > 0,
        'custom_claims_count': len(user_claims)
    }
    
    return jsonify({
        'token_info': token_info,
        'message': 'Token information retrieved successfully'
    }), 200

@auth_bp.route('/custom-access-token-hook', methods=['POST'])
def custom_access_token_hook():
    """
    HTTP endpoint for Custom Access Token Hook
    Called by Supabase Auth every time a new JWT is created
    """
    try:
        # Verify webhook signature
        signature = request.headers.get('webhook-signature')
        if not signature:
            current_app.logger.warning("Missing webhook signature in custom access token hook")
            return jsonify({"error": "Missing signature"}), 400
        
        # Get the webhook secret from app config (loaded from AWS Secrets Manager or .env)
        webhook_secret = current_app.config.get('SUPABASE_HOOK_SECRET')
        if not webhook_secret:
            current_app.logger.error("SUPABASE_HOOK_SECRET not configured")
            return jsonify({"error": "Server configuration error"}), 500
        
        # Extract secret from format: v1,whsec_xxxx
        if ',' in webhook_secret:
            version, secret = webhook_secret.split(',', 1)
            if secret.startswith('whsec_'):
                secret = secret[6:]  # Remove whsec_ prefix
        else:
            secret = webhook_secret
        
        # Verify signature
        payload = request.get_data()
        expected_signature = hmac.new(
            secret.encode('utf-8'),
            payload,
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(signature.split('=')[1], expected_signature):
            current_app.logger.warning("Invalid webhook signature in custom access token hook")
            return jsonify({"error": "Invalid signature"}), 401
        
        # Parse the event
        event = request.get_json()
        if not event:
            return jsonify({"error": "Invalid JSON payload"}), 400
        
        # Extract user information
        user = event.get('user', {})
        user_id = user.get('id')
        user_email = user.get('email')
        user_metadata = user.get('user_metadata', {})
        
        if not user_id:
            return jsonify({"error": "Missing user ID"}), 400
        
        # Initialize custom claims
        custom_claims = {
            'user_id': user_id,
            'email': user_email,
            'token_version': 'v2.0',
            'issued_at': int(request.timestamp) if hasattr(request, 'timestamp') else None,
            'refresh_enabled': True
        }
        
        # Add display name
        display_name = (
            user_metadata.get('full_name') or 
            user_metadata.get('name') or 
            user_email or 
            'User'
        )
        custom_claims['display_name'] = display_name
        
        # Get subscription plan (with error handling)
        try:
            admin_client = extensions.get_admin_client()
            sub_result = admin_client.table('user_subscriptions').select('plan_id').eq('user_id', user_id).limit(1).execute()
            
            if sub_result.data:
                custom_claims['subscription_plan_id'] = sub_result.data[0]['plan_id']
            else:
                custom_claims['subscription_plan_id'] = 1  # Default to Free plan
        except Exception as e:
            current_app.logger.warning(f"Could not fetch subscription for user {user_id[:8]}***: {e}")
            custom_claims['subscription_plan_id'] = 1  # Default to Free plan
        
        # Merge custom claims into existing claims
        existing_claims = event.get('claims', {})
        existing_claims.update(custom_claims)
        
        # Update the event
        event['claims'] = existing_claims
        
        current_app.logger.info(f"Custom Access Token Hook executed for user {user_id[:8]}*** (HTTP)")
        
        return jsonify(event), 200
        
    except json.JSONDecodeError:
        current_app.logger.error("Invalid JSON in custom access token hook")
        return jsonify({"error": "Invalid JSON"}), 400
    except Exception as e:
        current_app.logger.error(f"Custom Access Token Hook error: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500