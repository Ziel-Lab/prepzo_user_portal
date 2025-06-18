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
    Decorator to check quota, handle rollovers, and increment usage *after*
    the decorated function succeeds.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not hasattr(g, 'user'):
                current_app.logger.error("g.user not found. @require_authentication must be used before @check_and_use_feature.")
                return jsonify({"error": "Internal server error: user not authenticated for feature check."}), 500

            try:
                supabase = extensions.supabase
                uid = g.user.id
                display_name = g.user.user_metadata.get('full_name') or g.user.user_metadata.get('name', 'N/A')
                
                # Step 1 & 2: Get or create the user's subscription record.
                sub_res = supabase.table('user_subscriptions').select('*').eq('user_id', uid).maybe_single().execute()
                
                if not sub_res:
                    current_app.logger.error(f"DB query for subscription for user {uid} returned None. Aborting.")
                    return jsonify({"error": "A database error occurred. Please try again later."}), 503
                subscription_record = sub_res.data # Will be a dict if found, None if not.

                # If a subscription exists, handle a potential period rollover before any other checks.
                # This ensures the user is always associated with the correct billing period.
                if subscription_record:
                    handle_period_rollover(supabase, uid, subscription_record)
                    # After a potential rollover, we refetch the subscription to guarantee we have the latest dates.
                    sub_res = supabase.table('user_subscriptions').select('*').eq('user_id', uid).maybe_single().execute()
                    if not sub_res:
                        current_app.logger.error(f"DB query for subscription for user {uid} returned None after rollover. Aborting.")
                        return jsonify({"error": "A database error occurred. Please try again later."}), 503
                    subscription_record = sub_res.data

                if not subscription_record:
                    current_app.logger.info(f"User {uid} has no subscription record. Creating default free plan entries.")
                    period_start = date.today().replace(day=1)
                    period_end = get_last_day_of_month(date.today())

                    # Create the subscription record
                    new_sub_res = supabase.table('user_subscriptions').insert({
                        'user_id': uid, 'plan_id': 1, 'status': 'free', 'display_name': display_name,
                        'current_period_start': str(period_start), 'current_period_end': str(period_end)
                    }).execute()
                    
                    if not new_sub_res or not new_sub_res.data:
                        return jsonify({"error": "Failed to initialize your user profile."}), 500
                    subscription_record = new_sub_res.data[0] # insert() returns a list, take the first item.

                # Now that the subscription record is guaranteed to be up-to-date,
                # we can fetch or create the usage record for the correct period.
                plan_id = subscription_record['plan_id']
                plan_res = supabase.table('subscription_plans').select('*').eq('id', plan_id).single().execute()
                
                if not plan_res or not plan_res.data:
                    current_app.logger.error(f"Could not fetch plan details for plan_id {plan_id}.")
                    return jsonify({"error": "Could not verify your subscription plan details."}), 500
                
                period_start = subscription_record['current_period_start']
                period_end = subscription_record['current_period_end']
                
                usage_res = supabase.table('feature_usage').select('*').eq('user_id', uid).eq('period_start', period_start).eq('period_end', period_end).maybe_single().execute()
                
                if not usage_res:
                    current_app.logger.error(f"DB query for usage record for user {uid} returned None. Aborting.")
                    return jsonify({"error": "A database error occurred. Please try again later."}), 503
                usage_record = usage_res.data # Will be a dict if found, None if not.
                    
                if not usage_record:
                    current_app.logger.info(f"No usage record for user {uid} for period {period_start}-{period_end}. Creating one.")
                    initial_usage = {
                        'user_id': uid, 'plan_id': plan_id, 'period_start': period_start, 'period_end': period_end,
                        'resume_count': 0, 'linkedin_optimize_count': 0, 'cover_letter_count': 0, 'display_name': display_name
                    }
                    new_usage_res = supabase.table('feature_usage').insert(initial_usage).execute()
                    if not new_usage_res or not new_usage_res.data:
                        return jsonify({"error": "Failed to initialize usage tracking."}), 500
                    usage_record = new_usage_res.data[0]

                # Step 5: Compare usage against the plan's limit using the unified usage_record
                usage_count_col = f"{feature_name}_count"
                current_usage = usage_record.get(usage_count_col, 0) or 0
                
                plan_limit_col = f"{feature_name}_limit_per_month"
                plan_limit = plan_res.data.get(plan_limit_col, 0) or 0

                if current_usage + increment_by > plan_limit:
                    return jsonify({
                        "error": f"You have reached your monthly limit for {feature_name}.",
                        "limit": plan_limit,
                        "usage": current_usage
                    }), 429
                
                # --- END OF PRE-CHECK ---

            except APIError as e:
                # This error handling remains crucial for genuine network issues.
                if 'Missing response' in str(e.message):
                    current_app.logger.error(f"DATABASE NETWORK ERROR in check_and_use_feature pre-check: {e}", exc_info=True)
                    return jsonify({"error": "Service temporarily unavailable due to a database connection issue. Please try again later."}), 503
                else:
                    current_app.logger.error(f"DATABASE API_ERROR in check_and_use_feature pre-check: {e}", exc_info=True)
                    return jsonify({"error": "A database error occurred while verifying your plan.", "details": str(e.message)}), 500
            except Exception as e:
                current_app.logger.error(f"An unexpected error occurred in check_and_use_feature pre-check: {e}", exc_info=True)
                return jsonify({"error": "An internal server error occurred while checking feature usage."}), 500

            # Step 6: Proceed to execute the original function
            response, status_code = f(*args, **kwargs)

            # Step 7: Only if the function was successful, increment the usage
            if 200 <= status_code < 300:
                try:
                    current_app.logger.info(f"Feature '{feature_name}' used successfully. Incrementing usage for user {uid}.")
                    supabase.table('feature_usage') \
                        .update({usage_count_col: current_usage + increment_by}) \
                        .eq('id', usage_record['id']) \
                        .execute()
                except APIError as e:
                    # Log this failure, but don't fail the user's request since they got their response.
                    current_app.logger.error(f"CRITICAL: Failed to increment usage for user {uid} after a successful API call. Details: {e}", exc_info=True)
                
            return response, status_code
        
        return decorated_function
    return decorator
