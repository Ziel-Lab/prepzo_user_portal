from flask import Blueprint, request, jsonify, current_app
from flask_cors import CORS
import os
import magic
from app import extensions
from . import upload_bp 
from dotenv import load_dotenv
import logging

load_dotenv()

FRONTEND_URL = os.getenv("FRONTEND_ORIGIN") 


# Critical check for FRONTEND_URL, especially for production
# if not FRONTEND_URL:
#     # Fallback for local development if .env is missing FRONTEND_ORIGIN
#     if os.getenv("FLASK_ENV") == "development" or os.getenv("FLASK_DEBUG") == "1":
#         FRONTEND_URL = "http://localhost:3000"
#         # Use a simple print here if current_app.logger is not yet available during import
#         print(f"WARNING: [documents/routes.py] FRONTEND_ORIGIN not set, defaulting to {FRONTEND_URL} for CORS (dev mode).")
#     else:
#         raise RuntimeError("CRITICAL: [documents/routes.py] FRONTEND_ORIGIN environment variable is not set. CORS will not be configured correctly.")

print(f"INFO: [documents/routes.py] Configuring CORS for documents blueprint with origin: {FRONTEND_URL}")
CORS(upload_bp, origins=[FRONTEND_URL], supports_credentials=True,
     methods=["POST", "GET", "OPTIONS", "DELETE", "PATCH"])

# SUPABASE_BUCKET can also be from env, consistent with how XANO URLs are handled in other blueprints
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET_USER_DOCUMENTS", "user-documents") 
print(f"INFO: [documents/routes.py] SUPABASE_BUCKET set to: {SUPABASE_BUCKET}")

def get_authenticated_user():
    """Helper to extract and validate JWT token and return user ID."""
    # current_app.logger should be available once a request context is active
    logger = current_app.logger if current_app and hasattr(current_app, 'logger') else logging.getLogger(__name__)
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        logger.warning(f"get_authenticated_user (documents): Missing or invalid Authorization header. Header: {auth_header}")
        return None, jsonify({"error": "Missing or invalid Authorization header"}), 401

    jwt_token = auth_header.split(" ")[1]
    logger.info(f"get_authenticated_user (documents): Extracted token (first 10 chars): {jwt_token[:10]}...")

    try:
        user_response = extensions.supabase.auth.get_user(jwt=jwt_token)
        user = user_response.user
        if not user or not user.id:
            logger.warning("get_authenticated_user (documents): Supabase returned no user or user.id for the token.")
            return None, jsonify({"error": "Invalid token or user not found"}), 401
        logger.info(f"get_authenticated_user (documents): Successfully authenticated user {user.id}")
        return user, None, None
    except Exception as e:
        logger.error(f"get_authenticated_user (documents): Authentication failed. Exception type: {type(e).__name__}, Error: {str(e)}", exc_info=True)
        return None, jsonify({"error": f"Authentication failed: {str(e)}"}), 401

@upload_bp.route("/upload-document", methods=["POST", "OPTIONS"])
def upload_document():
    if request.method == "OPTIONS":
        return "", 204
    logger = current_app.logger # Ensure logger is defined for the route
    user, error_response, status = get_authenticated_user()
    if error_response:
        return error_response, status

    current_user_id = str(user.id)
    user_display_name = user.user_metadata.get('name') or \
                        user.user_metadata.get('display_name') or \
                        user.email or current_user_id

    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    file_bytes = file.read()
  
    flask_mimetype = file.mimetype

    final_content_type_for_storage = flask_mimetype 

    if flask_mimetype == 'application/pdf':
        final_content_type_for_storage = 'application/pdf'
    else:
        try:
            magic_mimetype = magic.from_buffer(file_bytes, mime=True)
            final_content_type_for_storage = magic_mimetype
        except Exception as e:
            logger.error(f"Upload: Error calling python-magic: {str(e)}. Falling back to Flask's mimetype: {flask_mimetype}", exc_info=True)

    storage_file_path = f"{current_user_id}/{file.filename}"
    document_comments = request.form.get("document_comments", "").strip()

    try:
        extensions.supabase.storage.from_(SUPABASE_BUCKET).upload(
            storage_file_path, 
            file_bytes,
            file_options={
                "content-type": final_content_type_for_storage,
                "content-disposition": f'inline; filename="{file.filename}"'
            }
        )
        public_url = extensions.supabase.storage.from_(SUPABASE_BUCKET).get_public_url(storage_file_path)

        document_data = {
            "uid": current_user_id,
            "document_name": file.filename, 
            "document_type": flask_mimetype,
            "document_url": public_url,
            "display_name": user_display_name,
            "document_comments": document_comments
        }

        db_data, _ = extensions.supabase.table("user_documents").insert(document_data).execute()
        return jsonify({"message": "File uploaded", "file_url": public_url, "db_response": db_data}), 201

    except Exception as e:
        logger.error(f"Error during document upload for user {current_user_id}, file {file.filename}: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to upload document due to a server error."}), 500

@upload_bp.route("/get-documents", methods=["GET", "OPTIONS"])
def get_documents():
    if request.method == "OPTIONS":
        return "", 204
    logger = current_app.logger
    user, error_response, status = get_authenticated_user()
    if error_response:
        return error_response, status

    try:
        response = extensions.supabase.table("user_documents") \
            .select("id, document_name, document_type, document_url, created_at, display_name, document_comments") \
            .eq("uid", str(user.id)) \
            .execute()
        return jsonify(response.data or []), 200
    except Exception as e:
        logger.error(f"Error fetching documents for user {user.id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Could not retrieve documents due to a server error."}), 500

@upload_bp.route("/delete-document/<int:document_id>", methods=["DELETE", "OPTIONS"])
def delete_document(document_id):
    if request.method == "OPTIONS":
        return "", 204
    logger = current_app.logger
    user, error_response, status = get_authenticated_user()
    if error_response:
        return error_response, status
    
    current_user_id = str(user.id)

    try:
        select_response = extensions.supabase.table("user_documents") \
            .select("document_name") \
            .eq("id", document_id) \
            .eq("uid", current_user_id) \
            .execute()

        if not select_response.data:
            return jsonify({"error": "Document not found or you do not have permission to delete it."}), 404

        document_name_from_db = select_response.data[0]["document_name"]
        file_path_in_storage = f"{current_user_id}/{document_name_from_db}"

        try:
            storage_remove_result = extensions.supabase.storage.from_(SUPABASE_BUCKET).remove([file_path_in_storage])
            if storage_remove_result and storage_remove_result.data:
                item_status = next((item for item in storage_remove_result.data if item.get('name') == file_path_in_storage), None)
                if item_status and item_status.get('error'):
                    logger.warning(f"Supabase storage could not delete file '{file_path_in_storage}'. Error: {item_status.get('error')}")
        except Exception as storage_err:
            logger.error(f"Error during Supabase storage file removal for '{file_path_in_storage}': {str(storage_err)}", exc_info=True)

        delete_db_response = extensions.supabase.table("user_documents") \
            .delete() \
            .eq("id", document_id) \
            .eq("uid", current_user_id) \
            .execute()

        if not delete_db_response.data:
            logger.warning(f"Document with id {document_id} for user {current_user_id} was not deleted from DB (it might have been already deleted or a policy prevented it).")

        return jsonify({"message": "Document deleted successfully"}), 200

    except Exception as e:
        logger.error(f"Error in /delete-document/{document_id} for user {current_user_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred while trying to delete the document."}), 500

@upload_bp.route("/update-document-comments/<int:document_id>", methods=["PATCH", "OPTIONS"])
def update_document_comments(document_id):
    if request.method == "OPTIONS": # Added OPTIONS handling
        return "", 204
    logger = current_app.logger
    user, error_response, status = get_authenticated_user()
    if error_response:
        return error_response, status

    try:
        check_response = extensions.supabase.table("user_documents") \
            .select("id") \
            .eq("id", document_id) \
            .eq("uid", str(user.id)) \
            .single() \
            .execute()

        if not check_response.data:
            return jsonify({"error": "Not found or unauthorized"}), 404

        request_data = request.get_json()
        if request_data is None:
            return jsonify({"error": "Invalid JSON payload"}), 400
        
        new_comment = request_data.get("comment", "").strip()

        update_response = extensions.supabase.table("user_documents") \
            .update({"document_comments": new_comment}) \
            .eq("id", document_id) \
            .eq("uid", str(user.id)) \
            .execute()

        return jsonify({"message": "Comment updated", "data": update_response.data}), 200

    except Exception as e:
        logger.error(f"Update comment error for doc id {document_id}, user {user.id}: {str(e)}", exc_info=True)
        return jsonify({"error": f"Could not update comment: {str(e)}"}), 500
