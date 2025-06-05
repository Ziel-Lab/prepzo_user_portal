from flask import request, jsonify, current_app
from flask_cors import CORS # Removed cross_origin, will rely on blueprint-level CORS
import requests 
import os
from app import extensions 
import json
import logging # Standard import for fallback logger
from gotrue.errors import AuthApiError
# from dotenv import load_dotenv

from . import linkedin_optimizer_bp

# load_dotenv() 
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN")
XANO_API_URL_LINKEDIN_OPTIMIZER = os.getenv("XANO_API_URL_LINKEDIN_OPTIMIZER")

# Log the loaded values at module import time
print(f"DEBUG: [linkedinOptimizer/routes.py] Attempting to load FRONTEND_ORIGIN: {FRONTEND_URL}")
print(f"DEBUG: [linkedinOptimizer/routes.py] Attempting to load XANO_API_URL_LINKEDIN_OPTIMIZER: {XANO_API_URL_LINKEDIN_OPTIMIZER}")

# Critical check for FRONTEND_URL (Restoring this check as it's important)
if not FRONTEND_URL:
    if os.getenv("FLASK_ENV") == "development" or os.getenv("FLASK_DEBUG") == "1":
        FRONTEND_URL = "http://localhost:3000"
        print(f"WARNING: [linkedinOptimizer/routes.py] FRONTEND_ORIGIN not set, defaulting to {FRONTEND_URL} for CORS (dev mode).")
    else:
        # Keeping RuntimeError for FRONTEND_URL as it's crucial for CORS setup for the whole blueprint
        raise RuntimeError("CRITICAL: [linkedinOptimizer/routes.py] FRONTEND_ORIGIN environment variable is not set. CORS will not be configured correctly.")

# Warning if XANO_API_URL is not set (consistent with previous change to not raise RuntimeError here)
if not XANO_API_URL_LINKEDIN_OPTIMIZER:
    print(f"WARNING: [linkedinOptimizer/routes.py] XANO_API_URL_LINKEDIN_OPTIMIZER environment variable is NOT SET. The /linkedin-optimizer endpoint will fail if called.")

print(f"INFO: [linkedinOptimizer/routes.py] Configuring CORS with FRONTEND_URL: {FRONTEND_URL}")

CORS(linkedin_optimizer_bp, origins=[FRONTEND_URL], supports_credentials=True, methods=["POST", "GET", "OPTIONS"]) 

# --- Authentication Helper ---
def get_authenticated_user():
    """Helper to extract and validate JWT token and return user object."""
    logger = current_app.logger if current_app and hasattr(current_app, 'logger') else logging.getLogger(__name__)
    
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        logger.warning("get_authenticated_user (linkedinOptimizer): Missing or invalid Authorization header from %s for path %s", request.remote_addr, request.path)
        return None, jsonify({"error": "Missing or invalid Authorization header"}), 401

    jwt_token = auth_header.split(" ")[1]
    try:
        logger.info("get_authenticated_user (linkedinOptimizer): Attempting to validate token for user from %s for path %s", request.remote_addr, request.path)
        user_response = extensions.supabase.auth.get_user(jwt=jwt_token)
        user = user_response.user
        if not user or not user.id:
            logger.warning("get_authenticated_user (linkedinOptimizer): Supabase returned no user or user.id for token from %s.", request.remote_addr)
            return None, jsonify({"error": "Invalid token or user not found"}), 401
        logger.info("get_authenticated_user (linkedinOptimizer): Successfully authenticated user %s from %s.", user.id, request.remote_addr)
        return user, None, None  
    except AuthApiError as e:
        logger.error(
            "get_authenticated_user (linkedinOptimizer): Supabase AuthApiError for user from %s - Status: %s, Message: %s",
            request.remote_addr,
            e.status if hasattr(e, 'status') else 'N/A',
            e.message if hasattr(e, 'message') else str(e),
            exc_info=True 
        )
        status_code = e.status if hasattr(e, 'status') and isinstance(e.status, int) and 100 <= e.status <= 599 else 401
        return None, jsonify({"error": f"Authentication error: {e.message if hasattr(e, 'message') else str(e)}"}), status_code
    except Exception as e:
        logger.error(
            "get_authenticated_user (linkedinOptimizer): Generic authentication failure for user from %s: %s",
            request.remote_addr,
            str(e),
            exc_info=True 
        )
        return None, jsonify({"error": f"An unexpected authentication error occurred: {str(e)}"}), 401

# --- Routes ---
@linkedin_optimizer_bp.route("/linkedin-optimizer/history", methods=["GET", "OPTIONS"])
def get_linkedin_optimizer_history():
    logger = current_app.logger # Ensure logger is available in route context
    if request.method == "OPTIONS": # Explicit OPTIONS handling remains good practice
        return "", 204

    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code
    
    current_user_id = str(user.id)

    try:
        logger.info(f"Fetching LinkedIn optimizer history for user {current_user_id}")
        query_response = (
            extensions.supabase.table("linkedIn_optimizer")
            .select("*")
            .eq("uid", current_user_id)
            .order('created_at', desc=True)
            .execute()
        )
        logger.info(f"Successfully fetched LinkedIn optimizer history for user {current_user_id}, found {len(query_response.data)} items.")
        return jsonify(query_response.data or []), 200
    except Exception as e:
        logger.error(f"Error fetching from linkedin_optimizer table for user {current_user_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Could not retrieve linkedin optimizer history due to a server error."}), 500

@linkedin_optimizer_bp.route("/linkedin-optimizer", methods=["POST", "OPTIONS"])
def create_linkedin_optimization():
    logger = current_app.logger # Ensure logger is available
    if request.method == "OPTIONS": # Explicit OPTIONS handling
        return "", 204

    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code
    
    current_user_id = str(user.id)
    
    data = request.get_json()
    if not data:
        logger.warning(f"Invalid JSON payload received for /linkedin-optimizer from user {current_user_id}")
        return jsonify({"error": "Invalid JSON payload"}), 400

    linkedin_url = data.get("linkedin_url")
    comments = data.get("comments")

    if not linkedin_url:
        logger.warning(f"Missing linkedin_url for /linkedin-optimizer from user {current_user_id}")
        return jsonify({"error": "linkedin_url is required"}), 400
    if not comments: # Assuming comments are also mandatory
        logger.warning(f"Missing comments for /linkedin-optimizer from user {current_user_id}")
        return jsonify({"error": "comments are required"}), 400
    
    if not XANO_API_URL_LINKEDIN_OPTIMIZER: 
        logger.critical("XANO_API_URL_LINKEDIN_OPTIMIZER is not configured within the route! Ensure it is set in the environment.")
        return jsonify({"error": "Service configuration error: LinkedIn Optimizer API URL is not set."}), 500

    try:
        xano_payload = {"linkedin_url": linkedin_url, "comments": comments}
        logger.info(f"User {current_user_id} sending payload to Xano LinkedIn Optimizer: {json.dumps(xano_payload)}") 

        xano_response = requests.post(XANO_API_URL_LINKEDIN_OPTIMIZER, json=xano_payload, timeout=120) 
        xano_response.raise_for_status() # Raises HTTPError for bad responses (4xx or 5xx)
        
        raw_xano_response_data = None
        try:
            raw_xano_response_data = xano_response.json()
        except requests.exceptions.JSONDecodeError as e: # If Xano response is not JSON at all
            logger.error(f"Failed to decode initial JSON response from Xano LinkedIn Optimizer for user {current_user_id}. Status: {xano_response.status_code}. Response text: {xano_response.text[:500]}", exc_info=True)
            return jsonify({"error": "Invalid JSON response from optimization service."}), 500

        api_data = None
        if isinstance(raw_xano_response_data, str):
            logger.info(f"Xano response parsed to a string for user {current_user_id}. Attempting to parse string content as JSON.")
            try:
                api_data = json.loads(raw_xano_response_data)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to decode JSON string returned by Xano for user {current_user_id}. String was: {raw_xano_response_data[:500]}", exc_info=True)
                return jsonify({"error": "Invalid JSON content in string from optimization service."}), 500
        elif isinstance(raw_xano_response_data, dict):
            api_data = raw_xano_response_data
        else:
            logger.error(f"Xano LinkedIn Optimizer response was not a dictionary or a parsable JSON string for user {current_user_id}. Type: {type(raw_xano_response_data)}. Data: {str(raw_xano_response_data)[:500]}")
            return jsonify({"error": "Optimization service returned an unexpected data format."}), 500
        
        if not isinstance(api_data, dict): # Final check, should be redundant if logic above is correct
            logger.error(f"api_data is not a dictionary after all parsing attempts for user {current_user_id}. Type: {type(api_data)}. Data: {str(api_data)[:500]}")
            return jsonify({"error": "Failed to obtain valid JSON object from optimization service after parsing."}), 500

        if not api_data: 
             logger.warning(f"Xano LinkedIn Optimizer API returned empty data object for user {current_user_id} after parsing. API Data: {api_data}")
             return jsonify({"error": "Optimization service returned empty data."}), 500

        user_display_name = (user.user_metadata.get('full_name') or
                             user.user_metadata.get('name') or
                             user.email or 
                             current_user_id) 

        insert_data = {
            "uid": current_user_id,
            "display name": user_display_name,
            "linkedin_url": linkedin_url,
            "comments": comments,
            "api_response": api_data 
        }
        
        logger.info(f"Inserting LinkedIn optimization data for user {current_user_id} into Supabase.")
        result = extensions.supabase.table("linkedIn_optimizer").insert(insert_data).execute()

        # More robust check for Supabase insert success
        if result.data: # Supabase often returns the inserted data array on success
            logger.info(f"Successfully saved LinkedIn optimization data for user {current_user_id}. DB Response: {result.data}")
        elif hasattr(result, 'error') and result.error:
            logger.error(f"Supabase insert failed for user {current_user_id}. Error: {result.error.message if hasattr(result.error, 'message') else result.error}", exc_info=True)
            return jsonify({"error": f"Failed to save linkedin optimization data: {result.error.message if hasattr(result.error, 'message') else 'Unknown Supabase error'}"}), 500
        elif not (hasattr(result, 'status_code') and 200 <= result.status_code < 300): # Fallback check if no data and no error object
             logger.error(f"Supabase insert failed or returned unexpected status for user {current_user_id}. Result: {result}", exc_info=True)
             return jsonify({"error": "Failed to save linkedin optimization data to database."}), 500
        else:
            logger.info(f"Supabase insert for user {current_user_id} reported success but returned no data (e.g., status {result.status_code}). Assuming OK.")
            # This case can happen, e.g. if `prefer="return=minimal"` was set, or for some operations.

        return jsonify(api_data), 200 # Return Xano's response to the client

    except requests.exceptions.HTTPError as http_err:
        error_message = f"Error from optimization service (HTTP {http_err.response.status_code})"
        try:
            xano_error_details = http_err.response.json()
            error_message += f" - Details: {xano_error_details}"
        except ValueError: 
            error_message += f" - Response body: {http_err.response.text}"
        logger.error(f"HTTPError calling Xano for /linkedin-optimizer, user {current_user_id}: {error_message}", exc_info=True)
        return jsonify({"error": error_message}), getattr(http_err.response, 'status_code', 502)
    except requests.exceptions.Timeout:
        logger.warning(f"Request to Xano API /linkedin-optimizer timed out for user {current_user_id}.")
        return jsonify({"error": "The optimization service timed out. Please try again."}), 504
    except requests.exceptions.RequestException as e: 
        logger.error(f"RequestException calling Xano API /linkedin-optimizer for user {current_user_id}: {str(e)}", exc_info=True)
        return jsonify({"error": f"Could not connect to optimization service: {str(e)}"}), 503
    except Exception as e: 
        logger.error(f"Unexpected error processing /linkedin-optimizer POST request for user {current_user_id}: {str(e)}", exc_info=True)
        return jsonify({"error": f"An unexpected server error occurred: {str(e)}"}), 500

        