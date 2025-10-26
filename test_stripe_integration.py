"""
Stripe Integration Test Script
Run this to test your Stripe integration locally

Usage:
    python test_stripe_integration.py

Requirements:
    - Flask app running on localhost:5000
    - Valid user JWT token
    - Stripe configured in test mode
"""

import requests
import json
import sys
import os
from datetime import datetime

# Configuration
BASE_URL = "http://localhost:5000"
# Replace with a valid JWT token for testing
JWT_TOKEN = os.getenv("TEST_JWT_TOKEN", "your-test-jwt-token-here")

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'


def print_header(text):
    print(f"\n{BLUE}{'=' * 60}")
    print(f"{text}")
    print(f"{'=' * 60}{RESET}\n")


def print_success(text):
    print(f"{GREEN}✓ {text}{RESET}")


def print_error(text):
    print(f"{RED}✗ {text}{RESET}")


def print_warning(text):
    print(f"{YELLOW}⚠ {text}{RESET}")


def print_info(text):
    print(f"{BLUE}ℹ {text}{RESET}")


def make_request(method, endpoint, data=None, auth=True):
    """Make HTTP request with proper headers"""
    url = f"{BASE_URL}{endpoint}"
    headers = {
        "Content-Type": "application/json"
    }
    
    if auth:
        headers["Authorization"] = f"Bearer {JWT_TOKEN}"
    
    try:
        if method == "GET":
            response = requests.get(url, headers=headers)
        elif method == "POST":
            response = requests.post(url, headers=headers, json=data)
        else:
            raise ValueError(f"Unsupported method: {method}")
        
        return response
    except requests.exceptions.ConnectionError:
        print_error(f"Could not connect to {BASE_URL}")
        print_warning("Make sure your Flask app is running on localhost:5000")
        sys.exit(1)
    except Exception as e:
        print_error(f"Request failed: {str(e)}")
        return None


def test_get_plans():
    """Test getting available subscription plans"""
    print_header("Test 1: Get Available Plans")
    
    response = make_request("GET", "/subscription/plans", auth=False)
    
    if response and response.status_code == 200:
        data = response.json()
        plans = data.get("plans", [])
        
        print_success(f"Retrieved {len(plans)} plans")
        
        for plan in plans:
            print(f"\n  Plan ID: {plan['id']}")
            print(f"  Name: {plan['name']}")
            print(f"  Price: ${plan['price_amount'] / 100:.2f}")
            print(f"  Stripe Price ID: {plan['stripe_price_id']}")
            print(f"  Mock Interviews: {plan.get('mock_interviews_limit_per_month', 0)}/month")
        
        return plans
    else:
        print_error(f"Failed to get plans: {response.status_code if response else 'No response'}")
        if response:
            print(f"  Response: {response.text}")
        return []


def test_subscription_status():
    """Test getting current subscription status"""
    print_header("Test 2: Get Subscription Status")
    
    response = make_request("GET", "/subscription/subscription-status")
    
    if response and response.status_code == 200:
        data = response.json()
        
        print_success("Retrieved subscription status")
        print(f"\n  Status: {data.get('status')}")
        print(f"  Plan: {data.get('plan_name')} (ID: {data.get('plan_id')})")
        print(f"  Active Subscription: {data.get('has_active_subscription')}")
        
        if data.get('current_period_start'):
            print(f"  Period: {data.get('current_period_start')} to {data.get('current_period_end')}")
            print(f"  Cancel at period end: {data.get('cancel_at_period_end')}")
        
        return data
    else:
        print_error(f"Failed to get status: {response.status_code if response else 'No response'}")
        if response:
            print(f"  Response: {response.text}")
        return None


def test_create_checkout(price_id):
    """Test creating a checkout session"""
    print_header("Test 3: Create Checkout Session")
    
    data = {
        "price_id": price_id,
        "success_url": "http://localhost:3000/success?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": "http://localhost:3000/pricing"
    }
    
    print_info(f"Creating checkout for price: {price_id}")
    
    response = make_request("POST", "/subscription/create-checkout-session", data=data)
    
    if response and response.status_code == 200:
        result = response.json()
        
        print_success("Checkout session created!")
        print(f"\n  Session ID: {result.get('session_id')}")
        print(f"\n  {YELLOW}Checkout URL:{RESET}")
        print(f"  {result.get('checkout_url')}")
        print(f"\n  {YELLOW}Test Card Numbers:{RESET}")
        print(f"  Success: 4242 4242 4242 4242")
        print(f"  Declined: 4000 0000 0000 9995")
        print(f"  3D Secure: 4000 0025 0000 3155")
        
        return result
    else:
        print_error(f"Failed to create checkout: {response.status_code if response else 'No response'}")
        if response:
            print(f"  Response: {response.text}")
        return None


def test_create_portal():
    """Test creating customer portal session"""
    print_header("Test 4: Create Customer Portal Session")
    
    data = {
        "return_url": "http://localhost:3000/account"
    }
    
    response = make_request("POST", "/subscription/create-portal-session", data=data)
    
    if response and response.status_code == 200:
        result = response.json()
        
        print_success("Portal session created!")
        print(f"\n  {YELLOW}Portal URL:{RESET}")
        print(f"  {result.get('portal_url')}")
        
        return result
    elif response and response.status_code == 404:
        print_warning("No Stripe customer found - subscribe first")
        return None
    else:
        print_error(f"Failed to create portal: {response.status_code if response else 'No response'}")
        if response:
            print(f"  Response: {response.text}")
        return None


def main():
    print(f"\n{BLUE}╔════════════════════════════════════════════════════════════╗")
    print(f"║        Stripe Integration Test Suite                      ║")
    print(f"║                                                            ║")
    print(f"║  Testing against: {BASE_URL:<38} ║")
    print(f"╚════════════════════════════════════════════════════════════╝{RESET}\n")
    
    # Check JWT token
    if JWT_TOKEN == "your-test-jwt-token-here":
        print_error("Please set a valid JWT token!")
        print_info("Set the TEST_JWT_TOKEN environment variable:")
        print(f"  export TEST_JWT_TOKEN='your-actual-jwt-token'")
        print(f"  python {sys.argv[0]}\n")
        sys.exit(1)
    
    # Test 1: Get Plans
    plans = test_get_plans()
    
    # Test 2: Get Subscription Status
    status = test_subscription_status()
    
    # Test 3: Create Checkout (if there are paid plans)
    paid_plans = [p for p in plans if p['price_amount'] > 0]
    if paid_plans:
        test_price_id = paid_plans[0]['stripe_price_id']
        checkout = test_create_checkout(test_price_id)
    else:
        print_warning("No paid plans found to test checkout")
    
    # Test 4: Create Portal (only if user has subscription)
    if status and status.get('has_active_subscription'):
        test_create_portal()
    else:
        print_warning("User has no active subscription - skipping portal test")
    
    # Summary
    print_header("Test Summary")
    print(f"  {YELLOW}Next Steps:{RESET}")
    print(f"  1. Open the checkout URL in your browser")
    print(f"  2. Use test card: 4242 4242 4242 4242")
    print(f"  3. Complete the payment")
    print(f"  4. Check your Flask logs for webhook events")
    print(f"  5. Run this script again to verify subscription status")
    print(f"\n  {YELLOW}Webhook Testing:{RESET}")
    print(f"  Make sure Stripe CLI is forwarding webhooks:")
    print(f"  $ stripe listen --forward-to localhost:5000/subscription/webhook")
    print()


if __name__ == "__main__":
    main()

