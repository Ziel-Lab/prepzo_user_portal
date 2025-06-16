from flask import request, jsonify, current_app, g
import requests 
from app import extensions 
import magic
import json
from app.userPortal.subscription.helpers import require_authentication, check_and_use_feature

from . import resume_analyze_bp 

@resume_analyze_bp.route("/analyze-resume", methods=["POST","OPTIONS"])
@require_authentication
@check_and_use_feature('resume')
def analyze_resume():
    current_user_id = str(g.user.id)
    user_name = g.user.user_metadata.get('name') or \
                g.user.user_metadata.get('display_name') or \
                g.user.email or current_user_id

    xano_api_url_resume_analyze = current_app.config.get("XANO_API_URL_RESUME_ANALYZE")
    
    try:
        data = request.form
        current_resume_url = data.get("current_resume") 
        job_description = data.get("job_description")
        company_website = data.get("company_website")
        additional_comment_text = data.get("additional_comments") 

        if not all([current_resume_url, job_description]):
            return jsonify({"error": "Missing required fields: current_resume (URL) and job_description"}), 400

        xano_payload = {
            "current_resume": current_resume_url,
            "job_description": job_description,
            "company_website": company_website,
            "additional_comments": additional_comment_text
        }

        xano_response = requests.post(xano_api_url_resume_analyze, json=xano_payload)
        xano_response.raise_for_status()
        xano_data = xano_response.json() 

        resume_id_from_db = None
        try:
            doc_query = extensions.supabase.table("user_documents") \
                .select("id") \
                .eq("document_url", current_resume_url) \
                .eq("uid", current_user_id) \
                .single() \
                .execute()
            if doc_query.data and doc_query.data.get("id"):
                resume_id_from_db = doc_query.data.get("id")
            else:
                current_app.logger.warning(f"Warning: Could not find resume_id for URL: {current_resume_url} and user: {current_user_id}")
        except Exception as e:
            current_app.logger.error(f"Error querying for resume_id: {str(e)}")
           
        db_payload = {
            "user_id": current_user_id,
            "user_name": user_name,
            "current_resume": current_resume_url, 
            "company_website": company_website, 
            "job_description": job_description, 
            "additional_comment": additional_comment_text, 
            "feedback_analysis": xano_data, 
            "resume_id": resume_id_from_db 
        }


        try:
            insert_response = extensions.supabase.table("analyze_resume").insert(db_payload).execute()
            if not insert_response.data:
                current_app.logger.warning(f"Warning: Supabase insert into analyze_resume may have failed or returned no data. Response: {insert_response}")
        except Exception as e:
            current_app.logger.error(f"Error inserting into analyze_resume table: {str(e)}")

        return jsonify(xano_data), xano_response.status_code

    except requests.exceptions.HTTPError as http_err:
        try:
            error_detail = http_err.response.json()
        except ValueError:
            error_detail = str(http_err)
        return jsonify({"error": "Xano API request failed", "details": error_detail}), http_err.response.status_code
    except requests.exceptions.RequestException as req_err:
        return jsonify({"error": "Request to Xano API failed", "details": str(req_err)}), 500
    except Exception as e:
        current_app.logger.error(f"A FATAL UNHANDLED EXCEPTION occurred in analyze_resume: {e}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500

@resume_analyze_bp.route("/get-analyze-resume", methods=["GET", "OPTIONS"])
@require_authentication
def get_analyze_resume():
    current_user_id = str(g.user.id)

    try:
        query_response = extensions.supabase.table("analyze_resume") \
            .select("*") \
            .eq("user_id", current_user_id) \
            .execute()

        return jsonify(query_response.data or []), 200
        
    except Exception as e:
        current_app.logger.error(f"Error fetching from analyze_resume table: {str(e)}")
        return jsonify({"error": f"Could not retrieve analyzed resume data: {str(e)}"}), 500
        

@resume_analyze_bp.route("/roast-resume", methods=["POST", "OPTIONS"])
@require_authentication
@check_and_use_feature('resume')
def roast_resume():
    current_user_id = str(g.user.id)
    user_name = g.user.user_metadata.get('name') or \
                g.user.user_metadata.get('display_name') or \
                g.user.email or current_user_id

    xano_api_url_resume_roast = current_app.config.get("XANO_API_URL_RESUME_ROAST")
    SUPABASE_BUCKET = "user-documents"

    resume_url_for_xano = None
    resume_id_from_db = None
    
    try:
        current_resume_url_form = request.form.get("current_resume_url")
        file_to_upload = request.files.get("file")

        if file_to_upload:
            if file_to_upload.filename == "":
                return jsonify({"error": "No selected file for upload"}), 400

            file_bytes = file_to_upload.read()
            file_to_upload.seek(0)

            flask_mimetype = file_to_upload.mimetype
            final_content_type_for_storage = flask_mimetype

            if flask_mimetype != 'application/pdf':
                try:
                    magic_mimetype = magic.from_buffer(file_bytes, mime=True)
                    final_content_type_for_storage = magic_mimetype
                except Exception as e:
                    current_app.logger.warning(f"Roast Resume: Error calling python-magic: {str(e)}. Falling back to Flask's mimetype: {flask_mimetype}")
            
            file_storage_path = file_to_upload.filename 

            extensions.supabase.storage.from_(SUPABASE_BUCKET).upload(
                file_storage_path,
                file_bytes,
                file_options={"content-type": final_content_type_for_storage}
            )
            resume_url_for_xano = extensions.supabase.storage.from_(SUPABASE_BUCKET).get_public_url(file_storage_path)


            document_data = {
                "uid": current_user_id, 
                "document_name": file_to_upload.filename,
                "document_type": flask_mimetype, 
                "document_url": resume_url_for_xano,
                "display_name": user_name,
                "document_comments": "Uploaded for resume roast"
            }
            doc_insert_response = extensions.supabase.table("user_documents").insert(document_data).execute()
            
            if doc_insert_response.data and len(doc_insert_response.data) > 0 and doc_insert_response.data[0].get("id"):
                resume_id_from_db = doc_insert_response.data[0].get("id")
            else:
                current_app.logger.warning(f"Warning: Could not get ID from user_documents insert for {resume_url_for_xano}. Response: {doc_insert_response}")

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
                    current_app.logger.warning(f"Warning: Could not find resume_id for existing URL: {resume_url_for_xano} and user: {current_user_id}")
            except Exception as e:
                current_app.logger.error(f"Error querying for resume_id for existing URL: {str(e)}")
        else:
            return jsonify({"error": "Missing resume input: provide 'current_resume_url' (form data) or upload a 'file' (multipart)"}), 400

        if not resume_url_for_xano:
             return jsonify({"error": "Failed to determine resume URL for processing"}), 500

        xano_payload = {"current_resume": resume_url_for_xano}
        xano_response = requests.post(xano_api_url_resume_roast, json=xano_payload)
        xano_response.raise_for_status()
        xano_data = xano_response.json()

        feedback_content_for_db = xano_data  

        raw_feedback_payload = xano_data.get("feedback")
        if isinstance(raw_feedback_payload, str):
            try:
                parsed_inner_json = json.loads(raw_feedback_payload)
                feedback_content_for_db = parsed_inner_json
            except json.JSONDecodeError as e:
                current_app.logger.warning(f"Roast Resume: Error decoding JSON string from 'feedback' key: {e}. Storing raw Xano response object instead.")
            except TypeError: 
                current_app.logger.warning(f"Roast Resume: Value for 'feedback' key was not a string (TypeError). Storing raw Xano response object.")

        elif raw_feedback_payload is not None: 
            current_app.logger.warning(f"Roast Resume: 'feedback' key present but not a string. Using raw Xano response for feedback_analysis. Type: {type(raw_feedback_payload)}")

        db_payload = {
            "user_id": current_user_id,
            "user_name": user_name,
            "current_resume": resume_url_for_xano,
            "job_description": None, 
            "company_website": None, 
            "additional_comment": "Resume Roast Feedback", 
            "feedback_analysis": feedback_content_for_db, 
            "resume_id": resume_id_from_db
        }
        
        try:
            insert_response = extensions.supabase.table("analyze_resume").insert(db_payload).execute()
            if not insert_response.data:
                current_app.logger.warning(f"Warning: Supabase insert into analyze_resume (roast) may have failed. Response: {insert_response}")
        except Exception as e:
            current_app.logger.error(f"Error inserting into analyze_resume table (roast): {str(e)}")

        return jsonify(xano_data), xano_response.status_code

    except requests.exceptions.HTTPError as http_err:
        try:
            error_detail = http_err.response.json()
        except ValueError: 
            error_detail = str(http_err.response.text)
        return jsonify({"error": "Xano API request failed", "details": error_detail}), http_err.response.status_code
    except requests.exceptions.RequestException as req_err:
        return jsonify({"error": "Request to Xano API failed", "details": str(req_err)}), 500
    except Exception as e:
        current_app.logger.error(f"Unexpected error in roast_resume: {str(e)}")
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500
