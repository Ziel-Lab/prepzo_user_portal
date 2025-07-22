from flask import request, jsonify, current_app, g
import requests 
from app import extensions 
import json
import logging
from threading import Thread
from app.userPortal.subscription.helpers import require_authentication, check_and_use_feature
from app.utils.amplitude import cover_letter_event

from . import cover_letter_bp 
import uuid

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
@check_and_use_feature('cover_letter', auto_increment=False)
def create_cover_letter():
    """
    Asynchronously triggers a long-running n8n workflow to generate a cover letter.

    This endpoint immediately returns a 202 Accepted response after triggering
    the webhook. It does not wait for the workflow to complete. The client
    is expected to poll the /get-cover-letters endpoint to retrieve the result.
    """
    n8n_api_url_cover_letter = "https://prepzo.app.n8n.cloud/webhook/cover-letter"

    try:
<<<<<<< HEAD
        data = request.get_json(silent=True) if request.is_json else request.form
        if not data:
            return jsonify({"error": "Invalid or missing request body"}), 400

        current_resume_url = data.get("current_resume") or data.get("resume_url")
=======
        data = get_request_data()
        current_resume_url = data.get("current_resume")
>>>>>>> 27ce8f9a2bf2db39c1d3128d774d35244ae0f132
        job_description_text = data.get("job_description")
        company_website_text = data.get("company_website") or data.get("company_url")
        user_additional_comments_text = data.get("additional_comments")

        if not all([current_resume_url, job_description_text]):
<<<<<<< HEAD
            return jsonify({"error": "Missing required fields: current_resume (or resume_url) and job_description"}), 400

        job_id = str(uuid.uuid4())

        # Save pending job to database
=======
            return jsonify({"error": "Missing required fields: current_resume (URL) and job_description"}), 400

        xano_payload = {
            "current_resume": current_resume_url,
            "job_description": job_description_text,
            "company_website": company_website_text,
            "additional_comments": user_additional_comments_text
        }

        # Increase timeout for better reliability
        xano_response = requests.post(xano_api_url_cover_letter, json=xano_payload, timeout=200)
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

>>>>>>> 27ce8f9a2bf2db39c1d3128d774d35244ae0f132
        db_payload = {
            "uid": str(g.user.id),
            "job_id": job_id,
            "job_description": job_description_text,
            "company_website": company_website_text,
            "current_resume": current_resume_url,
            "additional_comments": user_additional_comments_text,
            "status": "PENDING",  
            "created_at": "now()"
        }
        
        extensions.supabase.table("cover_letter").insert(db_payload).execute()
        
        return jsonify({"job_id": job_id, "message": "Cover letter generation has been started."}), 202

<<<<<<< HEAD
=======
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
>>>>>>> 27ce8f9a2bf2db39c1d3128d774d35244ae0f132
    except Exception as e:
        current_app.logger.error(f"Unexpected error in create_cover_letter: {str(e)}")
        return jsonify({"error": "An unexpected error occurred."}), 500


@cover_letter_bp.route("/get-cover-letters", methods=["GET", "OPTIONS"])
@require_authentication
def get_cover_letters():
    current_user_id = str(g.user.id)
    try:
        job_id_param = request.args.get("job_id")

        if job_id_param:
            # Attempt to fetch the specific cover letter generated for this job_id
            query_response = (
                extensions.supabase.table("cover_letter")
                .select("*")
                .eq("job_id", job_id_param)
                .eq("uid", current_user_id)
                .execute()
            )
        else:
            # Fallback: return the most-recent cover letter for the user
            query_response = (
                extensions.supabase.table("cover_letter")
                .select("*")
                .eq("uid", current_user_id)
                .order("id", desc=True)
                .execute()
            )

        # Supabase v2 may return dict/None instead of PostgrestResponse.
        if query_response is None:
            return jsonify(None), 200

        result_data = getattr(query_response, "data", query_response)
        return jsonify(result_data or None), 200
    except Exception as e:
        print(f"Error fetching from cover_letter table: {str(e)}")
        return jsonify({"error": f"Could not retrieve cover letters: {str(e)}"}), 500






