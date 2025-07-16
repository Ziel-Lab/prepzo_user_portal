from flask import request, jsonify, current_app, g
from app.userPortal.profile import profile_bp
from app.userPortal.subscription.helpers import require_authentication
from app import extensions
import PyPDF2
import io
import re

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
        file_bytes = file.read()
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        text = ''
        for page in pdf_reader.pages:
            text += page.extract_text() or ''

        # Simple regex-based extraction (for demo; real parsing may need more logic)
        def extract(pattern, text, group=1):
            match = re.search(pattern, text, re.IGNORECASE)
            return match.group(group).strip() if match else None

        name = extract(r"Name[:\s]+([A-Za-z\s]+)\n", text)
        title = extract(r"Headline[:\s]+(.+?)\n", text)
        email = extract(r"Email[:\s]+([\w\.-]+@[\w\.-]+)", text)
        phone = extract(r"Phone[:\s]+([\d\-\+\s\(\)]+)", text)
        location = extract(r"Location[:\s]+(.+?)\n", text)
        linkedin_url = extract(r"linkedin\.com/in/[\w\-]+", text)
        website = extract(r"Website[:\s]+(https?://\S+)", text)
        bio = extract(r"Summary[:\s]+(.+?)(?:\n\w|$)", text)

        user_id = str(g.user.id)
        profile_data = {
            'user_id': user_id,
            'name': name,
            'title': title,
            'bio': bio,
            'location': location,
            'email': email,
            'phone': phone,
            'linkedin_url': linkedin_url,
            'website': website,
        }

        # Insert into Supabase user_profiles table
        supabase = extensions.supabase
        insert_result = supabase.table('user_profiles').upsert(profile_data, on_conflict='user_id').execute()

        return jsonify({
            'extracted_text': text,
            'profile_data': profile_data,
            'db_result': insert_result.data if hasattr(insert_result, 'data') else str(insert_result)
        }), 200
    except Exception as e:
        current_app.logger.error(f'Failed to extract PDF or insert profile: {e}')
        return jsonify({'error': 'Failed to extract PDF or insert profile', 'details': str(e)}), 500 