from flask import request, jsonify, current_app, g
from app.userPortal.profile import profile_bp
from app.userPortal.subscription.helpers import require_authentication
from app import extensions
import requests
import uuid

# Supabase storage bucket used for user-uploaded resumes
SUPABASE_BUCKET = "user-documents"

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
        webhook_response = requests.post(webhook_url, json={"resume_url": public_url}, timeout=60)

        if webhook_response.status_code != 200:
            current_app.logger.error(f'n8n webhook call failed: {webhook_response.text}')
            return jsonify({'error': 'Failed to extract profile', 'details': webhook_response.text}), 500

        webhook_json = webhook_response.json()
        output = webhook_json.get('output', webhook_json)

        # --- 3. Prepare data for insertion into Supabase ---
        profile_fields = {
            'name': output.get('name'),
            'title': output.get('title'),
            'bio': output.get('bio'),
            'location': output.get('location'),
            'email': output.get('email'),
            'phone': output.get('phone'),
            'linkedin_url': output.get('linkedin_url'),
            'website': output.get('website'),
            'skills': output.get('skills'),
            'certifications': output.get('certification') or output.get('certifications'),
            'experience': output.get('experience'),
            'projects': output.get('projects'),
            'resume_url': public_url
        }

        profile_data = {'user_id': user_id, **profile_fields}

        # --- 4. Upsert into Supabase ---
        insert_result = supabase.table('user_profiles').upsert(profile_data, on_conflict='user_id').execute()

        return jsonify({
            'profile_data': profile_data,
            'db_result': insert_result.data if hasattr(insert_result, 'data') else str(insert_result),
            'webhook_output': output
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