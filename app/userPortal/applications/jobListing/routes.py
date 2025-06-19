from flask import request, jsonify, current_app
import requests

from app.userPortal.subscription.helpers import require_authentication, check_and_use_feature

from . import job_listing_bp

# --- NEW: add uniform CORS headers for every response from this blueprint ---
@job_listing_bp.after_request
def _add_cors_headers(resp):
    """
    Ensure all job-listing responses (including OPTIONS pre-flight) have
    the required CORS headers so the browser lets the request through.
    """
    origin = request.headers.get("Origin")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    return resp
# ---------------------------------------------------------------------------

@job_listing_bp.route("/search-jobs", methods=["POST", "OPTIONS"])
@require_authentication
@check_and_use_feature("job_search_results")
def search_jobs():
    """Proxy endpoint to search job listings via TheirStack API.

    The client sends a JSON payload that largely mirrors the TheirStack API
    parameters. We forward that payload to the upstream service and return
    the response. A valid JWT must be supplied in the Authorization header
    (handled by ``@require_authentication``).
    """
    # Handle CORS pre-flight quickly (already taken care of in require_authentication)

    # Retrieve configuration
    api_key = current_app.config.get("THEIRSTACK_API_KEY")
    theirstack_url = current_app.config.get(
        "THEIRSTACK_API_URL_JOBS_SEARCH", "https://api.theirstack.com/v1/jobs/search"
    )

    if not api_key:
        current_app.logger.error("Missing THEIRSTACK_API_KEY in application configuration.")
        return (
            jsonify({"error": "Server misconfiguration: missing external API key."}),
            500,
        )

    request_timeout = current_app.config.get("THEIRSTACK_HTTP_TIMEOUT", 30)  # seconds

    try:
        # Use the JSON body as-is; default to an empty dict if none supplied
        client_payload = request.get_json(silent=True) or {}

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        response = requests.post(
            theirstack_url,
            headers=headers,
            json=client_payload,
            timeout=request_timeout,
        )
        response.raise_for_status()

        return jsonify(response.json()), response.status_code

    except requests.exceptions.HTTPError as http_err:
        # Attempt to provide the upstream error payload when available
        try:
            error_detail = http_err.response.json()
        except ValueError:
            error_detail = http_err.response.text

        current_app.logger.warning(
            "TheirStack API request failed with status %s: %s", http_err.response.status_code, error_detail
        )
        return (
            jsonify({"error": "TheirStack API request failed", "details": error_detail}),
            http_err.response.status_code,
        )
    except requests.exceptions.RequestException as req_err:
        current_app.logger.error("Network error during TheirStack API request: %s", str(req_err))
        return (
            jsonify({"error": "Request to TheirStack API failed", "details": str(req_err)}),
            500,
        )
    except Exception as e:
        current_app.logger.error("Unexpected error in search_jobs: %s", str(e), exc_info=True)
        return (
            jsonify({"error": "An unexpected error occurred", "details": str(e)}),
            500,
        ) 

@job_listing_bp.route("/get-job-details", methods=["POST", "OPTIONS"])
@require_authentication
@check_and_use_feature("job_search_results")
def get_job_details():
    """Proxy endpoint to search job listings via TheirStack API.

    The client sends a JSON payload that largely mirrors the TheirStack API
    parameters. We forward that payload to the upstream service and return
    the response. A valid JWT must be supplied in the Authorization header
    (handled by ``@require_authentication``).
    """
    # Handle CORS pre-flight quickly (already taken care of in require_authentication)

    # Retrieve configuration
    api_key = current_app.config.get("THEIRSTACK_API_KEY")
    theirstack_url = current_app.config.get(
        "THEIRSTACK_API_URL_JOBS_SEARCH", "https://api.theirstack.com/v1/jobs/search"
    )

    if not api_key:
        current_app.logger.error("Missing THEIRSTACK_API_KEY in application configuration.")
        return (
            jsonify({"error": "Server misconfiguration: missing external API key."}),
            500,
        )

    request_timeout = current_app.config.get("THEIRSTACK_HTTP_TIMEOUT", 30)  # seconds

    try:
        # Use the JSON body as-is; default to an empty dict if none supplied
        client_payload = request.get_json(silent=True) or {}

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        response = requests.post(
            theirstack_url,
            headers=headers,
            json=client_payload,
            timeout=request_timeout,
        )
        response.raise_for_status()

        return jsonify(response.json()), response.status_code

    except requests.exceptions.HTTPError as http_err:
        # Attempt to provide the upstream error payload when available
        try:
            error_detail = http_err.response.json()
        except ValueError:
            error_detail = http_err.response.text

        current_app.logger.warning(
            "TheirStack API request failed with status %s: %s", http_err.response.status_code, error_detail
        )
        return (
            jsonify({"error": "TheirStack API request failed", "details": error_detail}),
            http_err.response.status_code,
        )
    except requests.exceptions.RequestException as req_err:
        current_app.logger.error("Network error during TheirStack API request: %s", str(req_err))
        return (
            jsonify({"error": "Request to TheirStack API failed", "details": str(req_err)}),
            500,
        )
    except Exception as e:
        current_app.logger.error("Unexpected error in search_jobs: %s", str(e), exc_info=True)
        return (
            jsonify({"error": "An unexpected error occurred", "details": str(e)}),
            500,
        ) 