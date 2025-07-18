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
    """
    Asynchronously triggers a long-running n8n workflow to generate a cover letter.

    This endpoint immediately returns a 202 Accepted response after triggering
    the webhook. It does not wait for the workflow to complete. The client
    is expected to poll the /get-cover-letters endpoint to retrieve the result.
    """
    n8n_api_url_cover_letter = "https://prepzo.app.n8n.cloud/webhook/cover-letter"

    try:
        data = request.get_json(silent=True) if request.is_json else request.form
        if not data:
            return jsonify({"error": "Invalid or missing request body"}), 400

        current_resume_url = data.get("current_resume") or data.get("resume_url")
        job_description_text = data.get("job_description")
        company_website_text = data.get("company_website") or data.get("company_url")
        user_additional_comments_text = data.get("additional_comments")

        if not all([current_resume_url, job_description_text]):
            return jsonify({"error": "Missing required fields: current_resume (or resume_url) and job_description"}), 400

        n8n_payload = {
            "resume_url": current_resume_url,
            "company_url": company_website_text,
            "job_description": job_description_text,
            "additional_comments": user_additional_comments_text,
            # Pass the user ID to the webhook so it can associate the result correctly
            "user_id": str(g.user.id)
        }
        
        # Trigger the n8n webhook but don't wait for it to finish.
        # A short timeout ensures we "fire and forget".
        try:
            requests.post(n8n_api_url_cover_letter, json=n8n_payload, timeout=2)
        except requests.exceptions.Timeout:
            # This is the expected outcome for a fire-and-forget request.
            pass
        except requests.exceptions.RequestException as e:
            # This catches actual network errors (e.g., DNS failure, connection refused).
            current_app.logger.error(f"Failed to trigger n8n webhook: {e}")
            return jsonify({"error": "Failed to trigger cover letter generation process."}), 500

        # The job was successfully accepted for processing.
        return jsonify({"message": "Cover letter generation has been started."}), 202

    except Exception as e:
        current_app.logger.error(f"Unexpected error in create_cover_letter: {str(e)}")
        return jsonify({"error": "An unexpected error occurred."}), 500


@cover_letter_bp.route("/get-cover-letters", methods=["GET", "OPTIONS"])
@require_authentication
def get_cover_letters():
    current_user_id = str(g.user.id)
    try:
        # Fetch only the most-recent entry for the current user
        query_response = (
            extensions.supabase.table("cover_letter")
            .select("*")
            .eq("uid", current_user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return jsonify(query_response.data or []), 200
    except Exception as e:
        print(f"Error fetching from cover_letter table: {str(e)}")
        return jsonify({"error": f"Could not retrieve cover letters: {str(e)}"}), 500






