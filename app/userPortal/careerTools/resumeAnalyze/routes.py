from flask import request, jsonify, current_app, g
import requests 
from app import extensions 
import magic
import json
import logging
import time
import uuid
from threading import Thread
from app.userPortal.subscription.helpers import require_authentication, check_and_use_feature
from app.utils.amplitude import resume_analyze_event
from . import resume_analyze_bp 
import tempfile
import os
import subprocess
from flask import send_file


def background_db_and_analytics(db_payload):
    """Fire-and-forget background processing for DB inserts (resume analyze & roast)."""
    try:
        result = extensions.supabase.table("analyze_resume").insert(db_payload).execute()
        if not result.data and not (hasattr(result, 'status_code') and 200 <= result.status_code < 300):
            logging.warning(f"Background Supabase insert failed or returned no data. Result: {result}")
    except Exception as e:
        logging.error(f"Background DB insert failed: {str(e)}")
        
@resume_analyze_bp.route("/analyze-resume", methods=["POST", "OPTIONS"])
@require_authentication
@check_and_use_feature('resume', auto_increment=False)
def analyze_resume():
    current_user_id = str(g.user.id)
    user_name = g.user.user_metadata.get('name') or \
                g.user.user_metadata.get('display_name') or \
                g.user.email or current_user_id

    try:
        data = request.get_json(silent=True) if request.is_json else request.form
        current_resume_url = data.get("current_resume") 
        job_description = data.get("job_description")
        company_website = data.get("company_website")
        additional_comment_text = data.get("additional_comments") 
        job_id = str(uuid.uuid4())

        if not all([current_resume_url, job_description]):
            return jsonify({"error": "Missing required fields: current_resume (URL) and job_description"}), 400

        # Prepare payload for n8n webhook
        n8n_payload = {
            "current_resume": current_resume_url,
            "job_description": job_description,
            "company_website": company_website,
            "additional_comments": additional_comment_text,
            "user_id": current_user_id,
            "user_name": user_name,
            "job_id": job_id,
        }

        # Remove None values
        n8n_payload = {k: v for k, v in n8n_payload.items() if v is not None}

        # Insert a pending record into analyze_resume (non-blocking)
        db_payload = {
            "user_id": current_user_id,
            "user_name": user_name,
            "job_id": job_id,
            "current_resume": current_resume_url,
            "company_website": company_website,
            "job_description": job_description,
            "additional_comment": additional_comment_text,
            "status": "PENDING",
            "feedback_analysis": None,
            "created_at": "now()",
        }
        Thread(target=background_db_and_analytics, args=(db_payload,)).start()

        # Call the n8n webhook
        n8n_url = "https://prepzo.app.n8n.cloud/webhook/prod_resume_analyzer"
        # n8n_response = requests.post(n8n_url, json=n8n_payload, timeout=30)

        # if n8n_response.status_code != 200:
        #     return jsonify({"error": "Resume analysis service failed", "details": n8n_response.text}), n8n_response.status_code

        # Return the response from n8n (assumes JSON)
        try:
            # return jsonify(n8n_response.json()), 200
            return jsonify({"message": "Resume analysis request accepted", "job_id": job_id}), 202
        except ValueError:
            # Fallback if response isn't JSON
            return jsonify({"message": "Resume analysis request accepted", "job_id": job_id}), 202

    except requests.exceptions.Timeout:
        logging.error("Resume analysis request timed out")
        return jsonify({"error": "The resume analysis service is taking too long to respond. Please try again later."}), 504
    except requests.exceptions.RequestException as req_err:
        return jsonify({"error": "Request to resume analysis service failed", "details": str(req_err)}), 500
    except Exception as e:
        logging.error(f"A FATAL UNHANDLED EXCEPTION occurred in analyze_resume: {e}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500

@resume_analyze_bp.route("/get-analyze-resume", methods=["GET", "OPTIONS"])
@require_authentication
def get_analyze_resume():
    current_user_id = str(g.user.id)
    try:
        job_id_param = request.args.get("job_id")

        if job_id_param:
            query_response = (
                extensions.supabase.table("analyze_resume")
                .select("*")
                .eq("user_id", current_user_id)
                .eq("job_id", job_id_param)
                .order("created_at", desc=True)
                .execute()
            )
        else:
            query_response = (
                extensions.supabase.table("analyze_resume")
                .select("*")
                .eq("user_id", current_user_id)
                .order("created_at", desc=True)
                .execute()
            )

        if query_response is None:
            return jsonify(None), 200

        result_data = getattr(query_response, "data", query_response)
        return jsonify(result_data or None), 200
        
    except Exception as e:
        logging.error(f"Error fetching from analyze_resume table: {str(e)}")
        return jsonify({"error": f"Could not retrieve analyzed resume data: {str(e)}"}), 500
        

@resume_analyze_bp.route("/roast-resume", methods=["POST", "OPTIONS"])
@require_authentication
@check_and_use_feature('resume')
def roast_resume():
    current_user_id = str(g.user.id)
    user_name = g.user.user_metadata.get('name') or \
                g.user.user_metadata.get('display_name') or \
                g.user.email or current_user_id

    SUPABASE_BUCKET = "user-documents"

    resume_url_for_xano = None
    resume_id_from_db = None
    
    try:
        current_resume_url_form = request.form.get("current_resume_url")
        file_to_upload = request.files.get("file")

        if file_to_upload:
            if file_to_upload.filename == "":
                return jsonify({"error": "No selected file for upload"}), 400

            document_type = request.form.get("document_type")
            if not document_type:
                return jsonify({"error": "Missing required document_type field"}), 400

            file_bytes = file_to_upload.read()
            file_to_upload.seek(0)

            flask_mimetype = file_to_upload.mimetype
            final_content_type_for_storage = flask_mimetype

            if flask_mimetype != 'application/pdf':
                try:
                    magic_mimetype = magic.from_buffer(file_bytes, mime=True)
                    final_content_type_for_storage = magic_mimetype
                except Exception as e:
                    logging.warning(f"Roast Resume: Error calling python-magic: {str(e)}. Falling back to Flask's mimetype: {flask_mimetype}")
            
            # Create unique file path with timestamp for versioning
            timestamp = str(int(time.time()))
            file_storage_path = f"{current_user_id}/{timestamp}_{file_to_upload.filename}"

            extensions.supabase.storage.from_(SUPABASE_BUCKET).upload(
                file_storage_path,
                file_bytes,
                file_options={"content-type": final_content_type_for_storage}
            )
            resume_url_for_xano = extensions.supabase.storage.from_(SUPABASE_BUCKET).get_public_url(file_storage_path)


            document_data = {
                "uid": current_user_id, 
                "document_name": file_to_upload.filename,
                "document_type": document_type, 
                "document_url": resume_url_for_xano,
                "display_name": user_name,
                "document_comments": "Uploaded for resume roast"
            }
            doc_insert_response = extensions.supabase.table("user_documents").insert(document_data).execute()
            
            if doc_insert_response.data and len(doc_insert_response.data) > 0 and doc_insert_response.data[0].get("id"):
                resume_id_from_db = doc_insert_response.data[0].get("id")
            else:
                logging.warning(f"Warning: Could not get ID from user_documents insert for {resume_url_for_xano}. Response: {doc_insert_response}")

        elif current_resume_url_form:
            resume_url_for_xano = current_resume_url_form
            try:
                doc_query = extensions.supabase.table("user_documents") \
                    .select("id") \
                    .eq("document_url", resume_url_for_xano) \
                    .eq("uid", current_user_id) \
                    .single() \
                    .execute()
                if doc_query.data and doc_query.data.get("id"):
                    resume_id_from_db = doc_query.data.get("id")
                else:
                    logging.warning(f"Warning: Could not find resume_id for existing URL: {resume_url_for_xano} and user: {current_user_id}")
            except Exception as e:
                logging.error(f"Error querying for resume_id for existing URL: {str(e)}")
        else:
            return jsonify({"error": "Missing resume input: provide 'current_resume_url' (form data) or upload a 'file' (multipart)"}), 400

        if not resume_url_for_xano:
             return jsonify({"error": "Failed to determine resume URL for processing"}), 500

        import uuid
        job_id = str(uuid.uuid4())  # correlation id for this roast job

        db_payload = {
            "user_id": current_user_id,
            "user_name": user_name,
            "job_id": job_id,
            "current_resume": resume_url_for_xano,
            "company_website": None,
            "job_description": None,
            "additional_comment": "Resume Roast",
            "status": "PENDING",
            "feedback_analysis": None,
            "resume_id": resume_id_from_db,
            "created_at": "now()",
        }

        try:
            insert_response = extensions.supabase.table("analyze_resume").insert(db_payload).execute()
            if not insert_response.data:
                logging.warning(f"Warning: Supabase insert into analyze_resume may have failed or returned no data. Response: {insert_response}")
        except Exception as e:
            current_app.logger.error(f"Error inserting into analyze_resume table: {str(e)}")

        
        return (
            jsonify(
                {
                    "job_id": job_id,
                    "message": "Resume roast has been queued and is now pending.",
                }
            ),
            202,
        )

    except requests.exceptions.Timeout:
        logging.error("Resume roast request timed out")
        return jsonify({"error": "The resume roast service is taking too long to respond. Please try again later."}), 504
    except requests.exceptions.HTTPError as http_err:
        # Log the downstream error but keep our API contract: always 202 Accepted once the job is queued.
        try:
            error_detail = http_err.response.json()
        except ValueError:
            error_detail = str(http_err.response.text)
        current_app.logger.warning(
            "Resume roast: downstream service returned HTTPError – proceeding as queued: %s", error_detail
        )
        return jsonify({
            "job_id": job_id if 'job_id' in locals() else None,
            "message": "Resume roast has been queued and is now pending (downstream service error logged).",
            "downstream_status": http_err.response.status_code,
            "downstream_details": error_detail
        }), 202
    except requests.exceptions.RequestException as req_err:
        logging.error(f"Request to n8n API failed: {str(req_err)}")
        return jsonify({"error": "Request to n8n API failed", "details": str(req_err)}), 500
    except Exception as e:
        current_app.logger.error(f"Unexpected error in roast_resume: {str(e)}")
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500


@resume_analyze_bp.route("/get-roast-resume", methods=["GET", "OPTIONS"])
@require_authentication
def get_roast_resume():
    """
    Retrieve resume roast feedback for the authenticated user.
    Optionally accepts a `job_id` query param to fetch a specific record.
    """
    current_user_id = str(g.user.id)
    try:
        job_id_param = request.args.get("job_id")

        query = (
            extensions.supabase.table("analyze_resume")
            .select("*")
            .eq("user_id", current_user_id)
            .eq("additional_comment", "Resume Roast")
            .order("created_at", desc=True)
        )

        if job_id_param:
            query = query.eq("job_id", job_id_param)

        query_response = query.execute()

        if query_response is None:
            return jsonify(None), 200

        result_data = getattr(query_response, "data", query_response)
        return jsonify(result_data or None), 200

    except Exception as e:
        logging.error(f"Error fetching resume roast data: {str(e)}")
        return jsonify({"error": f"Could not retrieve resume roast data: {str(e)}"}), 500


def markdown_to_pdf(markdown_content, template_path):
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path = os.path.join(tmpdir, "resume.md")
        pdf_path = os.path.join(tmpdir, "resume.pdf")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)
        try:
            subprocess.run([
                "pandoc", md_path,
                "--from", "markdown",
                "--template", template_path,
                "--pdf-engine", "pdflatex",
                "-o", pdf_path
            ], check=True)
        except Exception as e:
            raise RuntimeError(f"Pandoc PDF generation failed: {e}")
        return pdf_path

@resume_analyze_bp.route("/download-resume-pdf", methods=["GET"])
@require_authentication
def download_resume_pdf():
    current_user_id = str(g.user.id)
    try:
        # Fetch the latest analyze_resume record for this user
        query_response = (
            extensions.supabase.table("analyze_resume")
            .select("*")
            .eq("user_id", current_user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        result_data = getattr(query_response, "data", query_response)
        if not result_data or not isinstance(result_data, list) or not result_data[0].get("feedback_analysis"):
            return jsonify({"error": "No resume markdown found for this user."}), 404
        feedback_analysis = result_data[0]["feedback_analysis"]
        job_id = result_data[0].get("job_id", "latest")
        if isinstance(feedback_analysis, str):
            import json as _json
            feedback_analysis = _json.loads(feedback_analysis)
        markdown_content = feedback_analysis.get("new_resume", {}).get("new_resume")
        if not markdown_content:
            return jsonify({"error": "No markdown resume found in feedback_analysis."}), 404
        # Use the provided LaTeX template
        template_path = os.path.join(os.path.dirname(__file__), "templates", "resume.tex")
        if not os.path.exists(template_path):
            return jsonify({"error": "LaTeX template not found."}), 500
        # Define unique file path in bucket
        bucket_name = "user-documents"
        pdf_filename = f"{current_user_id}/{job_id}_resume.pdf"
        # Check if file already exists in Supabase bucket
        storage = extensions.supabase.storage.from_(bucket_name)
        public_url = storage.get_public_url(pdf_filename)
        # Try to fetch the file to see if it exists (Supabase returns a URL even if file doesn't exist, so we check with a HEAD request)
        import requests
        head_resp = requests.head(public_url)
        if head_resp.status_code == 200:
            return jsonify({"pdf_url": public_url}), 200
        # If not, generate and upload
        pdf_path = markdown_to_pdf(markdown_content, template_path)
        with open(pdf_path, "rb") as f:
            storage.upload(pdf_filename, f.read(), file_options={"content-type": "application/pdf"})
        public_url = storage.get_public_url(pdf_filename)
        return jsonify({"pdf_url": public_url}), 200
    except Exception as e:
        logging.error(f"Error generating/uploading PDF: {e}")
        return jsonify({"error": f"Failed to generate/upload PDF: {str(e)}"}), 500
