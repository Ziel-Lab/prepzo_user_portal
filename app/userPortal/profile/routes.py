from flask import request, jsonify, current_app, g
from app.userPortal.profile import profile_bp
from app.userPortal.subscription.helpers import require_authentication
from app import extensions
import PyPDF2
import io

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

        # Improved extraction logic for LinkedIn PDF
        def extract_profile_fields(text):
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            name = None
            title = None
            location = None
            email = None
            linkedin_url = None
            website = None
            bio = None

            # Find email, linkedin, website from the contact block (first 10 lines)
            for line in lines[:10]:
                if '@' in line and not email:
                    email = line
                if 'linkedin.com' in line and not linkedin_url:
                    linkedin_url = line
                if ('http' in line or 'www.' in line) and 'linkedin' not in line and not website:
                    website = line

            # Find the name: look for the first line after the contact block that is not an email/URL and is likely a name
            for i, line in enumerate(lines):
                if i < 5:  # skip contact block
                    continue
                # Heuristic: two words, both capitalized, not an email or URL
                if (
                    len(line.split()) >= 2 and
                    all(word[0].isupper() for word in line.split() if word) and
                    '@' not in line and
                    'www.' not in line and
                    'linkedin.com' not in line
                ):
                    name = line
                    # Title is likely the next line
                    if i + 1 < len(lines):
                        title = lines[i + 1]
                    # Location is likely after title or after 'Finalist'
                    for j in range(i + 1, min(i + 5, len(lines))):
                        if any(loc in lines[j] for loc in ["India", "Delhi", "Germany", "Karnataka"]):
                            location = lines[j]
                            break
                    break

            # Bio/Summary: Find the line 'Summary' and take the next few lines
            if 'Summary' in lines:
                idx = lines.index('Summary')
                bio = ' '.join(lines[idx+1:idx+5])  # Take next 4 lines as summary

            return {
                'name': name,
                'title': title,
                'location': location,
                'email': email,
                'linkedin_url': linkedin_url,
                'website': website,
                'bio': bio
            }

        profile_fields = extract_profile_fields(text)
        user_id = str(g.user.id)
        profile_data = {'user_id': user_id, **profile_fields}

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