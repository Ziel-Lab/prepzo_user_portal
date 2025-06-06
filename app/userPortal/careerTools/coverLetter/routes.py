from flask import request, jsonify, current_app
from flask_cors import CORS
import requests 
import os
from app import extensions 
import json
from dotenv import load_dotenv
import logging
import base64 # Added for Data URI conversion

from . import cover_letter_bp 

load_dotenv()
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN")
XANO_API_URL_COVER_LETTER = os.getenv("XANO_API_URL_COVER_LETTER")

# Critical check for FRONTEND_URL
if not FRONTEND_URL:
    if os.getenv("FLASK_ENV") == "development" or os.getenv("FLASK_DEBUG") == "1":
        FRONTEND_URL = "http://localhost:3000"
        print(f"WARNING: [coverLetter/routes.py] FRONTEND_ORIGIN not set, defaulting to {FRONTEND_URL} for CORS (dev mode).")
    else:
        raise RuntimeError("CRITICAL: [coverLetter/routes.py] FRONTEND_ORIGIN environment variable is not set. CORS will not be configured correctly.")


CORS(cover_letter_bp, origins=[FRONTEND_URL], supports_credentials=True, methods=["POST", "GET", "OPTIONS", "DELETE"])

def get_authenticated_user():
    """Helper to extract and validate JWT token and return user object."""
    logger = current_app.logger if current_app and hasattr(current_app, 'logger') else logging.getLogger(__name__)
    
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        logger.warning("get_authenticated_user (coverLetter): Missing or invalid Authorization header.")
        return None, jsonify({"error": "Missing or invalid Authorization header"}), 401

    jwt_token = auth_header.split(" ")[1]
    try:
        logger.info(f"get_authenticated_user (coverLetter): Attempting to validate token (first 10 chars): {jwt_token[:10]}...")
        user_response = extensions.supabase.auth.get_user(jwt=jwt_token)
        user = user_response.user
        if not user or not user.id:
            logger.warning("get_authenticated_user (coverLetter): Supabase returned no user or user.id for the token.")
            return None, jsonify({"error": "Invalid token or user not found"}), 401
        logger.info(f"get_authenticated_user (coverLetter): Successfully authenticated user {user.id}.")
        return user, None, None  
    except Exception as e: # Catching generic Exception is broad, consider specific AuthApiError if applicable
        logger.error(f"get_authenticated_user (coverLetter): Authentication failed. Exception type: {type(e).__name__}, Error: {str(e)}", exc_info=True)
        return None, jsonify({"error": f"Authentication failed: {str(e)}"}), 401

@cover_letter_bp.route("/create-cover-letter", methods=["POST", "OPTIONS"])
def create_cover_letter():
    logger = current_app.logger
    if request.method == "OPTIONS":
        return "", 204
        
    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code

    current_user_id = str(user.id)
    
    if not XANO_API_URL_COVER_LETTER:
        logger.critical(f"User {current_user_id}: XANO_API_URL_COVER_LETTER is not configured!")
        return jsonify({"error": "Server configuration error: Missing API URL."}), 500

    try:
        data = request.form
        current_resume_url_from_form = data.get("current_resume") # Original Supabase URL
        job_description_text = data.get("job_description")
        company_website_text = data.get("company_website")
        user_additional_comments_text = data.get("additional_comments")

        if not all([current_resume_url_from_form, job_description_text]):
            logger.warning(f"User {current_user_id} called /create-cover-letter with missing fields: current_resume_url or job_description.")
            return jsonify({"error": "Missing required fields: current_resume (URL) and job_description"}), 400

        # --- Logic to determine if resume is PDF URL or needs Data URI ---
        document_type = None
        resume_id_from_db = None # Optional, can be useful for linking if needed later
        try:
            logger.info(f"User {current_user_id}: Fetching document_type for resume URL: {current_resume_url_from_form}")
            doc_query_response = extensions.supabase.table("user_documents") \
                .select("id, document_type") \
                .eq("document_url", current_resume_url_from_form) \
                .eq("uid", current_user_id) \
                .single() \
                .execute()
            
            if doc_query_response.data:
                resume_id_from_db = doc_query_response.data.get("id")
                document_type = doc_query_response.data.get("document_type")
                logger.info(f"User {current_user_id}: Found document in DB. ID: {resume_id_from_db}, Type: {document_type}, URL: {current_resume_url_from_form}")
                if not document_type:
                    logger.error(f"User {current_user_id}: Document type (mimetype) is null in DB for resume URL: {current_resume_url_from_form}.")
                    return jsonify({"error": "Could not determine the file type of the resume from database."}), 400
            else:
                logger.warning(f"User {current_user_id}: Resume document not found in user_documents for URL: {current_resume_url_from_form} or does not belong to user.")
                return jsonify({"error": "Resume document not found or access denied for the provided URL."}), 404
        
        except Exception as e_db_query:
            logger.error(f"User {current_user_id}: Error querying user_documents for URL {current_resume_url_from_form}: {str(e_db_query)}", exc_info=True)
            return jsonify({"error": "Failed to verify resume document due to a database error."}), 500

        data_for_xano_resume = ""
        if document_type == "application/pdf":
            data_for_xano_resume = current_resume_url_from_form
            logger.info(f"User {current_user_id}: Sending PDF resume URL to Xano for cover letter: {current_resume_url_from_form}")
        else:
            logger.info(f"User {current_user_id}: Resume document type is {document_type}. Attempting to download and convert to Data URI for Xano.")
            try:
                file_response = requests.get(current_resume_url_from_form, timeout=30) 
                file_response.raise_for_status() 
                file_content = file_response.content
                base64_encoded_content = base64.b64encode(file_content).decode('utf-8')
                data_for_xano_resume = f"data:{document_type};base64,{base64_encoded_content}"
                logger.info(f"User {current_user_id}: Successfully created Data URI for resume (type {document_type}). Length: {len(data_for_xano_resume)}.")
            except requests.exceptions.RequestException as re:
                logger.error(f"User {current_user_id}: Failed to download resume file from URL {current_resume_url_from_form} for Data URI conversion. Error: {str(re)}", exc_info=True)
                return jsonify({"error": f"Failed to download resume file from URL: {str(re)}"}), 500
            except Exception as e_conv:
                logger.error(f"User {current_user_id}: Error during Data URI conversion for resume {current_resume_url_from_form}. Error: {str(e_conv)}", exc_info=True)
                return jsonify({"error": "Failed to convert resume file for cover letter generation."}), 500
        
        if not data_for_xano_resume: 
            logger.error(f"User {current_user_id}: data_for_xano_resume is unexpectedly empty after processing {current_resume_url_from_form}.")
            return jsonify({"error": "Internal error preparing resume data for cover letter generation."}), 500
        # --- End of resume data preparation ---

        xano_payload = {
            "current_resume": data_for_xano_resume, # This is now either URL or Data URI
            "job_description": job_description_text,
            "company_website": company_website_text,
            "additional_comments": user_additional_comments_text
        }

        logger.info(f"User {current_user_id} sending payload to Xano for cover letter. Resume part: {str(data_for_xano_resume)[:100] if document_type == 'application/pdf' else 'Data URI (content omitted)'}...")
        xano_response = requests.post(XANO_API_URL_COVER_LETTER, json=xano_payload, timeout=120)
        xano_response.raise_for_status()
        xano_data = xano_response.json()
        logger.info(f"User {current_user_id} received response from Xano for cover letter.")

        parsed_feedback_from_xano = None
        raw_feedback_payload_str = xano_data.get("feedback")

        if isinstance(raw_feedback_payload_str, str):
            try:
                parsed_feedback_from_xano = json.loads(raw_feedback_payload_str)
            except json.JSONDecodeError as e:
                logger.error(f"Cover Letter (user {current_user_id}): Error decoding JSON string from Xano 'feedback' key: {e}. Raw: {raw_feedback_payload_str[:200]}...", exc_info=True)
                parsed_feedback_from_xano = {"error": "Failed to parse feedback string", "raw_feedback": raw_feedback_payload_str}
        elif raw_feedback_payload_str is not None: 
             logger.warning(f"Cover Letter (user {current_user_id}): Xano 'feedback' key present but not a string. Type: {type(raw_feedback_payload_str)}. Value: {str(raw_feedback_payload_str)[:200]}...")
             parsed_feedback_from_xano = {"error": "Feedback key not a string", "raw_feedback": raw_feedback_payload_str}
        else: 
            logger.warning(f"Cover Letter (user {current_user_id}): Xano 'feedback' key missing in response: {xano_data}")
            parsed_feedback_from_xano = {"error": "Feedback key missing in Xano response"}

        db_payload = {
            "uid": current_user_id,
            "job_description": job_description_text,
            "company_website": company_website_text,
            "current_resume": current_resume_url_from_form, # Store the original Supabase URL
            "additional_comments": user_additional_comments_text, 
            "feedback": parsed_feedback_from_xano,
            "resume_id": resume_id_from_db # Storing the resume_id from user_documents
        }

        try:
            logger.info(f"Inserting cover letter data for user {current_user_id} into Supabase.")
            insert_response = extensions.supabase.table("cover_letter").insert(db_payload).execute()
            if insert_response.data:
                logger.info(f"Successfully saved cover letter data for user {current_user_id}. DB Response: {insert_response.data}")
            elif hasattr(insert_response, 'error') and insert_response.error:
                logger.error(f"Supabase insert failed for cover_letter (user {current_user_id}). Error: {insert_response.error.message if hasattr(insert_response.error, 'message') else insert_response.error}", exc_info=True)
            elif not (hasattr(insert_response, 'status_code') and 200 <= insert_response.status_code < 300):
                 logger.error(f"Supabase insert for cover_letter (user {current_user_id}) failed or returned unexpected status. Result: {insert_response}", exc_info=True)
            else:
                logger.info(f"Supabase insert for cover_letter (user {current_user_id}) reported success but returned no data (e.g., status {insert_response.status_code}). Assuming OK.")

        except Exception as e:
            logger.error(f"Error inserting into cover_letter table for user {current_user_id}: {str(e)}", exc_info=True)

        if parsed_feedback_from_xano and not parsed_feedback_from_xano.get("error"):
            return jsonify(parsed_feedback_from_xano), xano_response.status_code
        else: 
            error_detail_for_client = parsed_feedback_from_xano if parsed_feedback_from_xano else {"error": "Processing Xano response failed"}
            logger.warning(f"Cover letter for user {current_user_id} processed, but Xano feedback had issues or was missing. Details: {error_detail_for_client}")
            client_response_data = {
                "message": "Cover letter request processed, but there may have been an issue with the feedback content from the AI.", 
                "xano_response_status": xano_response.status_code,
                "details": error_detail_for_client,
                "full_xano_response_preview": {k:v for k,v in xano_data.items() if k != 'feedback'} if xano_data else None
            }
            return jsonify(client_response_data), 200

    except requests.exceptions.HTTPError as http_err:
        error_detail_msg = str(http_err.response.text) if http_err.response else str(http_err)
        logger.error(f"Xano API HTTPError in create_cover_letter (user {current_user_id}): {http_err}, Response: {error_detail_msg}", exc_info=True)
        try:
            error_detail = http_err.response.json()
        except ValueError:
            error_detail = error_detail_msg
        return jsonify({"error": "Cover letter generation service request failed", "details": error_detail}), http_err.response.status_code if http_err.response else 502
    except requests.exceptions.RequestException as req_err:
        logger.error(f"Xano API RequestException in create_cover_letter (user {current_user_id}): {req_err}", exc_info=True)
        return jsonify({"error": "Could not connect to cover letter generation service", "details": str(req_err)}), 503
    except Exception as e:
        logger.error(f"Unexpected error in create_cover_letter (user {current_user_id}): {str(e)}", exc_info=True)
        return jsonify({"error": "An unexpected server error occurred", "details": str(e)}), 500


@cover_letter_bp.route("/get-cover-letters", methods=["GET", "OPTIONS"])
def get_cover_letters():
    logger = current_app.logger
    if request.method == "OPTIONS":
        return "", 204

    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code
    current_user_id = str(user.id)

    try:
        logger.info(f"Fetching cover letters for user {current_user_id}.")
        query_response = (
            extensions.supabase.table("cover_letter")
            .select("*")  
            .eq("uid", current_user_id)
            .order('created_at', desc=True)
            .execute()
        )
        logger.info(f"Successfully fetched {len(query_response.data if query_response.data else [])} cover letters for user {current_user_id}.")
        return jsonify(query_response.data or []), 200
    except Exception as e:
        logger.error(f"Error fetching from cover_letter table for user {current_user_id}: {str(e)}", exc_info=True)
        return jsonify({"error": f"Could not retrieve cover letters due to a server error."}), 500

@cover_letter_bp.route("/delete-cover-letter/<int:letter_id>", methods=["DELETE", "OPTIONS"])
def delete_cover_letter(letter_id):
    logger = current_app.logger
    if request.method == "OPTIONS":
        return "", 204

    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code
    current_user_id = str(user.id)

    try:
        logger.info(f"Attempting to delete cover letter id {letter_id} for user {current_user_id}.")
        delete_response = extensions.supabase.table("cover_letter") \
            .delete() \
            .eq("id", letter_id) \
            .eq("uid", current_user_id) \
            .execute()

        if delete_response.data:
            logger.info(f"Successfully deleted cover letter id {letter_id} for user {current_user_id}.")
            return jsonify({"message": "Cover letter deleted successfully"}), 200
        else:
            if hasattr(delete_response, 'error') and delete_response.error:
                 logger.warning(f"Failed to delete cover letter id {letter_id} for user {current_user_id}. Error: {delete_response.error}")
                 return jsonify({"error": f"Could not delete cover letter: {delete_response.error.message if hasattr(delete_response.error, 'message') else 'Details unavailable'}"}), 404
            logger.warning(f"Cover letter id {letter_id} not found for user {current_user_id} or no rows affected by delete.")
            return jsonify({"error": "Cover letter not found or you do not have permission to delete it"}), 404

    except Exception as e:
        logger.error(f"Error deleting cover letter id {letter_id} for user {current_user_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred while deleting the cover letter."}), 500







