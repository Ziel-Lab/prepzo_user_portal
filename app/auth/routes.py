from flask import jsonify, g, current_app, request
from app.userPortal.subscription.helpers import require_authentication
from . import auth_bp
from app import extensions
from datetime import datetime, timezone
import os
import hmac
import hashlib
import json
import base64
import time
from collections import defaultdict, deque

# Simple in-memory rate limiter for hook endpoint (replace with Redis in production)
_hook_rate_limiter = defaultdict(lambda: deque())

def validate_hook_configuration(app):
    """
    Validate that the Supabase hook secret is properly configured from AWS Secrets Manager.
    Should be called during app initialization.
    """
    webhook_secret = app.config.get('SUPABASE_HOOK_SECRET')
    if not webhook_secret:
        app.logger.error("CRITICAL: SUPABASE_HOOK_SECRET not found in app.config - check AWS Secrets Manager configuration")
        return False
    
    if not webhook_secret.startswith('v1,whsec_'):
        app.logger.error(f"CRITICAL: SUPABASE_HOOK_SECRET has invalid format - expected 'v1,whsec_<base64>' but got: {webhook_secret[:10]}***")
        return False
    
    try:
        version, secret_part = webhook_secret.split(',', 1)
        base64.b64decode(secret_part[6:])  # Test decode
        app.logger.info("[OK] SUPABASE_HOOK_SECRET successfully loaded from AWS Secrets Manager and validated")
        return True
    except Exception as e:
        app.logger.error(f"CRITICAL: Failed to decode SUPABASE_HOOK_SECRET from AWS Secrets Manager: {e}")
        return False

def _check_hook_rate_limit(identifier, max_requests=30, window_seconds=60):
    """
    Simple rate limiter for webhook endpoints
    Returns True if rate limit exceeded, False otherwise
    """
    now = time.time()
    requests = _hook_rate_limiter[identifier]
    
    # Remove old requests outside the window
    while requests and requests[0] <= now - window_seconds:
        requests.popleft()
    
    # Check if limit exceeded
    if len(requests) >= max_requests:
        return True
    
    # Add current request
    requests.append(now)
    return False

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

@auth_bp.route('/realtime-token', methods=['POST'])
@require_authentication  
def get_realtime_token():
    """Get a token for real-time subscriptions (if needed for authenticated channels)"""
    try:
        # For now, return the user's JWT token for authenticated real-time channels
        # In production, you might want to generate a specific real-time token
        
        return jsonify({
            'token': request.headers.get('Authorization', '').replace('Bearer ', ''),
            'user_id': g.user.id,
            'expires_in': 3600  # 1 hour
        }), 200
        
    except Exception as e:
        current_app.logger.error(f"Error generating realtime token: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@auth_bp.route('/custom-access-token-hook', methods=['POST'])
def custom_access_token_hook():
    """
    Production-ready HTTP endpoint for Custom Access Token Hook
    Called by Supabase Auth every time a new JWT is created
    Follows Standard Webhooks specification for security
    """
    start_time = time.time()
    
    # Rate limiting check (use IP + User-Agent for identifier)
    client_ip = request.remote_addr or 'unknown'
    user_agent = request.headers.get('User-Agent', 'unknown')[:100]  # Limit UA length
    rate_limit_key = f"{client_ip}:{hash(user_agent)}"
    
    if _check_hook_rate_limit(rate_limit_key, max_requests=50, window_seconds=60):
        current_app.logger.warning(f"Rate limit exceeded for hook endpoint from {client_ip}")
        response = jsonify({"error": "Rate limit exceeded"})
        response.headers['Content-Type'] = 'application/json'
        response.headers['Retry-After'] = '60'
        return response, 429
    
    try:
        # Validate Content-Type
        if request.content_type != 'application/json':
            current_app.logger.warning(f"Invalid Content-Type: {request.content_type}")
            return jsonify({"error": "Content-Type must be application/json"}), 400
        
        # Extract Standard Webhooks headers
        webhook_id = request.headers.get('webhook-id')
        webhook_timestamp = request.headers.get('webhook-timestamp')
        webhook_signature = request.headers.get('webhook-signature')
        
        # Validate required headers
        if not all([webhook_id, webhook_timestamp, webhook_signature]):
            missing = [h for h, v in [('webhook-id', webhook_id), ('webhook-timestamp', webhook_timestamp), ('webhook-signature', webhook_signature)] if not v]
            current_app.logger.warning(f"Missing webhook headers: {missing}")
            return jsonify({"error": f"Missing required headers: {missing}"}), 400
        
        # Validate timestamp (prevent replay attacks)
        try:
            timestamp = int(webhook_timestamp)
            current_timestamp = int(time.time())
            # Allow 5 minutes tolerance
            if abs(current_timestamp - timestamp) > 300:
                current_app.logger.warning(f"Webhook timestamp too old or future: {timestamp} vs {current_timestamp}")
                return jsonify({"error": "Webhook timestamp invalid"}), 401
        except (ValueError, TypeError):
            current_app.logger.warning(f"Invalid webhook timestamp format: {webhook_timestamp}")
            return jsonify({"error": "Invalid timestamp format"}), 400
        
        # Get the webhook secret from app config (AWS Secrets Manager)
        webhook_secret = current_app.config.get('SUPABASE_HOOK_SECRET')
        if not webhook_secret:
            current_app.logger.error("SUPABASE_HOOK_SECRET not found in app.config - check AWS Secrets Manager configuration")
            return jsonify({"error": "Server configuration error"}), 500
        
        # Validate secret format early
        if not webhook_secret.startswith('v1,whsec_'):
            current_app.logger.error(f"SUPABASE_HOOK_SECRET has invalid format - expected 'v1,whsec_<base64>' but got format starting with: {webhook_secret[:10]}***")
            return jsonify({"error": "Invalid webhook secret configuration"}), 500
        
        # Extract and decode secret from Standard Webhooks format: v1,whsec_xxxx
        try:
            version, secret_part = webhook_secret.split(',', 1)
            secret_bytes = base64.b64decode(secret_part[6:])  # Remove whsec_ prefix and decode
        except Exception as e:
            current_app.logger.error(f"Failed to decode SUPABASE_HOOK_SECRET from AWS Secrets Manager: {e}")
            return jsonify({"error": "Invalid webhook secret format"}), 500
        
        # Verify signature according to Standard Webhooks spec
        payload = request.get_data()
        signed_payload = f"{webhook_id}.{webhook_timestamp}.{payload.decode('utf-8')}"
        
        expected_signature = base64.b64encode(
            hmac.new(secret_bytes, signed_payload.encode('utf-8'), hashlib.sha256).digest()
        ).decode('utf-8')
        
        # Extract actual signature (format: v1,<signature>)
        signature_parts = webhook_signature.split(',')
        if len(signature_parts) != 2 or signature_parts[0] != 'v1':
            current_app.logger.warning(f"Invalid signature format: {webhook_signature}")
            return jsonify({"error": "Invalid signature format"}), 401
        
        received_signature = signature_parts[1]
        
        if not hmac.compare_digest(expected_signature, received_signature):
            current_app.logger.warning(f"Signature verification failed for webhook {webhook_id}")
            current_app.logger.debug(f"Expected signature: {expected_signature[:20]}...")
            current_app.logger.debug(f"Received signature: {received_signature[:20]}...")
            current_app.logger.debug(f"Signed payload length: {len(signed_payload)}")
            current_app.logger.debug(f"Secret bytes length: {len(secret_bytes)}")
            return jsonify({"error": "Signature verification failed"}), 401
        
        # Parse the event with size limits
        if len(payload) > 20480:  # 20KB limit as per Supabase docs
            current_app.logger.warning(f"Payload too large: {len(payload)} bytes")
            return jsonify({"error": "Payload too large"}), 413
        
        try:
            event = request.get_json(force=True)
        except Exception as e:
            current_app.logger.error(f"JSON parsing failed: {e}")
            return jsonify({"error": "Invalid JSON payload"}), 400
        
        if not event:
            return jsonify({"error": "Empty JSON payload"}), 400
        
        # Extract and validate user information from Supabase Custom Access Token Hook payload
        user_id = event.get('user_id')
        claims = event.get('claims', {})
        user_email = claims.get('email')
        user_metadata = claims.get('user_metadata', {})
        user_created_at = claims.get('created_at') or claims.get('iat')
        
        if not user_id:
            current_app.logger.warning("Missing user ID in webhook payload")
            return jsonify({"error": "Missing user ID"}), 400
        
        # Initialize custom claims with production values
        new_custom_claims = {
            'user_id': user_id,
            'email': user_email,
            'token_version': 'v2.1',
            'issued_at': timestamp,
            'webhook_id': webhook_id,
            'refresh_enabled': True,
            'hook_processed_at': current_timestamp
        }
        
        # Add display name with sanitization
        display_name = (
            user_metadata.get('full_name') or 
            user_metadata.get('name') or 
            user_email.split('@')[0] if user_email else 'User'
        )
        # Sanitize display name (basic)
        display_name = ''.join(c for c in display_name if c.isprintable())[:50]
        new_custom_claims['display_name'] = display_name
        
        # Get subscription plan with timeout protection
        try:
            admin_client = extensions.get_admin_client()
            
            # Use single() instead of limit(1) for better error handling
            sub_result = admin_client.table('user_subscriptions')\
                .select('plan_id, status')\
                .eq('user_id', user_id)\
                .maybe_single()\
                .execute()
            
            if sub_result.data:
                new_custom_claims['subscription_plan_id'] = sub_result.data['plan_id']
                new_custom_claims['subscription_status'] = sub_result.data.get('status', 'active')
            else:
                new_custom_claims['subscription_plan_id'] = 1  # Default to Free plan
                new_custom_claims['subscription_status'] = 'active'
                
        except Exception as e:
            current_app.logger.warning(f"Subscription lookup failed for user {user_id[:8]}***: {e}")
            new_custom_claims['subscription_plan_id'] = 1  # Default to Free plan
            new_custom_claims['subscription_status'] = 'active'
        
        # Add user account metadata
        if user_created_at:
            try:
                created_timestamp = datetime.fromisoformat(user_created_at.replace('Z', '+00:00')).timestamp()
                new_custom_claims['account_created_at'] = int(created_timestamp)
            except Exception:
                pass
        
        # Merge custom claims into existing claims (preserve existing)
        existing_claims = event.get('claims', {})
        # Only update our custom claims, preserve any existing ones
        for key, value in new_custom_claims.items():
            existing_claims[key] = value
        
        # Update the event
        event['claims'] = existing_claims
        
        # Log successful processing
        processing_time = int((time.time() - start_time) * 1000)  # milliseconds
        current_app.logger.info(f"Custom Access Token Hook completed for user {user_id[:8]}*** in {processing_time}ms (webhook: {webhook_id})")
        
        # Return with proper headers
        response = jsonify(event)
        response.headers['Content-Type'] = 'application/json'
        return response, 200
        
    except json.JSONDecodeError as e:
        current_app.logger.error(f"JSON decode error in webhook: {e}")
        return jsonify({"error": "Invalid JSON format"}), 400
    except Exception as e:
        processing_time = int((time.time() - start_time) * 1000)
        current_app.logger.error(f"Custom Access Token Hook error after {processing_time}ms: {type(e).__name__}: {str(e)}", exc_info=True)
        
        # Return appropriate error response for retry
        if processing_time > 4500:  # Near 5s timeout
            response = jsonify({"error": "Processing timeout"})
            response.headers['Content-Type'] = 'application/json'
            response.headers['Retry-After'] = '2'
            return response, 503
        else:
            response = jsonify({"error": "Internal server error"})
            response.headers['Content-Type'] = 'application/json'
            return response, 500