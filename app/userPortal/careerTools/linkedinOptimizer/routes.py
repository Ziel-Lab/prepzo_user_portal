from flask import request, jsonify, current_app, g
import requests 
from app import extensions 
import json
import logging 
from gotrue.errors import AuthApiError
from app.userPortal.subscription.helpers import require_authentication, check_and_use_feature


from . import linkedin_optimizer_bp

@linkedin_optimizer_bp.route("/linkedin-optimizer/history", methods=["GET"])
@require_authentication
def get_linkedin_optimizer_history():
    current_user_id = str(g.user.id)

    try:
        query_response = (
            extensions.supabase.table("linkedIn_optimizer")
            .select("*")
            .eq("uid", current_user_id)
            .order('created_at', desc=True)
            .execute()
        )
        return jsonify(query_response.data or []), 200
    except Exception as e:
        print(f"Error fetching from linkedin_optimizer table: {str(e)}")
        return jsonify({"error": f"Could not retrieve linkedin optimizer history: {str(e)}"}), 500

@linkedin_optimizer_bp.route("/linkedin-optimizer", methods=["POST"])
@require_authentication
@check_and_use_feature('linkedin_optimize')
def create_linkedin_optimization():
    current_user_id = str(g.user.id)
    XANO_API_URL_LINKEDIN_OPTIMIZER = current_app.config.get("XANO_API_URL_LINKEDIN_OPTIMIZER")
    
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON payload"}), 400

    linkedin_url = data.get("linkedin_url")
    comments = data.get("comments")

    if not linkedin_url:
        return jsonify({"error": "linkedin_url is required"}), 400
    if not comments:
        return jsonify({"error": "comments are required"}), 400

    try:
        xano_payload = {"linkedin_url": linkedin_url, "comments": comments}
        logging.info(f"Sending payload to Xano: {json.dumps(xano_payload)}") 

        xano_response = requests.post(XANO_API_URL_LINKEDIN_OPTIMIZER, json=xano_payload) 
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
        
        result = extensions.supabase.table("linkedIn_optimizer").insert(insert_data).execute()

        if not result.data and not (hasattr(result, 'status_code') and 200 <= result.status_code < 300) : # Check for successful insert, some clients might not return data on success
             print(f"Supabase insert failed or returned no data. Result: {result}")
             error_detail = "Unknown error during Supabase insert."
             if hasattr(result, 'error') and result.error:
                 error_detail = str(result.error.message if hasattr(result.error, 'message') else result.error)
             elif hasattr(result, 'message') and result.message:
                 error_detail = result.message
             return jsonify({"error": f"Failed to save linkedin optimization data: {error_detail}"}), 500

        return jsonify(api_data), 200 # Return Xano's response

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

# Remove the old combined route if it exists or comment it out.
# For this edit, we are replacing the entire file content, so the old route will be gone.

        