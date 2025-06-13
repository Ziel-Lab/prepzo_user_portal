from supabase import create_client, ClientOptions
import logging

supabase = None

def init_supabase(app):
    global supabase
    logger = app.logger 

    SUPABASE_URL = app.config.get("SUPABASE_URL")
    SUPABASE_KEY = app.config.get("SUPABASE_SERVICE_ROLE_KEY")

    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("Error: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing from Flask configuration. Supabase client NOT initialized.")
        return

    try:
        logger.info(f"Attempting to initialize Supabase client for URL: {SUPABASE_URL}")
        supabase = create_client(
            SUPABASE_URL, 
            SUPABASE_KEY,
            options=ClientOptions(postgrest_client_timeout=10)
        )
        if supabase:
            logger.info("Supabase client initialized successfully.")
        else:
            logger.error("Supabase client initialization returned None.")
    except Exception as e:
        logger.error(f"FATAL: An exception occurred during Supabase client initialization: {e}", exc_info=True)
        supabase = None # Ensure supabase is None if an error occurs