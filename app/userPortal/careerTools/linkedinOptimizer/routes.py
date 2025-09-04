from flask import request, jsonify, current_app, g
import requests 
from app.extensions import get_user_client, get_admin_client
import json
import logging 
from threading import Thread
from gotrue.errors import AuthApiError
from app.userPortal.subscription.helpers import require_authentication, check_and_use_feature
from app.utils.amplitude import linkedin_optimizer_event

from . import linkedin_optimizer_bp

def background_db_and_analytics(db_payload, amplitude_payload=None):
    """Fire-and-forget background processing for DB inserts and analytics"""
    try:
        admin_supabase = get_admin_client()
        result = admin_supabase.table("linkedin_optimizer").insert(db_payload).execute()
        if not result.data and not (hasattr(result, 'status_code') and 200 <= result.status_code < 300):
            logging.warning(f"Background Supabase insert failed or returned no data. Result: {result}")
    except Exception as e:
        logging.error(f"Background DB insert failed: {str(e)}")
    
    if amplitude_payload:
        try:
            linkedin_optimizer_event(**amplitude_payload)
            logging.info("LinkedIn optimizer Amplitude event sent successfully.")
        except Exception as e:
            logging.warning(f"Background Amplitude event failed: {e}")

def get_request_data():
    """Unified request data handling for both JSON and form data"""
    if request.is_json:
        return request.get_json() or {}
    return request.form.to_dict()

@linkedin_optimizer_bp.route("/linkedin-optimizer/history", methods=["GET", "OPTIONS"])
@require_authentication
def get_linkedin_optimizer_history():
    current_user_id = str(g.user.id)
    # Use admin client with explicit user filtering for consistent data access
    admin_supabase = get_admin_client()

    try:
        query_response = (
            admin_supabase.table("linkedin_optimizer")
            .select("*")
            .eq("uid", current_user_id)
            .order('created_at', desc=True)
            .execute()
        )
        return jsonify(query_response.data or []), 200
    except Exception as e:
        print(f"Error fetching from linkedin_optimizer table: {str(e)}")
        return jsonify({"error": f"Could not retrieve linkedin optimizer history: {str(e)}"}), 500

@linkedin_optimizer_bp.route("/linkedin-optimizer/process-pdf", methods=["POST", "OPTIONS"])
@require_authentication
@check_and_use_feature("linkedin_optimizer")
def process_linkedin_pdf():
    """Process LinkedIn PDF by sending URL to webhook"""
    current_user_id = str(g.user.id)
    
    try:
        # Get request data
        data = get_request_data()
        
        # Validate required fields
        if not data.get('pdf_url'):
            return jsonify({"error": "pdf_url is required"}), 400
        
        pdf_url = data['pdf_url']
        
        # Validate URL format
        if not pdf_url.startswith(('http://', 'https://')):
            return jsonify({"error": "Invalid URL format"}), 400
        
        # Prepare webhook payload
        webhook_payload = {
            "user_id": current_user_id,
            "pdf_url": pdf_url,
            "timestamp": data.get('timestamp'),
            "additional_data": data.get('additional_data', {})
        }
        
        # Send to webhook
        webhook_url = "https://prepzo.app.n8n.cloud/webhook-test/4953ccbe-a9f0-4200-85d5-7c9df1a46f1e"
        
        try:
            response = requests.post(
                webhook_url,
                json=webhook_payload,
                headers={'Content-Type': 'application/json'},
                timeout=30
            )
            
            if response.status_code == 200:
                # Log successful webhook call
                logging.info(f"Successfully sent LinkedIn PDF to webhook for user {current_user_id}")
                
                # Prepare database payload for background processing
                db_payload = {
                    "uid": current_user_id,
                    "pdf_url": pdf_url,
                    "webhook_response": response.json() if response.content else None,
                    "status": "sent_to_webhook"
                }
                
                # Prepare analytics payload
                amplitude_payload = {
                    "user_id": current_user_id,
                    "pdf_url": pdf_url,
                    "webhook_status": "success"
                }
                
                # Process database and analytics in background
                Thread(target=background_db_and_analytics, args=(db_payload, amplitude_payload)).start()
                
                return jsonify({
                    "message": "LinkedIn PDF sent to webhook successfully",
                    "webhook_response": response.json() if response.content else None
                }), 200
            else:
                logging.error(f"Webhook returned status {response.status_code}: {response.text}")
                return jsonify({
                    "error": f"Webhook request failed with status {response.status_code}",
                    "details": response.text
                }), 500
                
        except requests.exceptions.Timeout:
            logging.error("Webhook request timed out")
            return jsonify({"error": "Webhook request timed out"}), 504
        except requests.exceptions.RequestException as e:
            logging.error(f"Webhook request failed: {str(e)}")
            return jsonify({"error": f"Failed to send to webhook: {str(e)}"}), 500
            
    except Exception as e:
        logging.error(f"Error processing LinkedIn PDF: {str(e)}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500

