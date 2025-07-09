import sys
import logging
from supabase import create_client, ClientOptions

# LiveKit imports - using the correct modern API
try:
    from livekit import api
    LIVEKIT_AVAILABLE = True
except ImportError:
    api = None
    LIVEKIT_AVAILABLE = False

supabase = None
livekit_client = None

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

def init_livekit(app):
    global livekit_client
    logger = app.logger if hasattr(app, "logger") else logging.getLogger("livekit")

    LIVEKIT_URL = app.config.get("LIVEKIT_URL")
    LIVEKIT_API_KEY = app.config.get("LIVEKIT_API_KEY")
    LIVEKIT_API_SECRET = app.config.get("LIVEKIT_API_SECRET")

    if not all([LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET]):
        logger.warning(
            "LiveKit credentials missing. LiveKit client NOT initialized. "
            "Mock interview functionality will not be available."
        )
        livekit_client = None
        app.extensions["livekit"] = None
        return

    # Check if LiveKit API is available
    if not LIVEKIT_AVAILABLE:
        logger.warning(
            "LiveKit package not installed. LiveKit client NOT initialized. "
            "Mock interview functionality will not be available. "
            "Install with: pip install livekit"
        )
        livekit_client = None
        app.extensions["livekit"] = None
        return

    try:
        logger.info(f"Initializing LiveKit client for URL: {LIVEKIT_URL}")

        # Initialize LiveKit API client 
        # For now, just store credentials - actual service will be initialized when needed
        livekit_client = {
            'url': LIVEKIT_URL,
            'api_key': LIVEKIT_API_KEY,
            'api_secret': LIVEKIT_API_SECRET,
            'available': LIVEKIT_AVAILABLE
        }

        logger.info("LiveKit client initialized successfully.")

    except Exception as e:
        logger.error(f"LiveKit initialization error: {e}", exc_info=True)
        livekit_client = None

    app.extensions["livekit"] = livekit_client
