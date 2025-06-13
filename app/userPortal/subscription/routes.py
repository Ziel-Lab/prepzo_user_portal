# routes.py
from flask import request, jsonify, current_app, g
from datetime import datetime, date, timedelta
import stripe
from . import subscription_bp
from app import extensions
from .helpers import check_and_use_feature, get_last_day_of_month, require_authentication
from postgrest.exceptions import APIError
from types import SimpleNamespace

@subscription_bp.route("/status", methods=["GET", "OPTIONS"])
@require_authentication
def get_subscription_status():
    """
    Endpoint for the frontend to get the user's full subscription and usage status.
    If a user has no subscription, it provisions the default 'free' plan for them.
    This function now uses a clear read-then-write pattern to avoid race conditions.
    """
    supabase = extensions.supabase
    uid = g.user.id

    try:
        # --- READ PHASE ---
        # Step 1: Attempt to fetch the user's subscription.
        sub_response = supabase.table('user_subscriptions').select('*').eq('user_id', uid).maybe_single().execute()
        subscription = sub_response.data

        # --- WRITE AND READ PHASE (only if necessary) ---
        if not subscription:
            current_app.logger.info(f"No subscription record for user {uid}. Provisioning default free plan.")
            period_start = date.today().replace(day=1)
            period_end = get_last_day_of_month(date.today())
            display_name = g.user.user_metadata.get('full_name') or g.user.user_metadata.get('name', 'N/A')

            # Upsert the subscription and usage records. This is an atomic "write" operation.
            supabase.table('user_subscriptions').upsert({
                'user_id': uid, 'plan_id': 1, 'status': 'active',
                'current_period_start': str(period_start), 'current_period_end': str(period_end)
            }, on_conflict='user_id').execute()

            supabase.table('feature_usage').upsert({
                'user_id': uid, 'plan_id': 1, 'period_start': str(period_start),
                'period_end': str(period_end), 'display_name': display_name
            }, on_conflict='user_id,period_start,period_end').execute()

            # Now that we are certain the records exist, re-fetch the subscription data. This is our second "read".
            subscription = supabase.table('user_subscriptions').select('*').eq('user_id', uid).single().execute().data
            if not subscription:
                current_app.logger.error(f"FATAL: Failed to fetch subscription for user {uid} immediately after upserting.")
                return jsonify({"error": "Failed to initialize your user profile."}), 500

        # --- DATA ASSEMBLY PHASE ---
        # Step 2: Fetch the plan details for the user's subscription.
        plan_response = supabase.table('subscription_plans').select('*').eq('id', subscription['plan_id']).single().execute()
        if not plan_response.data:
            current_app.logger.error(f"Could not load plan details for plan_id: {subscription['plan_id']}.")
            return jsonify({"error": "Subscription plan details could not be loaded."}), 500
        
        # Manually attach the plan data to the subscription object.
        subscription['subscription_plans'] = plan_response.data

        # Step 3: Fetch the usage for the current period.
        period_start_str = subscription['current_period_start']
        period_end_str = subscription['current_period_end']

        usage_response = supabase.table('feature_usage').select('*') \
            .eq('user_id', uid) \
            .eq('period_start', period_start_str) \
            .eq('period_end', period_end_str) \
            .maybe_single().execute()
            
        # Attach usage data, defaulting to an empty object if none is found.
        subscription['usage'] = usage_response.data if usage_response.data else {}

        return jsonify(subscription), 200
        
    except Exception as e:
        current_app.logger.error(f"An unexpected exception occurred in /subscription/status: {e}", exc_info=True)
        return jsonify({"error": "An unexpected server error occurred while fetching subscription status."}), 500

@subscription_bp.route("/stripe/create-checkout-session", methods=["POST"])
@require_authentication
def create_checkout_session():
    """Creates a Stripe Checkout session for upgrading to the paid plan."""
    stripe.api_key = current_app.config.get("STRIPE_SECRET_KEY")
    PAID_PLAN_PRICE_ID = current_app.config.get("STRIPE_PAID_PLAN_PRICE_ID")
    FRONTEND_URL = current_app.config.get("FRONTEND_URL")

    if not all([stripe.api_key, PAID_PLAN_PRICE_ID, FRONTEND_URL]):
        current_app.logger.warning("Stripe is not configured. Missing secret key, price ID, or frontend URL.")
        return jsonify({"error": "This feature is not configured on the server."}), 503

    supabase = extensions.supabase
    uid = g.user.id
    user_email = g.user.email

    try:
        sub_response = supabase.table('user_subscriptions').select('stripe_customer_id').eq('user_id', uid).single().execute()
        customer_id = sub_response.data.get('stripe_customer_id')

        if not customer_id:
            customer = stripe.Customer.create(email=user_email, metadata={'supabase_uid': uid})
            customer_id = customer.id
            supabase.table('user_subscriptions').update({'stripe_customer_id': customer_id}).eq('user_id', uid).execute()

        checkout_session = stripe.checkout.Session.create(
            mode='subscription',
            customer=customer_id,
            client_reference_id=uid,
            line_items=[{'price': PAID_PLAN_PRICE_ID, 'quantity': 1}],
            success_url=f'{FRONTEND_URL}/dashboard/settings?session_id={{CHECKOUT_SESSION_ID}}',
            cancel_url=f'{FRONTEND_URL}/billing/cancel',
        )
        return jsonify({'sessionId': checkout_session.id}), 200
    except Exception as e:
        current_app.logger.error(f"Stripe checkout session creation failed: {e}")
        return jsonify({'error': str(e)}), 500

@subscription_bp.route("/stripe/cancel-subscription", methods=["POST", "OPTIONS"])
@require_authentication
def cancel_subscription():
    """Cancels a user's active paid subscription via Stripe."""
    stripe.api_key = current_app.config.get("STRIPE_SECRET_API_KEY")
    if not stripe.api_key:
        current_app.logger.warning("Stripe is not configured. Missing secret key.")
        return jsonify({"error": "This feature is not configured on the server."}), 503

    supabase = extensions.supabase
    uid = g.user.id

    try:
        sub_response = supabase.table('user_subscriptions').select('stripe_subscription_id, status').eq('user_id', uid).single().execute()
        stripe_sub_id = sub_response.data.get('stripe_subscription_id')
        status = sub_response.data.get('status')

        if status != 'active' or not stripe_sub_id:
            return jsonify({"error": "No active paid subscription to cancel."}), 400

        # Retrieve the subscription object, modify it, and then save it.
        # This pattern is more compatible with older versions of the Stripe library.
        subscription = stripe.Subscription.retrieve(stripe_sub_id)
        subscription.cancel_at_period_end = True
        subscription.save()
        
        # Update our local database to reflect the pending cancellation.
        # This provides immediate feedback to the frontend.
        supabase.table('user_subscriptions').update({
            'status': 'canceling',
            'updated_at': datetime.utcnow().isoformat()
        }).eq('user_id', uid).execute()

        return jsonify({"message": "Subscription cancellation scheduled successfully. Your plan will remain active until the end of your current billing period."}), 200
    except Exception as e:
        current_app.logger.error(f"Stripe subscription cancellation failed: {e}")
        return jsonify({'error': str(e)}), 500

@subscription_bp.route("/stripe/webhook", methods=["POST", "OPTIONS"])
def stripe_webhook():
    """Handles incoming webhooks from Stripe to update subscription status in the DB."""
    stripe_webhook_secret = current_app.config.get("STRIPE_WEBHOOK_SECRET")
    stripe.api_key = current_app.config.get("STRIPE_SECRET_API_KEY")
    if not stripe_webhook_secret or not stripe.api_key:
        current_app.logger.warning("Stripe webhook secret or API key is not configured. Aborting webhook processing.")
        return jsonify({"error": "Stripe webhook is not configured on the server."}), 503
        
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    supabase = extensions.supabase

    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        current_app.logger.error(f"Stripe webhook error: {e}")
        return 'Invalid signature or payload', 400

    event_type = event['type']
    data = event['data']['object']
    current_app.logger.info(f"--- STRIPE WEBHOOK: Received event '{event_type}' ---")

    if event_type == 'checkout.session.completed':
        session = data
        customer_id = session.get('customer')
        subscription_id = session.get('subscription')
        uid = session.get('client_reference_id')

        if not uid:
            current_app.logger.error(f"Stripe 'checkout.session.completed' webhook received without a client_reference_id. Cannot process for session_id: {session.get('id')}")
            return jsonify(success=True)
        
        try:
            auth_user_res = supabase.auth.admin.get_user_by_id(uid)
            display_name = auth_user_res.user.user_metadata.get('full_name') or auth_user_res.user.user_metadata.get('name', 'N/A')
        except Exception as e:
            current_app.logger.error(f"Could not fetch user {uid} from Supabase Auth to get display_name: {e}")
            display_name = "N/A"

        paid_plan_id = 2 
        current_app.logger.info(f"Stripe checkout completed for uid {uid} (customer {customer_id}). Attempting to upgrade to paid plan_id: {paid_plan_id}.")

        try:
            stripe_sub = stripe.Subscription.retrieve(subscription_id)
            period_start = datetime.fromtimestamp(stripe_sub.current_period_start).date()
            period_end = datetime.fromtimestamp(stripe_sub.current_period_end).date()
        except Exception as e:
            current_app.logger.error(f"Failed to retrieve subscription {subscription_id} from Stripe: {e}. Falling back to manual date calculation.")
        period_start = date.today()
        period_end = get_last_day_of_month(period_start)

        # First, check if a subscription record already exists for this user.
        existing_sub_res = supabase.table('user_subscriptions').select('user_id').eq('user_id', uid).maybe_single().execute()

        subscription_data = {
            'plan_id': paid_plan_id, 
            'status': 'active', 
            'stripe_subscription_id': subscription_id,
            'stripe_customer_id': customer_id,
            'current_period_start': str(period_start), 
            'current_period_end': str(period_end),
            'next_billing_date': str(period_end),
            'updated_at': datetime.utcnow().isoformat(),
            'user_id': uid 
        }

        if existing_sub_res.data:
            # Record exists, so we update it.
            current_app.logger.info(f"Existing subscription found for user {uid}. Updating record.")
            sub_update_res = supabase.table('user_subscriptions').update(subscription_data).eq('user_id', uid).execute()
        else:
            # No record exists, so we insert a new one.
            current_app.logger.info(f"No existing subscription for user {uid}. Inserting new record.")
            sub_update_res = supabase.table('user_subscriptions').insert(subscription_data).execute()
        
        if not sub_update_res.data:
            current_app.logger.error(f"Failed to update/insert subscription for user_id: {uid}. Response: {sub_update_res}")
            # Still return 200 to Stripe, but log the error.
            return jsonify(success=True)

        current_app.logger.info(f"Successfully wrote subscription for user_id: {uid}.")
        
        usage_res = supabase.table('feature_usage').upsert({
            'user_id': uid, 
            'period_start': str(period_start), 
            'period_end': str(period_end), 
            'plan_id': paid_plan_id,
            'display_name': display_name
        }, on_conflict='user_id,period_start,period_end').execute()

        if not usage_res.data:
            current_app.logger.error(f"Failed to create feature usage record for user {uid} on new paid plan. Response: {usage_res}")

    elif event_type == 'invoice.payment_succeeded':
        invoice = data
        customer_id = invoice.get('customer')
        subscription_id = invoice.get('subscription') 
        billing_end_ts = invoice.get('period_end')

        if customer_id:
            sub_res = supabase.table('user_subscriptions').select('id, user_id, current_period_end').eq('stripe_customer_id', customer_id).single().execute()
            
            if sub_res.data:
                sub_id = sub_res.data['id']
                uid = sub_res.data['user_id']
                
                try:
                    auth_user_res = supabase.auth.admin.get_user_by_id(uid)
                    display_name = auth_user_res.user.user_metadata.get('full_name') or auth_user_res.user.user_metadata.get('name', 'N/A')
                except Exception as e:
                    current_app.logger.error(f"Could not fetch user {uid} from Supabase Auth to get display_name: {e}")
                    display_name = "N/A"

                next_period_start = datetime.fromtimestamp(invoice.get('period_start')).date()
                next_period_end = datetime.fromtimestamp(billing_end_ts).date()

                current_app.logger.info(f"--- STRIPE WEBHOOK: Updating subscription period for user {uid} to {next_period_start} - {next_period_end} ---")
                supabase.table('user_subscriptions').update({
                    'current_period_start': str(next_period_start),
                    'current_period_end': str(next_period_end),
                    'next_billing_date': str(next_period_end),
                    'updated_at': datetime.utcnow().isoformat(),
                    'stripe_subscription_id': subscription_id, 
                    'status': 'active' 
                }).eq('id', sub_id).execute()
                
                current_app.logger.info(f"--- STRIPE WEBHOOK: Creating new usage record for user {uid} for period {next_period_start} - {next_period_end} ---")
                supabase.table('feature_usage').insert({
                    'user_id': uid,
                    'plan_id': 2, 
                    'period_start': str(next_period_start),
                    'period_end': str(next_period_end),
                    'display_name': display_name
                }).execute()

    elif event_type == 'invoice.payment_failed':
        subscription_id = data.get('subscription')
        supabase.table('user_subscriptions').update({'status': 'past_due'}).eq('stripe_subscription_id', subscription_id).execute()

    elif event_type == 'customer.subscription.deleted':
        subscription = data
        customer_id = subscription['customer']
        
        sub_res = supabase.table('user_subscriptions').select('id').eq('stripe_customer_id', customer_id).maybe_single().execute()

        if sub_res.data:
            sub_id = sub_res.data['id']
            period_start = date.today().replace(day=1)
            period_end = get_last_day_of_month(date.today())

            supabase.table('user_subscriptions').update({
                'plan_id': 1,
                'stripe_subscription_id': None,
                'status': 'active',
                'current_period_start': str(period_start),
                'current_period_end': str(period_end),
                'next_billing_date': None,
                'updated_at': datetime.utcnow().isoformat()
            }).eq('id', sub_id).execute()

    return jsonify(success=True)

@subscription_bp.route("/protected/resume-analyzer", methods=["POST"])
@require_authentication
@check_and_use_feature('resume', increment_by=1)
def analyze_resume_example():
    # If the code reaches here, the user has quota and it has been decremented.
    # Proceed with the actual feature logic.
    return jsonify({"message": f"Successfully used the resume analyzer feature. User: {g.user.id}"})

@subscription_bp.route("/test-db-write", methods=["POST"])
@require_authentication
def test_db_write():
    """
    A temporary diagnostic endpoint to isolate database write failures.
    It attempts a single UPSERT operation. If this fails, it proves that
    the network environment is blocking POST/PATCH requests to Supabase.
    """
    supabase = extensions.supabase
    uid = g.user.id
    current_app.logger.info(f"--- DIAGNOSTIC: Testing database WRITE for user {uid} ---")
    
    try:
        # We will attempt to 'upsert' a dummy record. 
        # Using a non-existent date to avoid conflicts with real data.
        period_start = "1999-01-01"
        period_end = "1999-01-31"

        test_payload = {
            'user_id': uid, 
            'period_start': period_start, 
            'period_end': period_end,
            'resume_count': 999 # A dummy value to indicate a test
        }

        # Use on_conflict to avoid errors if the row already exists from a previous test
        response = supabase.table('feature_usage').upsert(
            test_payload, 
            on_conflict='user_id,period_start,period_end'
        ).execute()

        current_app.logger.info(f"--- DIAGNOSTIC: Database WRITE successful. Response: {response.data} ---")
        return jsonify({"message": "Database write successful.", "data": response.data}), 200

    except APIError as e:
        current_app.logger.error(f"--- DIAGNOSTIC: Database WRITE FAILED with APIError. This strongly suggests a network block on POST/PATCH requests. Details: {e}", exc_info=True)
        return jsonify({"error": "Database write failed.", "details": str(e)}), 500
    except Exception as e:
        current_app.logger.error(f"--- DIAGNOSTIC: Database WRITE FAILED with an unexpected exception. Details: {e}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred during the database write test.", "details": str(e)}), 500

