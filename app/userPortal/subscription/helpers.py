from datetime import date, datetime, timedelta
from flask import jsonify, g, request, current_app, make_response
from app import extensions 
from functools import wraps
import calendar
from postgrest.exceptions import APIError


class QuotaExceededError(Exception):
    pass

def require_authentication(f):
    """
    More robust decorator to protect routes and set g.user.
    - Handles CORS preflight OPTIONS requests manually.
    - Fails early if Authorization header is missing or malformed.
    - Uses the low-level Supabase API to avoid 204 response quirks.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'OPTIONS':
            response = make_response()
            frontend_url = current_app.config.get('FRONTEND_URL')
            if frontend_url:
                response.headers.add("Access-Control-Allow-Origin", frontend_url)
            response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
            response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS,PUT,PATCH,DELETE')
            response.headers.add('Access-Control-Allow-Credentials', 'true')
            return response

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
    """Handles the rollover to a new billing period if needed."""
    today = date.today()

    # --- Start: Robust check for required date fields ---
    sub_id = subscription.get('id')
    if not subscription.get('current_period_end'):
        current_app.logger.error(f"CRITICAL: Subscription {sub_id} for user {uid} is missing 'current_period_end'. Cannot check feature usage.")
        raise ValueError(f"Subscription {sub_id} is missing its period end date.")
    if not subscription.get('current_period_start'):
        current_app.logger.error(f"CRITICAL: Subscription {sub_id} for user {uid} is missing 'current_period_start'. Cannot check feature usage.")
        raise ValueError(f"Subscription {sub_id} is missing its period start date.")
    # --- End: Robust check ---

    current_period_end = datetime.strptime(subscription['current_period_end'], '%Y-%m-%d').date()

    if today > current_period_end:
        current_app.logger.info(f"User {uid} billing period expired on {current_period_end}. Rolling over.")
        
        next_period_start, next_period_end = get_next_period(current_period_end)
        
        # Update user_subscriptions with the new period
        updated_sub, error = supabase.table('user_subscriptions') \
            .update({
                'current_period_start': str(next_period_start),
                'current_period_end': str(next_period_end)
            }) \
            .eq('id', subscription['id']) \
            .execute()
        
        if error or not updated_sub.data:
             current_app.logger.error(f"Failed to update subscription period for user {uid}. Raw Error: {error}. Response Data: {updated_sub}")
             raise Exception(f"Failed to update subscription period for user {uid}")

        # Insert a new feature_usage record for the new period
        new_usage, error = supabase.table('feature_usage') \
            .insert({
                'user_id': uid,
                'plan_id': subscription['plan_id'],
                'period_start': str(next_period_start),
                'period_end': str(next_period_end)
            }) \
            .execute()
            
        if error or not new_usage.data:
            # This could happen if a rollover was already processed by a concurrent request
            current_app.logger.warning(f"Could not insert new feature usage for {uid}, it might already exist. Error: {error}")

        # Return the new period dates to be used in the calling function
        return next_period_start, next_period_end
    
    # No rollover needed, return current period
    return datetime.strptime(subscription['current_period_start'], '%Y-%m-%d').date(), current_period_end


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
                subscription_record = sub_res.data # Will be a dict if found, None if not.

                if not subscription_record:
                    current_app.logger.info(f"User {uid} has no subscription record. Creating default free plan entries.")
                    period_start = date.today().replace(day=1)
                    period_end = get_last_day_of_month(date.today())

                    # Create the subscription record
                    new_sub_res = supabase.table('user_subscriptions').insert({
                        'user_id': uid, 'plan_id': 1, 'status': 'active',
                        'current_period_start': str(period_start), 'current_period_end': str(period_end)
                    }).execute()
                    
                    if not new_sub_res.data:
                        return jsonify({"error": "Failed to initialize your user profile."}), 500
                    subscription_record = new_sub_res.data[0] # insert() returns a list, take the first item.

                    # Create the initial usage record
                    new_usage_res = supabase.table('feature_usage').insert({
                        'user_id': uid, 'plan_id': 1, 'period_start': str(period_start), 'period_end': str(period_end),
                        'resume_count': 0, 'linkedin_optimize_count': 0, 'cover_letter_count': 0, 'display_name': display_name
                    }).execute()
                    
                    if not new_usage_res.data:
                        return jsonify({"error": "Failed to initialize usage tracking."}), 500
                    usage_record = new_usage_res.data[0] # insert() returns a list, take the first item.

                # Now, get the plan limits from the unified subscription_record
                plan_id = subscription_record['plan_id']
                plan_res = supabase.table('subscription_plans').select('*').eq('id', plan_id).single().execute()

                # Step 3 & 4: Get or create the usage record for the current period.
                period_start = subscription_record['current_period_start']
                period_end = subscription_record['current_period_end']
                
                usage_res = supabase.table('feature_usage').select('*').eq('user_id', uid).eq('period_start', period_start).eq('period_end', period_end).maybe_single().execute()
                usage_record = usage_res.data # Will be a dict if found, None if not.
                    
                if not usage_record:
                    current_app.logger.info(f"No usage record for user {uid} for period {period_start}-{period_end}. Creating one.")
                    initial_usage = {
                        'user_id': uid, 'plan_id': plan_id, 'period_start': period_start, 'period_end': period_end,
                        'resume_count': 0, 'linkedin_optimize_count': 0, 'cover_letter_count': 0, 'display_name': display_name
                    }
                    new_usage_res = supabase.table('feature_usage').insert(initial_usage).execute()
                    if not new_usage_res.data:
                        return jsonify({"error": "Failed to initialize usage tracking."}), 500
                    usage_record = new_usage_res.data[0] # insert() returns a list, take the first item.

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
