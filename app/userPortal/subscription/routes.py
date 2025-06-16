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
    Relies on the `handle_new_user` database trigger to provision new users.
    """
    supabase = extensions.supabase
    uid = g.user.id

    try:
        # Step 1: Fetch the user's subscription.
        sub_response = supabase.table('user_subscriptions').select('*').eq('user_id', uid).execute()

        if not sub_response.data:
            current_app.logger.error(f"FATAL: No subscription found for user {uid}, but the DB trigger should have created one.")
            return jsonify({"error": "Your user profile is not configured correctly. Please contact support."}), 500
        
        subscription = sub_response.data[0]

        # Step 2: Fetch the plan details separately.
        plan_id = subscription.get('plan_id')
        if plan_id:
            plan_response = supabase.table('subscription_plans').select('*').eq('id', plan_id).execute()
            if plan_response.data:
                subscription['subscription_plans'] = plan_response.data[0]
            else:
                current_app.logger.warning(f"Subscription plan with id {plan_id} not found for user {uid}.")
                subscription['subscription_plans'] = None
        else:
            subscription['subscription_plans'] = None

        # Step 3: Fetch the usage for the current period.
        period_start_str = subscription['current_period_start']
        period_end_str = subscription['current_period_end']

        usage_response = supabase.table('feature_usage').select('*') \
            .eq('user_id', uid) \
            .eq('period_start', period_start_str) \
            .eq('period_end', period_end_str) \
            .execute()

        if usage_response.data:
            subscription['usage'] = usage_response.data[0]
        else:
            subscription['usage'] = {}

        return jsonify(subscription), 200
        
    except APIError as e:
        if 'Missing response' in str(e.message):
            current_app.logger.error(f"DATABASE NETWORK ERROR in /subscription/status for user {uid}: {e}", exc_info=False)
            return jsonify({"error": "Service temporarily unavailable due to a database connection issue. Please try again later."}), 503
        else:
            current_app.logger.error(f"DATABASE API_ERROR in /subscription/status for user {uid}: {e}", exc_info=True)
            return jsonify({"error": "A database error occurred while fetching your subscription.", "details": str(e.message)}), 500
    except Exception as e:
        current_app.logger.error(f"An unexpected exception occurred in /subscription/status for user {uid}: {e}", exc_info=True)
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

        if status != 'pro' or not stripe_sub_id:
            return jsonify({"error": "No active Pro subscription to cancel."}), 400

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
    current_app.logger.critical("--- STRIPE WEBHOOK ENDPOINT HIT! ---") # <-- TEMPORARY DIAGNOSTIC LOG

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
            current_app.logger.error(f"Stripe 'checkout.session.completed' webhook received without a client_reference_id. Cannot process session: {session.get('id')}")
            return jsonify(success=True) # Acknowledge the webhook
        
        try:
            # Fetch all necessary data before calling the database function
            paid_plan_id = 2 # Assuming 'Pro' plan is ID 2
            auth_user_res = supabase.auth.admin.get_user_by_id(uid)
            display_name = auth_user_res.user.user_metadata.get('full_name') or auth_user_res.user.user_metadata.get('name', 'N/A')

            # Retrieve the invoice to get the accurate billing period and price.
            # This is more reliable than retrieving the subscription immediately after creation.
            invoice_id = session.get('invoice')
            if not invoice_id:
                raise ValueError(f"Webhook Error: Checkout session {session.get('id')} is missing the 'invoice' ID.")
            
            invoice = stripe.Invoice.retrieve(invoice_id)
            period_start = str(datetime.fromtimestamp(invoice.period_start).date())
            period_end = str(datetime.fromtimestamp(invoice.period_end).date())
            stripe_price_id = invoice.lines.data[0].price.id if invoice.lines.data else None
            
            # Atomically update the database using our new function
            current_app.logger.info(f"Calling RPC 'handle_new_paid_subscription' for user {uid}")
            supabase.rpc('handle_new_paid_subscription', {
                'p_user_id': uid,
                'p_plan_id': paid_plan_id,
                'p_display_name': display_name,
                'p_stripe_subscription_id': subscription_id,
                'p_stripe_customer_id': customer_id,
                'p_stripe_price_id': stripe_price_id,
                'p_period_start': period_start,
                'p_period_end': period_end
            }).execute()
            current_app.logger.info(f"Successfully processed 'checkout.session.completed' for user {uid}")

        except Exception as e:
            # Log any error, but still return a 200 to Stripe to prevent retries for logic errors.
            current_app.logger.error(f"Error processing 'checkout.session.completed' for user {uid}: {e}", exc_info=True)

    elif event_type == 'invoice.payment_succeeded':
        invoice = data
        customer_id = invoice.get('customer')
        subscription_id = invoice.get('subscription') 
        
        if customer_id and subscription_id:
            try:
                # Extract new period dates from the invoice
                next_period_start = str(datetime.fromtimestamp(invoice.get('period_start')).date())
                next_period_end = str(datetime.fromtimestamp(invoice.get('period_end')).date())

                # Atomically update the database using our renewal function
                current_app.logger.info(f"Calling RPC 'handle_subscription_renewal' for customer {customer_id}")
                supabase.rpc('handle_subscription_renewal', {
                    'p_stripe_customer_id': customer_id,
                    'p_stripe_subscription_id': subscription_id,
                    'p_next_period_start': next_period_start,
                    'p_next_period_end': next_period_end
                }).execute()
                current_app.logger.info(f"Successfully processed 'invoice.payment_succeeded' for customer {customer_id}")

            except Exception as e:
                current_app.logger.error(f"Error processing 'invoice.payment_succeeded' for customer {customer_id}: {e}", exc_info=True)

    elif event_type == 'invoice.payment_failed':
        subscription_id = data.get('subscription')
        if subscription_id:
            supabase.table('user_subscriptions').update({'status': 'past_due'}).eq('stripe_subscription_id', subscription_id).execute()

    elif event_type == 'customer.subscription.deleted':
        subscription = data
        customer_id = subscription.get('customer')
        
        if customer_id:
            sub_res = supabase.table('user_subscriptions').select('id, user_id').eq('stripe_customer_id', customer_id).maybe_single().execute()

            if sub_res.data:
                sub_id = sub_res.data['id']
                uid = sub_res.data['user_id']
                period_start = date.today().replace(day=1)
                period_end = get_last_day_of_month(date.today())
                
                # Downgrade user to the free plan (id=1) and set status to 'free'
                current_app.logger.info(f"Subscription deleted for user {uid}. Downgrading to free plan.")
                supabase.table('user_subscriptions').update({
                    'plan_id': 1,
                    'status': 'free',
                    'stripe_subscription_id': None,
                    'stripe_customer_id': None,
                    'stripe_price_id': None,
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

