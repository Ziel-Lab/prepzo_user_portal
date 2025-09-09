from flask import Blueprint, request, jsonify, g
import os
import magic
import time
from app import extensions
from app.extensions import get_admin_client
from app.userPortal.subscription.helpers import require_authentication
from . import upload_bp 

SUPABASE_BUCKET = "user-documents"

@upload_bp.route("/upload-document", methods=["POST", "OPTIONS"])
@require_authentication
def upload_document():
    current_user_id = str(g.user.id)
    user_display_name = g.user.user_metadata.get('name') or \
                        g.user.user_metadata.get('display_name') or \
                        g.user.email or current_user_id

    # Use admin client for INSERT operations (with explicit user filtering for security)
    admin_supabase = get_admin_client()
    # Use user client for SELECT operations (RLS enforced)
    user_supabase = g.supabase_user

    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400
    
    # Check file extension for PDF
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({"error": "Only PDF files are allowed"}), 400

    document_type = request.form.get("document_type")
    if not document_type:
        return jsonify({"error": "Missing required document_type field"}), 400

    # Input validation and sanitization
    if not document_type.replace('_', '').isalnum():
        return jsonify({"error": "Invalid document_type format"}), 400
    
    if len(file.filename) > 255:
        return jsonify({"error": "Filename too long"}), 400

    dangerous_chars = set('/\\:*?"<>|')
    if any(c in dangerous_chars for c in file.filename):
        return jsonify({"error": "Filename contains invalid characters. The following characters are not allowed: / \\ : * ? \" < > |"}), 400

    # Create timestamp once for consistency
    timestamp = str(int(time.time()))

    # Sanitize filename for security - allow foreign language characters
    # Allow alphanumeric, common punctuation, spaces, and Unicode characters (for foreign languages)
    safe_filename = "".join(c for c in file.filename if c.isalnum() or c in (' ', '.', '_', '-', '(', ')', '[', ']', '&', '+', ',', ';', '=', '@', '#', '%', '!', '?') or ord(c) > 127).rstrip()
    if not safe_filename:
        safe_filename = f"document_{timestamp}"

    # Create storage-safe filename (replace spaces with underscores for Supabase storage)
    storage_safe_filename = safe_filename.replace(' ', '_')

    file_bytes = file.read()
    
    # File size validation (10MB limit)
    if len(file_bytes) > 10 * 1024 * 1024:
        return jsonify({"error": "File size exceeds 10MB limit"}), 400
  
    flask_mimetype = file.mimetype

    try:
        magic_mimetype = magic.from_buffer(file_bytes, mime=True)
        if magic_mimetype != 'application/pdf':
            return jsonify({"error": "File content is not a valid PDF"}), 400
    except Exception as e:
        print(f"Upload: Error calling python-magic: {str(e)}. Falling back to Flask's mimetype: {flask_mimetype}")
        if flask_mimetype != 'application/pdf':
            return jsonify({"error": "File content is not a valid PDF"}), 400

    final_content_type_for_storage = 'application/pdf'

    # Construct a unique path in storage using user ID, timestamp, and storage-safe filename
    storage_file_path = f"{current_user_id}/{timestamp}_{storage_safe_filename}"

    document_comments = request.form.get("document_comments", "").strip()
    # Limit comment length
    if len(document_comments) > 1000:
        return jsonify({"error": "Comments too long (max 1000 characters)"}), 400

    try:
        # Upload file to storage using admin client (upsert=True to overwrite existing files)
        admin_supabase.storage.from_(SUPABASE_BUCKET).upload(
            storage_file_path,  # Use the unique path for storage
            file_bytes,
            file_options={"content-type": final_content_type_for_storage}
        )
        public_url = admin_supabase.storage.from_(SUPABASE_BUCKET).get_public_url(storage_file_path)

        # Check if document with same name already exists for this user
        try:
            existing_doc_check = extensions.supabase.table("user_documents") \
                .select("id") \
                .eq("uid", current_user_id) \
                .eq("document_name", file.filename) \
                .execute()
            
            has_existing_file = existing_doc_check.data and len(existing_doc_check.data) > 0
        except Exception as e:
            # If query fails, assume no existing file
            has_existing_file = False

        document_data = {
            "uid": current_user_id,
            "document_name": safe_filename,  # Store sanitized filename
            "document_type": document_type,
            "document_url": public_url,
            "display_name": user_display_name,
            "document_comments": document_comments
        }

        # Only add status if this is a replacement file
        if has_existing_file:
            document_data["status"] = "Updated"

        data, _ = extensions.supabase.table("user_documents").insert(document_data).execute()
        return jsonify({"message": "File uploaded", "file_url": public_url, "db_response": data}), 201

    except Exception as e:
        # Log the error but don't expose internal details to frontend
        print(f"Upload error: {str(e)}")
        return jsonify({"error": "Upload failed"}), 500


@upload_bp.route("/get-documents", methods=["GET", "OPTIONS"])
@require_authentication
def get_documents():
    # Use admin client with explicit user filtering for consistent data access
    admin_supabase = get_admin_client()
    
    try:
        # Explicit user filtering for security (equivalent to RLS but using admin client)
        response = admin_supabase.table("user_documents") \
            .select("id, document_name, document_type, document_url, created_at, display_name, document_comments") \
            .eq("uid", str(g.user.id)) \
            .execute()

        return jsonify(response.data or []), 200

    except Exception as e:
        print(f"Fetch error: {str(e)}")
        return jsonify({"error": "Could not retrieve documents"}), 500


@upload_bp.route("/delete-document/<int:document_id>", methods=["DELETE", "OPTIONS"])
@require_authentication
def delete_document(document_id):
    current_user_id = str(g.user.id)
    # Use admin client for DELETE operations (with explicit user filtering for security)
    admin_supabase = get_admin_client()
    # Use user client for SELECT operations (RLS enforced)
    user_supabase = g.supabase_user

    try:
        # Use user client for SELECT (RLS enforced)
        select_response = user_supabase.table("user_documents") \
            .select("document_name") \
            .eq("id", document_id) \
            .eq("uid", current_user_id) \
            .execute()

        if not select_response.data:
            return jsonify({"error": "Document not found or you do not have permission to delete it."}), 404

        document_name_from_db = select_response.data[0]["document_name"]
        # Construct the correct storage path using user ID and the document name from DB
        file_path_in_storage = f"{current_user_id}/{document_name_from_db}"

        # 1. Attempt to delete from Supabase Storage using admin client
        try:
            storage_remove_result = admin_supabase.storage.from_(SUPABASE_BUCKET).remove([file_path_in_storage])
            # Check if there was an error removing the specific file from storage
            if storage_remove_result and storage_remove_result.data:
                item_status = next((item for item in storage_remove_result.data if item.get('name') == file_path_in_storage), None)
                if item_status and item_status.get('error'):
                    print(f"Warning: Supabase storage could not delete file '{file_path_in_storage}'. Error: {item_status.get('error')}")
        except Exception as storage_err:
            print(f"Error during Supabase storage file removal for '{file_path_in_storage}': {str(storage_err)}")

        # 2. Delete document metadata using admin client with explicit user filtering for security
        delete_db_response = admin_supabase.table("user_documents") \
            .delete() \
            .eq("id", document_id) \
            .eq("uid", current_user_id) \
            .execute()

        # Check if the database deletion was successful
        if not delete_db_response.data:
            print(f"Warning: Document with id {document_id} for user {current_user_id[:8]}*** was not deleted from DB (might have been already deleted).")

        return jsonify({"message": "Document deleted successfully"}), 200

    except Exception as e:
        print(f"Error in /delete-document/{document_id}: {str(e)}")
        return jsonify({"error": "An unexpected error occurred while trying to delete the document."}), 500


@upload_bp.route("/update-document-comments/<int:document_id>", methods=["PATCH"])
@require_authentication
def update_document_comments(document_id):
    # Use admin client for UPDATE operations (with explicit user filtering for security)
    admin_supabase = get_admin_client()
    # Use user client for SELECT operations (RLS enforced)
    user_supabase = g.supabase_user
    
    try:
        # Use user client for SELECT (RLS enforced)
        check_response = user_supabase.table("user_documents") \
            .select("id") \
            .eq("id", document_id) \
            .eq("uid", str(g.user.id)) \
            .single() \
            .execute()

        if not check_response.data:
            return jsonify({"error": "Not found or unauthorized"}), 404

        request_data = request.get_json()

        if request_data is None:
            return jsonify({"error": "Invalid JSON payload"}), 400
        
        new_comment = request_data.get("comment", "").strip()
        
        # Input validation
        if len(new_comment) > 1000:
            return jsonify({"error": "Comments too long (max 1000 characters)"}), 400

        # Use admin client for UPDATE with explicit user filtering for security
        update_response = admin_supabase.table("user_documents") \
            .update({"document_comments": new_comment}) \
            .eq("id", document_id) \
            .eq("uid", str(g.user.id)) \
            .execute()

        return jsonify({"message": "Comment updated", "data": update_response.data}), 200

    except Exception as e:
        print(f"Update comment error: {str(e)}")
        return jsonify({"error": "Could not update comment"}), 500