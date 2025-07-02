import requests
import uuid
import time
from flask import current_app

AMPLITUDE_API_URL = "https://api2.amplitude.com/2/httpapi"

def send_amplitude_event(user_id, event_type, event_properties=None, user_properties=None):
    api_key = current_app.config.get("AMPLITUDE_API_KEY")
    if not api_key:
        current_app.logger.error("Amplitude API key not configured")
        raise Exception("Amplitude API key not configured")

    event = {
        "user_id": str(user_id),
        "event_type": event_type,
        "time": int(time.time() * 1000),  # milliseconds since epoch
        "insert_id": str(uuid.uuid4()),   # unique event id for deduplication
    }
    if event_properties:
        event["event_properties"] = event_properties
    if user_properties:
        event["user_properties"] = user_properties

    payload = {
        "api_key": api_key,
        "events": [event]
    }
    current_app.logger.info(f"Sending Amplitude event: {event}")
    response = requests.post(AMPLITUDE_API_URL, json=payload)
    if response.status_code != 200:
        current_app.logger.error(f"Amplitude error: {response.status_code} {response.text}")
        raise Exception(f"Amplitude error: {response.status_code} {response.text}")
    else:
        current_app.logger.info(f"Amplitude event sent successfully: {event}")
    return response

def sign_up_event(user_uuid, user_email, user_name):
    send_amplitude_event(
        user_id=user_uuid,
        event_type="sign_up",
        event_properties={
            "source": "Google",  # or "Linkedin"
        },
        user_properties={
            "email": user_email,
            "name": user_name,
            "subscription_status": "Active",  # or "Expired"
            "subscription_plan": "Pro",       # or plan name
        }
    )

def initial_questionnaire_filled_event(user_uuid, answer_1, answer_2):
    send_amplitude_event(
        user_id=user_uuid,
        event_type="initial_questionnaire_filled",
        event_properties={
            "question_1": answer_1,
            "question_2": answer_2,
            # ...map all questions and answers
        }
    )

def resume_analyze_event(user_uuid, company_url, original_resume_url, score, improved_score, feedback, new_resume_url):
    send_amplitude_event(
        user_id=user_uuid,
        event_type="resume_analyze",
        event_properties={
            "company_url": company_url,
            "original_resume_url": original_resume_url,
            "score": score,
            "improved_score": improved_score,
            "feedback": feedback,
            "new_resume_url": new_resume_url,
        }
    )

def cover_letter_event(user_uuid, company_url, resume_url, cover_letter_text, additional_feedback, feedback):
    send_amplitude_event(
        user_id=user_uuid,
        event_type="cover_letter",
        event_properties={
            "company_url": company_url,
            "resume_url": resume_url,
            "cover_letter": cover_letter_text,
            "additional_feedback": additional_feedback,
            "feedback": feedback,
        }
    )

def job_search_event(user_uuid, filters_dict):
    send_amplitude_event(
        user_id=user_uuid,
        event_type="job_search",
        event_properties={
            "filters": filters_dict,  # e.g., {"location": "NY", "role": "Engineer"}
        }
    )

def job_reveal_event(user_uuid, job_id, job_title, company_name, feedback):
    send_amplitude_event(
        user_id=user_uuid,
        event_type="job_reveal",
        event_properties={
            "job_id": job_id,
            "job_title": job_title,
            "company": company_name,
            "feedback": feedback,
        }
    )

def linkedin_optimizer_event(user_uuid, linkedin_url, goals, feedback):
    send_amplitude_event(
        user_id=user_uuid,
        event_type="linkedin_optimizer",
        event_properties={
            "linkedin_url": linkedin_url,
            "goals": goals,
            "feedback": feedback,
        }
    )