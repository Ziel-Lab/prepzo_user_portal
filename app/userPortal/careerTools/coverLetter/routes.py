from flask import request, jsonify, current_app, g
import requests 
from app import extensions 
import json
from app.userPortal.subscription.helpers import require_authentication, check_and_use_feature
from app.utils.amplitude import cover_letter_event

from . import cover_letter_bp 
import uuid

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
        data = request.get_json(silent=True) if request.is_json else request.form
        if not data:
            return jsonify({"error": "Invalid or missing request body"}), 400

        current_resume_url = data.get("current_resume") or data.get("resume_url")
        job_description_text = data.get("job_description")
        company_website_text = data.get("company_website") or data.get("company_url")
        user_additional_comments_text = data.get("additional_comments")

        if not all([current_resume_url, job_description_text]):
            return jsonify({"error": "Missing required fields: current_resume (or resume_url) and job_description"}), 400

        job_id = str(uuid.uuid4())

        # Save pending job to database
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







