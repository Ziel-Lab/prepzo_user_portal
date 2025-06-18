# routes.py
from flask import request, jsonify, current_app, g
from datetime import datetime, date, timedelta, timezone
import stripe
from dateutil.relativedelta import relativedelta
from . import subscription_bp
from app import extensions
from .helpers import check_and_use_feature, get_last_day_of_month, require_authentication
from postgrest.exceptions import APIError
from types import SimpleNamespace
import json

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

@subscription_bp.route("/customer-portal", methods=["POST", "OPTIONS"])
@require_authentication
def create_customer_portal_session():
    """
    Creates a Stripe Customer Portal session for the user to manage their subscription.
    """
    stripe.api_key = current_app.config.get("STRIPE_SECRET_API_KEY")
    if not stripe.api_key:
        return jsonify({"error": "This feature is not configured on the server."}), 503

    supabase = extensions.supabase
    uid = g.user.id

    try:
        # Fetch the user's stripe_customer_id
        sub_response = supabase.table('user_subscriptions').select('stripe_customer_id').eq('user_id', uid).single().execute()
        
        if not sub_response.data or not sub_response.data.get('stripe_customer_id'):
            return jsonify({"error": "Stripe customer information not found."}), 404

        stripe_customer_id = sub_response.data['stripe_customer_id']
        
        # Base return URL from config
        frontend_url = current_app.config.get('FRONTEND_ORIGIN')
        if not frontend_url:
             current_app.logger.error("FATAL: FRONTEND_ORIGIN is not configured on the server.")
             return jsonify({"error": "Application is not configured correctly. Unable to determine a return URL."}), 503

        return_url = f"{frontend_url}/dashboard/settings/subscription"

        # For POST requests, allow the frontend to override the return URL.
        # GET requests with bodies are not reliable.
        if request.method == "POST":
            try:
                data = request.get_json()
                if data and 'return_url' in data:
                    # Basic validation to ensure it's a URL within the app's domain
                    if data['return_url'].startswith(frontend_url):
                        return_url = data['return_url']
            except Exception:
                # Ignore if body is not valid json or other parsing issues.
                pass

        portal_session = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=return_url,
        )
        
        return jsonify({"url": portal_session.url}), 200

    except Exception as e:
        current_app.logger.error(f"Stripe customer portal session creation failed for user {uid}: {e}", exc_info=True)
        return jsonify({'error': "Could not create a billing management session."}), 500

@subscription_bp.route("/invoices", methods=["GET"])
@require_authentication
def get_invoices():
    """
    Fetches a list of the user's past invoices from Stripe.
    """
    stripe.api_key = current_app.config.get("STRIPE_SECRET_API_KEY")
    if not stripe.api_key:
        return jsonify({"error": "This feature is not configured on the server."}), 503

    supabase = extensions.supabase
    uid = g.user.id

    try:
        # Fetch the user's stripe_customer_id
        sub_response = supabase.table('user_subscriptions').select('stripe_customer_id').eq('user_id', uid).single().execute()
        
        stripe_customer_id = sub_response.data.get('stripe_customer_id') if sub_response.data else None

        if not stripe_customer_id:
            # If there's no customer ID, they have no invoices. Return empty list.
            return jsonify([]), 200

        # Fetch invoices from Stripe, expanding the charge to get payment details
        invoices = stripe.Invoice.list(customer=stripe_customer_id, limit=24, expand=['data.charge'])
        
        return jsonify(invoices.data), 200

    except APIError as e:
        current_app.logger.error(f"DATABASE API_ERROR in /subscription/invoices for user {uid}: {e}", exc_info=True)
        return jsonify({"error": "A database error occurred while fetching your billing history.", "details": str(e.message)}), 500
    except Exception as e:
        current_app.logger.error(f"Stripe invoice fetching failed for user {uid}: {e}", exc_info=True)
        return jsonify({'error': "Could not retrieve billing history."}), 500

@subscription_bp.route("/stripe/cancel-subscription", methods=["POST", "OPTIONS"])
@require_authentication
def cancel_subscription():
    stripe.api_key = current_app.config.get("STRIPE_SECRET_API_KEY")
    if not stripe.api_key:
        return jsonify({"error": "This feature is not configured on the server."}), 503

    supabase = extensions.supabase
    uid = g.user.id

    try:
        sub_response = supabase\
            .table('user_subscriptions')\
            .select('stripe_subscription_id, status')\
            .eq('user_id', uid)\
            .single()\
            .execute()

        stripe_sub_id = sub_response.data.get('stripe_subscription_id')
        status        = sub_response.data.get('status')

        # allow cancellation if status is active OR processing
        if status not in ('active', 'processing') or not stripe_sub_id:
            return jsonify({"error": "No active subscription to cancel."}), 400

        # tell Stripe to cancel at period end
        subscription = stripe.Subscription.retrieve(stripe_sub_id)
        subscription.cancel_at_period_end = True
        subscription.save()

        # mark us "canceling" locally
        supabase.table('user_subscriptions').update({
            'status': 'canceling',
            'updated_at': datetime.utcnow().isoformat()
        }).eq('user_id', uid).execute()

        return jsonify({
            "message": "Subscription cancellation scheduled successfully."
        }), 200

    except Exception as e:
        current_app.logger.error(f"Stripe cancellation failed: {e}")
        return jsonify({'error': str(e)}), 500

@subscription_bp.route("/stripe/reactivate-subscription", methods=["POST"])
@require_authentication
def reactivate_subscription():
    """
    Allows a user to undo their subscription cancellation before the period ends.
    This simply resets the status to 'active' without changing any dates.
    """
    stripe.api_key = current_app.config.get("STRIPE_SECRET_API_KEY")
    if not stripe.api_key:
        return jsonify({"error": "This feature is not configured on the server."}), 503

    supabase = extensions.supabase
    uid = g.user.id

    try:
        # 1. Fetch the user's current subscription details
        sub_response = supabase.table('user_subscriptions').select(
            'stripe_subscription_id, status'
        ).eq('user_id', uid).single().execute()

        if not sub_response.data:
            return jsonify({"error": "Subscription not found."}), 404

        sub_data = sub_response.data
        stripe_sub_id = sub_data.get('stripe_subscription_id')
        status = sub_data.get('status')

        # 2. Check if the subscription is actually in the 'canceling' state
        if status != 'canceling' or not stripe_sub_id:
            return jsonify({"error": "Subscription is not scheduled for cancellation."}), 400

        # 3. Tell Stripe to reactivate the subscription by clearing the cancellation flag
        stripe.Subscription.modify(
            stripe_sub_id,
            cancel_at_period_end=False
        )

        update_payload = {
            'status': 'active',
            'updated_at': datetime.utcnow().isoformat()
        }
        
        supabase.table('user_subscriptions').update(update_payload).eq('user_id', uid).execute()

        return jsonify({"message": "Subscription reactivated successfully."}), 200

    except Exception as e:
        current_app.logger.error(f"Stripe reactivation failed for user {uid}: {e}", exc_info=True)
        return jsonify({'error': "Could not reactivate subscription."}), 500

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
        uid = session.get('client_reference_id')
        customer_id = session.get('customer')
        subscription_id = session.get('subscription')

        if not all([uid, customer_id, subscription_id]):
            current_app.logger.error(f"Webhook Error: 'checkout.session.completed' is missing required IDs. Session: {session.get('id')}")
            return jsonify(success=True)

        try:
            paid_plan_id = 2 # The ID for your "Pro" plan

            # Fetch the user's auth record to get their correct, up-to-date name.
            auth_user_res = supabase.auth.admin.get_user_by_id(uid)
            display_name = auth_user_res.user.user_metadata.get('full_name') or auth_user_res.user.user_metadata.get('name', 'N/A')

            current_app.logger.info(f"Provisioning Stripe IDs for user {uid} ({display_name}) from checkout session {session.get('id')}.")
            supabase.rpc('provision_stripe_subscription', {
                'p_user_id': uid,
                'p_stripe_customer_id': customer_id,
                'p_stripe_subscription_id': subscription_id,
                'p_plan_id': paid_plan_id,
                'p_display_name': display_name # Pass the correct name to the DB function
            }).execute()
            current_app.logger.info(f"Successfully provisioned Stripe info for user {uid}")

        except Exception as e:
            error_message = f"Webhook processing failed for '{event_type}'. User: {uid}. Error: {e}"
            current_app.logger.error(error_message, exc_info=True)
            return jsonify({"status": "error", "message": str(e)}), 500

    elif event_type == 'invoice.payment_succeeded':
        invoice = data
        customer_id = invoice.get('customer')
        subscription_id = invoice.get('subscription')
        
        if not all([customer_id, subscription_id]):
            current_app.logger.error(f"Webhook Error: 'invoice.payment_succeeded' is missing required IDs. Invoice: {invoice.get('id')}")
            return jsonify(success=True)

        try:
            # The subscription object is the source of truth for the billing period.
            subscription = stripe.Subscription.retrieve(subscription_id)

            # Get the authoritative start and end dates from Stripe.
            # The database function will handle the one-month logic for the end date.
            next_period_start = str(datetime.fromtimestamp(subscription.current_period_start, tz=timezone.utc).date())
            next_period_end = str(datetime.fromtimestamp(subscription.current_period_end, tz=timezone.utc).date())

            current_app.logger.info(f"Activating subscription for customer {customer_id} from invoice {invoice.get('id')}.")
            supabase.rpc('activate_subscription_from_invoice', {
                'p_stripe_customer_id': customer_id,
                'p_stripe_subscription_id': subscription_id,
                'p_next_period_start': next_period_start,
                'p_next_period_end': next_period_end # NOTE: This value is now ignored by the DB function.
            }).execute()
            current_app.logger.info(f"Successfully activated subscription for customer {customer_id}")

        except Exception as e:
            error_message = f"Webhook processing failed for '{event_type}'. Customer: {customer_id}. Error: {e}"
            current_app.logger.error(error_message, exc_info=True)
            return jsonify({"status": "error", "message": str(e)}), 500

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

