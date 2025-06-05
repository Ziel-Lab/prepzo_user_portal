from flask import request, jsonify, current_app
from flask_cors import CORS
import requests 
import os
from app import extensions 
import json
from dotenv import load_dotenv
import logging

from . import cover_letter_bp 

load_dotenv()
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
XANO_API_URL_COVER_LETTER = os.getenv("XANO_API_URL_COVER_LETTER")

CORS(cover_letter_bp, origins=[FRONTEND_URL], supports_credentials=True, methods=["POST", "GET", "OPTIONS"])

def get_authenticated_user():
    """Helper to extract and validate JWT token and return user object."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        logger = current_app.logger if hasattr(current_app, 'logger') else logging.getLogger(__name__)
        logger.warning("get_authenticated_user: Missing or invalid Authorization header.")
        return None, jsonify({"error": "Missing or invalid Authorization header"}), 401

    jwt_token = auth_header.split(" ")[1]
    try:
        user_response = extensions.supabase.auth.get_user(jwt=jwt_token)
        user = user_response.user
        if not user or not user.id:
            logger = current_app.logger if hasattr(current_app, 'logger') else logging.getLogger(__name__)
            logger.warning("get_authenticated_user: Supabase returned no user or user.id for the token.")
            return None, jsonify({"error": "Invalid token or user not found"}), 401
        return user, None, None  
    except Exception as e:
        logger = current_app.logger if hasattr(current_app, 'logger') else logging.getLogger(__name__)
        logger.error(f"get_authenticated_user: Authentication failed. Exception type: {type(e).__name__}, Error: {str(e)}")
        return None, jsonify({"error": f"Authentication failed: {str(e)}"}), 401

@cover_letter_bp.route("/create-cover-letter", methods=["POST", "OPTIONS"])
@cross_origin(origins=[FRONTEND_URL], supports_credentials=True, methods=["POST", "OPTIONS"])
def create_cover_letter():
    if request.method == "OPTIONS":
        return "", 204
        
    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code

    current_user_id = str(user.id)
    logger = current_app.logger

    logger.info(f"XANO_API_URL_COVER_LETTER for create-cover-letter: {XANO_API_URL_COVER_LETTER}")

    if not XANO_API_URL_COVER_LETTER:
        logger.error("XANO_API_URL_COVER_LETTER is not set. Ensure it's in .env or app config.")
        return jsonify({"error": "Server configuration error: Missing API URL."}), 500

    try:
        data = request.form
        current_resume_url = data.get("current_resume")
        job_description_text = data.get("job_description")
        company_website_text = data.get("company_website")
        user_additional_comments_text = data.get("additional_comments")

        if not all([current_resume_url, job_description_text]):
            return jsonify({"error": "Missing required fields: current_resume (URL) and job_description"}), 400

        xano_payload = {
            "current_resume": current_resume_url,
            "job_description": job_description_text,
            "company_website": company_website_text,
            "additional_comments": user_additional_comments_text
        }

        logger.info(f"Sending payload to Xano for cover letter: {json.dumps(xano_payload)[:200]}...")
        xano_response = requests.post(XANO_API_URL_COVER_LETTER, json=xano_payload, timeout=120)
        xano_response.raise_for_status()
        xano_data = xano_response.json()
        logger.info("Received response from Xano for cover letter.")

        parsed_feedback_from_xano = None
        raw_feedback_payload_str = xano_data.get("feedback")

        if isinstance(raw_feedback_payload_str, str):
            try:
                parsed_feedback_from_xano = json.loads(raw_feedback_payload_str)
            except json.JSONDecodeError as e:
                logger.error(f"Cover Letter: Error decoding JSON string from Xano 'feedback' key: {e}. Storing raw string or null.")
                parsed_feedback_from_xano = {"error": "Failed to parse feedback string", "raw_feedback": raw_feedback_payload_str}

        elif raw_feedback_payload_str is not None: # It exists but is not a string
             logger.warning(f"Cover Letter: Xano 'feedback' key present but not a string. Type: {type(raw_feedback_payload_str)}")
             parsed_feedback_from_xano = {"error": "Feedback key not a string", "raw_feedback": raw_feedback_payload_str}
        else: # feedback key is missing
            logger.warning(f"Cover Letter: Xano 'feedback' key missing in response.")
            parsed_feedback_from_xano = {"error": "Feedback key missing in Xano response"}

        db_payload = {
            "uid": current_user_id,
            "job_description": job_description_text,
            "company_website": company_website_text,
            "current_resume": current_resume_url,
            "additional_comments": user_additional_comments_text, 
            "feedback": parsed_feedback_from_xano 
        }

        try:
            insert_response = extensions.supabase.table("cover_letter").insert(db_payload).execute()
            if not insert_response.data:
                logger.warning(f"Supabase insert into cover_letter may have failed or returned no data. Response: {insert_response}")
        except Exception as e:
            logger.error(f"Error inserting into cover_letter table: {str(e)}", exc_info=True)

        if parsed_feedback_from_xano and "error" not in parsed_feedback_from_xano:
            return jsonify(parsed_feedback_from_xano), xano_response.status_code
        else: 
            error_detail_for_client = parsed_feedback_from_xano if parsed_feedback_from_xano else {"error": "Processing Xano response failed"}
            return jsonify({"message": "Xano request processed, but there was an issue with feedback content.", 
                            "xano_response_status": xano_response.status_code,
                            "details": error_detail_for_client,
                            "full_xano_response_preview": xano_data if 'feedback' not in xano_data else {k:v for k,v in xano_data.items() if k != 'feedback'} 
                           }), 200 

    except requests.exceptions.HTTPError as http_err:
        error_detail_msg = str(http_err.response.text) if http_err.response else str(http_err)
        logger.error(f"Xano API HTTPError in create_cover_letter: {http_err}, Response: {error_detail_msg}", exc_info=True)
        try:
            error_detail = http_err.response.json()
        except ValueError:
            error_detail = error_detail_msg
        return jsonify({"error": "Xano API request failed", "details": error_detail}), http_err.response.status_code if http_err.response else 500
    except requests.exceptions.RequestException as req_err:
        logger.error(f"Xano API RequestException in create_cover_letter: {req_err}", exc_info=True)
        return jsonify({"error": "Request to Xano API failed", "details": str(req_err)}), 500
    except Exception as e:
        logger.error(f"Unexpected error in create_cover_letter: {str(e)}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500


@cover_letter_bp.route("/get-cover-letters", methods=["GET", "OPTIONS"])
@cross_origin(origins=[FRONTEND_URL], supports_credentials=True, methods=["GET", "OPTIONS"])
def get_cover_letters():
    if request.method == "OPTIONS":
        return "", 204

    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code
    current_user_id = str(user.id)
    logger = current_app.logger

    try:
        query_response = (
            extensions.supabase.table("cover_letter")
            .select("*")  
            .eq("uid", current_user_id)
            .execute()
        )
        return jsonify(query_response.data or []), 200
    except Exception as e:
        logger.error(f"Error fetching from cover_letter table: {str(e)}", exc_info=True)
        return jsonify({"error": f"Could not retrieve cover letters: {str(e)}"}), 500







