from flask import request, jsonify, current_app
from flask_cors import CORS
import requests 
import os
from app import extensions 
import magic
import json
from dotenv import load_dotenv
import logging

from . import resume_analyze_bp 

load_dotenv()
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
XANO_API_URL_RESUME_ANALYZE = os.getenv("XANO_API_URL_RESUME_ANALYZE")
XANO_API_URL_RESUME_ROAST = os.getenv("XANO_API_URL_RESUME_ROAST")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET_USER_DOCUMENTS", "user-documents")

CORS(resume_analyze_bp, origins=[FRONTEND_URL], supports_credentials=True, methods=["POST", "OPTIONS", "GET"])

def get_authenticated_user():
    """Helper to extract and validate JWT token and return user object."""
    logger = current_app.logger if hasattr(current_app, 'logger') else logging.getLogger(__name__)
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        logger.warning("get_authenticated_user (resumeAnalyze): Missing or invalid Authorization header.")
        return None, jsonify({"error": "Missing or invalid Authorization header"}), 401

    jwt_token = auth_header.split(" ")[1]
    try:
        user_response = extensions.supabase.auth.get_user(jwt=jwt_token)
        user = user_response.user
        if not user or not user.id:
            logger.warning("get_authenticated_user (resumeAnalyze): Supabase returned no user or user.id for the token.")
            return None, jsonify({"error": "Invalid token or user not found"}), 401
        return user, None, None
    except Exception as e:
        logger.error(f"get_authenticated_user (resumeAnalyze): Authentication failed. Type: {type(e).__name__}, Error: {str(e)}")
        return None, jsonify({"error": f"Authentication failed: {str(e)}"}), 401

@resume_analyze_bp.route("/analyze-resume", methods=["POST", "OPTIONS"])
@cross_origin(origins=[FRONTEND_URL], supports_credentials=True, methods=["POST", "OPTIONS"])
def analyze_resume():
    if request.method == "OPTIONS":
        return "", 204

    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code

    logger = current_app.logger
    current_user_id = str(user.id)
    user_name = user.user_metadata.get('name') or \
                user.user_metadata.get('display_name') or \
                user.email or current_user_id

    logger.info(f"XANO_API_URL_RESUME_ANALYZE: {XANO_API_URL_RESUME_ANALYZE}")
    if not XANO_API_URL_RESUME_ANALYZE:
        logger.error("XANO_API_URL_RESUME_ANALYZE is not set. Ensure it's in .env.")
        return jsonify({"error": "Server configuration error: Missing resume analysis API URL."}), 500

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
        
        logger.info(f"Sending payload to Xano for resume analysis: {json.dumps(xano_payload)[:200]}...")
        xano_response = requests.post(XANO_API_URL_RESUME_ANALYZE, json=xano_payload, timeout=120)
        xano_response.raise_for_status()
        xano_data = xano_response.json()
        logger.info("Received response from Xano for resume analysis.")

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
                logger.warning(f"Could not find resume_id for URL: {current_resume_url} and user: {current_user_id}")
        except Exception as e:
            logger.error(f"Error querying for resume_id in analyze_resume: {str(e)}", exc_info=True)
           
        db_payload = {
            "user": current_user_id,
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
                logger.warning(f"Supabase insert into analyze_resume may have failed. Response: {insert_response}")
        except Exception as e:
            logger.error(f"Error inserting into analyze_resume table: {str(e)}", exc_info=True)

        return jsonify(xano_data), xano_response.status_code

    except requests.exceptions.HTTPError as http_err:
        error_detail_msg = str(http_err.response.text) if http_err.response else str(http_err)
        logger.error(f"Xano API HTTPError in analyze_resume: {http_err}, Response: {error_detail_msg}", exc_info=True)
        try:
            error_detail = http_err.response.json()
        except ValueError:
            error_detail = error_detail_msg
        return jsonify({"error": "Xano API request failed", "details": error_detail}), http_err.response.status_code if http_err.response else 500
    except requests.exceptions.RequestException as req_err:
        logger.error(f"Xano API RequestException in analyze_resume: {req_err}", exc_info=True)
        return jsonify({"error": "Request to Xano API failed", "details": str(req_err)}), 500
    except Exception as e:
        logger.error(f"Unexpected error in analyze_resume: {str(e)}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500

@resume_analyze_bp.route("/get-analyze-resume", methods=["GET", "OPTIONS"])
@cross_origin(origins=[FRONTEND_URL], supports_credentials=True, methods=["GET", "OPTIONS"])
def get_analyze_resume():
    if request.method == "OPTIONS":
        return "", 204  

    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code

    logger = current_app.logger
    current_user_id = str(user.id)

    try:
        query_response = extensions.supabase.table("analyze_resume") \
            .select("*") \
            .eq("user", current_user_id) \
            .execute()

        return jsonify(query_response.data or []), 200 
        
    except Exception as e:
        logger.error(f"Error fetching from analyze_resume table: {str(e)}", exc_info=True)
        return jsonify({"error": f"Could not retrieve analyzed resume data: {str(e)}"}), 500
        

@resume_analyze_bp.route("/roast-resume", methods=["POST", "OPTIONS"])
def roast_resume():
    if request.method == "OPTIONS":
        return "", 204

    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code

    logger = current_app.logger
    current_user_id = str(user.id)
    user_name = user.user_metadata.get('name') or \
                user.user_metadata.get('display_name') or \
                user.email or current_user_id

    logger.info(f"XANO_API_URL_RESUME_ROAST: {XANO_API_URL_RESUME_ROAST}")
    if not XANO_API_URL_RESUME_ROAST:
        logger.error("XANO_API_URL_RESUME_ROAST is not set. Ensure it's in .env.")
        return jsonify({"error": "Server configuration error: Missing resume roast API URL."}), 500

    resume_url_for_xano = None
    resume_id_from_db = None
    
    try:
        current_resume_url_form = request.form.get("current_resume_url")
        file_to_upload = request.files.get("file")

        if file_to_upload:
            if file_to_upload.filename == "":
                return jsonify({"error": "No selected file for upload"}), 400

            file_bytes = file_to_upload.read()
            file_to_upload.seek(0)  # Reset stream position after read for magic

            flask_mimetype = file_to_upload.mimetype
            final_content_type_for_storage = flask_mimetype

            if flask_mimetype != 'application/pdf':
                try:
                    magic_mimetype = magic.from_buffer(file_bytes, mime=True)
                    final_content_type_for_storage = magic_mimetype
                except Exception as e:
                    logger.error(f"Roast Resume: Error calling python-magic: {str(e)}. Falling back to Flask's mimetype: {flask_mimetype}", exc_info=True)
            
            storage_file_path = f"{current_user_id}/{file_to_upload.filename}"

            extensions.supabase.storage.from_(SUPABASE_BUCKET).upload(
                storage_file_path,
                file_bytes,
                file_options={"content-type": final_content_type_for_storage}
            )
            resume_url_for_xano = extensions.supabase.storage.from_(SUPABASE_BUCKET).get_public_url(storage_file_path)

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
                logger.warning(f"Could not get ID from user_documents insert for {resume_url_for_xano}. Response: {doc_insert_response}")

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
                    logger.warning(f"Could not find resume_id for existing URL: {resume_url_for_xano} and user: {current_user_id}")
            except Exception as e:
                logger.error(f"Error querying for resume_id for existing URL in roast_resume: {str(e)}", exc_info=True)
        else:
            return jsonify({"error": "Missing resume input: provide 'current_resume_url' (form data) or upload a 'file' (multipart)"}), 400

        if not resume_url_for_xano:
             return jsonify({"error": "Failed to determine resume URL for processing"}), 500

        xano_payload = {"current_resume": resume_url_for_xano}
        logger.info(f"Sending payload to Xano for resume roast: {json.dumps(xano_payload)}")
        xano_response = requests.post(XANO_API_URL_RESUME_ROAST, json=xano_payload, timeout=120)
        xano_response.raise_for_status()
        xano_data = xano_response.json()
        logger.info("Received response from Xano for resume roast.")

        feedback_content_for_db = xano_data  

        raw_feedback_payload = xano_data.get("feedback")
        if isinstance(raw_feedback_payload, str):
            try:
                parsed_inner_json = json.loads(raw_feedback_payload)
                feedback_content_for_db = parsed_inner_json
            except json.JSONDecodeError as e:
                logger.error(f"Roast Resume: Error decoding JSON string from 'feedback' key: {e}. Storing raw Xano response object instead.", exc_info=True)
            except TypeError:
                logger.error(f"Roast Resume: Value for 'feedback' key was not a string (TypeError). Storing raw Xano response object.", exc_info=True)
        elif raw_feedback_payload is not None: 
            logger.warning(f"Roast Resume: 'feedback' key present but not a string. Using raw Xano response for feedback_analysis. Type: {type(raw_feedback_payload)}")

        db_payload = {
            "user": current_user_id,
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
                logger.warning(f"Supabase insert into analyze_resume (roast) may have failed. Response: {insert_response}")
        except Exception as e:
            logger.error(f"Error inserting into analyze_resume table (roast): {str(e)}", exc_info=True)

        return jsonify(xano_data), xano_response.status_code

    except requests.exceptions.HTTPError as http_err:
        error_detail_msg = str(http_err.response.text) if http_err.response else str(http_err)
        logger.error(f"Xano API HTTPError in roast_resume: {http_err}, Response: {error_detail_msg}", exc_info=True)
        try:
            error_detail = http_err.response.json()
        except ValueError: 
            error_detail = error_detail_msg
        return jsonify({"error": "Xano API request failed", "details": error_detail}), http_err.response.status_code if http_err.response else 500
    except requests.exceptions.RequestException as req_err:
        logger.error(f"Xano API RequestException in roast_resume: {req_err}", exc_info=True)
        return jsonify({"error": "Request to Xano API failed", "details": str(req_err)}), 500
    except Exception as e:
        logger.error(f"Unexpected error in roast_resume: {str(e)}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500

