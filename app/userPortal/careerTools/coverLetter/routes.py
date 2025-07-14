from flask import request, jsonify, current_app, g
import requests 
from app import extensions 
import json
import logging
from threading import Thread
from app.userPortal.subscription.helpers import require_authentication, check_and_use_feature
from app.utils.amplitude import cover_letter_event

from . import cover_letter_bp 

def background_db_and_analytics(db_payload, amplitude_payload=None):
    """Fire-and-forget background processing for DB inserts and analytics"""
    try:
        insert_response = extensions.supabase.table("cover_letter").insert(db_payload).execute()
        if not insert_response.data:
            logging.warning(f"Warning: Supabase insert into cover_letter may have failed or returned no data. Response: {insert_response}")
    except Exception as e:
        logging.error(f"Background DB insert failed: {str(e)}")
    
    if amplitude_payload:
        try:
            cover_letter_event(**amplitude_payload)
            logging.info("Cover letter Amplitude event sent successfully.")
        except Exception as e:
            logging.warning(f"Background Amplitude event failed: {e}")

def get_request_data():
    """Unified request data handling for both JSON and form data"""
    if request.is_json:
        return request.get_json() or {}
    return request.form.to_dict()

@cover_letter_bp.route("/create-cover-letter", methods=["POST", "OPTIONS"])
@require_authentication
@check_and_use_feature('cover_letter')
def create_cover_letter():
    current_user_id = str(g.user.id)

    frontend_url = current_app.config.get("FRONTEND_ORIGIN", "http://localhost:3000")
    xano_api_url_cover_letter = current_app.config.get("XANO_API_URL_COVER_LETTER")

    try:
        data = get_request_data()
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

        # Reduce timeout for better reliability
        xano_response = requests.post(xano_api_url_cover_letter, json=xano_payload, timeout=60)
        xano_response.raise_for_status()
        xano_data = xano_response.json()

        parsed_feedback_from_xano = None
        raw_feedback_payload_str = xano_data.get("feedback")

        if isinstance(raw_feedback_payload_str, str):
            try:
                parsed_feedback_from_xano = json.loads(raw_feedback_payload_str)
            except json.JSONDecodeError as e:
                print(f"Cover Letter: Error decoding JSON string from Xano 'feedback' key: {e}. Storing raw string or null.")
                parsed_feedback_from_xano = {"error": "Failed to parse feedback string", "raw_feedback": raw_feedback_payload_str}

        elif raw_feedback_payload_str is not None: # It exists but is not a string
             print(f"Cover Letter: Xano 'feedback' key present but not a string. Type: {type(raw_feedback_payload_str)}")
             parsed_feedback_from_xano = {"error": "Feedback key not a string", "raw_feedback": raw_feedback_payload_str}
        else: # feedback key is missing
            print(f"Cover Letter: Xano 'feedback' key missing in response.")
            parsed_feedback_from_xano = {"error": "Feedback key missing in Xano response"}

        db_payload = {
            "uid": current_user_id,
            "job_description": job_description_text,
            "company_website": company_website_text,
            "current_resume": current_resume_url,
            "additional_comments": user_additional_comments_text, 
            "feedback": parsed_feedback_from_xano 
        }

        amplitude_payload = {
            "user_uuid": current_user_id,
            "company_url": company_website_text,           
            "original_resume_url": current_resume_url,             
            "cover_letter": xano_data.get("cover_letter"),              
            "additional_comments": user_additional_comments_text,  
            "feedback": parsed_feedback_from_xano       
        }

        # Fire-and-forget background processing
        Thread(
            target=background_db_and_analytics, 
            args=(db_payload, amplitude_payload), 
            daemon=True
        ).start()

        # Return immediately after Xano success
        if parsed_feedback_from_xano and "error" not in parsed_feedback_from_xano:
            return jsonify(parsed_feedback_from_xano), 200
        else: 
            error_detail_for_client = parsed_feedback_from_xano if parsed_feedback_from_xano else {"error": "Processing Xano response failed"}
            return jsonify({"message": "Xano request processed, but there was an issue with feedback content.", 
                            "xano_response_status": xano_response.status_code,
                            "details": error_detail_for_client,
                            "full_xano_response_preview": xano_data if 'feedback' not in xano_data else {k:v for k,v in xano_data.items() if k != 'feedback'}
                           }), 207

    except requests.exceptions.HTTPError as http_err:
        try:
            error_detail = http_err.response.json()
        except ValueError:
            error_detail = str(http_err.response.text) 
        return jsonify({"error": "Xano API request failed", "details": error_detail}), http_err.response.status_code
    except requests.exceptions.RequestException as req_err:
        return jsonify({"error": "Request to Xano API failed", "details": str(req_err)}), 500
    except Exception as e:
        print(f"Unexpected error in create_cover_letter: {str(e)}")
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500


@cover_letter_bp.route("/get-cover-letters", methods=["GET", "OPTIONS"])
@require_authentication
def get_cover_letters():
    current_user_id = str(g.user.id)
    try:
        query_response = (
            extensions.supabase.table("cover_letter")
            .select("*")  
            .eq("uid", current_user_id)
            .execute()
        )
        return jsonify(query_response.data or []), 200
    except Exception as e:
        print(f"Error fetching from cover_letter table: {str(e)}")
        return jsonify({"error": f"Could not retrieve cover letters: {str(e)}"}), 500






