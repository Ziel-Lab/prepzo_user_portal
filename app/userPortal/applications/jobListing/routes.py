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

# In-memory store for demo purposes (replace with persistent store for production)
n8n_push_store = {}

@job_listing_bp.route("/n8n-push", methods=["POST"])
def n8n_push():
    """Endpoint for n8n to push data to the backend."""
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "Missing 'user_id' in payload."}), 400
    n8n_push_store[user_id] = data
    return jsonify({"message": "Data received and stored.", "user_id": user_id}), 200

@job_listing_bp.route("/n8n-push", methods=["GET"])
@require_authentication
def get_n8n_push():
    """Frontend fetches the latest data pushed by n8n for the current user."""
    user_id = str(g.user.id)
    data = n8n_push_store.get(user_id)
    if not data:
        return jsonify({"message": "No data available for this user."}), 404
    return jsonify(data), 200

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

    request_timeout = current_app.config.get("THEIRSTACK_HTTP_TIMEOUT", 200)  # seconds

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
        try:
            # Fetch all jobs this user has already revealed (cached locally)
            # We retrieve both job_id and the cached job_details so we can merge the
            # full information into the search results and avoid spending credits
            # again for already-revealed jobs.
            revealed_res = (
                extensions.supabase
                .table("revealed_jobs")
                .select("job_id, job_details")
                .eq("user_id", current_user_id)
                .execute()
            )

            revealed_job_ids = set()
            revealed_jobs_map = {}

            if revealed_res.data:
                for row in revealed_res.data:
                    jid = str(row.get("job_id"))
                    if jid:
                        revealed_job_ids.add(jid)
                        # Store cached details when available; may be None
                        if row.get("job_details"):
                            revealed_jobs_map[jid] = row["job_details"]
            else:
                revealed_jobs_map = {}
        except Exception as e:
            current_app.logger.warning(f"Could not fetch revealed job list from Supabase: {e}")
            # Gracefully degrade by falling back to empty structures
            revealed_job_ids = set()
            revealed_jobs_map = {}

        try:
            # TheirStack search API returns list of jobs under the 'data' key
            jobs_list = response_payload.get("data") if isinstance(response_payload, dict) else None
            if jobs_list and isinstance(jobs_list, list):
                for job in jobs_list:
                    # Normalise job_id field name variations
                    candidate_id = job.get("id") or job.get("job_id")
                    if candidate_id is None:
                        continue

                    candidate_id_str = str(candidate_id)

                    if candidate_id_str in revealed_job_ids:
                        # Mark as already revealed
                        job["already_revealed"] = True

                        # If we have cached job_details, merge them so the
                        # client receives the full, un-masked data without
                        # having to hit the reveal endpoint (saves credits).
                        cached_details = revealed_jobs_map.get(candidate_id_str)

                        if cached_details and isinstance(cached_details, dict):
                            # TheirStack job_details are typically returned as
                            # {"data": [ { ...full job info... } ] }
                            cached_data_list = cached_details.get("data")

                            if (
                                cached_data_list
                                and isinstance(cached_data_list, list)
                                and len(cached_data_list) > 0
                                and isinstance(cached_data_list[0], dict)
                            ):
                                # Merge detailed fields into the existing job
                                job.update(cached_data_list[0])
                            else:
                                # Fallback: merge whatever top-level keys we
                                # have (handles unexpected shapes)
                                job.update(cached_details)
                    else:
                        job["already_revealed"] = False
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

    request_timeout = current_app.config.get("THEIRSTACK_HTTP_TIMEOUT", 200)  # seconds

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
            if cached_res is not None and cached_res.data and cached_res.data.get("job_details"):
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
                "status": "revealed",  # Default status when job is first revealed
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

@job_listing_bp.route("/revealed-jobs-history", methods=["GET", "OPTIONS"])
@require_authentication
def get_revealed_jobs_history():
    """Return a list of jobs this user has previously revealed.

    The response is ordered by ``revealed_at`` descending. An optional ``limit``
    query-string parameter can be provided to restrict the number of records
    returned (default = 50). Requires a valid JWT and therefore uses the
    ``@require_authentication`` decorator.
    """
    # Handle CORS pre-flight quickly (already taken care of in require_authentication)

    current_user_id = str(g.user.id)
    supabase = extensions.supabase

    try:
        # Optional ?limit=n param for pagination / UI convenience
        try:
            limit = int(request.args.get("limit", 50))
            if limit <= 0:
                limit = 50
        except (TypeError, ValueError):
            limit = 50

        # Fetch rows and order by revealed_at DESC so newest first
        query = (
            supabase
            .table("revealed_jobs")
            .select("job_id, job_details,status, revealed_at")
            .eq("user_id", current_user_id)
            .order("revealed_at", desc=True)
            .limit(limit)
        )
        res = query.execute()
        data = res.data or []

        return jsonify(data), 200

    except Exception as e:
        current_app.logger.error(
            f"Failed to retrieve revealed jobs history for user {current_user_id}: {e}",
            exc_info=True,
        )
        return jsonify({"error": "Could not fetch job history."}), 500 

@job_listing_bp.route("/update-job-status", methods=["POST", "OPTIONS"])
@require_authentication
def update_job_status():
    """Update the status of a previously revealed job.

    Accepts JSON payload with job_id and status. Valid statuses are:
    'revealed', 'applied', 'scheduled', 'interview', 'rejected', 'offered', 'accepted'
    
    Requires a valid JWT and the job must have been previously revealed by this user.
    """
    current_user_id = str(g.user.id)
    supabase = extensions.supabase

    # Define valid job statuses (enum-like validation)
    VALID_STATUSES = {
        "revealed",     # Default when job is first revealed
        "applied",      # User applied to this job
        "scheduled",    # Interview/call scheduled
        "interview",    # Interview in progress or completed
        "rejected",     # Application rejected
        "offered",      # Job offer received
        "accepted",     # Job offer accepted
        "withdrawn"     # User withdrew application
    }

    try:
        data = request.get_json(silent=True) or {}
        job_id = data.get("job_id")
        new_status = data.get("status")

        if not job_id:
            return jsonify({"error": "Missing required field 'job_id'."}), 400

        if not new_status:
            return jsonify({"error": "Missing required field 'status'."}), 400

        if new_status not in VALID_STATUSES:
            return jsonify({
                "error": f"Invalid status '{new_status}'. Valid statuses are: {', '.join(sorted(VALID_STATUSES))}"
            }), 400

        # Check if the job exists for this user
        existing_job = supabase.table("revealed_jobs").select("job_id").eq("user_id", current_user_id).eq("job_id", job_id).maybe_single().execute()

        if not existing_job.data:
            return jsonify({"error": "Job not found. You can only update status for jobs you have previously revealed."}), 404

        # Update the status
        update_payload = {
            "status": new_status,
            "updated_at": datetime.utcnow().isoformat()
        }

        result = supabase.table("revealed_jobs").update(update_payload).eq("user_id", current_user_id).eq("job_id", job_id).execute()

        if not result.data:
            return jsonify({"error": "Failed to update job status."}), 500

        return jsonify({
            "message": "Job status updated successfully.",
            "job_id": job_id,
            "status": new_status
        }), 200

    except Exception as e:
        current_app.logger.error(
            f"Failed to update job status for user {current_user_id}: {e}",
            exc_info=True,
        )
        return jsonify({"error": "Could not update job status."}), 500 

@job_listing_bp.route("/ai-job-search", methods=["POST", "OPTIONS"])
@require_authentication
def ai_job_search():
    """Endpoint to forward a prompt and user_id to the n8n AI job search webhook and return its response."""
    if request.method == "OPTIONS":
        # Handle CORS pre-flight
        response = jsonify({"message": "CORS preflight"})
        response.headers.add("Access-Control-Allow-Origin", request.headers.get("Origin", "*"))
        response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
        response.headers.add("Access-Control-Allow-Methods", "POST,OPTIONS")
        response.headers.add("Access-Control-Allow-Credentials", "true")
        return response, 200

    try:
        data = request.get_json(silent=True) or {}
        prompt = data.get("prompt")
        if not prompt:
            return jsonify({"error": "Missing 'prompt' in request body."}), 400

        user_id = str(g.user.id)
        n8n_webhook_url = "https://prepzo.app.n8n.cloud/webhook/a3b6a2b0-471f-4ed1-a89b-7440c4b9356d"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        payload = {"prompt": prompt, "user_id": user_id}
        n8n_response = requests.post(n8n_webhook_url, headers=headers, json=payload, timeout=60)
        n8n_response.raise_for_status()
        return jsonify(n8n_response.json()), n8n_response.status_code
    except requests.exceptions.HTTPError as http_err:
        return jsonify({"error": "n8n webhook request failed", "details": str(http_err)}), http_err.response.status_code if http_err.response else 500
    except Exception as e:
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500 