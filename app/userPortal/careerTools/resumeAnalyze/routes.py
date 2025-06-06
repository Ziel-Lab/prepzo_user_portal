from flask import request, jsonify, current_app
from flask_cors import CORS
import requests 
import os
from app import extensions 
import magic
import json
from dotenv import load_dotenv
import logging
import base64

from . import resume_analyze_bp 

load_dotenv()
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
XANO_API_URL_RESUME_ANALYZE = os.getenv("XANO_API_URL_RESUME_ANALYZE")
XANO_API_URL_RESUME_ROAST = os.getenv("XANO_API_URL_RESUME_ROAST")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET_USER_DOCUMENTS", "user-documents")

CORS(resume_analyze_bp, origins=[FRONTEND_URL], supports_credentials=True, methods=["POST", "OPTIONS", "GET", "DELETE"])

def get_authenticated_user():
    """Helper to extract and validate JWT token and return user object."""
    logger = current_app.logger if hasattr(current_app, 'logger') else logging.getLogger(__name__)
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        logger.warning("get_authenticated_user (resumeAnalyze): Missing or invalid Authorization header.")
        return None, jsonify({"error": "Missing or invalid Authorization header"}), 401

    jwt_token = auth_header.split(" ")[1]
    try:
        logger.info(f"get_authenticated_user (resumeAnalyze): Attempting token validation (first 10 chars): {jwt_token[:10]}...")
        user_response = extensions.supabase.auth.get_user(jwt=jwt_token)
        user = user_response.user
        if not user or not user.id:
            logger.warning("get_authenticated_user (resumeAnalyze): Supabase returned no user or user.id for the token.")
            return None, jsonify({"error": "Invalid token or user not found"}), 401
        logger.info(f"get_authenticated_user (resumeAnalyze): Successfully authenticated user {user.id}.")
        return user, None, None
    except Exception as e:
        logger.error(f"get_authenticated_user (resumeAnalyze): Authentication failed. Type: {type(e).__name__}, Error: {str(e)}", exc_info=True)
        return None, jsonify({"error": f"Authentication failed: {str(e)}"}), 401

@resume_analyze_bp.route("/analyze-resume", methods=["POST", "OPTIONS"])
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
        logger.critical("XANO_API_URL_RESUME_ANALYZE is not set. Cannot analyze resume.")
        return jsonify({"error": "Server configuration error: Missing resume analysis API URL."}), 500

    try:
        data = request.form
        current_resume_url = data.get("current_resume") 
        job_description = data.get("job_description")
        company_website = data.get("company_website")
        additional_comment_text = data.get("additional_comments") 

        if not all([current_resume_url, job_description]):
            logger.warning(f"User {current_user_id} called /analyze-resume with missing fields: current_resume_url or job_description.")
            return jsonify({"error": "Missing required fields: current_resume (URL) and job_description"}), 400

        # Fetch document metadata (including document_type) from user_documents table
        resume_id_from_db = None
        document_type = None
        db_doc_data = None
        try:
            doc_query_response = extensions.supabase.table("user_documents") \
                .select("id, document_type, document_name") \
                .eq("document_url", current_resume_url) \
                .eq("uid", current_user_id) \
                .single() \
                .execute()
            
            if doc_query_response.data:
                db_doc_data = doc_query_response.data
                resume_id_from_db = db_doc_data.get("id")
                document_type = db_doc_data.get("document_type")
                logger.info(f"User {current_user_id}: Found document in DB. ID: {resume_id_from_db}, Type: {document_type}, URL: {current_resume_url}")
            else:
                logger.warning(f"User {current_user_id}: Document not found in user_documents for URL: {current_resume_url}. Or, it does not belong to the user.")
                return jsonify({"error": "Resume document not found or access denied for the provided URL."}), 404
        
        except Exception as e:
            logger.error(f"User {current_user_id}: Error querying user_documents for URL {current_resume_url}: {str(e)}", exc_info=True)
            return jsonify({"error": "Failed to verify resume document due to a database error."}), 500

        if not document_type:
            logger.error(f"User {current_user_id}: Document type (mimetype) could not be determined for resume URL: {current_resume_url}.")
            return jsonify({"error": "Could not determine the file type of the resume."}), 400

        data_for_xano = ""
        if document_type == "application/pdf":
            data_for_xano = current_resume_url
            logger.info(f"User {current_user_id}: Sending PDF URL to Xano for resume analysis: {current_resume_url}")
        else:
            logger.info(f"User {current_user_id}: Document type is {document_type}. Attempting to download and convert to Data URI for Xano.")
            try:
                file_response = requests.get(current_resume_url, timeout=30) # Timeout for download
                file_response.raise_for_status() # Ensure download was successful
                file_content = file_response.content
                base64_encoded_content = base64.b64encode(file_content).decode('utf-8')
                data_for_xano = f"data:{document_type};base64,{base64_encoded_content}"
                logger.info(f"User {current_user_id}: Successfully created Data URI for {document_type}. Length: {len(data_for_xano)}.")
            except requests.exceptions.RequestException as re:
                logger.error(f"User {current_user_id}: Failed to download file from URL {current_resume_url} for Data URI conversion. Error: {str(re)}", exc_info=True)
                return jsonify({"error": f"Failed to download resume file from URL: {str(re)}"}), 500
            except Exception as e_conv:
                logger.error(f"User {current_user_id}: Error during Data URI conversion for {current_resume_url}. Error: {str(e_conv)}", exc_info=True)
                return jsonify({"error": "Failed to convert resume file for analysis."}), 500
        
        if not data_for_xano: # Should not happen if logic above is correct
            logger.error(f"User {current_user_id}: data_for_xano is unexpectedly empty after processing {current_resume_url}.")
            return jsonify({"error": "Internal error preparing resume data for analysis."}), 500
            
        xano_payload = {
            "current_resume": data_for_xano, # This is now either URL or Data URI
            "job_description": job_description,
            "company_website": company_website,
            "additional_comments": additional_comment_text
        }
        
        logger.info(f"User {current_user_id}: Sending payload to Xano for resume analysis. Resume part preview (if URL): {str(data_for_xano)[:100] if document_type == 'application/pdf' else 'Data URI (content omitted)'}...")
        xano_response = requests.post(XANO_API_URL_RESUME_ANALYZE, json=xano_payload, timeout=120)
        xano_response.raise_for_status()
        xano_data = xano_response.json() # Assuming Xano returns parsable JSON
        logger.info(f"User {current_user_id}: Received response from Xano for resume analysis.")
           
        db_payload = {
            "user": current_user_id, # Changed from "uid" to "user" to match prior convention in this table
            "user_name": user_name,
            "current_resume": current_resume_url, # Store the original Supabase URL
            "company_website": company_website, 
            "job_description": job_description, 
            "additional_comment": additional_comment_text, 
            "feedback_analysis": xano_data, 
            "resume_id": resume_id_from_db 
        }

        try:
            logger.info(f"User {current_user_id}: Saving analysis result to 'analyze_resume' table.")
            insert_response = extensions.supabase.table("analyze_resume").insert(db_payload).execute()
            if insert_response.data:
                 logger.info(f"User {current_user_id}: Successfully saved analysis to DB. Response: {insert_response.data}")
            elif hasattr(insert_response, 'error') and insert_response.error:
                logger.error(f"User {current_user_id}: Supabase insert failed for 'analyze_resume'. Error: {insert_response.error.message if hasattr(insert_response.error, 'message') else insert_response.error}", exc_info=True)
            elif not (hasattr(insert_response, 'status_code') and 200 <= insert_response.status_code < 300):
                 logger.error(f"User {current_user_id}: Supabase insert for 'analyze_resume' failed or returned unexpected status. Result: {insert_response}", exc_info=True)
            else:
                 logger.info(f"User {current_user_id}: Supabase insert for 'analyze_resume' success, no data returned (status {insert_response.status_code}).")

        except Exception as e_db:
            logger.error(f"User {current_user_id}: Error inserting into analyze_resume table: {str(e_db)}", exc_info=True)
            # Decide if this should fail the whole request or just log. For now, Xano response is still returned.

        return jsonify(xano_data), xano_response.status_code

    except requests.exceptions.HTTPError as http_err:
        error_detail_msg = str(http_err.response.text) if http_err.response else str(http_err)
        logger.error(f"User {current_user_id}: Xano API HTTPError in analyze_resume: {http_err}, Response: {error_detail_msg}", exc_info=True)
        try:
            error_detail = http_err.response.json()
        except ValueError: # If Xano error response isn't JSON
            error_detail = {"raw_error": error_detail_msg}
        return jsonify({"error": "Resume analysis service request failed.", "details": error_detail}), http_err.response.status_code if http_err.response else 502 # 502 for bad gateway
    except requests.exceptions.RequestException as req_err: # For network errors, DNS failures etc.
        logger.error(f"User {current_user_id}: Xano API RequestException in analyze_resume: {req_err}", exc_info=True)
        return jsonify({"error": "Could not connect to resume analysis service.", "details": str(req_err)}), 503 # 503 for service unavailable
    except Exception as e:
        logger.error(f"User {current_user_id}: Unexpected error in analyze_resume: {str(e)}", exc_info=True)
        return jsonify({"error": "An unexpected server error occurred.", "details": str(e)}), 500

@resume_analyze_bp.route("/get-analyze-resume", methods=["GET", "OPTIONS"])
def get_analyze_resume():
    if request.method == "OPTIONS":
        return "", 204  

    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code

    logger = current_app.logger
    current_user_id = str(user.id)

    try:
        logger.info(f"User {current_user_id}: Fetching analyze_resume history.")
        query_response = extensions.supabase.table("analyze_resume") \
            .select("*") \
            .eq("user", current_user_id) \
            .order('created_at', desc=True) \
            .execute()
        logger.info(f"User {current_user_id}: Successfully fetched {len(query_response.data if query_response.data else [])} items from analyze_resume.")
        return jsonify(query_response.data or []), 200 
    except Exception as e:
        logger.error(f"User {current_user_id}: Error fetching from analyze_resume table: {str(e)}", exc_info=True)
        return jsonify({"error": "Could not retrieve analyzed resume data due to a server error."}), 500
        

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

    # resume_supabase_url will store the actual Supabase URL of the document for DB logging
    # data_for_xano will be either the URL (for PDF) or Data URI (for other types)
    resume_supabase_url = None 
    data_for_xano = None
    resume_id_from_db = None
    
    try:
        current_resume_url_form = request.form.get("current_resume_url")
        file_to_upload = request.files.get("file")
        document_type_for_xano_processing = None # To store the determined mimetype

        if file_to_upload:
            if file_to_upload.filename == "":
                logger.warning(f"User {current_user_id} (roast_resume): No selected file for upload.")
                return jsonify({"error": "No selected file for upload"}), 400

            file_bytes = file_to_upload.read()
            file_to_upload.seek(0)

            flask_mimetype = file_to_upload.mimetype
            final_content_type_for_storage = flask_mimetype # Used for Supabase upload
            document_type_for_xano_processing = flask_mimetype # Used for Xano logic

            if flask_mimetype != 'application/pdf':
                try:
                    magic_mimetype_val = magic.from_buffer(file_bytes, mime=True)
                    final_content_type_for_storage = magic_mimetype_val
                    document_type_for_xano_processing = magic_mimetype_val # Prefer magic for Xano if available
                    logger.info(f"User {current_user_id} (roast_resume): Uploaded file '{file_to_upload.filename}', Flask mimetype: {flask_mimetype}, Magic mimetype: {magic_mimetype_val}")
                except Exception as e:
                    logger.error(f"User {current_user_id} (roast_resume): Error calling python-magic: {str(e)}. Falling back to Flask's mimetype: {flask_mimetype}", exc_info=True)
            else:
                logger.info(f"User {current_user_id} (roast_resume): Uploaded PDF file '{file_to_upload.filename}', mimetype: {flask_mimetype}")

            
            storage_file_path = f"{current_user_id}/{file_to_upload.filename}"

            logger.info(f"User {current_user_id} (roast_resume): Uploading '{file_to_upload.filename}' to Supabase path '{storage_file_path}' with content-type '{final_content_type_for_storage}'.")
            extensions.supabase.storage.from_(SUPABASE_BUCKET).upload(
                storage_file_path,
                file_bytes,
                file_options={"content-type": final_content_type_for_storage, "upsert": "true"} # Added upsert
            )
            resume_supabase_url = extensions.supabase.storage.from_(SUPABASE_BUCKET).get_public_url(storage_file_path)
            logger.info(f"User {current_user_id} (roast_resume): File '{file_to_upload.filename}' uploaded. Public URL: {resume_supabase_url}")

            document_data = {
                "uid": current_user_id, 
                "document_name": file_to_upload.filename,
                "document_type": document_type_for_xano_processing, # Store the most accurate mimetype
                "document_url": resume_supabase_url,
                "display_name": user_name, # Consider if user.user_metadata.get('display_name') is better
                "document_comments": "Uploaded for resume roast"
            }
            logger.info(f"User {current_user_id} (roast_resume): Saving new document metadata to user_documents for: {file_to_upload.filename}")
            doc_insert_response = extensions.supabase.table("user_documents").insert(document_data).execute()
            
            if doc_insert_response.data and len(doc_insert_response.data) > 0 and doc_insert_response.data[0].get("id"):
                resume_id_from_db = doc_insert_response.data[0].get("id")
                logger.info(f"User {current_user_id} (roast_resume): New document saved to user_documents. ID: {resume_id_from_db}")
            else: # Log warning but proceed if ID retrieval fails, as main flow is roast
                error_msg = doc_insert_response.error.message if hasattr(doc_insert_response, 'error') and doc_insert_response.error else str(doc_insert_response)
                logger.warning(f"User {current_user_id} (roast_resume): Could not get ID from user_documents insert for '{file_to_upload.filename}' (URL: {resume_supabase_url}). Response/Error: {error_msg}")

            # Now prepare data_for_xano based on the uploaded file's type
            if document_type_for_xano_processing == "application/pdf":
                data_for_xano = resume_supabase_url
                logger.info(f"User {current_user_id} (roast_resume): New PDF uploaded. Sending URL to Xano: {resume_supabase_url}")
            else:
                logger.info(f"User {current_user_id} (roast_resume): New non-PDF ({document_type_for_xano_processing}) uploaded. Creating Data URI.")
                try:
                    base64_encoded_content = base64.b64encode(file_bytes).decode('utf-8')
                    data_for_xano = f"data:{document_type_for_xano_processing};base64,{base64_encoded_content}"
                    logger.info(f"User {current_user_id} (roast_resume): Successfully created Data URI for new file '{file_to_upload.filename}'. Length: {len(data_for_xano)}.")
                except Exception as e_conv:
                    logger.error(f"User {current_user_id} (roast_resume): Error during Data URI conversion for new file '{file_to_upload.filename}'. Error: {str(e_conv)}", exc_info=True)
                    return jsonify({"error": "Failed to convert uploaded resume file for analysis."}), 500

        elif current_resume_url_form:
            resume_supabase_url = current_resume_url_form
            logger.info(f"User {current_user_id} (roast_resume): Using existing resume URL: {resume_supabase_url}")
            
            # Fetch document_type for existing URL
            try:
                doc_query = extensions.supabase.table("user_documents") \
                    .select("id, document_type") \
                    .eq("document_url", resume_supabase_url) \
                    .eq("uid", current_user_id) \
                    .single() \
                    .execute()

                if doc_query.data:
                    resume_id_from_db = doc_query.data.get("id")
                    document_type_for_xano_processing = doc_query.data.get("document_type")
                    logger.info(f"User {current_user_id} (roast_resume): Found existing document in DB. ID: {resume_id_from_db}, Type: {document_type_for_xano_processing}")
                    if not document_type_for_xano_processing:
                        logger.error(f"User {current_user_id} (roast_resume): Document type missing in DB for URL: {resume_supabase_url}")
                        return jsonify({"error": "Document type missing for the provided resume URL."}), 400
                else:
                    logger.warning(f"User {current_user_id} (roast_resume): Existing document not found or access denied for URL: {resume_supabase_url}")
                    return jsonify({"error": "Resume document not found or access denied."}), 404
            except Exception as e_db_query:
                logger.error(f"User {current_user_id} (roast_resume): Error querying user_documents for existing URL {resume_supabase_url}: {str(e_db_query)}", exc_info=True)
                return jsonify({"error": "Failed to verify existing resume document."}), 500

            # Prepare data_for_xano based on existing document's type
            if document_type_for_xano_processing == "application/pdf":
                data_for_xano = resume_supabase_url
                logger.info(f"User {current_user_id} (roast_resume): Existing document is PDF. Sending URL to Xano: {resume_supabase_url}")
            else:
                logger.info(f"User {current_user_id} (roast_resume): Existing document type is {document_type_for_xano_processing}. Downloading and converting to Data URI.")
                try:
                    file_response = requests.get(resume_supabase_url, timeout=30)
                    file_response.raise_for_status()
                    file_content = file_response.content
                    base64_encoded_content = base64.b64encode(file_content).decode('utf-8')
                    data_for_xano = f"data:{document_type_for_xano_processing};base64,{base64_encoded_content}"
                    logger.info(f"User {current_user_id} (roast_resume): Successfully created Data URI for existing doc. Length: {len(data_for_xano)}.")
                except requests.exceptions.RequestException as re:
                    logger.error(f"User {current_user_id} (roast_resume): Failed to download file from URL {resume_supabase_url} for Data URI conversion. Error: {str(re)}", exc_info=True)
                    return jsonify({"error": f"Failed to download resume file from URL: {str(re)}"}), 500
                except Exception as e_conv:
                    logger.error(f"User {current_user_id} (roast_resume): Error during Data URI conversion for {resume_supabase_url}. Error: {str(e_conv)}", exc_info=True)
                    return jsonify({"error": "Failed to convert resume file for analysis."}), 500
        else:
            logger.warning(f"User {current_user_id} (roast_resume): Missing resume input.")
            return jsonify({"error": "Missing resume input: provide 'current_resume_url' (form data) or upload a 'file' (multipart)"}), 400

        if not data_for_xano: # This implies an issue in the logic above
             logger.error(f"User {current_user_id} (roast_resume): data_for_xano is unexpectedly empty. Original resume_supabase_url was {resume_supabase_url}")
             return jsonify({"error": "Failed to determine resume data for processing"}), 500

        xano_payload = {"current_resume": data_for_xano}
        logger.info(f"User {current_user_id} (roast_resume): Sending payload to Xano for resume roast. Resume part preview: {str(data_for_xano)[:100] if document_type_for_xano_processing == 'application/pdf' else 'Data URI (content omitted)'}...")
        xano_response = requests.post(XANO_API_URL_RESUME_ROAST, json=xano_payload, timeout=120)
        xano_response.raise_for_status()
        xano_data = xano_response.json()
        logger.info(f"User {current_user_id} (roast_resume): Received response from Xano for resume roast.")

        feedback_content_for_db = xano_data  

        raw_feedback_payload = xano_data.get("feedback")
        if isinstance(raw_feedback_payload, str):
            try:
                parsed_inner_json = json.loads(raw_feedback_payload)
                feedback_content_for_db = parsed_inner_json
            except json.JSONDecodeError as e_json:
                logger.error(f"User {current_user_id} (roast_resume): Error decoding JSON string from 'feedback' key: {e_json}. Storing raw Xano response object instead.", exc_info=True)
            except TypeError: # Should not happen if isinstance is str, but for safety
                logger.error(f"User {current_user_id} (roast_resume): Value for 'feedback' key was not a string (TypeError). Storing raw Xano response object.", exc_info=True)
        elif raw_feedback_payload is not None: 
            logger.warning(f"User {current_user_id} (roast_resume): 'feedback' key present but not a string. Using raw Xano response for feedback_analysis. Type: {type(raw_feedback_payload)}")

        db_payload = {
            "user": current_user_id,
            "user_name": user_name,
            "current_resume": resume_supabase_url, # Always store the Supabase URL
            "job_description": None, 
            "company_website": None, 
            "additional_comment": "Resume Roast Feedback", 
            "feedback_analysis": feedback_content_for_db, 
            "resume_id": resume_id_from_db
        }
        
        try:
            logger.info(f"User {current_user_id} (roast_resume): Saving roast result to 'analyze_resume' table. Resume ID: {resume_id_from_db}, Supabase URL: {resume_supabase_url}")
            insert_response = extensions.supabase.table("analyze_resume").insert(db_payload).execute()
            if insert_response.data:
                logger.info(f"User {current_user_id} (roast_resume): Successfully saved roast result to DB. Response: {insert_response.data}")
            elif hasattr(insert_response, 'error') and insert_response.error:
                logger.error(f"User {current_user_id} (roast_resume): Supabase insert failed for 'analyze_resume'. Error: {insert_response.error.message if hasattr(insert_response.error, 'message') else insert_response.error}", exc_info=True)
            elif not (hasattr(insert_response, 'status_code') and 200 <= insert_response.status_code < 300):
                 logger.error(f"User {current_user_id} (roast_resume): Supabase insert for 'analyze_resume' failed or returned unexpected status. Result: {insert_response}", exc_info=True)
            else:
                 logger.info(f"User {current_user_id} (roast_resume): Supabase insert for 'analyze_resume' success, no data returned (status {insert_response.status_code}).")
        except Exception as e_db_insert:
            logger.error(f"User {current_user_id} (roast_resume): Error inserting into analyze_resume table: {str(e_db_insert)}", exc_info=True)

        return jsonify(xano_data), xano_response.status_code

    except requests.exceptions.HTTPError as http_err:
        error_detail_msg = str(http_err.response.text) if http_err.response else str(http_err)
        logger.error(f"User {current_user_id}: Xano API HTTPError in roast_resume: {http_err}, Response: {error_detail_msg}", exc_info=True)
        try:
            error_detail = http_err.response.json()
        except ValueError: 
            error_detail = {"raw_error": error_detail_msg}
        return jsonify({"error": "Resume roast service request failed.", "details": error_detail}), http_err.response.status_code if http_err.response else 502
    except requests.exceptions.RequestException as req_err:
        logger.error(f"User {current_user_id}: Xano API RequestException in roast_resume: {req_err}", exc_info=True)
        return jsonify({"error": "Could not connect to resume roast service.", "details": str(req_err)}), 503
    except Exception as e:
        logger.error(f"User {current_user_id}: Unexpected error in roast_resume: {str(e)}", exc_info=True)
        return jsonify({"error": "An unexpected server error occurred.", "details": str(e)}), 500

