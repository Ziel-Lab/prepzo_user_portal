from flask import request, jsonify
from flask_cors import CORS
import requests 
import os
from app import extensions 
import json
import logging 
from gotrue.errors import AuthApiError
from dotenv import load_dotenv


from . import linkedin_optimizer_bp

load_dotenv()
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000") 
XANO_API_URL_LINKEDIN_OPTIMIZER = os.getenv("XANO_API_URL_LINKEDIN_OPTIMIZER")


CORS(linkedin_optimizer_bp, origins=[FRONTEND_URL], supports_credentials=True, methods=["POST", "GET", "OPTIONS"])

def get_authenticated_user():
    """Helper to extract and validate JWT token and return user object."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        logging.warning("get_authenticated_user: Missing or invalid Authorization header from %s for path %s", request.remote_addr, request.path)
        return None, jsonify({"error": "Missing or invalid Authorization header"}), 401

    jwt_token = auth_header.split(" ")[1]
    try:
        logging.info("get_authenticated_user: Attempting to validate token for user from %s for path %s", request.remote_addr, request.path)
        user_response = extensions.supabase.auth.get_user(jwt=jwt_token)
        user = user_response.user
        if not user or not user.id:
            logging.warning("get_authenticated_user: Supabase returned no user or user.id for token from %s.", request.remote_addr)
            return None, jsonify({"error": "Invalid token or user not found"}), 401
        logging.info("get_authenticated_user: Successfully authenticated user %s from %s.", user.id, request.remote_addr)
        return user, None, None  
    except AuthApiError as e:
        logging.error(
            "get_authenticated_user: Supabase AuthApiError for user from %s - Status: %s, Message: %s",
            request.remote_addr,
            e.status if hasattr(e, 'status') else 'N/A',
            e.message if hasattr(e, 'message') else str(e),
            exc_info=True 
        )

        status_code = e.status if hasattr(e, 'status') and isinstance(e.status, int) and 100 <= e.status <= 599 else 401
        return None, jsonify({"error": f"Authentication error: {e.message if hasattr(e, 'message') else str(e)}"}), status_code
    except Exception as e:
        logging.error(
            "get_authenticated_user: Generic authentication failure for user from %s: %s",
            request.remote_addr,
            str(e),
            exc_info=True 
        )
        return None, jsonify({"error": f"An unexpected authentication error occurred: {str(e)}"}), 401

@linkedin_optimizer_bp.route("/linkedin-optimizer/history", methods=["GET"])
def get_linkedin_optimizer_history():
    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code
    
    current_user_id = str(user.id)

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
def create_linkedin_optimization():
    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code
    
    current_user_id = str(user.id)
    
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

        xano_response = requests.post(XANO_API_URL_LINKEDIN_OPTIMIZER, json=xano_payload, timeout=60) 
        xano_response.raise_for_status() 
        
        xano_response_text = xano_response.text
        api_data = None 

        try:
            parsed_once = json.loads(xano_response_text)

            if isinstance(parsed_once, str):
                logging.info("Xano response text parsed to a string; attempting to parse string content as JSON.")
                api_data = json.loads(parsed_once)
            elif isinstance(parsed_once, dict):
                api_data = parsed_once
            else:

                logging.error(f"Xano response parsed to unexpected type: {type(parsed_once)}. Raw: {xano_response_text}")
                return jsonify({"error": "Invalid or unexpected data format from optimization service."}), 500

        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse Xano response with json.loads: {e}. Raw: {xano_response_text}")
            try:
                logging.info("Attempting fallback parsing with response.json()")
                api_data = xano_response.json() 
                if not isinstance(api_data, dict):
                     logging.error(f"Fallback response.json() did not yield a dict. Type: {type(api_data)}")
                     return jsonify({"error": "Invalid data format from optimization service after fallback."}), 500
            except json.JSONDecodeError as e2:
                logging.error(f"Fallback parsing with response.json() also failed: {e2}. Raw: {xano_response_text}")
                return jsonify({"error": "Failed to parse response from optimization service. Invalid JSON."}), 500
        
        if not isinstance(api_data, dict):
            logging.error(f"api_data is not a dictionary after parsing attempts. Type: {type(api_data)}. Value: {api_data}")
            return jsonify({"error": "Failed to obtain valid JSON object from optimization service."}), 500

        if not api_data: 
             logging.warning(f"Xano API returned empty data after parsing: {api_data}")

             return jsonify({"error": "Invalid response from optimization service: received empty data after parsing."}), 500

        user_display_name = (user.user_metadata.get('full_name') or
                             user.user_metadata.get('name') or
                             user.email) 

        insert_data = {
            "uid": current_user_id,
            "display name": user_display_name, 
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
        print(f"Error parsing Xano API response string: {str(e)}. Response text was: {xano_response_text if 'xano_response_text' in locals() else 'not captured'}")
        return jsonify({"error": "Invalid response format from optimization service."}), 500
    except Exception as e: # Catch-all for other unexpected errors
        error_str = str(e)
        print(f"Error processing linkedin optimization POST request: {error_str}")
        return jsonify({"error": f"An unexpected error occurred: {error_str}"}), 500

# Remove the old combined route if it exists or comment it out.
# For this edit, we are replacing the entire file content, so the old route will be gone.

        