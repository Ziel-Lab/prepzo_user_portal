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
import uuid

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
            try:
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
                except APIError as e:
                    if e.code == 'PGRST116': # "JSON object requested, multiple (or no) rows returned"
                        current_app.logger.warning(f"Webhook 'invoice.payment_succeeded' could not find subscription {subscription_id}. This is likely a race condition with 'checkout.session.completed'. Asking Stripe to retry.")
                        return jsonify(error="Subscription not processed yet, retry later"), 503
                    else:
                        raise # Re-raise other API errors

                try:
                    user_id = user_res.data['user_id']
                except (KeyError, TypeError) as e:
                    current_app.logger.error(f"Webhook 'invoice.payment_succeeded' could not extract user_id from subscription {subscription_id}. Error: {e}")
                    return jsonify(error="Invalid subscription data"), 400

                # Find the corresponding internal plan in our database using the Stripe price_id from the invoice.
                # This is the correct, database-driven way to link a Stripe payment to an internal plan.
                plan_res = supabase.table('subscription_plans').select('id').eq('stripe_price_id', price_id).single().execute()

                if not (plan_res and plan_res.data):
                    # This is a critical configuration error. A payment succeeded for a Stripe Price ID
                    # that does not exist in our subscription_plans table.
                    error_message = f"Configuration Error: No plan found in the database for stripe_price_id '{price_id}'. Payment cannot be processed for user {user_id}."
                    current_app.logger.error(error_message)
                    return jsonify(error=error_message), 500
                
                new_plan_id = plan_res.data['id']
                
                # Get dates from the invoice object
                period_start_ts = invoice['period_start']
                period_end_ts = invoice['period_end']
                started_at = datetime.fromtimestamp(period_start_ts, tz=timezone.utc)
                ends_at = datetime.fromtimestamp(period_end_ts, tz=timezone.utc)

                # Update user's current subscription state in the user_subscriptions table
                update_payload = {
                    'status': 'active',
                    'plan_id': new_plan_id,
                    'current_period_start': started_at.isoformat(),
                    'current_period_end': ends_at.isoformat(),
                    'next_billing_date': ends_at.isoformat(),
                    'stripe_price_id': price_id,
                }
                supabase.table('user_subscriptions').update(update_payload).eq('user_id', user_id).execute()

                # === NEW: Update feature_usage table with new plan_id ===
                try:
                    # Get the user's most recent feature_usage record
                    usage_res = supabase.table('feature_usage') \
                        .select('*') \
                        .eq('user_id', user_id) \
                        .order('period_end', desc=True) \
                        .limit(1) \
                        .maybe_single() \
                        .execute()
                    
                    usage_record = usage_res.data if usage_res else None
                    today = date.today()
                    
                    if usage_record and usage_record.get('period_end'):
                        # Check if the usage record is for the current period (not expired)
                        usage_period_end = datetime.strptime(usage_record['period_end'], '%Y-%m-%d').date()
                        is_current_period = today <= usage_period_end
                        
                        if is_current_period and usage_record.get('plan_id') != new_plan_id:
                            # Update existing current period record with new plan_id
                            supabase.table('feature_usage') \
                                .update({'plan_id': new_plan_id}) \
                                .eq('user_id', user_id) \
                                .eq('period_start', usage_record['period_start']) \
                                .execute()
                            current_app.logger.info(f"Webhook: Updated feature_usage plan_id from {usage_record.get('plan_id')} to {new_plan_id} for user {user_id}")
                        elif not is_current_period:
                            # Period expired, create new record for current period with new plan_id
                            # Get user info for period calculation
                            user_res = supabase.auth.admin.get_user_by_id(user_id)
                            if user_res.user:
                                period_start, period_end = get_anniversary_period(user_res.user.created_at, today)
                                display_name = get_user_display_name(user_res.user)
                                
                                new_usage_payload = {
                                    'user_id': user_id,
                                    'plan_id': new_plan_id,
                                    'display_name': display_name,
                                    'period_start': str(period_start),
                                    'period_end': str(period_end)
                                }
                                
                                # Carry over lifetime counts from expired record
                                for key, value in usage_record.items():
                                    if key.endswith('_lifetime_count'):
                                        new_usage_payload[key] = value or 0
                                        # Reset corresponding period count
                                        period_key = key.replace('_lifetime_count', '_period_count')
                                        new_usage_payload[period_key] = 0
                                
                                supabase.table('feature_usage').upsert(
                                    new_usage_payload,
                                    on_conflict='user_id',
                                    returning='minimal'
                                ).execute()
                                current_app.logger.info(f"Webhook: Created new feature_usage record with plan_id {new_plan_id} for user {user_id} (period expired)")
                        else:
                            # No usage record exists, create one for the current period
                            user_res = supabase.auth.admin.get_user_by_id(user_id)
                            if user_res.user:
                                period_start, period_end = get_anniversary_period(user_res.user.created_at, today)
                                display_name = get_user_display_name(user_res.user)
                                
                                new_usage_payload = {
                                    'user_id': user_id,
                                    'plan_id': new_plan_id,
                                    'display_name': display_name,
                                    'period_start': str(period_start),
                                    'period_end': str(period_end)
                                }
                                
                                supabase.table('feature_usage').upsert(
                                    new_usage_payload,
                                    on_conflict='user_id',
                                    returning='minimal'
                                ).execute()
                                current_app.logger.info(f"Webhook: Created initial feature_usage record with plan_id {new_plan_id} for user {user_id}")
                                
                except Exception as feature_usage_error:
                    # Don't fail the entire webhook if feature_usage update fails
                    # This ensures payment processing completes even if feature tracking has issues
                    current_app.logger.error(f"Webhook: Failed to update feature_usage for user {user_id} after successful payment. Error: {feature_usage_error}")
                # === END: Feature usage update ===

                # Log this event to the history table
                history_payload = {
                    'user_id': user_id,
                    'plan_id': new_plan_id,
                    'status': 'active',
                    'stripe_subscription_id': subscription_id,
                    'started_at': started_at.isoformat(),
                    'hosted_invoice_url': invoice.get('hosted_invoice_url')
                }
                supabase.table('subscription_histories').insert(history_payload).execute()

                current_app.logger.info(f"Successfully logged 'active' (payment succeeded) subscription for user {user_id}")

            except Exception as e:
                error_message = f"Webhook processing failed for '{event_type}'. Customer: {customer_id}. Error: {e}"
                current_app.logger.error(error_message, exc_info=True)
                return jsonify(error=error_message), 500

        elif event_type == 'customer.subscription.updated':
            subscription = data
            subscription_id = subscription.get('id')
            customer_id = subscription.get('customer')
            status = subscription.get('status')
            current_app.logger.info(f"Webhook: 'customer.subscription.updated' for sub_id {subscription_id} with status '{status}'")

            if not all([subscription_id, customer_id, status]):
                current_app.logger.error(f"Webhook Error: 'customer.subscription.updated' is missing required IDs. Sub ID: {subscription_id}")
                return jsonify(success=True)

            try:
                # Find the user by their Stripe subscription ID.
                sub_res = supabase.table('user_subscriptions').select('user_id, display_name').eq('stripe_subscription_id', subscription_id).single().execute()
                user_id = sub_res.data['user_id']
                display_name = sub_res.data['display_name']

                if not user_id:
                    raise Exception(f"No user found for subscription_id {subscription_id}")

                # Extract the price ID from the first line item.
                if not subscription.get('items') or not subscription['items'].get('data'):
                    raise Exception(f"Subscription {subscription_id} has no line items.")
                
                price_id = subscription['items']['data'][0]['price']['id']
                
                # Find the corresponding internal plan ID from our database.
                plan_res = supabase.table('subscription_plans').select('id').eq('stripe_price_id', price_id).single().execute()
                if not plan_res.data:
                    raise Exception(f"Configuration Error: No plan found for stripe_price_id '{price_id}'.")
                
                new_plan_id = plan_res.data['id']
                
                # Get period dates from the subscription object.
                period_start_ts = subscription['current_period_start']
                period_end_ts = subscription['current_period_end']
                started_at = datetime.fromtimestamp(period_start_ts, tz=timezone.utc)
                ends_at = datetime.fromtimestamp(period_end_ts, tz=timezone.utc)
                
                # --- NEW: Fetch the invoice to get the hosted_invoice_url ---
                hosted_invoice_url = None
                latest_invoice_id = subscription.get('latest_invoice')
                if latest_invoice_id:
                    try:
                        invoice = stripe.Invoice.retrieve(latest_invoice_id)
                        hosted_invoice_url = invoice.get('hosted_invoice_url')
                    except Exception as e:
                        current_app.logger.error(f"Failed to retrieve invoice {latest_invoice_id} for user {user_id}. Error: {e}")
                # --- END NEW ---

                # Update user's current subscription state.
                update_payload = {
                    'status': status,
                    'plan_id': new_plan_id,
                    'current_period_start': started_at.isoformat(),
                    'current_period_end': ends_at.isoformat(),
                    'next_billing_date': ends_at.isoformat(),
                    'stripe_price_id': price_id,
                    'updated_at': datetime.utcnow().isoformat()
                }
                supabase.table('user_subscriptions').update(update_payload).eq('user_id', user_id).execute()

                # === NEW: Update feature_usage table with new plan_id ===
                try:
                    # Get the user's most recent feature_usage record
                    usage_res = supabase.table('feature_usage') \
                        .select('*') \
                        .eq('user_id', user_id) \
                        .order('period_end', desc=True) \
                        .limit(1) \
                        .maybe_single() \
                        .execute()
                    
                    usage_record = usage_res.data if usage_res else None
                    today = date.today()
                    
                    if usage_record and usage_record.get('period_end'):
                        # Check if the usage record is for the current period (not expired)
                        usage_period_end = datetime.strptime(usage_record['period_end'], '%Y-%m-%d').date()
                        is_current_period = today <= usage_period_end
                        
                        if is_current_period and usage_record.get('plan_id') != new_plan_id:
                            # Update existing current period record with new plan_id
                            supabase.table('feature_usage') \
                                .update({'plan_id': new_plan_id}) \
                                .eq('user_id', user_id) \
                                .eq('period_start', usage_record['period_start']) \
                                .execute()
                            current_app.logger.info(f"Webhook (subscription.updated): Updated feature_usage plan_id from {usage_record.get('plan_id')} to {new_plan_id} for user {user_id}")
                        elif not is_current_period:
                            # Period expired, create new record for current period with new plan_id
                            # Get user info for period calculation
                            user_res = supabase.auth.admin.get_user_by_id(user_id)
                            if user_res.user:
                                period_start, period_end = get_anniversary_period(user_res.user.created_at, today)
                                
                                new_usage_payload = {
                                    'user_id': user_id,
                                    'plan_id': new_plan_id,
                                    'display_name': display_name,
                                    'period_start': str(period_start),
                                    'period_end': str(period_end)
                                }
                                
                                # Carry over lifetime counts from expired record
                                for key, value in usage_record.items():
                                    if key.endswith('_lifetime_count'):
                                        new_usage_payload[key] = value or 0
                                        # Reset corresponding period count
                                        period_key = key.replace('_lifetime_count', '_period_count')
                                        new_usage_payload[period_key] = 0
                                
                                supabase.table('feature_usage').upsert(
                                    new_usage_payload,
                                    on_conflict='user_id',
                                    returning='minimal'
                                ).execute()
                                current_app.logger.info(f"Webhook (subscription.updated): Created new feature_usage record with plan_id {new_plan_id} for user {user_id} (period expired)")
                        else:
                            # No usage record exists, create one for the current period
                            user_res = supabase.auth.admin.get_user_by_id(user_id)
                            if user_res.user:
                                period_start, period_end = get_anniversary_period(user_res.user.created_at, today)
                                
                                new_usage_payload = {
                                    'user_id': user_id,
                                    'plan_id': new_plan_id,
                                    'display_name': display_name,
                                    'period_start': str(period_start),
                                    'period_end': str(period_end)
                                }
                                
                                supabase.table('feature_usage').upsert(
                                    new_usage_payload,
                                    on_conflict='user_id',
                                    returning='minimal'
                                ).execute()
                                current_app.logger.info(f"Webhook (subscription.updated): Created initial feature_usage record with plan_id {new_plan_id} for user {user_id}")
                                
                except Exception as feature_usage_error:
                    # Don't fail the entire webhook if feature_usage update fails
                    # This ensures payment processing completes even if feature tracking has issues
                    current_app.logger.error(f"Webhook (subscription.updated): Failed to update feature_usage for user {user_id} after subscription update. Error: {feature_usage_error}")
                # === END: Feature usage update ===

                # Log this update to the history table for auditing.
                history_payload = {
                    'user_id': user_id,
                    'display_name': display_name,
                    'plan_id': new_plan_id,
                    'status': status,
                    'stripe_subscription_id': subscription_id,
                    'stripe_price_id': price_id,
                    'started_at': started_at.isoformat(),
                    'hosted_invoice_url': hosted_invoice_url # Add the URL here
                }
                supabase.table('subscription_histories').insert(history_payload).execute()

                current_app.logger.info(f"Successfully processed 'customer.subscription.updated' for user {user_id}, plan_id {new_plan_id}.")

            except Exception as e:
                error_message = f"Webhook processing failed for '{event_type}'. Subscription: {subscription_id}. Error: {e}"
                current_app.logger.error(error_message, exc_info=True)
                return jsonify(error=error_message), 500

        elif event_type == 'invoice.payment_failed':
            invoice = data
            customer_id = invoice.get('customer')
            subscription_id = invoice.get('subscription')
            billing_reason = invoice.get('billing_reason')

            # On failure, we just log it for now. We can add more robust handling later,
            # like emailing the user.
            current_app.logger.warning(f"Webhook: 'invoice.payment_failed' for subscription {subscription_id}, customer {customer_id}. Reason: {billing_reason}")

        elif event_type == 'customer.subscription.deleted':
            # Handles subscription cancellations at the end of the billing period
            subscription = data
            subscription_id = subscription.get('id')
            customer_id = subscription.get('customer')

            if not subscription_id:
                return jsonify(status="error", message="No subscription id provided"), 400

            if customer_id:
                # We need user_id and created_at for the new period
                user_res = supabase.table('user_subscriptions').select('id, user_id, display_name').eq('stripe_customer_id', customer_id).maybe_single().execute()

                if user_res.data:
                    sub_id = user_res.data['id']
                    uid = user_res.data['user_id']
                    display_name = user_res.data['display_name']
                    
                    auth_user_res = supabase.auth.admin.get_user_by_id(uid)
                    if not auth_user_res.user:
                        period_start = date.today().replace(day=1)
                        period_end = period_start + relativedelta(months=1) - relativedelta(days=1)
                    else:
                        period_start, period_end = get_anniversary_period(auth_user_res.user.created_at, date.today())
                    
                    period_start_dt = datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc)

                    # Update main table
                    update_payload = {
                        'plan_id': 1, 'status': 'free', 'stripe_subscription_id': None,
                        'stripe_customer_id': None, 'stripe_price_id': None,
                        'current_period_start': period_start_dt.isoformat(),
                        'current_period_end': str(period_end), 'next_billing_date': None,
                        'updated_at': datetime.utcnow().isoformat()
                    }
                    supabase.table('user_subscriptions').update(update_payload).eq('id', sub_id).execute()

                    # === NEW: Update feature_usage table to free plan ===
                    try:
                        # Get the user's most recent feature_usage record
                        usage_res = supabase.table('feature_usage') \
                            .select('*') \
                            .eq('user_id', uid) \
                            .order('period_end', desc=True) \
                            .limit(1) \
                            .maybe_single() \
                            .execute()
                        
                        usage_record = usage_res.data if usage_res else None
                        today = date.today()
                        
                        if usage_record and usage_record.get('period_end'):
                            # Check if the usage record is for the current period (not expired)
                            usage_period_end = datetime.strptime(usage_record['period_end'], '%Y-%m-%d').date()
                            is_current_period = today <= usage_period_end
                            
                            if is_current_period and usage_record.get('plan_id') != 1:
                                # Update existing current period record to free plan
                                supabase.table('feature_usage') \
                                    .update({'plan_id': 1}) \
                                    .eq('user_id', uid) \
                                    .eq('period_start', usage_record['period_start']) \
                                    .execute()
                                current_app.logger.info(f"Webhook (subscription.deleted): Updated feature_usage plan_id from {usage_record.get('plan_id')} to 1 (free) for user {uid}")
                            elif not is_current_period:
                                # Period expired, create new record for current period with free plan
                                new_usage_payload = {
                                    'user_id': uid,
                                    'plan_id': 1,  # Free plan
                                    'display_name': display_name,
                                    'period_start': str(period_start),
                                    'period_end': str(period_end)
                                }
                                
                                # Carry over lifetime counts from expired record
                                for key, value in usage_record.items():
                                    if key.endswith('_lifetime_count'):
                                        new_usage_payload[key] = value or 0
                                        # Reset corresponding period count
                                        period_key = key.replace('_lifetime_count', '_period_count')
                                        new_usage_payload[period_key] = 0
                                
                                supabase.table('feature_usage').upsert(
                                    new_usage_payload,
                                    on_conflict='user_id',
                                    returning='minimal'
                                ).execute()
                                current_app.logger.info(f"Webhook (subscription.deleted): Created new feature_usage record with plan_id 1 (free) for user {uid} (period expired)")
                        else:
                            # No usage record exists, create one for the current period with free plan
                            new_usage_payload = {
                                'user_id': uid,
                                'plan_id': 1,  # Free plan
                                'display_name': display_name,
                                'period_start': str(period_start),
                                'period_end': str(period_end)
                            }
                            
                            supabase.table('feature_usage').upsert(
                                new_usage_payload,
                                on_conflict='user_id',
                                returning='minimal'
                            ).execute()
                            current_app.logger.info(f"Webhook (subscription.deleted): Created initial feature_usage record with plan_id 1 (free) for user {uid}")
                            
                    except Exception as feature_usage_error:
                        # Don't fail the entire webhook if feature_usage update fails
                        current_app.logger.error(f"Webhook (subscription.deleted): Failed to update feature_usage for user {uid} after subscription deletion. Error: {feature_usage_error}")
                    # === END: Feature usage update ===

                    # Log to history table
                    history_payload = {
                        'user_id': uid, 'display_name': display_name,
                        'plan_id': 1, 'status': 'free',
                        'started_at': period_start_dt.isoformat(),
                    }
                    supabase.table('subscription_histories').insert(history_payload).execute()
                    current_app.logger.info(f"Successfully logged 'free' (subscription deleted) history for user {uid}")

    except Exception as e:
        current_app.logger.error(f"Unexpected error in webhook processing: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

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

@subscription_bp.route("/mock-interviews", methods=["GET", "OPTIONS"])
@require_authentication
def get_user_mock_interviews():
    """
    Get user's mock interview attempts with their current status and feedback
    """
    supabase = extensions.supabase
    uid = g.user.id
    
    try:
        # Get user's mock interview attempts
        result = supabase.table('mock_interview_attempts').select('*')\
            .eq('mock_interview_id', uid)\
            .order('created_at', desc=True)\
            .execute()
        
        interviews = result.data if result.data else []
        
        # Process and format the data for frontend
        formatted_interviews = []
        for interview in interviews:
            formatted_interview = {
                'id': interview['id'],
                'attempt_number': interview['attempt_number'],
                'status': interview['status'],
                'started_at': interview['started_at'],
                'completed_at': interview['completed_at'],
                'duration_minutes': interview.get('actual_duration_minutes'),
                'evaluation_score': interview.get('evaluation_score'),
                'has_feedback': interview['status'] == 'PROCESSED' and interview.get('feedback'),
                'created_at': interview['created_at']
            }
            formatted_interviews.append(formatted_interview)
        
        return jsonify({
            'interviews': formatted_interviews,
            'total_attempts': len(interviews)
        }), 200
        
    except Exception as e:
        current_app.logger.error(f"Failed to get mock interviews for user {uid}: {e}", exc_info=True)
        return jsonify({'error': 'Failed to retrieve interview history'}), 500

@subscription_bp.route("/mock-interviews/<attempt_id>/feedback", methods=["GET", "OPTIONS"])
@require_authentication  
def get_interview_feedback(attempt_id):
    """
    Get detailed feedback for a specific interview attempt
    """
    supabase = extensions.supabase
    uid = g.user.id
    
    try:
        # Get the specific interview attempt
        result = supabase.table('mock_interview_attempts').select('*')\
            .eq('id', attempt_id)\
            .eq('mock_interview_id', uid)\
            .single()\
            .execute()
        
        if not result.data:
            return jsonify({'error': 'Interview attempt not found'}), 404
        
        interview = result.data
        
        # Check if feedback is available
        if interview['status'] != 'PROCESSED' or not interview.get('feedback'):
            return jsonify({
                'status': interview['status'],
                'feedback_ready': False,
                'message': 'Feedback is still being processed. Please check back in a few minutes.'
            }), 202
        
        # Parse and format feedback JSON
        try:
            feedback_data = json.loads(interview['feedback']) if isinstance(interview['feedback'], str) else interview['feedback']
        except json.JSONDecodeError:
            current_app.logger.error(f"Invalid feedback JSON for interview {attempt_id}")
            return jsonify({'error': 'Feedback data is corrupted'}), 500
        
        # Format feedback for motivational display
        formatted_feedback = {
            'interview_info': {
                'id': interview['id'],
                'attempt_number': interview['attempt_number'], 
                'completed_at': interview['completed_at'],
                'duration_minutes': interview.get('actual_duration_minutes'),
                'room_name': interview.get('room_name')
            },
            'overall_score': {
                'score': feedback_data.get('Score', 'N/A'),
                'out_of': '10'  # Assuming 1-10 scale, adjust as needed
            },
            'strengths': feedback_data.get('Strengths of the interview', ''),
            'opportunities': feedback_data.get('Opportunities of the interview', ''),
            'areas_for_improvement': feedback_data.get('Weaknesses of the interview', ''),
            'improvement_tips': feedback_data.get('How can questions be answered better', ''),
            'practice_questions': feedback_data.get('additional_questions_and_answers', ''),
            'threats': feedback_data.get('Threats of the interview', ''),
            'feedback_ready': True,
            'processed_at': interview.get('updated_at')
        }
        
        return jsonify(formatted_feedback), 200
        
    except Exception as e:
        current_app.logger.error(f"Failed to get feedback for interview {attempt_id}: {e}", exc_info=True)
        return jsonify({'error': 'Failed to retrieve interview feedback'}), 500

@subscription_bp.route("/mock-interviews/<attempt_id>/status", methods=["GET", "OPTIONS"])
@require_authentication
def get_interview_status(attempt_id):
    """
    Check the processing status of a specific interview attempt
    Useful for polling until feedback is ready
    """
    supabase = extensions.supabase
    uid = g.user.id
    
    try:
        result = supabase.table('mock_interview_attempts').select('id, status, evaluation_score, updated_at')\
            .eq('id', attempt_id)\
            .eq('mock_interview_id', uid)\
            .single()\
            .execute()
        
        if not result.data:
            return jsonify({'error': 'Interview attempt not found'}), 404
        
        interview = result.data
        
        status_info = {
            'id': interview['id'],
            'status': interview['status'],
            'feedback_ready': interview['status'] == 'PROCESSED',
            'last_updated': interview['updated_at'],
            'evaluation_score': interview.get('evaluation_score')
        }
        
        # Add estimated processing time for user experience
        if interview['status'] == 'completed':
            status_info['estimated_completion'] = 'Processing typically takes 2-5 minutes'
        
        return jsonify(status_info), 200
        
    except Exception as e:
        current_app.logger.error(f"Failed to get status for interview {attempt_id}: {e}", exc_info=True)
        return jsonify({'error': 'Failed to check interview status'}), 500

@subscription_bp.route("/mock-interviews/start-session", methods=["POST", "OPTIONS"])
@require_authentication
def start_mock_interview_session():
    """
    Create a new mock interview attempt record when user starts an interview
    """
    supabase = extensions.supabase
    uid = g.user.id
    
    try:
        data = request.get_json()
        interview_type = data.get('interview_type', 'general')
        room_name = data.get('room_name')
        
        if not room_name:
            return jsonify({'error': 'Room name is required'}), 400
        
        # Get user's next attempt number
        attempts_result = supabase.table('mock_interview_attempts').select('attempt_number')\
            .eq('mock_interview_id', uid)\
            .order('attempt_number', desc=True)\
            .limit(1)\
            .execute()
        
        next_attempt = 1
        if attempts_result.data:
            next_attempt = attempts_result.data[0]['attempt_number'] + 1
        
        # Create new attempt record
        attempt_data = {
            'id': str(uuid.uuid4()),
            'mock_interview_id': uid,
            'attempt_number': next_attempt,
            'room_name': room_name,
            'status': 'active',
            'started_at': datetime.now(timezone.utc).isoformat(),
            'created_at': datetime.now(timezone.utc).isoformat()
        }
        
        result = supabase.table('mock_interview_attempts').insert(attempt_data).execute()
        
        if result.data:
            return jsonify({
                'attempt_id': result.data[0]['id'],
                'attempt_number': next_attempt,
                'status': 'active',
                'room_name': room_name
            }), 201
        else:
            return jsonify({'error': 'Failed to create interview attempt'}), 500
        
    except Exception as e:
        current_app.logger.error(f"Failed to start mock interview session for user {uid}: {e}", exc_info=True)
        return jsonify({'error': 'Failed to start interview session'}), 500

@subscription_bp.route("/mock-interviews/<attempt_id>/complete", methods=["POST", "OPTIONS"])
@require_authentication
def complete_mock_interview_session(attempt_id):
    """
    Mark an interview attempt as completed, triggering N8N processing
    """
    supabase = extensions.supabase
    uid = g.user.id
    
    try:
        data = request.get_json()
        transcript_data = data.get('transcript', {})
        live_transcription_data = data.get('live_transcription', {})
        duration_minutes = data.get('duration_minutes', 0)
        
        # Update the attempt record
        update_data = {
            'status': 'completed',
            'completed_at': datetime.now(timezone.utc).isoformat(),
            'actual_duration_minutes': duration_minutes,
            'transcript': json.dumps(transcript_data) if transcript_data else None,
            'live_transcription': json.dumps(live_transcription_data) if live_transcription_data else None,
            'updated_at': datetime.now(timezone.utc).isoformat()
        }
        
        result = supabase.table('mock_interview_attempts').update(update_data)\
            .eq('id', attempt_id)\
            .eq('mock_interview_id', uid)\
            .execute()
        
        if result.data:
            current_app.logger.info(f"Mock interview {attempt_id} completed for user {uid}")
            return jsonify({
                'message': 'Interview completed successfully',
                'status': 'completed',
                'processing_message': 'Your feedback is being generated and will be ready in a few minutes.'
            }), 200
        else:
            return jsonify({'error': 'Interview attempt not found'}), 404
        
    except Exception as e:
        current_app.logger.error(f"Failed to complete mock interview {attempt_id} for user {uid}: {e}", exc_info=True)
        return jsonify({'error': 'Failed to complete interview session'}), 500

