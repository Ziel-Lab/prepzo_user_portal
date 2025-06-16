import uuid
import threading
import time
import json
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

resume_analyze_bp = Blueprint('resume_analyze_bp', __name__)

# This in-memory dictionary will store the state of our tasks.
analysis_jobs = {}

def perform_resume_analysis(task_id, user_id, resume_url, job_description, company_website, additional_comments, resume_title):
    """
    This function simulates a long-running resume analysis.
    Replace the 'time.sleep' and mock data with your actual analysis logic.
    """
    try:
        # Simulate a long-running task like an API call
        time.sleep(15) 

        # --- YOUR ACTUAL ANALYSIS LOGIC GOES HERE ---
        mock_feedback = { "score": 85, "feedback": "This is a great resume." }
        mock_new_resume = { "changes": "- Updated summary.", "new_resume": "Full text of new resume.", "new_score": 95 }
        analysis_result = {
            "feedback": json.dumps(mock_feedback),
            "new_resume": json.dumps(mock_new_resume),
            "analysis_id": "mock_analysis_" + str(uuid.uuid4()),
            "message": "Analysis complete"
        }
        # --- END OF YOUR LOGIC ---

        analysis_jobs[task_id] = {'status': 'SUCCESS', 'result': analysis_result}

    except Exception as e:
        analysis_jobs[task_id] = {'status': 'FAILURE', 'result': {'error': str(e)}}


@resume_analyze_bp.route("/start-analysis", methods=["POST"])
@jwt_required()
def start_analysis():
    user_id = get_jwt_identity()
    data = request.get_json()
    
    resume_url = data.get('current_resume_url')
    job_description = data.get('job_description')
    company_website = data.get('company_website')
    
    if not all([resume_url, job_description, company_website]):
        return jsonify({"error": "Missing required fields"}), 400

    task_id = str(uuid.uuid4())
    analysis_jobs[task_id] = {'status': 'PENDING', 'result': None}

    thread = threading.Thread(
        target=perform_resume_analysis,
        args=(
            task_id, 
            user_id, 
            resume_url, 
            job_description, 
            data.get('company_website'), 
            data.get('additional_comments', ''), 
            data.get('resume_title', 'Uploaded Resume')
        )
    )
    thread.start()
    
    return jsonify({'task_id': task_id}), 202


@resume_analyze_bp.route('/task-status/<task_id>', methods=['GET'])
@jwt_required()
def get_task_status(task_id):
    job = analysis_jobs.get(task_id)
    if not job:
        return jsonify({'status': 'NOT_FOUND', 'result': None}), 404
        
    return jsonify(job), 200
