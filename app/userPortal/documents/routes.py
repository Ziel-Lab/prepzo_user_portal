from flask import Blueprint, request, jsonify
from flask_cors import CORS
import os
import magic
from app import extensions
from . import upload_bp 
from dotenv import load_dotenv

load_dotenv()

CORS(upload_bp, origins=["*"], supports_credentials=True,
     methods=["POST", "GET", "OPTIONS", "DELETE", "PATCH"])

SUPABASE_BUCKET = "user-documents"


def get_authenticated_user():
    """Helper to extract and validate JWT token and return user ID."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None, jsonify({"error": "Missing or invalid Authorization header"}), 401

    jwt_token = auth_header.split(" ")[1]
    try:
        user_response = extensions.supabase.auth.get_user(jwt=jwt_token)
        user = user_response.user
        if not user or not user.id:
            return None, jsonify({"error": "Invalid token or user not found"}), 401
        return user, None, None
    except Exception as e:
        return None, jsonify({"error": f"Authentication failed: {str(e)}"}), 401


@upload_bp.route("/upload-document", methods=["POST", "OPTIONS"])
def upload_document():
    if request.method == "OPTIONS":
        return "", 204

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
            print(f"Upload: Error calling python-magic: {str(e)}. Falling back to Flask's mimetype: {flask_mimetype}")


    # Construct a unique path in storage using user ID and original filename
    storage_file_path = f"{current_user_id}/{file.filename}"

    document_comments = request.form.get("document_comments", "").strip()

    try:
        extensions.supabase.storage.from_(SUPABASE_BUCKET).upload(
            storage_file_path,  # Use the unique path for storage
            file_bytes,
            file_options={
                "content-type": final_content_type_for_storage,
                "content-disposition": f'inline; filename="{file.filename}"'
            }
        )
        public_url = extensions.supabase.storage.from_(SUPABASE_BUCKET).get_public_url(storage_file_path) # Get URL based on unique path

        document_data = {
            "uid": current_user_id,
            "document_name": file.filename,  # Store original filename for display
            "document_type": flask_mimetype,
            "document_url": public_url,
            "display_name": user_display_name,
            "document_comments": document_comments
        }

        data, _ = extensions.supabase.table("user_documents").insert(document_data).execute()
        return jsonify({"message": "File uploaded", "file_url": public_url, "db_response": data}), 201

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@upload_bp.route("/get-documents", methods=["GET", "OPTIONS"])
def get_documents():
    if request.method == "OPTIONS":
        return "", 204

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
        print(f"Fetch error: {str(e)}")
        return jsonify({"error": f"Could not retrieve documents: {str(e)}"}), 500


@upload_bp.route("/delete-document/<int:document_id>", methods=["DELETE", "OPTIONS"])
def delete_document(document_id):
    if request.method == "OPTIONS":
        return "", 204

    user, error_response, status = get_authenticated_user()
    if error_response:
        return error_response, status
    
    current_user_id = str(user.id)

    try:
        # First, retrieve the document to verify existence and ownership, and to get its name for storage deletion
        select_response = extensions.supabase.table("user_documents") \
            .select("document_name") \
            .eq("id", document_id) \
            .eq("uid", current_user_id) \
            .execute() # Removed .single()

        if not select_response.data:
            return jsonify({"error": "Document not found or you do not have permission to delete it."}), 404

        document_name_from_db = select_response.data[0]["document_name"]
        # Construct the correct storage path using user ID and the document name from DB
        file_path_in_storage = f"{current_user_id}/{document_name_from_db}"

        # 1. Attempt to delete from Supabase Storage
        try:
            storage_remove_result = extensions.supabase.storage.from_(SUPABASE_BUCKET).remove([file_path_in_storage])
            # Check if there was an error removing the specific file from storage
            if storage_remove_result and storage_remove_result.data:
                item_status = next((item for item in storage_remove_result.data if item.get('name') == file_path_in_storage), None)
                if item_status and item_status.get('error'):
                    print(f"Warning: Supabase storage could not delete file '{file_path_in_storage}'. Error: {item_status.get('error')}")
                    # Depending on policy, you might want to make this a hard stop or just log and continue.
        except Exception as storage_err:
            print(f"Error during Supabase storage file removal for '{file_path_in_storage}': {str(storage_err)}")
            # Depending on policy, may return error here.

        # 2. Delete document metadata from the user_documents table
        delete_db_response = extensions.supabase.table("user_documents") \
            .delete() \
            .eq("id", document_id) \
            .eq("uid", current_user_id) \
            .execute()

        # Check if the database deletion was successful (i.e., if data was returned)
        if not delete_db_response.data:
            # This could happen if the record was deleted by another process between the select and delete,
            # or if there's a policy preventing deletion not caught by the initial select.
            print(f"Warning: Document with id {document_id} for user {current_user_id} was not deleted from DB (it might have been already deleted or a policy prevented it).")
            # Consider if a 404 is more appropriate if the record is already gone.
            # For now, let's assume if we got here, the aim was to delete, and if it's gone, that's acceptable.
            # If an error needs to be raised, a 500 might be too generic if the record is just already gone.
            # However, if select_response.data was populated, delete_db_response.data should also be.

        return jsonify({"message": "Document deleted successfully"}), 200

    except Exception as e:
        print(f"Error in /delete-document/{document_id}: {str(e)}")
        return jsonify({"error": "An unexpected error occurred while trying to delete the document."}), 500


@upload_bp.route("/update-document-comments/<int:document_id>", methods=["PATCH"])
def update_document_comments(document_id):
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
        print(f"Update comment error: {str(e)}")
        return jsonify({"error": f"Could not update comment: {str(e)}"}), 500