from flask import request, jsonify, current_app, g
import requests 
from app import extensions 
import json
from app.userPortal.subscription.helpers import require_authentication, check_and_use_feature
from app.utils.amplitude import cover_letter_event

from . import cover_letter_bp 

@cover_letter_bp.route("/create-cover-letter", methods=["POST", "OPTIONS"])
@require_authentication
@check_and_use_feature('cover_letter')
def create_cover_letter():
    current_user_id = str(g.user.id)

    frontend_url = current_app.config.get("FRONTEND_ORIGIN", "http://localhost:3000")

    # Use the n8n webhook for cover-letter generation instead of the legacy Xano endpoint
    n8n_api_url_cover_letter = "https://prepzo.app.n8n.cloud/webhook/cover-letter"

    try:
        # Accept either JSON or form-encoded payloads for maximum compatibility with clients
        data = request.get_json(silent=True) if request.is_json else request.form

        # Map legacy field names (used by previous Xano integration) to the new n8n parameter names
        current_resume_url = data.get("current_resume") or data.get("resume_url")
        job_description_text = data.get("job_description")
        company_website_text = data.get("company_website") or data.get("company_url")
        user_additional_comments_text = data.get("additional_comments")

        if not all([current_resume_url, job_description_text]):
            return jsonify({"error": "Missing required fields: current_resume (URL) and job_description"}), 400

        # n8n expects the following payload keys
        n8n_payload = {
            "resume_url": current_resume_url,
            "company_url": company_website_text,
            "job_description": job_description_text,
            "additional_comments": user_additional_comments_text
        }

        n8n_response = requests.post(n8n_api_url_cover_letter, json=n8n_payload, timeout=180)
        n8n_response.raise_for_status()

        n8n_data = n8n_response.json()

        # The webhook returns a list with a single element containing an 'output' object
        parsed_feedback_from_xano = None  # kept variable name for DB compatibility

        try:
            if isinstance(n8n_data, list) and len(n8n_data) > 0 and isinstance(n8n_data[0], dict):
                parsed_feedback_from_xano = n8n_data[0].get("output")
                if parsed_feedback_from_xano is None:
                    raise ValueError("'output' key missing in n8n response item")
            else:
                raise ValueError("Unexpected n8n response format – expected list with first item as dict")
        except Exception as e:
            print(f"Cover Letter: Failed to parse n8n response: {e}. Raw response: {n8n_data}")
            parsed_feedback_from_xano = {"error": "Unexpected response format", "raw_response": n8n_data}


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
                print(f"Warning: Supabase insert into cover_letter may have failed or returned no data. Response: {insert_response}")
        except Exception as e:
            print(f"Error inserting into cover_letter table: {str(e)}")

        # Send the event to Amplitude
        try:
            cover_letter_event(
                current_user_id,
                company_website_text,
                current_resume_url,
                (parsed_feedback_from_xano or {}).get("cover_letter"),
                user_additional_comments_text,
                parsed_feedback_from_xano,
            )
        except Exception as e:
            current_app.logger.warning(f"Failed to send Amplitude event: {e}")

        if parsed_feedback_from_xano and "error" not in parsed_feedback_from_xano:
            return jsonify(parsed_feedback_from_xano), 200
        else: 
            error_detail_for_client = parsed_feedback_from_xano if parsed_feedback_from_xano else {"error": "Processing Xano response failed"}
            return jsonify({
                "message": "n8n request processed, but there was an issue with feedback content.",
                "n8n_response_status": n8n_response.status_code,
                "details": error_detail_for_client,
                "raw_n8n_response": n8n_data,
            }), 207


    except requests.exceptions.HTTPError as http_err:
        try:
            error_detail = http_err.response.json()
        except ValueError:
            error_detail = str(http_err.response.text) 
        return jsonify({"error": "n8n webhook request failed", "details": error_detail}), http_err.response.status_code
    except requests.exceptions.RequestException as req_err:
        return jsonify({"error": "Request to n8n webhook failed", "details": str(req_err)}), 500
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






