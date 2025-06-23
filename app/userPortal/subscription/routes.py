# routes.py
from flask import request, jsonify, current_app, g
from datetime import datetime, date, timedelta, timezone
import stripe
from dateutil.relativedelta import relativedelta
from . import subscription_bp
from app import extensions
from .helpers import check_and_use_feature, get_last_day_of_month, require_authentication, get_first_day_of_month, get_user_display_name
from postgrest.exceptions import APIError
from types import SimpleNamespace
import json

def _create_and_get_usage_record(supabase, uid, plan_id, display_name):
    """Helper to create a new usage record for the current month."""
    period_start = get_first_day_of_month(date.today())
    period_end = get_last_day_of_month(date.today())
    
    current_app.logger.info(f"Creating new usage record for user {uid} for period {period_start}-{period_end}.")
    
    usage_insert_res = supabase.table('feature_usage').insert({
        'user_id': uid,
        'plan_id': plan_id,
        'period_start': str(period_start),
        'period_end': str(period_end),
        'display_name': display_name
    }, returning='representation').execute()

    # The 'insert' command returns a list, so we safely access the first element.
    return usage_insert_res.data[0] if (usage_insert_res and usage_insert_res.data) else None

@subscription_bp.route("/status", methods=["GET", "OPTIONS"])
@require_authentication
def get_subscription_status():
    """
    Endpoint for the frontend to get a user's subscription and usage status.
    It robustly handles creating records for new users or fixing inconsistent data.
    """
    supabase = extensions.supabase
    uid = g.user.id
    display_name = get_user_display_name(g.user)

    try:
        # Step 1: Check for the main subscription record.
        sub_res = supabase.table('user_subscriptions').select('*').eq('user_id', uid).maybe_single().execute()
        subscription = sub_res.data if (sub_res and sub_res.data) else None

        # Step 2: If no subscription exists at all, create everything from scratch.
        if not subscription:
            current_app.logger.info(f"No subscription found for user {uid}. Provisioning new records.")
            period_start = get_first_day_of_month(date.today())
            period_end = get_last_day_of_month(date.today())

            # Create the main subscription record (defaults to free plan)
            sub_insert_res = supabase.table('user_subscriptions').insert({
                'user_id': uid, 'display_name': display_name,
                'current_period_start': str(period_start), 'current_period_end': str(period_end)
            }, returning='representation').execute()

            if not (sub_insert_res and sub_insert_res.data):
                return jsonify({"error": "Failed to initialize your user profile."}), 500
            
            subscription = sub_insert_res.data[0]
            
            # Now create the corresponding usage record for the new subscription.
            usage_record = _create_and_get_usage_record(supabase, uid, subscription.get('plan_id', 1), display_name)
            subscription['usage'] = usage_record

        # Step 3: If a subscription exists, ensure it has a corresponding usage record.
        else:
            usage_res = supabase.table('feature_usage') \
                .select('*').eq('user_id', uid).order('period_end', desc=True).limit(1).maybe_single().execute()
            
            usage_record = usage_res.data if (usage_res and usage_res.data) else None

            # If no usage record is found for an existing subscription, create one.
            if not usage_record:
                usage_record = _create_and_get_usage_record(supabase, uid, subscription.get('plan_id', 1), display_name)
            
            subscription['usage'] = usage_record
            
        # Step 4: Ensure plan details are attached.
        plan_res = supabase.table('subscription_plans').select('*').eq('id', subscription.get('plan_id', 1)).maybe_single().execute()
        subscription['subscription_plans'] = plan_res.data if (plan_res and plan_res.data) else None

        # Step 5: As a final safety net, ensure the 'usage' key is never null.
        if subscription.get('usage') is None:
            subscription['usage'] = {'resume_count': 0, 'cover_letter_count': 0, 'linkedin_optimize_count': 0, 'job_search_results_count': 0}

        current_app.logger.info(f"--- FINAL SUBSCRIPTION DATA SENT TO FRONTEND ---")
        current_app.logger.info(json.dumps(subscription, indent=2, default=str))
        return jsonify(subscription), 200
        
    except Exception as e:
        current_app.logger.error(f"An unexpected error occurred in /subscription/status: {e}", exc_info=True)
        return jsonify({"error": "An unexpected server error occurred."}), 500

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
    current_app.logger.critical("--- STRIPE WEBHOOK ENDPOINT HIT! ---")

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
            # Fetch the user from Supabase auth to get their display name, as requested.
            user_res = supabase.auth.admin.get_user_by_id(uid)
            display_name = get_user_display_name(user_res.user)

            current_app.logger.info(f"Provisioning Stripe IDs and display name '{display_name}' for user {uid} from session {session.get('id')}. Waiting for payment success to activate.")
            update_payload = {
                'stripe_customer_id': customer_id,
                'stripe_subscription_id': subscription_id,
                'status': 'processing', # Mark as processing until first payment succeeds
                'display_name': display_name,
                'updated_at': datetime.utcnow().isoformat()
            }
            supabase.table('user_subscriptions').update(update_payload).eq('user_id', uid).execute()
            current_app.logger.info(f"Successfully provisioned Stripe IDs for user {uid}")
        except Exception as e:
            error_message = f"Webhook processing failed for '{event_type}'. User: {uid}. Error: {e}"
            current_app.logger.error(error_message, exc_info=True)
            return jsonify({"status": "error", "message": str(e)}), 500

    elif event_type == 'invoice.payment_succeeded':
        invoice = data
        customer_id = invoice.get('customer')
        subscription_id = invoice.get('subscription')
        billing_reason = invoice.get('billing_reason')
        
        # This webhook can fire for one-off payments. We only care about activating/renewing subscriptions.
        if not customer_id or not subscription_id or billing_reason not in ['subscription_cycle', 'subscription_create']:
            current_app.logger.info(f"Ignoring 'invoice.payment_succeeded' for non-subscription event. Reason: {billing_reason}")
            return jsonify(success=True)

        try:
            # The invoice object from the webhook has all the info we need.
            # No need for an extra API call to Stripe.
            price_id = invoice['lines']['data'][0]['price']['id']

            # Per user request, hardcode the plan_id to 2 for any successful payment.
            plan_id = 2
            plan_name = "Pro" # Assuming plan 2 is the paid plan
            current_app.logger.info(f"Activating '{plan_name}' plan (ID: {plan_id}) for customer {customer_id}.")
            
            # Prepare the update payload for our database
            period_start = datetime.fromtimestamp(invoice.period_start, tz=timezone.utc)
            period_end = datetime.fromtimestamp(invoice.period_end, tz=timezone.utc)
            
            # The subscription_id from the invoice might be new if the payment_succeeded event
            # arrives before checkout_completed. We'll update based on customer_id, which should be stable.
            update_payload = {
                'plan_id': plan_id,
                'status': 'active',
                'stripe_subscription_id': subscription_id, # Ensure this is updated
                'stripe_price_id': price_id,
                'current_period_start': str(period_start.date()),
                'current_period_end': str(period_end.date()),
                'next_billing_date': str(period_end.date()),
                'updated_at': datetime.utcnow().isoformat()
            }

            # Update the user's subscription record using the CUSTOMER ID as the key
            supabase.table('user_subscriptions').update(update_payload).eq('stripe_customer_id', customer_id).execute()
            
            # Now fetch the user details using the same customer ID to create the usage record
            user_res = supabase.table('user_subscriptions').select('user_id, display_name').eq('stripe_customer_id', customer_id).single().execute()
            if user_res.data:
                uid = user_res.data['user_id']
                display_name = user_res.data['display_name']
                _create_and_get_usage_record(supabase, uid, plan_id, display_name)
                current_app.logger.info(f"Successfully created new usage record for user {uid} for plan '{plan_name}'.")

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

