from flask import request, jsonify, current_app, g
from . import subscription_bp
from .helpers import require_authentication, get_user_display_name, get_anniversary_period
from app import extensions
import stripe
import os
from datetime import datetime, date
import time
import hmac
import hashlib

# Initialize Stripe with API key from config
def get_stripe_client():
    """Get Stripe client with API key from app config"""
    api_key = current_app.config.get('STRIPE_SECRET_API_KEY')
    if not api_key:
        raise ValueError("STRIPE_SECRET_API_KEY not configured in AWS Secrets Manager")
    stripe.api_key = api_key
    return stripe

# =============================================================================
# CHECKOUT SESSION CREATION
# =============================================================================

@subscription_bp.route('/create-checkout-session', methods=['POST'])
@require_authentication
def create_checkout_session():
    """
    Create a Stripe Checkout Session for subscription
    
    Expected request body:
    {
        "price_id": "price_xxxxx",  # Stripe Price ID
        "success_url": "https://yourapp.com/success",
        "cancel_url": "https://yourapp.com/cancel"
    }
    """
    try:
        get_stripe_client()
        data = request.get_json()
        
        price_id = data.get('price_id')
        success_url = data.get('success_url')
        cancel_url = data.get('cancel_url')
        
        if not all([price_id, success_url, cancel_url]):
            return jsonify({
                "error": "Missing required fields: price_id, success_url, cancel_url"
            }), 400
        
        user_id = g.user.id
        user_email = g.user.email
        
        # Check if user already has a Stripe customer ID
        supabase = extensions.get_admin_client()
        
        stripe_customer_id = None
        
        # Step 1: Check our database first (user_subscriptions)
        try:
            sub_res = supabase.table('user_subscriptions').select('stripe_customer_id').eq('user_id', user_id).limit(1).execute()
            
            if sub_res.data and len(sub_res.data) > 0:
                stripe_customer_id = sub_res.data[0].get('stripe_customer_id')
                if stripe_customer_id:
                    current_app.logger.info(f"Using existing Stripe customer from DB: {stripe_customer_id} for user {user_id[:8]}***")
        except Exception as e:
            current_app.logger.warning(f"Could not fetch subscription for user {user_id[:8]}***: {e}")
        
        # Step 2: If not in DB, check Stripe directly by email (prevent duplicates)
        if not stripe_customer_id:
            try:
                # Search for existing customer in Stripe by email
                existing_customers = stripe.Customer.list(email=user_email, limit=1)
                
                if existing_customers.data and len(existing_customers.data) > 0:
                    stripe_customer_id = existing_customers.data[0].id
                    current_app.logger.info(f"Found existing Stripe customer by email: {stripe_customer_id} for user {user_id[:8]}***")
                    
                    # Update our database with the found customer ID
                    try:
                        supabase.table('user_subscriptions').upsert({
                            'user_id': user_id,
                            'stripe_customer_id': stripe_customer_id,
                            'plan_id': 1,
                            'status': 'free'
                        }, on_conflict='user_id').execute()
                    except:
                        pass  # Database update can fail, continue with checkout
            except Exception as e:
                current_app.logger.warning(f"Could not search Stripe customers: {e}")
        
        # Step 3: Create new Stripe customer only if none exists
        if not stripe_customer_id:
            display_name = get_user_display_name(g.user)
            customer = stripe.Customer.create(
                email=user_email,
                name=display_name,
                metadata={
                    'user_id': user_id,
                    'created_from': 'checkout_session'
                }
            )
            stripe_customer_id = customer.id
            current_app.logger.info(f"Created NEW Stripe customer {stripe_customer_id} for user {user_id[:8]}***")
        
        # Create checkout session with trial period
        checkout_session = stripe.checkout.Session.create(
            customer=stripe_customer_id,
            payment_method_types=['card'],
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=success_url + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=cancel_url,
            metadata={
                'user_id': user_id
            },
            subscription_data={
                'trial_period_days': 3,  # 3-day trial period
                'metadata': {
                    'user_id': user_id
                }
            },
            payment_method_collection='always'  # Require payment method even during trial
        )
        
        current_app.logger.info(f"Created checkout session {checkout_session.id} for user {user_id[:8]}***")
        
        return jsonify({
            "checkout_url": checkout_session.url,
            "session_id": checkout_session.id
        }), 200
        
    except stripe.StripeError as e:
        current_app.logger.error(f"Stripe error in create_checkout_session: {str(e)}")
        return jsonify({"error": f"Stripe error: {str(e)}"}), 400
    except Exception as e:
        current_app.logger.error(f"Error creating checkout session: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to create checkout session"}), 500


# =============================================================================
# CUSTOMER PORTAL ACCESS  
# =============================================================================

@subscription_bp.route('/create-portal-session', methods=['POST'])
@require_authentication
def create_portal_session():
    """
    Create a Stripe Customer Portal session for managing subscriptions
    
    Expected request body:
    {
        "return_url": "https://yourapp.com/account"
    }
    """
    try:
        get_stripe_client()
        data = request.get_json()
        
        return_url = data.get('return_url')
        if not return_url:
            return jsonify({"error": "Missing required field: return_url"}), 400
        
        user_id = g.user.id
        
        # Get user's Stripe customer ID (from user_subscriptions)
        supabase = extensions.get_admin_client()
        
        stripe_customer_id = None
        try:
            # Get from user_subscriptions table
            sub_res = supabase.table('user_subscriptions').select('stripe_customer_id').eq('user_id', user_id).limit(1).execute()
            
            if sub_res.data and len(sub_res.data) > 0:
                stripe_customer_id = sub_res.data[0].get('stripe_customer_id')
        except Exception as e:
            current_app.logger.warning(f"Could not fetch subscription for user {user_id[:8]}***: {e}")
        
        if not stripe_customer_id:
            return jsonify({
                "error": "No active Stripe customer found. Please subscribe first."
            }), 404
        
        # Create portal session
        portal_session = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=return_url,
        )
        
        current_app.logger.info(f"Created portal session for user {user_id[:8]}***")
        
        return jsonify({
            "portal_url": portal_session.url
        }), 200
        
    except stripe.StripeError as e:
        current_app.logger.error(f"Stripe error in create_portal_session: {str(e)}")
        return jsonify({"error": f"Stripe error: {str(e)}"}), 400
    except Exception as e:
        current_app.logger.error(f"Error creating portal session: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to create portal session"}), 500


# =============================================================================
# WEBHOOK HANDLER
# =============================================================================

@subscription_bp.route('/webhook', methods=['POST'])
def stripe_webhook():
    """
    Handle Stripe webhook events
    
    This endpoint receives events from Stripe about subscription changes:
    - checkout.session.completed: New subscription created
    - invoice.paid: Subscription payment succeeded
    - invoice.payment_failed: Payment failed
    - customer.subscription.updated: Subscription changed (plan, status, etc.)
    - customer.subscription.deleted: Subscription cancelled
    """
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = current_app.config.get('STRIPE_WEBHOOK_SECRET')
    
    if not webhook_secret:
        current_app.logger.error("STRIPE_WEBHOOK_SECRET not configured")
        return jsonify({"error": "Webhook not configured"}), 500
    
    try:
        # Verify webhook signature
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
        
        current_app.logger.info(f"Received Stripe webhook event: {event['type']} (ID: {event['id']})")
        
        # Handle different event types
        event_type = event['type']
        
        if event_type == 'checkout.session.completed':
            handle_checkout_completed(event['data']['object'])
        
        elif event_type in ['invoice.paid', 'invoice.payment_succeeded']:
            handle_invoice_paid(event['data']['object'])
        
        elif event_type == 'invoice.payment_failed':
            handle_payment_failed(event['data']['object'])
        
        elif event_type == 'customer.subscription.updated':
            handle_subscription_updated(event['data']['object'])
        
        elif event_type == 'customer.subscription.deleted':
            handle_subscription_deleted(event['data']['object'])
        
        else:
            current_app.logger.info(f"Unhandled event type: {event_type}")
        
        return jsonify({"status": "success"}), 200
        
    except stripe.SignatureVerificationError as e:
        current_app.logger.error(f"Invalid webhook signature: {str(e)}")
        return jsonify({"error": "Invalid signature"}), 400
    except Exception as e:
        current_app.logger.error(f"Error processing webhook: {str(e)}", exc_info=True)
        return jsonify({"error": "Webhook processing failed"}), 500


# =============================================================================
# WEBHOOK EVENT HANDLERS
# =============================================================================

def handle_checkout_completed(session):
    """Handle successful checkout session completion"""
    try:
        get_stripe_client()
        user_id = session['metadata'].get('user_id')
        if not user_id:
            current_app.logger.error(f"No user_id in checkout session metadata: {session['id']}")
            return
        
        subscription_id = session.get('subscription')
        if not subscription_id:
            current_app.logger.warning(f"No subscription in checkout session {session['id']}")
            return
        
        # Retrieve full subscription details from Stripe
        subscription = stripe.Subscription.retrieve(subscription_id)
        
        # Get the price ID and plan details
        price_id = subscription['items']['data'][0]['price']['id']
        
        # Find corresponding plan in our database
        supabase = extensions.get_admin_client()
        
        plan_id = 1  # Default to free plan
        try:
            plan_res = supabase.table('subscription_plans').select('id').eq('stripe_price_id', price_id).limit(1).execute()
            
            if plan_res.data and len(plan_res.data) > 0:
                plan_id = plan_res.data[0]['id']
            else:
                current_app.logger.error(f"No plan found for Stripe price {price_id}, using free plan")
        except Exception as e:
            current_app.logger.error(f"Error fetching plan for price {price_id}: {e}, using free plan")
        
        # Calculate billing period - safely access with .get()
        period_start_ts = subscription.get('current_period_start')
        period_end_ts = subscription.get('current_period_end')
        
        if period_start_ts and period_end_ts:
            period_start = datetime.fromtimestamp(period_start_ts).date()
            period_end = datetime.fromtimestamp(period_end_ts).date()
        else:
            # Fallback to today + 30 days if not available
            from dateutil.relativedelta import relativedelta
            period_start = date.today()
            period_end = period_start + relativedelta(months=1)
        
        # Get user display name
        try:
            user_res = supabase.auth.admin.get_user_by_id(user_id)
            display_name = get_user_display_name(user_res.user) if user_res else "User"
        except:
            display_name = "User"
        
        # Get subscription status - handle trialing vs active
        sub_status = subscription.get('status', 'active')
        # Map Stripe 'trialing' to our 'active' so user gets access during trial
        if sub_status == 'trialing':
            sub_status = 'active'  # Give full access during trial
        
        # Update or create user subscription
        subscription_data = {
            'user_id': user_id,
            'plan_id': plan_id,
            'status': sub_status,
            'stripe_subscription_id': subscription_id,
            'stripe_customer_id': subscription['customer'],
            'stripe_price_id': price_id,  # Store price ID in user_subscriptions
            'current_period_start': str(period_start),
            'current_period_end': str(period_end),
            'next_billing_date': str(period_end),  # Next billing is at period end
            'cancel_at_period_end': subscription.get('cancel_at_period_end', False),
            'display_name': display_name
        }
        
        # Upsert subscription
        result = supabase.table('user_subscriptions').upsert(
            subscription_data,
            on_conflict='user_id'
        ).execute()
        
        current_app.logger.info(f"Checkout completed for user {user_id[:8]}***: Plan {plan_id}, Subscription {subscription_id}")
        
        # Initialize usage tracking for the new period
        _initialize_usage_tracking(user_id, plan_id, period_start, period_end)
        
    except Exception as e:
        current_app.logger.error(f"Error handling checkout completion: {str(e)}", exc_info=True)


def handle_invoice_paid(invoice):
    """Handle successful invoice payment"""
    try:
        get_stripe_client()
        subscription_id = invoice.get('subscription')
        if not subscription_id:
            current_app.logger.info(f"Invoice {invoice['id']} has no subscription (one-time payment)")
            return
        
        # Retrieve subscription to get user_id
        subscription = stripe.Subscription.retrieve(subscription_id)
        user_id = subscription['metadata'].get('user_id')
        
        if not user_id:
            current_app.logger.error(f"No user_id in subscription metadata: {subscription_id}")
            return
        
        # Update subscription status to active
        supabase = extensions.get_admin_client()
        
        # Get the price ID
        price_id = subscription['items']['data'][0]['price']['id']
        
        plan_id = 1  # Default
        try:
            plan_res = supabase.table('subscription_plans').select('id').eq('stripe_price_id', price_id).limit(1).execute()
            if plan_res.data and len(plan_res.data) > 0:
                plan_id = plan_res.data[0]['id']
        except Exception:
            pass  # Use default
        
        # Update subscription record - safely access timestamps
        period_start_ts = subscription.get('current_period_start')
        period_end_ts = subscription.get('current_period_end')
        
        if period_start_ts and period_end_ts:
            period_start = datetime.fromtimestamp(period_start_ts).date()
            period_end = datetime.fromtimestamp(period_end_ts).date()
            
            supabase.table('user_subscriptions').update({
                'status': 'active',
                'plan_id': plan_id,
                'stripe_price_id': price_id,
                'current_period_start': str(period_start),
                'current_period_end': str(period_end),
                'next_billing_date': str(period_end),
                'cancel_at_period_end': subscription.get('cancel_at_period_end', False)
            }).eq('stripe_subscription_id', subscription_id).execute()
        else:
            # Just update status and plan if period info not available
            supabase.table('user_subscriptions').update({
                'status': 'active',
                'plan_id': plan_id,
                'stripe_price_id': price_id,
                'cancel_at_period_end': subscription.get('cancel_at_period_end', False)
            }).eq('stripe_subscription_id', subscription_id).execute()
        
        current_app.logger.info(f"Invoice paid for user {user_id[:8]}***: Subscription {subscription_id}")
        
        # Check if this is a renewal - initialize new usage period if needed
        today = date.today()
        try:
            usage_res = supabase.table('feature_usage').select('period_end').eq('user_id', user_id).limit(1).execute()
            
            if usage_res.data and len(usage_res.data) > 0:
                current_period_end = datetime.strptime(usage_res.data[0]['period_end'], '%Y-%m-%d').date()
                if today > current_period_end:
                    # Period rolled over - initialize new usage tracking
                    _initialize_usage_tracking(user_id, plan_id, period_start, period_end)
        except Exception:
            pass  # Skip rollover check if query fails
        
    except Exception as e:
        current_app.logger.error(f"Error handling invoice paid: {str(e)}", exc_info=True)


def handle_payment_failed(invoice):
    """Handle failed invoice payment"""
    try:
        get_stripe_client()
        subscription_id = invoice.get('subscription')
        if not subscription_id:
            return
        
        # Retrieve subscription to get user_id
        subscription = stripe.Subscription.retrieve(subscription_id)
        user_id = subscription['metadata'].get('user_id')
        
        if not user_id:
            current_app.logger.error(f"No user_id in subscription metadata: {subscription_id}")
            return
        
        # Update subscription status
        supabase = extensions.get_admin_client()
        supabase.table('user_subscriptions').update({
            'status': 'past_due'
        }).eq('stripe_subscription_id', subscription_id).execute()
        
        current_app.logger.warning(f"Payment failed for user {user_id[:8]}***: Subscription {subscription_id}")
        
        # TODO: Send email notification to user about payment failure
        
    except Exception as e:
        current_app.logger.error(f"Error handling payment failure: {str(e)}", exc_info=True)


def handle_subscription_updated(subscription):
    """Handle subscription updates (plan changes, cancellations, etc.)"""
    try:
        get_stripe_client()
        user_id = subscription['metadata'].get('user_id')
        if not user_id:
            current_app.logger.error(f"No user_id in subscription metadata: {subscription['id']}")
            return
        
        # Get the current price ID
        price_id = subscription['items']['data'][0]['price']['id']
        
        # Find corresponding plan
        supabase = extensions.get_admin_client()
        
        plan_id = 1  # Default
        try:
            plan_res = supabase.table('subscription_plans').select('id').eq('stripe_price_id', price_id).limit(1).execute()
            if plan_res.data and len(plan_res.data) > 0:
                plan_id = plan_res.data[0]['id']
        except Exception:
            pass  # Use default
        
        # Update subscription record - safely access timestamps
        period_start_ts = subscription.get('current_period_start')
        period_end_ts = subscription.get('current_period_end')
        
        if period_start_ts and period_end_ts:
            period_start = datetime.fromtimestamp(period_start_ts).date()
            period_end = datetime.fromtimestamp(period_end_ts).date()
        else:
            from dateutil.relativedelta import relativedelta
            period_start = date.today()
            period_end = period_start + relativedelta(months=1)
        
        update_data = {
            'plan_id': plan_id,
            'status': subscription.get('status', 'active'),
            'stripe_price_id': price_id,
            'current_period_start': str(period_start),
            'current_period_end': str(period_end),
            'next_billing_date': str(period_end),
            'cancel_at_period_end': subscription.get('cancel_at_period_end', False)
        }
        
        supabase.table('user_subscriptions').update(update_data).eq('stripe_subscription_id', subscription['id']).execute()
        
        current_app.logger.info(f"Subscription updated for user {user_id[:8]}***: Plan {plan_id}, Status {subscription.get('status', 'unknown')}")
        
        # If plan changed mid-cycle, update feature_usage plan_id
        try:
            usage_res = supabase.table('feature_usage').select('plan_id').eq('user_id', user_id).limit(1).execute()
            if usage_res.data and len(usage_res.data) > 0:
                old_plan_id = usage_res.data[0].get('plan_id')
                if old_plan_id != plan_id:
                    supabase.table('feature_usage').update({'plan_id': plan_id}).eq('user_id', user_id).execute()
                    current_app.logger.info(f"Updated feature_usage plan_id for user {user_id[:8]}*** from {old_plan_id} to {plan_id}")
        except Exception:
            pass  # Skip if query fails
        
    except Exception as e:
        current_app.logger.error(f"Error handling subscription update: {str(e)}", exc_info=True)


def handle_subscription_deleted(subscription):
    """Handle subscription cancellation"""
    try:
        user_id = subscription['metadata'].get('user_id')
        if not user_id:
            current_app.logger.error(f"No user_id in subscription metadata: {subscription['id']}")
            return
        
        # Update subscription to cancelled and revert to free plan
        supabase = extensions.get_admin_client()
        supabase.table('user_subscriptions').update({
            'status': 'free',  # Your schema uses 'free' status
            'plan_id': 1,  # Free plan
            'stripe_subscription_id': None,
            'stripe_price_id': 'free_plan',
            'cancel_at_period_end': False
        }).eq('stripe_subscription_id', subscription['id']).execute()
        
        current_app.logger.info(f"Subscription cancelled for user {user_id[:8]}***: Reverted to free plan")
        
        # Update feature_usage to free plan
        supabase.table('feature_usage').update({'plan_id': 1}).eq('user_id', user_id).execute()
        
    except Exception as e:
        current_app.logger.error(f"Error handling subscription deletion: {str(e)}", exc_info=True)


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def _initialize_usage_tracking(user_id, plan_id, period_start, period_end):
    """Initialize or reset usage tracking for a billing period"""
    try:
        supabase = extensions.get_admin_client()
        
        # Get user display name
        user_res = supabase.auth.admin.get_user_by_id(user_id)
        display_name = get_user_display_name(user_res.user) if user_res else "User"
        
        # Get existing usage to carry over lifetime counts
        initial_usage = {
            'user_id': user_id,
            'plan_id': plan_id,
            'display_name': display_name,
            'period_start': str(period_start),
            'period_end': str(period_end)
        }
        
        # Carry over lifetime counts if exists
        try:
            usage_res = supabase.table('feature_usage').select('*').eq('user_id', user_id).limit(1).execute()
            
            if usage_res.data and len(usage_res.data) > 0:
                usage_data = usage_res.data[0]
                for key, value in usage_data.items():
                    if key.endswith('_lifetime_count'):
                        initial_usage[key] = value or 0
                        # Reset period count
                        period_col = key.replace('_lifetime_count', '_period_count')
                        initial_usage[period_col] = 0
        except Exception:
            pass  # Skip if query fails
        
        # Upsert usage record
        supabase.table('feature_usage').upsert(
            initial_usage,
            on_conflict='user_id'
        ).execute()
        
        current_app.logger.info(f"Initialized usage tracking for user {user_id[:8]}***: Period {period_start} to {period_end}")
        
    except Exception as e:
        current_app.logger.error(f"Error initializing usage tracking: {str(e)}", exc_info=True)


# =============================================================================
# ADMIN/UTILITY ENDPOINTS
# =============================================================================

@subscription_bp.route('/subscription-status', methods=['GET'])
@require_authentication
def get_subscription_status():
    """Get current user's subscription status"""
    try:
        user_id = g.user.id
        supabase = extensions.get_admin_client()
        
        # Get subscription from database
        try:
            sub_res = supabase.table('user_subscriptions').select(
                'plan_id, status, current_period_start, current_period_end, cancel_at_period_end, stripe_subscription_id'
            ).eq('user_id', user_id).limit(1).execute()
            
            if not sub_res.data or len(sub_res.data) == 0:
                return jsonify({
                    "status": "no_subscription",
                    "plan_id": 1,
                    "plan_name": "Free"
                }), 200
            
            subscription = sub_res.data[0]
        except Exception:
            return jsonify({
                "status": "no_subscription",
                "plan_id": 1,
                "plan_name": "Free"
            }), 200
        
        # Get plan details
        plan_res = supabase.table('subscription_plans').select('*').eq('id', subscription['plan_id']).single().execute()
        
        return jsonify({
            "status": subscription['status'],
            "plan_id": subscription['plan_id'],
            "plan_name": plan_res.data.get('name') if plan_res.data else 'Unknown',
            "current_period_start": subscription.get('current_period_start'),
            "current_period_end": subscription.get('current_period_end'),
            "cancel_at_period_end": subscription.get('cancel_at_period_end', False),
            "has_active_subscription": subscription['status'] == 'active'
        }), 200
        
    except Exception as e:
        current_app.logger.error(f"Error getting subscription status: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to get subscription status"}), 500


@subscription_bp.route('/plans', methods=['GET'])
def get_available_plans():
    """Get all available subscription plans"""
    try:
        supabase = extensions.get_admin_client()
        
        # Get all active plans
        plans_res = supabase.table('subscription_plans').select('*').eq('active', True).order('id').execute()
        
        return jsonify({
            "plans": plans_res.data
        }), 200
        
    except Exception as e:
        current_app.logger.error(f"Error getting plans: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to get plans"}), 500

