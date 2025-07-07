from flask import request, jsonify, current_app, g
import requests
from datetime import datetime
from app import extensions  # Provides initialized Supabase client

from app.userPortal.subscription.helpers import require_authentication, check_and_use_feature

from . import job_listing_bp
from app.utils.amplitude import job_reveal_event, job_search_event, amplitude_identify_user

# @job_listing_bp.after_request
# def _add_cors_headers(resp):
#     """
#     Ensure all job-listing responses (including OPTIONS pre-flight) have
#     the required CORS headers so the browser lets the request through.
#     """
#     origin = request.headers.get("Origin")
#     if origin:
#         resp.headers["Access-Control-Allow-Origin"] = origin
#     resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
#     resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
#     resp.headers["Access-Control-Allow-Credentials"] = "true"
#     return resp
# ---------------------------------------------------------------------------

@job_listing_bp.route("/search-jobs", methods=["POST", "OPTIONS"])
@require_authentication
def search_jobs():
    """Proxy endpoint to search job listings via TheirStack API.

    The client sends a JSON payload that largely mirrors the TheirStack API
    parameters. We forward that payload to the upstream service and return
    the response. A valid JWT must be supplied in the Authorization header
    (handled by ``@require_authentication``).
    """
    # Handle CORS pre-flight quickly (already taken care of in require_authentication)

    # Retrieve configuration
    current_user_id = str(g.user.id)
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

        response_payload = response.json()

        # -------------------------------------------------------------------
        # Mark jobs that were previously revealed by the current user
        # -------------------------------------------------------------------
        revealed_job_ids = set()
        try:
            # Fetch all job_ids this user has already revealed (cached locally)
            revealed_res = extensions.supabase.table("revealed_jobs").select("job_id").eq("user_id", current_user_id).execute()
            if revealed_res.data:
                revealed_job_ids = {str(row["job_id"]) for row in revealed_res.data}
        except Exception as e:
            current_app.logger.warning(f"Could not fetch revealed job list from Supabase: {e}")

        try:
            # TheirStack search API returns list of jobs under the 'data' key
            jobs_list = response_payload.get("data") if isinstance(response_payload, dict) else None
            if jobs_list and isinstance(jobs_list, list):
                for job in jobs_list:
                    # Normalise job_id field name variations
                    candidate_id = job.get("id") or job.get("job_id")
                    if candidate_id is not None:
                        job["already_revealed"] = str(candidate_id) in revealed_job_ids
        except Exception as e:
            current_app.logger.warning(f"Failed while tagging already revealed jobs: {e}")

        # Send the event to Amplitude
        try:
            user_email = g.user.email or g.user.user_metadata.get("email")
            job_search_event(current_user_id, client_payload, user_properties={
                "email": user_email,
            })
            try:
                amplitude_identify_user(current_user_id, {"email": user_email})
            except Exception as e:
                current_app.logger.warning(f"Failed to send Amplitude Identify call: {e}")
        except Exception as e:
            current_app.logger.warning(f"Failed to send Amplitude event: {e}")

        return jsonify(response_payload), response.status_code

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
    current_user_id = str(g.user.id)
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

        job_id = client_payload.get("job_id") or client_payload.get("id")
        if not job_id:
            return jsonify({"error": "Missing required field 'job_id' in request payload."}), 400

        # -------------------------------------------------------------------
        # Attempt to serve job details from local cache to save TheirStack credits
        # -------------------------------------------------------------------
        try:
            cached_res = extensions.supabase.table("revealed_jobs").select("job_details").eq("user_id", current_user_id).eq("job_id", job_id).maybe_single().execute()
            if cached_res.data and cached_res.data.get("job_details"):
                current_app.logger.info(f"Serving cached job details for job_id {job_id} and user {current_user_id}")
                return jsonify(cached_res.data.get("job_details")), 200
        except Exception as e:
            current_app.logger.warning(f"Failed to fetch cached job details: {e}")

        # -------------------------------------------------------------------
        # No cached data – call TheirStack API and cache the response
        # -------------------------------------------------------------------
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        upstream_payload = {
            k: v for k, v in client_payload.items() if k not in ("job_id","id")
        }
        upstream_payload.setdefault("job_id_or",[job_id])
        upstream_payload.setdefault("limit",1)
        

        response = requests.post(
            theirstack_url,
            headers=headers,
            json=upstream_payload,
            timeout=request_timeout,
        )
        response.raise_for_status()

        response_payload = response.json()

        # Cache the revealed job so future requests won't cost credits
        try:
            cache_payload = {
                "user_id": current_user_id,
                "job_id": job_id,
                "job_details": response_payload,
                "revealed_at": datetime.utcnow().isoformat(),
            }
            # Supabase expects a single comma-separated string for the `on_conflict` argument
            # when specifying multiple columns. Passing a Python list generates multiple
            # query parameters (one per element) which PostgREST treats as invalid and
            # results in the upsert silently failing. Use the canonical string form instead.
            extensions.supabase.table("revealed_jobs").upsert(
                cache_payload,
                on_conflict="user_id, job_id"
            ).execute()
        except Exception as e:
            current_app.logger.warning(f"Failed to cache revealed job in Supabase: {e}")

        # Send the event to Amplitude
        try:
            user_email = g.user.email or g.user.user_metadata.get("email")
            job_reveal_event(
                current_user_id,
                job_id,
                client_payload.get("job_title"),
                client_payload.get("company_name"),
                client_payload.get("feedback"),
                user_properties={
                    "email": user_email,
                }
            )
            try:
                amplitude_identify_user(current_user_id, {"email": user_email})
            except Exception as e:
                current_app.logger.warning(f"Failed to send Amplitude Identify call: {e}")
        except Exception as e:
            current_app.logger.warning(f"Failed to send Amplitude event: {e}")

        return jsonify(response_payload), response.status_code

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