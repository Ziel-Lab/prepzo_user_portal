# routes.py
from flask import request, jsonify, current_app, g
from datetime import datetime, date, timedelta, timezone
import stripe
from dateutil.relativedelta import relativedelta
from . import subscription_bp
from app import extensions
from .helpers import get_anniversary_period, require_authentication, get_user_display_name
from postgrest.exceptions import APIError
from types import SimpleNamespace
import json
import hmac
import hashlib

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

        # Step 2: If no subscription exists at all, create everything from scratch using an anniversary period.
        if not subscription:
            current_app.logger.info(f"No subscription found for user {uid}. Provisioning new records.")
            period_start, period_end = get_anniversary_period(g.user.created_at, date.today())
            period_start_dt = datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc)

            # Create the payload for the main subscription table
            user_sub_payload = {
                'user_id': uid, 
                'display_name': display_name,
                'plan_id': 1, # Default to free plan
                'status': 'free',
                'current_period_start': period_start_dt.isoformat(), 
                'current_period_end': str(period_end),
                'started_at': g.user.created_at
            }
            
            # Use UPSERT to prevent race conditions.
            sub_insert_res = supabase.table('user_subscriptions').upsert(
                user_sub_payload, 
                on_conflict='user_id', 
                returning='representation'
            ).execute()
            
            # The initial upsert might not return data if there was a conflict.
            # If so, re-fetch the now-guaranteed-to-exist subscription record.
            if not (sub_insert_res and sub_insert_res.data):
                current_app.logger.warning(f"Upsert for user {uid} returned no data (likely a resolved race condition), re-fetching.")
                sub_res = supabase.table('user_subscriptions').select('*').eq('user_id', uid).maybe_single().execute()
                subscription = sub_res.data if sub_res else None
                if not subscription:
                    return jsonify({"error": "Failed to initialize or retrieve your user profile."}), 500
            else:
                subscription = sub_insert_res.data[0]
                # Manually log the initial 'free' state to the history table.
                history_payload = {
                    'user_id': uid,
                    'display_name': display_name,
                    'plan_id': 1,
                    'status': 'free',
                    'started_at': user_sub_payload.get('current_period_start')
                }
                supabase.table('subscription_histories').insert(history_payload).execute()
                current_app.logger.info(f"Successfully logged initial 'free' subscription history for user {uid}")
        
        # Step 3: Fetch the most recent usage record. check_and_use_feature will create one if needed.
        usage_res = supabase.table('feature_usage') \
            .select('*').eq('user_id', uid).order('period_end', desc=True).limit(1).maybe_single().execute()
        
        subscription['usage'] = usage_res.data if (usage_res and usage_res.data) else {}
            
        # Step 4: Ensure plan details are attached.
        plan_res = supabase.table('subscription_plans').select('*').eq('id', subscription.get('plan_id', 1)).maybe_single().execute()
        subscription['subscription_plans'] = plan_res.data if (plan_res and plan_res.data) else None

        # Step 5: As a final safety net, ensure the 'usage' key is never null.
        if subscription.get('usage') is None:
            subscription['usage'] = {}

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
    Fetches a list of the user's subscription history from the database.
    """
    supabase = extensions.supabase
    uid = g.user.id

    try:
        # Fetch the user's subscription history, ordered by creation date
        # Only show relevant, non-transient statuses to the user.
        history_response = supabase.table('subscription_histories').select('*').eq('user_id', uid).in_('status', ['active', 'free', 'canceling']).order('created_at', desc=True).execute()
        
        return jsonify(history_response.data), 200

    except APIError as e:
        current_app.logger.error(f"DATABASE API_ERROR in /subscription/invoices for user {uid}: {e}", exc_info=True)
        return jsonify({"error": "A database error occurred while fetching your billing history.", "details": str(e.message)}), 500
    except Exception as e:
        current_app.logger.error(f"Subscription history fetching failed for user {uid}: {e}", exc_info=True)
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
            .select('*')\
            .eq('user_id', uid)\
            .single()\
            .execute()

        if not sub_response.data:
            return jsonify({"error": "Subscription not found."}), 404
        
        current_sub = sub_response.data
        stripe_sub_id = current_sub.get('stripe_subscription_id')
        status = current_sub.get('status')

        if status not in ('active', 'processing') or not stripe_sub_id:
            return jsonify({"error": "No active subscription to cancel."}), 400

        # tell Stripe to cancel at period end
        subscription = stripe.Subscription.retrieve(stripe_sub_id)
        subscription.cancel_at_period_end = True
        subscription.save()

        # Update main table
        supabase.table('user_subscriptions').update({
            'status': 'canceling',
            'updated_at': datetime.utcnow().isoformat()
        }).eq('user_id', uid).execute()
        
        # Log to history table
        history_payload = {
            'user_id': uid,
            'display_name': current_sub.get('display_name'),
            'plan_id': current_sub.get('plan_id'),
            'status': 'canceling',
            'stripe_customer_id': current_sub.get('stripe_customer_id'),
            'stripe_subscription_id': stripe_sub_id,
            'stripe_price_id': current_sub.get('stripe_price_id'),
            'started_at': current_sub.get('current_period_start'),
            'next_billing_date': current_sub.get('next_billing_date')
        }
        supabase.table('subscription_histories').insert(history_payload).execute()
        current_app.logger.info(f"Successfully logged 'canceling' subscription history for user {uid}")

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
    """
    stripe.api_key = current_app.config.get("STRIPE_SECRET_API_KEY")
    if not stripe.api_key:
        return jsonify({"error": "This feature is not configured on the server."}), 503

    supabase = extensions.supabase
    uid = g.user.id

    try:
        # Fetch the user's current subscription details
        sub_response = supabase.table('user_subscriptions').select(
            '*'
        ).eq('user_id', uid).single().execute()

        if not sub_response.data:
            return jsonify({"error": "Subscription not found."}), 404

        current_sub = sub_response.data
        stripe_sub_id = current_sub.get('stripe_subscription_id')
        status = current_sub.get('status')

        if status != 'canceling' or not stripe_sub_id:
            return jsonify({"error": "Subscription is not scheduled for cancellation."}), 400

        # Tell Stripe to reactivate
        stripe.Subscription.modify(stripe_sub_id, cancel_at_period_end=False)
        
        # Update main table
        supabase.table('user_subscriptions').update({
            'status': 'active',
            'updated_at': datetime.utcnow().isoformat()
        }).eq('user_id', uid).execute()

        # Log to history table
        history_payload = {
            'user_id': uid,
            'display_name': current_sub.get('display_name'),
            'plan_id': current_sub.get('plan_id'),
            'status': 'active',
            'stripe_customer_id': current_sub.get('stripe_customer_id'),
            'stripe_subscription_id': stripe_sub_id,
            'stripe_price_id': current_sub.get('stripe_price_id'),
            'started_at': current_sub.get('current_period_start'),
            'next_billing_date': current_sub.get('next_billing_date')
        }
        supabase.table('subscription_histories').insert(history_payload).execute()
        current_app.logger.info(f"Successfully logged 'active' (reactivated) subscription history for user {uid}")

        return jsonify({"message": "Subscription reactivated successfully."}), 200

    except Exception as e:
        current_app.logger.error(f"Stripe reactivation failed for user {uid}: {e}", exc_info=True)
        return jsonify({'error': "Could not reactivate subscription."}), 500

@subscription_bp.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """
    Handles incoming webhooks from Stripe to update subscription status in the DB.
    SECURITY: This endpoint validates Stripe signatures and uses admin client to bypass RLS.
    """
    current_app.logger.critical("--- STRIPE WEBHOOK ENDPOINT HIT! ---")

    stripe_webhook_secret = current_app.config.get("STRIPE_WEBHOOK_SECRET")
    stripe.api_key = current_app.config.get("STRIPE_SECRET_API_KEY")
    
    if not stripe_webhook_secret or not stripe.api_key:
        current_app.logger.error("SECURITY ERROR: Stripe webhook secret or API key is not configured. Rejecting webhook.")
        return jsonify({"error": "Webhook not configured"}), 503
        
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    
    # SECURITY: Validate request size to prevent DoS
    if len(payload) > 1024 * 1024:  # 1MB limit
        current_app.logger.error("SECURITY ERROR: Webhook payload too large")
        return jsonify({"error": "Payload too large"}), 413
    
    # SECURITY: Validate signature header format
    if not sig_header or not sig_header.startswith('t='):
        current_app.logger.error("SECURITY ERROR: Invalid or missing Stripe signature header")
        return jsonify({"error": "Invalid signature"}), 400

    # Use admin client for webhook operations (bypasses RLS as needed)
    try:
        supabase = extensions.get_admin_client()
    except RuntimeError as e:
        current_app.logger.error(f"FATAL: Admin Supabase client not available: {e}")
        return jsonify({"error": "Service temporarily unavailable"}), 503

    try:
        # SECURITY: Stripe signature verification
        event = stripe.Webhook.construct_event(
            payload=payload, 
            sig_header=sig_header, 
            secret=stripe_webhook_secret
        )
    except ValueError as e:
        current_app.logger.error(f"SECURITY ERROR: Invalid webhook payload: {e}")
        return jsonify({"error": "Invalid payload"}), 400
    except stripe.error.SignatureVerificationError as e:
        current_app.logger.error(f"SECURITY ERROR: Stripe signature verification failed: {e}")
        return jsonify({"error": "Invalid signature"}), 400

    event_type = event['type']
    data = event['data']['object']
    current_app.logger.info(f"--- STRIPE WEBHOOK: Received event '{event_type}' ---")

    # Input validation for event data
    if not isinstance(data, dict):
        current_app.logger.error("SECURITY ERROR: Invalid event data format")
        return jsonify({"error": "Invalid event data"}), 400

    try:
        if event_type == 'checkout.session.completed':
            session = data
            user_id = session.get('client_reference_id')
            customer_id = session.get('customer')
            subscription_id = session.get('subscription')

            # SECURITY: Validate required fields
            if not all([user_id, customer_id, subscription_id]):
                current_app.logger.error(f"SECURITY WARNING: 'checkout.session.completed' missing required IDs. Session: {session.get('id')}")
                return jsonify(success=True) # Acknowledge the event to prevent retries

            # SECURITY: Validate user_id format (should be UUID)
            if not isinstance(user_id, str) or len(user_id) != 36:
                current_app.logger.error(f"SECURITY ERROR: Invalid user_id format: {user_id}")
                return jsonify({"error": "Invalid user ID"}), 400

            try:
                # Get user's display name for logging purposes (using admin client)
                user_res = supabase.auth.admin.get_user_by_id(user_id)
                display_name = get_user_display_name(user_res.user) if user_res.user else "User"

                # Update the 'processing' subscription record with Stripe IDs
                # Using admin client to bypass RLS as this is a system operation
                update_payload = {
                    'stripe_customer_id': customer_id,
                    'stripe_subscription_id': subscription_id,
                    'display_name': display_name,
                    'updated_at': datetime.utcnow().isoformat()
                }
                
                res = supabase.table('user_subscriptions').update(update_payload).eq('user_id', user_id).execute()

                if not res.data:
                    error_message = f"Webhook 'checkout.session.completed' failed. Could not find a 'processing' user_subscription record for user_id: {user_id} to update."
                    current_app.logger.error(error_message)
                    return jsonify({"status": "error", "message": "User record not found"}), 500

                current_app.logger.info(f"✅ Successfully updated subscription record for user {user_id[:8]}*** with Stripe IDs")
                
                # Log to subscription history for audit trail
                updated_record = res.data[0]
                history_payload = {
                    'user_id': user_id,
                    'display_name': display_name,
                    'plan_id': updated_record.get('plan_id'),
                    'status': 'processing',
                    'stripe_customer_id': customer_id,
                    'stripe_subscription_id': subscription_id,
                    'stripe_price_id': updated_record.get('stripe_price_id'),
                    'started_at': updated_record.get('started_at')
                }
                supabase.table('subscription_histories').insert(history_payload).execute()
                
                return jsonify(success=True), 200
                
            except Exception as e:
                current_app.logger.error(f"Error processing checkout.session.completed for user {user_id[:8]}***: {type(e).__name__}", exc_info=True)
                return jsonify({"error": "Processing error"}), 500

        elif event_type == 'invoice.payment_succeeded':
            invoice = data
            subscription_id = invoice.get('subscription')
            price_id = invoice.get('lines', {}).get('data', [{}])[0].get('price', {}).get('id')
            
            # SECURITY: Validate required fields
            if not subscription_id or not isinstance(subscription_id, str):
                current_app.logger.error("SECURITY ERROR: Invalid subscription_id in invoice.payment_succeeded")
                return jsonify({"error": "Invalid subscription ID"}), 400

            if not price_id:
                current_app.logger.error("SECURITY ERROR: No price_id found in invoice.payment_succeeded")
                return jsonify({"error": "Invalid price ID"}), 400

            try:
                # Find the user by stripe_subscription_id (using admin client)
                user_res = supabase.table('user_subscriptions').select('user_id').eq('stripe_subscription_id', subscription_id).single().execute()
                user_id = user_res.data['user_id']

                # Find the corresponding internal plan using the Stripe price_id
                plan_res = supabase.table('subscription_plans').select('id').eq('stripe_price_id', price_id).single().execute()
                if not plan_res.data:
                    error_message = f"Configuration Error: No plan found for stripe_price_id '{price_id}'. Payment cannot be processed for user {user_id[:8]}***."
                    current_app.logger.error(error_message)
                    return jsonify(error=error_message), 500
                
                new_plan_id = plan_res.data['id']
                
                # Get billing period dates from invoice
                period_start_ts = invoice['period_start']
                period_end_ts = invoice['period_end']
                period_start = datetime.fromtimestamp(period_start_ts, tz=timezone.utc).date()
                period_end = datetime.fromtimestamp(period_end_ts, tz=timezone.utc).date()
                
                # Update subscription to active status (using admin client)
                update_payload = {
                    'plan_id': new_plan_id,
                    'status': 'active',
                    'current_period_start': str(period_start),
                    'current_period_end': str(period_end),
                    'updated_at': datetime.utcnow().isoformat()
                }
                
                res = supabase.table('user_subscriptions').update(update_payload).eq('user_id', user_id).execute()
                
                if res.data:
                    current_app.logger.info(f"✅ Successfully activated subscription for user {user_id[:8]}*** with plan {new_plan_id}")
                    
                    # Log to history
                    updated_record = res.data[0]
                    history_payload = {
                        'user_id': user_id,
                        'display_name': updated_record.get('display_name'),
                        'plan_id': new_plan_id,
                        'status': 'active',
                        'stripe_customer_id': updated_record.get('stripe_customer_id'),
                        'stripe_subscription_id': subscription_id,
                        'stripe_price_id': price_id,
                        'started_at': str(period_start),
                        'next_billing_date': str(period_end)
                    }
                    supabase.table('subscription_histories').insert(history_payload).execute()
                
                return jsonify(success=True), 200
                
            except APIError as e:
                if e.code == 'PGRST116':
                    current_app.logger.warning(f"Webhook 'invoice.payment_succeeded' could not find subscription {subscription_id}. Race condition with 'checkout.session.completed'.")
                    return jsonify(error="Subscription not processed yet, retry later"), 503
                else:
                    raise

        elif event_type == 'customer.subscription.deleted':
            subscription = data
            subscription_id = subscription.get('id')
            
            if subscription_id:
                try:
                    # Find user and downgrade to free plan (using admin client)
                    user_res = supabase.table('user_subscriptions').select('user_id, display_name').eq('stripe_subscription_id', subscription_id).single().execute()
                    
                    if user_res.data:
                        user_id = user_res.data['user_id']
                        display_name = user_res.data['display_name']
                        
                        # Reset to free plan
                        downgrade_payload = {
                            'plan_id': 1,  # Free plan
                            'status': 'free',
                            'stripe_subscription_id': None,
                            'updated_at': datetime.utcnow().isoformat()
                        }
                        
                        supabase.table('user_subscriptions').update(downgrade_payload).eq('user_id', user_id).execute()
                        
                        # Log to history
                        history_payload = {
                            'user_id': user_id,
                            'display_name': display_name,
                            'plan_id': 1,
                            'status': 'free'
                        }
                        supabase.table('subscription_histories').insert(history_payload).execute()
                        
                        current_app.logger.info(f"✅ Successfully downgraded user {user_id[:8]}*** to free plan")
                        
                except Exception as e:
                    current_app.logger.error(f"Error processing subscription deletion: {type(e).__name__}", exc_info=True)

        else:
            # Log unknown event types for monitoring
            current_app.logger.info(f"Unhandled webhook event type: {event_type}")
            
        return jsonify(success=True), 200
            
    except Exception as e:
        current_app.logger.error(f"Unexpected error processing webhook {event_type}: {type(e).__name__}", exc_info=True)
        return jsonify({"error": "Processing error"}), 500

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
            on_conflict='user_id'
        ).execute()

        current_app.logger.info(f"--- DIAGNOSTIC: Database WRITE successful. Response: {response.data} ---")
        return jsonify({"message": "Database write successful.", "data": response.data}), 200

    except APIError as e:
        current_app.logger.error(f"--- DIAGNOSTIC: Database WRITE FAILED with APIError. This strongly suggests a network block on POST/PATCH requests. Details: {e}", exc_info=True)
        return jsonify({"error": "Database write failed.", "details": str(e)}), 500
    except Exception as e:
        current_app.logger.error(f"--- DIAGNOSTIC: Database WRITE FAILED with an unexpected exception. Details: {e}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred during the database write test.", "details": str(e)}), 500

@subscription_bp.route("/config", methods=["GET"])
@require_authentication
def get_config():
    """
    Endpoint for the frontend to get configuration data.
    """
    config = {
        "STRIPE_SECRET_API_KEY": current_app.config.get("STRIPE_SECRET_API_KEY"),
        "STRIPE_WEBHOOK_SECRET": current_app.config.get("STRIPE_WEBHOOK_SECRET"),
        "STRIPE_PAID_PLAN_PRICE_ID_1": current_app.config.get("STRIPE_PAID_PLAN_PRICE_ID_1"),
        "STRIPE_PAID_PLAN_PRICE_ID_2": current_app.config.get("STRIPE_PAID_PLAN_PRICE_ID_2"),
        "FRONTEND_ORIGIN": current_app.config.get("FRONTEND_ORIGIN")
    }
    return jsonify(config), 200

@subscription_bp.route("/create-checkout-session", methods=["POST"])
@require_authentication
def create_checkout_session():
    """
    Creates a Stripe Checkout session. This endpoint is now idempotent and robust.
    It creates or updates a 'processing' subscription record BEFORE redirecting
    the user to Stripe, making our database the source of truth from the start.
    """
    data = request.get_json()
    plan_id = data.get('planId')
    user_id = g.user.id
    
    if not plan_id or not user_id:
        return jsonify(error={'message': 'Missing required parameters: planId or user_id.'}), 400

    # Look up the Stripe Price ID from the server's configuration based on the internal plan_id.
    # This is more secure and reliable than expecting the frontend to provide it.
    # Plan ID 2 = Pro (STRIPE_PAID_PLAN_PRICE_ID_1)
    # Plan ID 3 = Premium (STRIPE_PAID_PLAN_PRICE_ID_2)
    price_id = None
    if plan_id == 2:
        price_id = current_app.config.get("STRIPE_PAID_PLAN_PRICE_ID_1")
    elif plan_id == 3:
        price_id = current_app.config.get("STRIPE_PAID_PLAN_PRICE_ID_2")

    if not price_id:
        current_app.logger.error(f"Could not find a Stripe Price ID for plan_id {plan_id}. Check server environment variables STRIPE_PAID_PLAN_PRICE_ID_1 and STRIPE_PAID_PLAN_PRICE_ID_2.")
        return jsonify(error={'message': f'The payment link for the selected plan is not configured. Please contact support.'}), 500

    supabase = extensions.supabase
    stripe.api_key = current_app.config.get("STRIPE_SECRET_API_KEY")

    try:
        # Step 1: Check for an existing ACTIVE subscription to get the customer_id if it exists.
        # This allows Stripe to apply credits or handle upgrades/downgrades correctly.
        existing_sub_res = supabase.table('user_subscriptions').select('stripe_customer_id').eq('user_id', user_id).in_('status', ['active', 'trialing', 'canceling']).maybe_single().execute()
        
        customer_id = existing_sub_res.data.get('stripe_customer_id') if (existing_sub_res and existing_sub_res.data) else None

        # Step 2: Create or update a 'processing' subscription record.
        # This is the core of the new robust flow. We use upsert to make this idempotent.
        # This record now holds all necessary info *before* we call Stripe.
        processing_sub_payload = {
            'user_id': user_id,
            'plan_id': plan_id,
            'stripe_price_id': price_id,
            'status': 'processing',
            'started_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat(),
            'stripe_customer_id': customer_id # Can be null, that's fine
        }
        
        # We upsert on the user_id. This means a user can only have one subscription record.
        # If they retry payment, this will just update the existing 'processing' record.
        upsert_res = supabase.table('user_subscriptions').upsert(
            processing_sub_payload, 
            on_conflict='user_id',
            returning='minimal' # We don't need the data back, just success.
        ).execute()

        # Minimal validation on the upsert response
        if not upsert_res:
             raise Exception("Upsert operation to create 'processing' subscription failed.")

        # Step 3: Create the Stripe Checkout Session
        checkout_session = stripe.checkout.Session.create(
            customer=customer_id, # Pass existing customer ID if available
            client_reference_id=user_id, # Reliably links the session back to our user
            payment_method_types=['card'],
            line_items=[
                {
                    'price': price_id,
                    'quantity': 1,
                },
            ],
            mode='subscription',
            success_url=current_app.config['STRIPE_SUCCESS_URL'] + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=current_app.config['STRIPE_CANCEL_URL'],
            metadata={
                'user_id': user_id,
                'plan_id': plan_id
            }
        )
        return jsonify(id=checkout_session.id)
    except Exception as e:
        current_app.logger.error(f"Error creating checkout session for user {user_id}: {e}", exc_info=True)
        return jsonify(error={'message': f"An unexpected error occurred: {str(e)}"}), 500

