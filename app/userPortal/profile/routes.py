from flask import request, jsonify, current_app, g
from app.userPortal.profile import profile_bp
from app.userPortal.subscription.helpers import require_authentication
from app import extensions
import requests
import uuid

# Supabase storage bucket used for user-uploaded resumes
SUPABASE_BUCKET = "user-documents"
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
# Maximum allowed avatar image size (in bytes)
MAX_AVATAR_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB limit for avatar files

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

        # Enforce file size limit before proceeding
        if len(file_bytes) > MAX_AVATAR_SIZE_BYTES:
            return jsonify({'error': f'Avatar file exceeds the {MAX_AVATAR_SIZE_BYTES // (1024 * 1024)} MB limit'}), 413

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

@profile_bp.route('/make-public', methods=['POST', 'OPTIONS'])
@require_authentication
def make_profile_public():
    """Mark the authenticated user's profile as public, generating a public slug
    if one does not yet exist.
    """
    if request.method == 'OPTIONS':
        return '', 204

    try:
        supabase = extensions.supabase
        if supabase is None:
            return jsonify({'error': 'Supabase client not initialized'}), 500

        user_id = str(g.user.id)

        # Retrieve existing slug if any to keep URLs stable
        existing = supabase.table('user_profiles').select('public_slug').eq('user_id', user_id).maybe_single().execute()
        public_slug = None
        if existing and existing.data and existing.data.get('public_slug'):
            public_slug = existing.data['public_slug']
        else:
            public_slug = uuid.uuid4().hex[:8]  # simple unique slug

        supabase.table('user_profiles').upsert({
            'user_id': user_id,
            'is_public': True,
            'public_slug': public_slug
        }, on_conflict='user_id').execute()

        return jsonify({'message': 'Profile made public', 'public_slug': public_slug}), 200

    except Exception as e:
        current_app.logger.error(f'Failed to make profile public: {e}', exc_info=True)
        return jsonify({'error': 'Failed to change profile visibility', 'details': str(e)}), 500


@profile_bp.route('/make-private', methods=['POST', 'OPTIONS'])
@require_authentication
def make_profile_private():
    """Set the authenticated user's profile back to private."""
    if request.method == 'OPTIONS':
        return '', 204

    try:
        supabase = extensions.supabase
        if supabase is None:
            return jsonify({'error': 'Supabase client not initialized'}), 500

        user_id = str(g.user.id)

        # Simply flip the visibility flag; keep slug for potential future re-publishing
        supabase.table('user_profiles').upsert({
            'user_id': user_id,
            'is_public': False
        }, on_conflict='user_id').execute()

        return jsonify({'message': 'Profile made private'}), 200

    except Exception as e:
        current_app.logger.error(f'Failed to make profile private: {e}', exc_info=True)
        return jsonify({'error': 'Failed to change profile visibility', 'details': str(e)}), 500


@profile_bp.route('/public/<string:slug>', methods=['GET', 'OPTIONS'])
def get_public_profile(slug):
    """Return a public user profile by slug.

    Only profiles marked `is_public = true` are returned. If not found, 404."""
    if request.method == 'OPTIONS':
        return '', 204

    try:
        supabase = extensions.supabase
        if supabase is None:
            return jsonify({'error': 'Supabase client not initialized'}), 500

        response = supabase.table('user_profiles').select('*').eq('public_slug', slug).eq('is_public', True).maybe_single().execute()

        if not response or not response.data:
            return jsonify({'error': 'Profile not found or not public'}), 404

        # Optionally redact sensitive fields
        public_data = response.data.copy()
        public_data.pop('email', None)
        public_data.pop('phone', None)
        public_data.pop('resume_url', None)

        return jsonify({'profile': public_data}), 200

    except Exception as e:
        current_app.logger.error(f'Failed to fetch public profile for slug {slug}: {e}', exc_info=True)
        return jsonify({'error': 'Failed to fetch public profile', 'details': str(e)}), 500


@profile_bp.route('/edit-slug', methods=['POST', 'OPTIONS'])
@require_authentication
def edit_public_slug():
    """Allow the authenticated user to change their public slug.

    Expects JSON: { "new_slug": "desired-slug" }
    Slug must be unique across all public profiles and consist of
    alphanumerics plus hyphen/underscore.
    """
    if request.method == 'OPTIONS':
        return '', 204

    data = request.get_json(silent=True) or {}
    new_slug = (data.get('new_slug') or '').strip()

    if not new_slug:
        return jsonify({'error': 'new_slug is required'}), 400

    import re
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,32}', new_slug):
        return jsonify({'error': 'Slug must be 3-32 chars, letters, numbers, hyphen or underscore'}), 400

    try:
        supabase = extensions.supabase
        if supabase is None:
            return jsonify({'error': 'Supabase client not initialized'}), 500

        user_id = str(g.user.id)

        # Check uniqueness (slug can be in use by this user already)
        existing = supabase.table('user_profiles').select('user_id').eq('public_slug', new_slug).maybe_single().execute()
        if existing and existing.data and existing.data.get('user_id') != user_id:
            return jsonify({'error': 'Slug already in use'}), 409

        supabase.table('user_profiles').upsert({
            'user_id': user_id,
            'public_slug': new_slug
        }, on_conflict='user_id').execute()

        return jsonify({'message': 'Slug updated', 'public_slug': new_slug}), 200

    except Exception as e:
        current_app.logger.error(f'Failed to edit public slug: {e}', exc_info=True)
        return jsonify({'error': 'Failed to edit slug', 'details': str(e)}), 500