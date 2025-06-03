from flask import request, jsonify
from flask_cors import CORS
import requests 
import os
from app import extensions 
import magic
import json

from . import resume_analyze_bp 

FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000") 
XANO_API_URL = "https://xfsf-9afu-ywqu.m2.xano.io/api:_jdqfybN/resume_analyze"
XANO_ROAST_API_URL = "https://xfsf-9afu-ywqu.m2.xano.io/api:_jdqfybN/resume_roast"
SUPABASE_BUCKET = "user-documents"

# Enable CORS for this blueprint
CORS(resume_analyze_bp, origins=[FRONTEND_URL], supports_credentials=True, methods=["POST", "OPTIONS", "GET"])

def get_authenticated_user():
    """Helper to extract and validate JWT token and return user object."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None, jsonify({"error": "Missing or invalid Authorization header"}), 401

    jwt_token = auth_header.split(" ")[1]
    try:
        user_response = extensions.supabase.auth.get_user(jwt=jwt_token)
        user = user_response.user
        if not user or not user.id:
            return None, jsonify({"error": "Invalid token or user not found"}), 401
        return user, None, None  # Return user object, no error, no status
    except Exception as e:
        return None, jsonify({"error": f"Authentication failed: {str(e)}"}), 401

@resume_analyze_bp.route("/analyze-resume", methods=["POST"])
def analyze_resume():
    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code

    current_user_id = str(user.id)
    user_name = user.user_metadata.get('name') or \
                user.user_metadata.get('display_name') or \
                user.email or current_user_id

    try:
        data = request.form
        current_resume_url = data.get("current_resume") # This is a URL
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

        xano_response = requests.post(XANO_API_URL, json=xano_payload)
        xano_response.raise_for_status()
        xano_data = xano_response.json() 

        # Attempt to find resume_id from user_documents table by URL
        resume_id_from_db = None
        try:
            doc_query = extensions.supabase.table("user_documents") \
                .select("id") \
                .eq("document_url", current_resume_url) \
                .cs("uid", [current_user_id]) \
                .single() \
                .execute()
            if doc_query.data and doc_query.data.get("id"):
                resume_id_from_db = doc_query.data.get("id")
            else:
                print(f"Warning: Could not find resume_id for URL: {current_resume_url} and user: {current_user_id}")
        except Exception as e:
            print(f"Error querying for resume_id: {str(e)}")
           
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
                print(f"Warning: Supabase insert into analyze_resume may have failed or returned no data. Response: {insert_response}")
        except Exception as e:
            print(f"Error inserting into analyze_resume table: {str(e)}")
            # Decide how to handle this: return error to client or just log?
            # For now, we will still return Xano's response even if DB insert fails.

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
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500

@resume_analyze_bp.route("/get-analyze-resume", methods=["GET"])
def get_analyze_resume():
    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code

    current_user_id = str(user.id)

    try:
        query_response = extensions.supabase.table("analyze_resume") \
            .select("*") \
            .eq("user", current_user_id) \
            .execute()

        return jsonify(query_response.data or []), 200 
        
    except Exception as e:
        print(f"Error fetching from analyze_resume table: {str(e)}")
        return jsonify({"error": f"Could not retrieve analyzed resume data: {str(e)}"}), 500
        

@resume_analyze_bp.route("/roast-resume", methods=["POST"])
def roast_resume():
    user, error_response, status_code = get_authenticated_user()
    if error_response:
        return error_response, status_code

    current_user_id = str(user.id)
    user_name = user.user_metadata.get('name') or \
                user.user_metadata.get('display_name') or \
                user.email or current_user_id

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

            # Use python-magic for more accurate MIME type detection if not PDF
            if flask_mimetype != 'application/pdf':
                try:
                    magic_mimetype = magic.from_buffer(file_bytes, mime=True)
                    final_content_type_for_storage = magic_mimetype
                except Exception as e:
                    print(f"Roast Resume: Error calling python-magic: {str(e)}. Falling back to Flask's mimetype: {flask_mimetype}")
            
            # Use filename as path, consistent with documents/routes.py
            # Consider implications if multiple users upload files with the same name.
            file_storage_path = file_to_upload.filename 

            extensions.supabase.storage.from_(SUPABASE_BUCKET).upload(
                file_storage_path,
                file_bytes,
                file_options={"content-type": final_content_type_for_storage}
            )
            resume_url_for_xano = extensions.supabase.storage.from_(SUPABASE_BUCKET).get_public_url(file_storage_path)

            # Save metadata to user_documents table
            document_data = {
                "uid": [current_user_id], # Assuming 'uid' is an array field as per documents/routes.py
                "document_name": file_to_upload.filename,
                "document_type": flask_mimetype, # Storing Flask's detected mimetype
                "document_url": resume_url_for_xano,
                "display_name": user_name,
                "document_comments": "Uploaded for resume roast"
            }
            doc_insert_response = extensions.supabase.table("user_documents").insert(document_data).execute()
            
            if doc_insert_response.data and len(doc_insert_response.data) > 0 and doc_insert_response.data[0].get("id"):
                resume_id_from_db = doc_insert_response.data[0].get("id")
            else:
                print(f"Warning: Could not get ID from user_documents insert for {resume_url_for_xano}. Response: {doc_insert_response}")

        elif current_resume_url_form:
            resume_url_for_xano = current_resume_url_form
            # Attempt to find resume_id from user_documents table by URL
            try:
                doc_query = extensions.supabase.table("user_documents") \
                    .select("id") \
                    .eq("document_url", resume_url_for_xano) \
                    .cs("uid", [current_user_id]) \
                    .single() \
                    .execute()
                if doc_query.data and doc_query.data.get("id"):
                    resume_id_from_db = doc_query.data.get("id")
                else:
                    print(f"Warning: Could not find resume_id for existing URL: {resume_url_for_xano} and user: {current_user_id}")
            except Exception as e:
                print(f"Error querying for resume_id for existing URL: {str(e)}")
        else:
            return jsonify({"error": "Missing resume input: provide 'current_resume_url' (form data) or upload a 'file' (multipart)"}), 400

        if not resume_url_for_xano:
             return jsonify({"error": "Failed to determine resume URL for processing"}), 500

        # Call Xano Roast API
        xano_payload = {"current_resume": resume_url_for_xano}
        xano_response = requests.post(XANO_ROAST_API_URL, json=xano_payload)
        xano_response.raise_for_status()
        xano_data = xano_response.json()

        # Process Xano response to extract and parse nested feedback if present
        feedback_content_for_db = xano_data  # Default to the whole Xano response

        raw_feedback_payload = xano_data.get("feedback")
        if isinstance(raw_feedback_payload, str):
            try:
                # Attempt to parse the string value of the "feedback" key
                parsed_inner_json = json.loads(raw_feedback_payload)
                feedback_content_for_db = parsed_inner_json
            except json.JSONDecodeError as e:
                print(f"Roast Resume: Error decoding JSON string from 'feedback' key: {e}. Storing raw Xano response object instead.")
                # feedback_content_for_db remains xano_data (the full outer object)
            except TypeError: # Should be caught by isinstance, but as a safeguard
                print(f"Roast Resume: Value for 'feedback' key was not a string (TypeError). Storing raw Xano response object.")
                # feedback_content_for_db remains xano_data
        elif raw_feedback_payload is not None: # It exists but is not a string
            print(f"Roast Resume: 'feedback' key present but not a string. Using raw Xano response for feedback_analysis. Type: {type(raw_feedback_payload)}")
            # feedback_content_for_db remains xano_data
        # If raw_feedback_payload is None (key "feedback" is missing), feedback_content_for_db also remains xano_data

        # Store results in analyze_resume table
        db_payload = {
            "user": current_user_id,
            "user_name": user_name,
            "current_resume": resume_url_for_xano,
            "job_description": None, # Not applicable for roast
            "company_website": None, # Not applicable for roast
            "additional_comment": "Resume Roast Feedback", # Default comment
            "feedback_analysis": feedback_content_for_db, # Use processed content
            "resume_id": resume_id_from_db
        }
        
        try:
            insert_response = extensions.supabase.table("analyze_resume").insert(db_payload).execute()
            if not insert_response.data:
                print(f"Warning: Supabase insert into analyze_resume (roast) may have failed. Response: {insert_response}")
        except Exception as e:
            print(f"Error inserting into analyze_resume table (roast): {str(e)}")
            # Continue to return Xano response even if DB insert fails

        return jsonify(xano_data), xano_response.status_code

    except requests.exceptions.HTTPError as http_err:
        try:
            error_detail = http_err.response.json()
        except ValueError: # If Xano error response is not JSON
            error_detail = str(http_err.response.text)
        return jsonify({"error": "Xano API request failed", "details": error_detail}), http_err.response.status_code
    except requests.exceptions.RequestException as req_err:
        return jsonify({"error": "Request to Xano API failed", "details": str(req_err)}), 500
    except Exception as e:
        print(f"Unexpected error in roast_resume: {str(e)}") # Log the full error server-side
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500

