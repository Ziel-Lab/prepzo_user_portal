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
    This provides a secure way for the frontend to get user details
    without directly querying the database.
    It also backfills subscription data for existing users who may not have it.
    """
    user = g.user
    supabase = extensions.supabase
    uid = user.id

    try:
        # Check if a subscription already exists for this user.
        sub_res = supabase.table('user_subscriptions').select('user_id', count='exact').eq('user_id', uid).execute()

        # If no subscription is found (count is 0), this is an existing user who needs to be backfilled.
        if sub_res.count == 0:
            current_app.logger.info(f"No subscription found for user {uid}. Backfilling with free plan.")
            
            free_plan_res = supabase.table('subscription_plans').select('id, name').eq('name', 'Free').single().execute()
            
            if free_plan_res.data:
                free_plan = free_plan_res.data
                period_start = date.today().replace(day=1)
                period_end = get_last_day_of_month(date.today())

                # Correctly get the display name from the authenticated user object
                display_name = g.user.user_metadata.get('full_name') or g.user.user_metadata.get('name', 'N/A')

                # Use the same logic as the DB trigger to create records
                supabase.table('user_subscriptions').insert({
                    'user_id': uid, 'plan_id': free_plan['id'], 'status': 'free',
                    'display_name': display_name, 'started_at': date.today().isoformat(),
                    'current_period_start': period_start.isoformat(), 'current_period_end': period_end.isoformat(),
                }).execute()

                supabase.table('feature_usage').insert({
                    'user_id': uid, 'plan_id': free_plan['id'], 'display_name': display_name,
                    'period_start': period_start.isoformat(), 'period_end': period_end.isoformat(),
                    'resume_count': 0, 'cover_letter_count': 0, 
                    'linkedin_optimize_count': 0, 'job_search_results_count': 0
                }).execute()
                current_app.logger.info(f"Successfully backfilled subscription for user {uid}.")
            else:
                current_app.logger.error(f"Could not backfill user {uid}: 'Free' plan not found in DB.")
    
    except Exception as e:
        # Log the error but do not fail the request. The main goal is to return profile data.
        current_app.logger.error(f"An error occurred during subscription backfill for user {uid}: {e}", exc_info=True)
    
    # Extract relevant, safe-to-share user information
    profile_data = {
        'id': user.id,
        'email': user.email,
        'full_name': user.user_metadata.get('full_name'),
        'avatar_url': user.user_metadata.get('avatar_url') or user.user_metadata.get('picture'),
    }
    
    return jsonify(profile_data), 200 