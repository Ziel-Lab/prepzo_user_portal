import sys
import logging
from supabase import create_client, ClientOptions

supabase = None

def init_supabase(app):
    global supabase
    logger = app.logger if hasattr(app, "logger") else logging.getLogger("supabase")

    SUPABASE_URL = app.config.get("SUPABASE_URL")
    SUPABASE_KEY = app.config.get("SUPABASE_SERVICE_ROLE_KEY")

    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error(
            "Error: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing. Supabase client NOT initialized."
        )
        return

    try:
        logger.info(f"Initializing Supabase client for URL: {SUPABASE_URL}")

        # Set timeouts using ClientOptions (only accepted ones)
        options = ClientOptions(
            postgrest_client_timeout=10
        )

        # Initialize Supabase client
        supabase = create_client(
            SUPABASE_URL,
            SUPABASE_KEY,
            options=options
        )

        if supabase is None or not hasattr(supabase, "auth"):
            logger.error("Supabase client initialized but missing .auth. Check Supabase configuration.")
        else:
            logger.info("Supabase client initialized successfully.")

    except Exception as e:
        logger.error(f"FATAL: Supabase initialization error: {e}", exc_info=True)
        supabase = None

    app.extensions["supabase"] = supabase
