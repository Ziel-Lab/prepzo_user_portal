from supabase import create_client
from flask import current_app
import os
import logging

supabase = None

def init_supabase(app_config):
    global supabase
    
    # Try to get from app_config first, then fall back to os.getenv as a safety measure
    # This makes it robust whether secrets are fully loaded into app.config or are just in .env
    SUPABASE_URL = app_config.get("SUPABASE_URL") or os.getenv("SUPABASE_URL")
    SUPABASE_KEY = app_config.get("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    # Ensure logger is available (it should be as init_supabase is called after logger setup in create_app)
    logger = current_app.logger if current_app else logging.getLogger(__name__)
    
    logger.info(f"Attempting to initialize Supabase in extensions.py...")
    logger.info(f"SUPABASE_URL determined as: {SUPABASE_URL}")
    logger.info(f"SUPABASE_KEY is set: {bool(SUPABASE_KEY)}") 

    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("Error: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing from app_config/os.env. Supabase client NOT initialized.")
        supabase = None # Explicitly set to None
        return

    try:
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        if supabase_client:
            supabase = supabase_client # Assign to global if successful
            logger.info("Supabase client initialized successfully in extensions.py.")
        else:
            # This case is unlikely if create_client itself doesn't raise an error but returns None
            logger.error("Supabase client initialization call (create_client) returned None. Client NOT initialized.")
            supabase = None # Explicitly set to None
    except Exception as e:
        logger.error(f"Error during Supabase client initialization in extensions.py: {type(e).__name__} - {e}", exc_info=True)
        supabase = None # Ensure supabase is None if an error occurs
