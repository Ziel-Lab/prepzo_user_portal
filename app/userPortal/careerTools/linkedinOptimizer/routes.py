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
        result = extensions.supabase.table("linkedin_optimizer").insert(db_payload).execute()
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

@linkedin_optimizer_bp.route("/linkedin-optimizer", methods=["POST","OPTIONS"])
@require_authentication
@check_and_use_feature('linkedin_optimize')
def create_linkedin_optimization():
    current_user_id = str(g.user.id)
    # Use admin client for INSERT operations (with explicit user filtering for security)
    admin_supabase = get_admin_client()
    XANO_API_URL_LINKEDIN_OPTIMIZER = current_app.config.get("XANO_API_URL_LINKEDIN_OPTIMIZER")
    
    data = get_request_data()
    if not data:
        return jsonify({"error": "Invalid or missing payload"}), 400

    linkedin_url = data.get("linkedin_url")
    comments = data.get("comments")

    if not linkedin_url:
        return jsonify({"error": "linkedin_url is required"}), 400
    if not comments:
        return jsonify({"error": "comments are required"}), 400

    try:
        xano_payload = {"linkedin_url": linkedin_url, "comments": comments}
        logging.info(f"Sending payload to Xano: {json.dumps(xano_payload)}") 

        # Increase timeout for better reliability  
        xano_response = requests.post(XANO_API_URL_LINKEDIN_OPTIMIZER, json=xano_payload, timeout=200) 
        xano_response.raise_for_status() 
        
        # The new Xano response is a clean JSON object with 'changes' and 'explanation' keys.
        # This simplifies the parsing logic significantly compared to the old implementation.
        try:
            api_data = xano_response.json()
            if not isinstance(api_data, dict):
                logging.error(f"Xano response was not a JSON object. Raw: {xano_response.text}")
                return jsonify({"error": "Invalid data format from optimization service."}), 500
        except json.JSONDecodeError:
            logging.error(f"Failed to parse Xano response as JSON. Raw: {xano_response.text}")
            return jsonify({"error": "Failed to parse response from optimization service."}), 500
        
        if not api_data: 
             logging.warning(f"Xano API returned empty or null data after parsing.")
             return jsonify({"error": "Invalid response from optimization service: received empty data."}), 500

        user_display_name = (g.user.user_metadata.get('full_name') or
                             g.user.user_metadata.get('name') or
                             g.user.email) 

        insert_data = {
            "uid": current_user_id,
            "display_name": user_display_name, 
            "linkedin_url": linkedin_url,
            "comments": comments,
            "api_response": api_data 
        }
        
        amplitude_payload = {
            "user_uuid": current_user_id,
            "linkedin_url": linkedin_url,
            "goals": comments,
            "feedback": api_data
        }

        # Fire-and-forget background processing
        Thread(
            target=background_db_and_analytics, 
            args=(insert_data, amplitude_payload), 
            daemon=True
        ).start()

        # Return immediately after Xano success
        return jsonify(api_data), 200

    except requests.exceptions.HTTPError as http_err:
        error_message = f"Error from optimization service (HTTP {http_err.response.status_code})"
        try:
            xano_error_details = http_err.response.json()
            error_message += f" - Details: {xano_error_details}"
        except ValueError: # If Xano error response is not JSON
            error_message += f" - Response body: {http_err.response.text}"
        print(error_message)
        # Use Xano's status code if available, otherwise 502
        return jsonify({"error": error_message}), getattr(http_err.response, 'status_code', 502)
    except requests.exceptions.Timeout:
        print("Request to Xano API timed out.")
        return jsonify({"error": "The optimization service timed out. Please try again."}), 504
    except requests.exceptions.RequestException as e: # For network errors, DNS failures, etc.
        print(f"Error calling Xano API: {str(e)}")
        return jsonify({"error": f"Could not connect to optimization service: {str(e)}"}), 503
    except json.JSONDecodeError as e: # Catch issues from json.loads(xano_response_text) specifically
        print(f"Error parsing Xano API response string: {str(e)}. Response text was: {xano_response.text if 'xano_response' in locals() else 'not captured'}")
        return jsonify({"error": "Invalid response format from optimization service."}), 500
    except Exception as e: # Catch-all for other unexpected errors
        error_str = str(e)
        print(f"Error processing linkedin optimization POST request: {error_str}")
        return jsonify({"error": f"An unexpected error occurred: {error_str}"}), 500

        