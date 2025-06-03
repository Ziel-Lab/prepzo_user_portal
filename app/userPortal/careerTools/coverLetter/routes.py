from flask import request, jsonify
from flask_cors import CORS
import requests 
import os
from app import extensions 
import json

from . import cover_letter_bp 

FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000") 
XANO_API_URL = "https://xfsf-9afu-ywqu.m2.xano.io/api:_jdqfybN/cover_letter"

# Ensure this CORS configuration is active and includes all necessary methods
CORS(cover_letter_bp, origins=[FRONTEND_URL], supports_credentials=True, methods=["POST", "GET", "OPTIONS"])

def get_authenticated_user():
    """Helper to extract and validate JWT token and return user object."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None, jsonify({"error": "Missing or invalid Authorization header"}), 401

    jwt_token = auth_header.split(" ")[1]
    try:
        user_response = extensions.supabase.auth.get_user(jwt=jwt_token)
        user = user_response.user
        if not user or not user.id:
            return None, jsonify({"error": "Invalid token or user not found"}), 401
        return user, None, None  # Return user object, no error, no status
    except Exception as e:
        return None, jsonify({"error": f"Authentication failed: {str(e)}"}), 401

@cover_letter_bp.route("/create-cover-letter", methods=["POST", "OPTIONS"])
def create_cover_letter():
    if request.method == "OPTIONS":
        return "", 204
        
    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code

    current_user_id = str(user.id)
    # Not fetching user_name as it's not in the provided cover_letter table schema

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

        xano_response = requests.post(XANO_API_URL, json=xano_payload)
        xano_response.raise_for_status()
        xano_data = xano_response.json()

        parsed_feedback_from_xano = None
        # According to user, xano_data["feedback"] is a JSON string
        # {"cover_letter": "...", "additional_comments": "..."}
        raw_feedback_payload_str = xano_data.get("feedback")

        if isinstance(raw_feedback_payload_str, str):
            try:
                parsed_feedback_from_xano = json.loads(raw_feedback_payload_str)
            except json.JSONDecodeError as e:
                print(f"Cover Letter: Error decoding JSON string from Xano 'feedback' key: {e}. Storing raw string or null.")
                # Keep parsed_feedback_from_xano as None or potentially store raw_feedback_payload_str
                # For now, we will try to return the raw xano_data if parsing fails
                # and store None or the raw string in `generated_outputs`
                parsed_feedback_from_xano = {"error": "Failed to parse feedback string", "raw_feedback": raw_feedback_payload_str}

        elif raw_feedback_payload_str is not None: # It exists but is not a string
             print(f"Cover Letter: Xano 'feedback' key present but not a string. Type: {type(raw_feedback_payload_str)}")
             parsed_feedback_from_xano = {"error": "Feedback key not a string", "raw_feedback": raw_feedback_payload_str}
        else: # feedback key is missing
            print(f"Cover Letter: Xano 'feedback' key missing in response.")
            parsed_feedback_from_xano = {"error": "Feedback key missing in Xano response"}


        # Prepare DB payload based on provided schema + assumed 'generated_outputs'
        db_payload = {
            "uid": current_user_id,
            "job_description": job_description_text,
            "company_website": company_website_text,
            "current_resume": current_resume_url,
            "additional_comments": user_additional_comments_text, # User's input comments
            "feedback": parsed_feedback_from_xano # Use the new 'feedback' column
        }

        try:
            # Note: This assumes 'cover_letter' table exists and 'uid' is a direct uuid field
            # and there's a column named 'feedback' (JSON/JSONB)
            insert_response = extensions.supabase.table("cover_letter").insert(db_payload).execute()
            if not insert_response.data:
                print(f"Warning: Supabase insert into cover_letter may have failed or returned no data. Response: {insert_response}")
        except Exception as e:
            print(f"Error inserting into cover_letter table: {str(e)}")
            # Still return Xano's response even if DB insert fails for now

        # Return the parsed feedback from Xano (or error object if parsing failed)
        if parsed_feedback_from_xano and "error" not in parsed_feedback_from_xano:
            return jsonify(parsed_feedback_from_xano), xano_response.status_code
        else: # If parsing failed or feedback was missing, return the raw Xano data or specific error
             # This provides more context to the client than just the parsed_feedback_from_xano error object alone
            error_detail_for_client = parsed_feedback_from_xano if parsed_feedback_from_xano else {"error": "Processing Xano response failed"}
            return jsonify({"message": "Xano request processed, but there was an issue with feedback content.", 
                            "xano_response_status": xano_response.status_code,
                            "details": error_detail_for_client,
                            "full_xano_response_preview": xano_data if 'feedback' not in xano_data else {k:v for k,v in xano_data.items() if k != 'feedback'} # Avoid sending large string back again
                           }), 200 # 200 because Xano call was successful, issue is in parsing its content


    except requests.exceptions.HTTPError as http_err:
        try:
            error_detail = http_err.response.json()
        except ValueError:
            error_detail = str(http_err.response.text) # Use .text for non-JSON Xano errors
        return jsonify({"error": "Xano API request failed", "details": error_detail}), http_err.response.status_code
    except requests.exceptions.RequestException as req_err:
        return jsonify({"error": "Request to Xano API failed", "details": str(req_err)}), 500
    except Exception as e:
        print(f"Unexpected error in create_cover_letter: {str(e)}")
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500

# GET endpoint to retrieve cover letters for the user (optional, can be added if needed)
@cover_letter_bp.route("/get-cover-letters", methods=["GET", "OPTIONS"])
def get_cover_letters():
    if request.method == "OPTIONS":
        return "", 204

    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code
    current_user_id = str(user.id)
    try:
        query_response = (
            extensions.supabase.table("cover_letter")
            .select("*")  # Select all fields, including the assumed 'generated_outputs'
            .eq("uid", current_user_id)
            .execute()
        )
        return jsonify(query_response.data or []), 200
    except Exception as e:
        print(f"Error fetching from cover_letter table: {str(e)}")
        return jsonify({"error": f"Could not retrieve cover letters: {str(e)}"}), 500







