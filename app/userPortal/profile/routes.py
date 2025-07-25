from flask import request, jsonify, current_app, g
from app.userPortal.profile import profile_bp
from app.userPortal.subscription.helpers import require_authentication
from app import extensions
import requests
import uuid

# Supabase storage bucket used for user-uploaded resumes
SUPABASE_BUCKET = "user-documents"
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def allowed_image_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS

@profile_bp.route('/upload-linkedin-pdf', methods=['POST', 'OPTIONS'])
@require_authentication
def upload_linkedin_pdf():
    if request.method == 'OPTIONS':
        return '', 204

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Only PDF files are allowed'}), 400

    try:
        # --- 1. Upload the resume to Supabase Storage ---
        supabase = extensions.supabase
        if supabase is None:
            return jsonify({'error': 'Supabase client not initialized'}), 500

        file_bytes = file.read()
        user_id = str(g.user.id)
        unique_file_name = f"{uuid.uuid4()}_{file.filename}"
        storage_path = f"{user_id}/{unique_file_name}"

        supabase.storage.from_(SUPABASE_BUCKET).upload(
            storage_path,
            file_bytes,
            file_options={
                "content-type": "application/pdf",
                "content-disposition": f'inline; filename="{file.filename}"'
            }
        )

        public_url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(storage_path)

        # --- 2. Call the n8n webhook to extract profile data ---
        webhook_url = "https://prepzo.app.n8n.cloud/webhook/fetch_profile"
        try:
            requests.post(webhook_url, json={
                "resume_url": public_url,
                "user_id": user_id  # include this so n8n can return it later
            }, timeout=2)  # short timeout to avoid blocking
        except requests.exceptions.RequestException as req_err:
            current_app.logger.warning(f"n8n webhook call failed (non-blocking): {req_err}")

        return jsonify({
            "message": "Resume uploaded successfully. Profile extraction in progress.",
            "resume_url": public_url
        }), 200

    except Exception as e:
        current_app.logger.error(f'Failed to upload or process resume: {e}', exc_info=True)
        return jsonify({'error': 'Failed to process resume', 'details': str(e)}), 500 


# ---------------------------------------------------------------------------
# GET /profile – fetch the stored LinkedIn-profile data for the current user
# ---------------------------------------------------------------------------

@profile_bp.route('', methods=['GET', 'OPTIONS'])  # maps to /profile because of blueprint prefix
@require_authentication
def get_linkedin_profile():
    """Return the profile record stored in the user_profiles table for the
    authenticated user.
    """
    if request.method == 'OPTIONS':
        return '', 204

    try:
        supabase = extensions.supabase
        if supabase is None:
            return jsonify({'error': 'Supabase client not initialized'}), 500

        uid = str(g.user.id)
        response = supabase.table('user_profiles').select('*').eq('user_id', uid).maybe_single().execute()

        return jsonify({'db_result': response.data if response and response.data else None}), 200

    except Exception as e:
        current_app.logger.error(f'Failed to fetch profile for user {g.user.id}: {e}', exc_info=True)
        return jsonify({'error': 'Failed to fetch profile', 'details': str(e)}), 500 
    
@profile_bp.route('/save-linkedin-profile', methods=['POST'])
def save_linkedin_profile():
    data = request.json
    user_id = data.get('user_id')
    resume_url = data.get('resume_url')

    profile_fields = {
        'name': data.get('name'),
        'title': data.get('title'),
        'bio': data.get('bio'),
        'location': data.get('location'),
        'email': data.get('email'),
        'phone': data.get('phone'),
        'linkedin_url': data.get('linkedin_url'),
        'website': data.get('website'),
        'skills': data.get('skills'),
        'achievements': data.get('achievements'),
        'certifications': data.get('certification') or data.get('certifications'),
        'experience': data.get('experience'),
        'projects': data.get('projects'),
        'resume_url': resume_url
    }

    supabase = extensions.supabase
    supabase.table('user_profiles').upsert(
        {'user_id': user_id, **profile_fields}, on_conflict='user_id'
    ).execute()

    return jsonify({"message": "Profile saved"}), 200

@profile_bp.route('/upload-avatar', methods=['POST', 'OPTIONS'])
@require_authentication
def upload_avatar():
    if request.method == 'OPTIONS':
        return '', 204

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if not allowed_image_file(file.filename):
        return jsonify({'error': 'Only image files (PNG, JPG, JPEG, GIF) are allowed'}), 400

    try:
        supabase = extensions.supabase
        if supabase is None:
            return jsonify({'error': 'Supabase client not initialized'}), 500

        file_bytes = file.read()
        user_id = str(g.user.id)
        file_extension = file.filename.rsplit('.', 1)[1].lower()
        unique_file_name = f"avatar_{user_id}.{file_extension}"
        storage_path = f"avatars/{user_id}/{unique_file_name}"

        # Delete existing avatar if any
        try:
            existing_files = supabase.storage.from_(SUPABASE_BUCKET).list(f"avatars/{user_id}")
            for existing_file in existing_files:
                if existing_file['name'].startswith('avatar_'):
                    supabase.storage.from_(SUPABASE_BUCKET).remove([f"avatars/{user_id}/{existing_file['name']}"])
        except Exception as e:
            current_app.logger.warning(f"Failed to remove old avatar (non-blocking): {e}")

        # Upload new avatar
        supabase.storage.from_(SUPABASE_BUCKET).upload(
            storage_path,
            file_bytes,
            file_options={
                "content-type": f"image/{file_extension}",
                "content-disposition": f'inline; filename="{unique_file_name}"'
            }
        )

        avatar_url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(storage_path)

        # Update user profile with new avatar URL
        supabase.table('user_profiles').upsert({
            'user_id': user_id,
            'avatar_url': avatar_url
        }, on_conflict='user_id').execute()

        return jsonify({
            "message": "Avatar uploaded successfully",
            "avatar_url": avatar_url
        }), 200

    except Exception as e:
        current_app.logger.error(f'Failed to upload avatar: {e}', exc_info=True)
        return jsonify({'error': 'Failed to upload avatar', 'details': str(e)}), 500
