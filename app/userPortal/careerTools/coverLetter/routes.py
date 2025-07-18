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
            "status": "pending",  # ← New status field
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
                .eq("uid", current_user_id)
                .eq("job_id", job_id_param)
                .maybe_single()
                .execute()
            )
        else:
            # Fallback: return the most-recent cover letter for the user
            query_response = (
                extensions.supabase.table("cover_letter")
                .select("*")
                .eq("uid", current_user_id)
                .order("id", desc=True)
                .limit(1)
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


@cover_letter_bp.route("/job-completed", methods=["POST"])
def job_completed():
    """
    Endpoint called by n8n when a cover letter job completes.
    This is where we increment usage since the job was successful.
    """
    # Simple API key authentication for n8n
    api_key = request.headers.get("X-API-Key")
    expected_key = current_app.config.get("N8N_API_KEY")
    
    if not api_key or api_key != expected_key:
        current_app.logger.warning(f"Unauthorized job-completed call from {request.remote_addr}")
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Missing request body"}), 400
            
        job_id = data.get("job_id")
        success = data.get("success", False)
        feedback = data.get("feedback")  # The generated cover letter content
        
        if not job_id:
            return jsonify({"error": "Missing job_id"}), 400
            
        # Find the job in the database
        job_query = extensions.supabase.table("cover_letter") \
            .select("*") \
            .eq("job_id", job_id) \
            .maybe_single() \
            .execute()
            
        if not job_query.data:
            return jsonify({"error": "Job not found"}), 404
            
        job = job_query.data
        user_id = job.get("uid")
        
        if success:
            # Update the job with completed status and feedback
            update_data = {
                "status": "completed",
                "feedback": feedback,
                "completed_at": "now()"
            }
            
            extensions.supabase.table("cover_letter") \
                .update(update_data) \
                .eq("job_id", job_id) \
                .execute()
            
            # Now increment usage since job succeeded
            try:
                current_app.logger.info(f"Cover letter job {job_id} completed successfully. Incrementing usage for user {user_id}.")
                
                # Get user's current usage record
                usage_res = extensions.supabase.table('feature_usage') \
                    .select('*') \
                    .eq('user_id', user_id) \
                    .order('period_end', desc=True) \
                    .limit(1) \
                    .maybe_single() \
                    .execute()
                
                if usage_res.data:
                    usage_record = usage_res.data
                    
                    # Increment the cover_letter usage counter
                    supabase = extensions.supabase
                    supabase.rpc('increment_feature_counters', {
                        'p_user_id': user_id,
                        'p_period_start': usage_record['period_start'],
                        'p_feature_base_name': 'cover_letter',
                        'p_increment_by': 1
                    }).execute()
                    
                    current_app.logger.info(f"Successfully incremented cover_letter usage for user {user_id}")
                else:
                    current_app.logger.warning(f"No usage record found for user {user_id}")
                    
            except Exception as e:
                current_app.logger.error(f"Failed to increment usage for completed job {job_id}: {e}")
                # Don't fail the whole request if usage tracking fails
                
            return jsonify({"message": "Job completed and usage incremented"}), 200
            
        else:
            # Job failed - update status but don't increment usage
            error_message = data.get("error", "Unknown error occurred")
            
            update_data = {
                "status": "failed",
                "feedback": {"error": error_message},
                "completed_at": "now()"
            }
            
            extensions.supabase.table("cover_letter") \
                .update(update_data) \
                .eq("job_id", job_id) \
                .execute()
                
            current_app.logger.warning(f"Cover letter job {job_id} failed: {error_message}")
            return jsonify({"message": "Job marked as failed"}), 200
            
    except Exception as e:
        current_app.logger.error(f"Error in job_completed endpoint: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500






