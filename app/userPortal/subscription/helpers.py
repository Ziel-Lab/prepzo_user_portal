from datetime import date, datetime, timedelta
from flask import jsonify, g, request, current_app, make_response
from app import extensions 
from functools import wraps
import calendar
import time
from postgrest.exceptions import APIError
from gotrue.errors import AuthApiError, AuthRetryableError
from dateutil.relativedelta import relativedelta


class QuotaExceededError(Exception):
    pass

def _retry_auth_with_backoff(admin_client, jwt_token, max_retries=3):
    """
    Retry authentication with exponential backoff for AuthRetryableError
    """
    for attempt in range(max_retries):
        try:
            return admin_client.auth.get_user(jwt_token)
        except AuthRetryableError as e:
            if attempt == max_retries - 1:
                # Last attempt, re-raise the error
                raise e
            
            # Exponential backoff: 0.1s, 0.2s, 0.4s
            delay = 0.1 * (2 ** attempt)
            current_app.logger.warning(f"Auth retry {attempt + 1}/{max_retries} failed with {type(e).__name__}, retrying in {delay}s...")
            time.sleep(delay)
    
    # This should never be reached, but just in case
    raise AuthRetryableError("Max retries exceeded")

def require_authentication(f):
    """
    Decorator to protect routes, set g.user, and handle CORS preflight requests.
    Uses dual Supabase client architecture:
    - Admin client for JWT verification 
    - User client for all data operations with RLS
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Handle CORS preflight requests
        if request.method == 'OPTIONS':
            response = make_response(jsonify(success=True))
            
            origin = request.headers.get('Origin')
            if origin:
                response.headers.add('Access-Control-Allow-Origin', origin)
            else:
                response.headers.add('Access-Control-Allow-Origin', '*')

            response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
            response.headers.add('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS,PATCH')
            response.headers.add('Access-Control-Allow-Credentials', 'true')
            return response, 204

        # Extract and validate JWT token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            current_app.logger.warning(f"Authentication failed: Missing or malformed Authorization header from IP {request.remote_addr}")
            return jsonify({"error": "Missing or malformed Authorization header"}), 401

        jwt_token = auth_header.split(" ", 1)[1]
        if not jwt_token or len(jwt_token.split(".")) != 3:
            current_app.logger.warning(f"Authentication failed: Malformed JWT received from IP {request.remote_addr}")
            return jsonify({"error": "Invalid token format"}), 401

        try:
            # Get both Supabase clients
            admin_client = extensions.get_admin_client()
            user_client = extensions.get_user_client()
            
            # Use admin client to verify JWT token with retry for network issues
            user_response = _retry_auth_with_backoff(admin_client, jwt_token)
            user = user_response.user
            if not user or not user.id:
                raise ValueError("Supabase did not return a user object in the response.")
            
            # Set JWT token on user client for RLS context
            # This ensures all subsequent operations are scoped to this user
            try:
                # Set the auth header directly for RLS context
                user_client.auth._headers["Authorization"] = f"Bearer {jwt_token}"
            except Exception as e:
                current_app.logger.warning(f"Failed to set user client session: {e}")
            
            # Store user and clients in Flask g for route access
            g.user = user
            g.supabase = user_client  # Default to user client for data operations
            g.supabase_user = user_client  # Explicit user client
            g.supabase_admin = admin_client  # Admin client for special operations
            
            # Extract custom claims from JWT if available (from Custom Access Token Hook)
            g.user_claims = {}
            try:
                # Try to get custom claims from the user object
                if hasattr(user, 'app_metadata') and user.app_metadata:
                    g.user_claims = user.app_metadata
                elif hasattr(user, 'user_metadata') and user.user_metadata:
                    # Fallback to user_metadata if app_metadata not available
                    g.user_claims = user.user_metadata
                
                # Validate token freshness if available
                token_version = g.user_claims.get('token_version', 'legacy')
                hook_processed_at = g.user_claims.get('hook_processed_at')
                issued_at = g.user_claims.get('issued_at')
                
                # Check if token is stale (older than 24 hours) for v2+ tokens
                if token_version != 'legacy' and issued_at:
                    token_age = time.time() - issued_at
                    if token_age > 86400:  # 24 hours
                        current_app.logger.info(f"Token for user {user.id[:8]}*** is {int(token_age/3600)}h old, may need refresh")
                        g.user_claims['token_age_hours'] = int(token_age / 3600)
                
                # Enhanced logging with security metrics
                subscription_plan = g.user_claims.get('subscription_plan_id', 'unknown')
                webhook_id = g.user_claims.get('webhook_id', 'N/A')
                current_app.logger.info(f"Authentication successful for user {user.id[:8]}*** (token: {token_version}, plan: {subscription_plan}, webhook: {webhook_id[:8]}***) using dual-client architecture")
                
            except Exception as e:
                current_app.logger.warning(f"Could not extract custom claims for user {user.id[:8]}***: {e}")
                current_app.logger.info(f"Authentication successful for user {user.id[:8]}*** using dual-client architecture")

        except AuthApiError as e:
            error_message = str(e).lower()
            
            # Differentiate between different auth failures for frontend handling
            # Enhanced error categorization for production
            if ("expired" in error_message or "stale" in error_message or "invalid claim" in error_message or 
                "signature is invalid" in error_message or "unable to parse or verify" in error_message):
                current_app.logger.warning(f"Authentication failed: Stale/Expired JWT from IP {request.remote_addr} - {type(e).__name__}")
                response = jsonify({
                    "error": "token_expired",
                    "error_type": "stale_jwt", 
                    "message": "Your session token has expired",
                    "action": "refresh_token",
                    "timestamp": int(time.time())
                })
                response.headers['Cache-Control'] = 'no-store'
                response.headers['Pragma'] = 'no-cache'
                return response, 401
            elif "malformed" in error_message or "not found" in error_message or "invalid jwt" in error_message:
                current_app.logger.warning(f"Authentication failed: Invalid JWT from IP {request.remote_addr} - {type(e).__name__}")
                response = jsonify({
                    "error": "token_invalid",
                    "error_type": "invalid_jwt",
                    "message": "Invalid authentication token",
                    "action": "login_required",
                    "timestamp": int(time.time())
                })
                response.headers['Cache-Control'] = 'no-store'
                response.headers['Pragma'] = 'no-cache'
                return response, 401
            elif "rate limit" in error_message or "too many" in error_message:
                current_app.logger.warning(f"Authentication failed: Rate limited from IP {request.remote_addr}")
                response = jsonify({
                    "error": "rate_limited",
                    "error_type": "rate_limit",
                    "message": "Too many authentication attempts. Please wait.",
                    "action": "wait_and_retry",
                    "timestamp": int(time.time())
                })
                response.headers['Retry-After'] = '60'
                return response, 429
            else:
                # Default to allowing refresh attempt for unknown errors
                current_app.logger.warning(f"Authentication failed: Unknown auth error from IP {request.remote_addr} - {type(e).__name__}: {str(e)}")
                response = jsonify({
                    "error": "token_expired",
                    "error_type": "stale_jwt",
                    "message": "Authentication failed - please refresh",
                    "action": "refresh_token",
                    "timestamp": int(time.time())
                })
                response.headers['Cache-Control'] = 'no-store'
                response.headers['Pragma'] = 'no-cache'
                return response, 401
        except AuthRetryableError as e:
            current_app.logger.error(f"Authentication failed after retries due to network issues from IP {request.remote_addr}: {str(e)}")
            response = jsonify({
                "error": "authentication_service_unavailable",
                "error_type": "service_error",
                "message": "Authentication service temporarily unavailable. Please try again in a moment.",
                "action": "retry_later",
                "timestamp": int(time.time())
            })
            response.headers['Retry-After'] = '5'
            return response, 503
        except APIError as e:
            current_app.logger.error(f"Authentication API call failed from IP {request.remote_addr}: {type(e).__name__}", exc_info=True)
            status_code = getattr(e, 'status', 401)
            response = jsonify({
                "error": "authentication_api_error",
                "error_type": "api_error",
                "message": "Authentication service error",
                "action": "refresh_token" if status_code == 401 else "retry_later",
                "timestamp": int(time.time())
            })
            return response, status_code
        except Exception as e:
            current_app.logger.error(f"Unexpected authentication error from IP {request.remote_addr}: {type(e).__name__}", exc_info=True)
            response = jsonify({
                "error": "internal_auth_error",
                "error_type": "internal_error",
                "message": "An internal error occurred during authentication",
                "action": "contact_support",
                "timestamp": int(time.time())
            })
            return response, 500
            
        return f(*args, **kwargs)
            
    return decorated_function

def get_user_display_name(user):
    """
    Safely extract display name from user object
    """
    if not user or not hasattr(user, 'user_metadata'):
        return "User"
    
    metadata = user.user_metadata or {}
    return (
        metadata.get('full_name') or 
        metadata.get('name') or 
        metadata.get('display_name') or 
        user.email or 
        "User"
    )

def get_anniversary_period(user_created_at, reference_date):
    """
    Calculate billing period based on user creation date
    """
    # Convert string to date if needed
    if isinstance(user_created_at, str):
        user_created_at = datetime.fromisoformat(user_created_at.replace('Z', '+00:00')).date()
    elif hasattr(user_created_at, 'date'):
        user_created_at = user_created_at.date()
    
    # Calculate the anniversary month for the reference date
    if reference_date.day >= user_created_at.day:
        # We're past the anniversary day this month
        period_start = reference_date.replace(day=user_created_at.day)
        period_end = (period_start + relativedelta(months=1)) - relativedelta(days=1)
    else:
        # We're before the anniversary day this month
        period_end = reference_date.replace(day=user_created_at.day) - relativedelta(days=1)
        period_start = period_end.replace(day=user_created_at.day) - relativedelta(months=1) + relativedelta(days=1)
    
    return period_start, period_end

def handle_period_rollover(supabase, uid, subscription):
    """
    Checks if the user's billing period has expired and, if so, updates
    the user_subscriptions table with the new period dates.
    It does NOT create a feature_usage record; that is left to the caller.
    """
    today = date.today()

    # --- Start: Robust check for required date fields ---
    sub_id = subscription.get('id')
    if not subscription.get('current_period_end'):
        current_app.logger.error(f"CRITICAL: Subscription {sub_id} for user {uid[:8]}*** is missing 'current_period_end'. Cannot check for rollover.")
        raise ValueError(f"Subscription {sub_id} is missing its period end date.")
    # --- End: Robust check ---

    current_period_end = datetime.strptime(subscription['current_period_end'], '%Y-%m-%d').date()

    if today > current_period_end:
        current_app.logger.info(f"User {uid[:8]}*** billing period expired on {current_period_end}. Rolling over subscription dates.")
        
        next_period_start, next_period_end = get_anniversary_period(g.user.created_at, today)
        
        # Update user_subscriptions with the new period
        updated_sub_res = supabase.table('user_subscriptions') \
            .update({
                'current_period_start': str(next_period_start),
                'current_period_end': str(next_period_end)
            }) \
            .eq('id', subscription['id']) \
            .execute()
        
        if not updated_sub_res or not updated_sub_res.data:
             error_msg = f"Failed to update subscription period for user {uid[:8]}***. DB response was empty."
             current_app.logger.error(error_msg)
             raise Exception(error_msg)

def check_and_use_feature(feature_name, increment_by=1, *, auto_increment=True):
    """
    Decorator that checks a user's feature usage against their plan limits.
    Uses the user Supabase client to ensure RLS is enforced.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not hasattr(g, 'user'):
                return jsonify({"error": "User not authenticated for feature check."}), 500

            try:
                # Use admin client for subscription check (temporary workaround for JWT session issue)
                from app.extensions import get_admin_client
                admin_supabase = get_admin_client()
                supabase = g.supabase_user
                uid = g.user.id
                display_name = get_user_display_name(g.user)

                # Step 1: Get the user's current subscription plan (using admin client due to JWT session issue)
                sub_res = admin_supabase.table('user_subscriptions').select('plan_id,status').eq('user_id', uid).single().execute()
                if sub_res.data and sub_res.data.get('status') == 'active':
                    current_plan_id = sub_res.data['plan_id']
                else:
                    current_plan_id = 1

                # Step 2: Get the limits for the user's CURRENT plan (this table is public read)
                plan_res = admin_supabase.table('subscription_plans').select('*').eq('id', current_plan_id).single().execute()
                if not plan_res.data:
                    return jsonify({"error": "Could not verify your current subscription plan details."}), 500
                current_plan_limits = plan_res.data

                # Step 3: Get the user's most recent usage record (using admin client with explicit user filter)
                usage_res = admin_supabase.table('feature_usage') \
                    .select('*') \
                    .eq('user_id', uid) \
                    .order('period_end', desc=True) \
                    .limit(1) \
                    .maybe_single() \
                    .execute()
                
                usage_record = usage_res.data if usage_res else None
                
                # Step 4: Check if the period is expired or if no record exists.
                today = date.today()
                # Safely determine if the current usage period has expired.
                # Handle the case where `period_end` might be NULL or in an unexpected format to avoid
                # `TypeError: strptime() argument 1 must be str, not None` reported in production.
                period_end_str = usage_record.get('period_end') if usage_record else None

                is_expired = False
                if usage_record:
                    if period_end_str:
                        try:
                            period_end_date = datetime.strptime(period_end_str, '%Y-%m-%d').date()
                            is_expired = today > period_end_date
                        except (TypeError, ValueError):
                            # Malformed date string – treat as expired to force creation of a fresh record
                            current_app.logger.warning(
                                f"Invalid period_end value '{period_end_str}' for user {uid}. Treating as expired.")
                            is_expired = True
                    else:
                        # Missing period_end value – treat as expired so that a new record is created
                        current_app.logger.warning(
                            f"Missing period_end for user {uid}. Treating current usage period as expired.")
                        is_expired = True
                
                if not usage_record or is_expired:
                    if is_expired:
                        current_app.logger.info(f"Usage period for user {uid[:8]}*** expired on {usage_record['period_end']}. Creating new record with current plan {current_plan_id}.")
                    else:
                        current_app.logger.info(f"No usage record for user {uid[:8]}***. Creating one with current plan {current_plan_id}.")

                    period_start, period_end = get_anniversary_period(g.user.created_at, today)
                    
                    initial_usage = {
                        'user_id': uid, 'plan_id': current_plan_id, 'display_name': display_name,
                        'period_start': str(period_start), 'period_end': str(period_end)
                    }

                    # On rollover, dynamically carry over lifetime counts and reset period counts.
                    if is_expired and usage_record:
                        for key, value in usage_record.items():
                            if key.endswith('_lifetime_count'):
                                initial_usage[key] = value or 0 # Carry over lifetime value
                                period_col = key.replace('_lifetime_count', '_period_count')
                                initial_usage[period_col] = 0 # Reset period count

                    new_usage_res = admin_supabase.table('feature_usage').upsert(
                        initial_usage, 
                        on_conflict='user_id',
                        returning='representation'
                    ).execute()
                    
                    if not new_usage_res.data:
                        return jsonify({"error": "Failed to initialize usage tracking."}), 500
                    usage_record = new_usage_res.data[0]

                # Step 5: Handle mid-cycle plan changes.
                elif usage_record.get('plan_id') != current_plan_id:
                    current_app.logger.info(f"User {uid[:8]}*** plan changed mid-cycle from {usage_record.get('plan_id')} to {current_plan_id}. Updating usage record.")
                    admin_supabase.table('feature_usage') \
                        .update({'plan_id': current_plan_id}) \
                        .eq('user_id', uid) \
                        .eq('period_start', usage_record['period_start']) \
                        .execute()
                    usage_record['plan_id'] = current_plan_id # Update local copy for immediate use

                # Step 6: Perform the limit check against the CURRENT plan's limits.
                usage_count_col = f"{feature_name}_period_count"
                current_usage = usage_record.get(usage_count_col, 0) or 0
                
                plan_limit_col = f"{feature_name}_limit_per_month"
                plan_limit = current_plan_limits.get(plan_limit_col, 0) or 0

                if current_usage + increment_by > plan_limit:
                    return jsonify({
                        "error": f"You have reached your monthly limit for {feature_name}.",
                        "limit": plan_limit,
                        "usage": current_usage
                    }), 429
                
            except Exception as e:
                current_app.logger.error(f"An unexpected error occurred in check_and_use_feature pre-check for user {uid[:8] if 'uid' in locals() else 'unknown'}***: {type(e).__name__}", exc_info=True)
                return jsonify({"error": "An internal server error occurred."}), 500

            # --- PRE-CHECK COMPLETE ---
            
            response, status_code = f(*args, **kwargs)

            # Only increment if we’re told to
            if auto_increment and 200 <= status_code < 300:
                # Use admin client for counter increment to avoid user JWT expiry issues
                try:
                    admin_supabase.rpc('increment_feature_counters', {
                        'p_user_id': uid,
                        'p_period_start': usage_record['period_start'],
                        'p_feature_base_name': feature_name,
                        'p_increment_by': increment_by
                    }).execute()
                except APIError as e:
                    current_app.logger.warning(
                        f"Feature counter increment skipped due to API error for user {uid[:8]}***: {type(e).__name__}: {str(e)}"
                    )
                except Exception as e:
                    current_app.logger.warning(
                        f"Feature counter increment skipped due to unexpected error for user {uid[:8]}***: {type(e).__name__}: {str(e)}"
                    )
            return response, status_code
        
        return decorated_function
    return decorator