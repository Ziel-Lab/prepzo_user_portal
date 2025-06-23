from datetime import date, datetime, timedelta
from flask import jsonify, g, request, current_app, make_response
from app import extensions 
from functools import wraps
import calendar
from postgrest.exceptions import APIError
from gotrue.errors import AuthApiError


class QuotaExceededError(Exception):
    pass

def require_authentication(f):
    """
    Decorator to protect routes, set g.user, and handle CORS preflight requests.
    - This decorator now creates a full, self-contained response for OPTIONS requests
      to ensure CORS preflights succeed before the main app logic is hit.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Manually handle CORS preflight requests.
        # This is necessary because this decorator runs before the main Flask-CORS extension.
        if request.method == 'OPTIONS':
            response = make_response()
            
            # The browser needs to know which origin is allowed to make the request.
            # We reflect the request's Origin header, which is standard and secure practice.
            # The app-level Flask-CORS config will still validate this origin on the actual request.
            origin = request.headers.get('Origin')
            if origin:
                response.headers.add('Access-Control-Allow-Origin', origin)
            
            # Specify what headers and methods are allowed in the actual request.
            response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
            response.headers.add('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS,PATCH')
            response.headers.add('Access-Control-Allow-Credentials', 'true')
            return response, 200

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            current_app.logger.warning(f"Bad or missing Authorization header received: {auth_header!r}")
            return jsonify({"error": "Missing or malformed Authorization header"}), 401

        jwt_token = auth_header.split(" ", 1)[1]
        if not jwt_token or len(jwt_token.split(".")) != 3:
            current_app.logger.warning(f"Malformed JWT received: {jwt_token!r}")
            return jsonify({"error": "Invalid token format"}), 401

        try:
            user_response = extensions.supabase.auth.get_user(jwt_token)
            user = user_response.user
            if not user or not user.id:
                raise ValueError("Supabase did not return a user object in the response.")
            g.user = user

        except AuthApiError as e:
            # This specific error means the user's JWT is valid but the session/user
            # is not found on Supabase side (e.g., user deleted, session logged out).
            # This is a client-side issue (stale token).
            current_app.logger.warning(f"Authentication failed with stale JWT: {e.message}")
            return jsonify({"error": "Your session has expired. Please log in again.", "details": str(e.message)}), 401
        except APIError as e:
            current_app.logger.error(f"Authentication API call failed: {e}", exc_info=True)
            error_details = e.message if isinstance(e.message, dict) else str(e.message)
            status_code = getattr(e, 'status', 401) 
            return jsonify({"error": "Authentication failed", "details": error_details}), status_code
        except Exception as e:
            current_app.logger.error(f"An unexpected exception occurred during authentication: {e}", exc_info=True)
            return jsonify({"error": "An internal error occurred during authentication"}), 500
            
        return f(*args, **kwargs)
            
    return decorated_function

def get_first_day_of_month(dt):
    return dt.replace(day=1)

def get_last_day_of_month(dt):
    return dt.replace(day=calendar.monthrange(dt.year, dt.month)[1])

def get_user_display_name(user):
    """Safely retrieves the display name from a user object."""
    if not user or not hasattr(user, 'user_metadata') or not user.user_metadata:
        return 'N/A'
    # Prioritize 'full_name', then 'name', and finally 'N/A'
    return user.user_metadata.get('full_name') or user.user_metadata.get('name') or 'N/A'

def get_next_period(current_period_end):
    """Calculates the start and end of the next billing period."""
    # Start of the next month
    next_period_start = get_first_day_of_month(current_period_end + timedelta(days=1))
    # End of that next month
    next_period_end = get_last_day_of_month(next_period_start)
    return next_period_start, next_period_end

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
        current_app.logger.error(f"CRITICAL: Subscription {sub_id} for user {uid} is missing 'current_period_end'. Cannot check for rollover.")
        raise ValueError(f"Subscription {sub_id} is missing its period end date.")
    # --- End: Robust check ---

    current_period_end = datetime.strptime(subscription['current_period_end'], '%Y-%m-%d').date()

    if today > current_period_end:
        current_app.logger.info(f"User {uid} billing period expired on {current_period_end}. Rolling over subscription dates.")
        
        next_period_start, next_period_end = get_next_period(current_period_end)
        
        # Update user_subscriptions with the new period
        updated_sub_res = supabase.table('user_subscriptions') \
            .update({
                'current_period_start': str(next_period_start),
                'current_period_end': str(next_period_end)
            }) \
            .eq('id', subscription['id']) \
            .execute()
        
        if not updated_sub_res or not updated_sub_res.data:
             error_msg = f"Failed to update subscription period for user {uid}. DB response was empty."
             current_app.logger.error(error_msg)
             raise Exception(error_msg)

def check_and_use_feature(feature_name, increment_by=1):
    """
    Decorator that checks a user's feature usage against their plan limits.
    It identifies the current usage period and handles rollovers by creating a new
    usage record for the new month if necessary.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not hasattr(g, 'user'):
                return jsonify({"error": "User not authenticated for feature check."}), 500

            try:
                supabase = extensions.supabase
                uid = g.user.id
                display_name = get_user_display_name(g.user)

                # Step 1: Get the user's most recent usage record. This is the simplest source of truth.
                usage_res = supabase.table('feature_usage') \
                    .select('*') \
                    .eq('user_id', uid) \
                    .order('period_end', desc=True) \
                    .limit(1) \
                    .maybe_single() \
                    .execute()
                
                usage_record = usage_res.data if usage_res else None
                
                # Step 2: Check if the period is expired or if no record exists.
                today = date.today()
                is_expired = usage_record and today > datetime.strptime(usage_record['period_end'], '%Y-%m-%d').date()
                
                if not usage_record or is_expired:
                    if is_expired:
                        current_app.logger.info(f"Usage period for user {uid} expired on {usage_record['period_end']}. Creating new record for current month.")
                    else:
                        current_app.logger.info(f"No usage record found for user {uid}. Creating one for current month.")

                    # Create a new record for the current calendar month.
                    period_start = get_first_day_of_month(today)
                    period_end = get_last_day_of_month(today)
                    
                    # We need the user's plan_id to create the new usage record.
                    sub_res = supabase.table('user_subscriptions').select('plan_id').eq('user_id', uid).single().execute()
                    plan_id = sub_res.data['plan_id'] if sub_res.data else 1 # Default to Free plan

                    initial_usage = {
                        'user_id': uid, 'plan_id': plan_id, 'display_name': display_name,
                        'period_start': str(period_start), 'period_end': str(period_end)
                    }
                    new_usage_res = supabase.table('feature_usage').insert(initial_usage, returning='representation').execute()
                    
                    if not new_usage_res.data:
                        return jsonify({"error": "Failed to initialize usage tracking."}), 500
                    
                    usage_record = new_usage_res.data[0]

                # Step 3: Get plan limits.
                plan_res = supabase.table('subscription_plans').select('*').eq('id', usage_record['plan_id']).single().execute()
                if not plan_res.data:
                    return jsonify({"error": "Could not verify your subscription plan details."}), 500
                
                # Step 4: Compare usage against the plan's limit.
                usage_count_col = f"{feature_name}_count"
                current_usage = usage_record.get(usage_count_col, 0) or 0
                
                plan_limit_col = 'linkedin_optimize_limit' if feature_name == 'linkedin_optimize' else f"{feature_name}_limit_per_month"
                plan_limit = plan_res.data.get(plan_limit_col, 0) or 0

                if current_usage + increment_by > plan_limit:
                    return jsonify({
                        "error": f"You have reached your monthly limit for {feature_name}.",
                        "limit": plan_limit,
                        "usage": current_usage
                    }), 429
                
            except Exception as e:
                current_app.logger.error(f"An unexpected error occurred in check_and_use_feature pre-check: {e}", exc_info=True)
                return jsonify({"error": "An internal server error occurred."}), 500

            # --- PRE-CHECK COMPLETE ---
            
            # Execute the original function.
            response, status_code = f(*args, **kwargs)

            # Only if the function was successful, increment the usage.
            if 200 <= status_code < 300:
                try:
                    current_app.logger.info(f"Feature '{feature_name}' used successfully. Incrementing usage for user {uid}.")
                    
                    # Use a remote procedure call (RPC) to safely increment the value.
                    # This prevents race conditions where two requests could overwrite each other's updates.
                    supabase.rpc('increment_feature_usage', {
                        'p_user_id': uid,
                        'p_period_start': usage_record['period_start'],
                        'p_feature_column': usage_count_col,
                        'p_increment_by': increment_by
                    }).execute()

                except APIError as e:
                    current_app.logger.error(f"CRITICAL: Failed to increment usage for user {uid} via RPC. Details: {e}", exc_info=True)
                
            return response, status_code
        
        return decorated_function
    return decorator
