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

# Dual Supabase client architecture
supabase_admin = None  # Service role key - for admin operations
supabase_user = None   # Anon key - for user operations with RLS

# Legacy reference for backward compatibility
supabase = None

livekit_client = None

def init_supabase(app):
    global supabase_admin, supabase_user, supabase
    logger = app.logger if hasattr(app, "logger") else logging.getLogger("supabase")

    SUPABASE_URL = app.config.get("SUPABASE_URL")
    SUPABASE_SERVICE_KEY = app.config.get("SUPABASE_SERVICE_ROLE_KEY")
    SUPABASE_ANON_KEY = app.config.get("SUPABASE_ANON_KEY")

    if not SUPABASE_URL:
        logger.error("Error: SUPABASE_URL is missing. Supabase clients NOT initialized.")
        return

    if not SUPABASE_SERVICE_KEY:
        logger.error("Error: SUPABASE_SERVICE_ROLE_KEY is missing. Admin operations will be disabled.")
    
    if not SUPABASE_ANON_KEY:
        logger.error("Error: SUPABASE_ANON_KEY is missing. User operations will be disabled.")

    try:
        logger.info(f"Initializing dual Supabase clients for URL: {SUPABASE_URL}")

        # Set timeouts using ClientOptions
        options = ClientOptions(
            postgrest_client_timeout=10
        )

        # Initialize admin client (service role key) for admin operations
        if SUPABASE_SERVICE_KEY:
            supabase_admin = create_client(
                SUPABASE_URL,
                SUPABASE_SERVICE_KEY,
                options=options
            )
            logger.info("✅ Supabase ADMIN client initialized (service role)")
        
        # Initialize user client (anon key) for user operations with RLS
        if SUPABASE_ANON_KEY:
            supabase_user = create_client(
                SUPABASE_URL,
                SUPABASE_ANON_KEY,
                options=options
            )
            logger.info("✅ Supabase USER client initialized (anon key + RLS)")

        # Set legacy reference to admin client for backward compatibility
        # TODO: Gradually migrate all admin operations to use supabase_admin explicitly
        supabase = supabase_admin

        if not supabase_admin and not supabase_user:
            logger.error("FATAL: Neither Supabase client could be initialized.")
            return

        logger.info("🔒 Dual Supabase architecture initialized successfully")
        logger.info("   - Admin operations: service role key (bypasses RLS)")
        logger.info("   - User operations: anon key + JWT (enforces RLS)")

    except Exception as e:
        logger.error(f"FATAL: Supabase initialization error: {e}", exc_info=True)
        supabase_admin = None
        supabase_user = None
        supabase = None

    # Store both clients in app extensions
    app.extensions["supabase_admin"] = supabase_admin
    app.extensions["supabase_user"] = supabase_user
    app.extensions["supabase"] = supabase  # Legacy support


def get_user_client(jwt_token=None):
    """
    Get the user Supabase client with optional JWT token for RLS context.
    This should be used for all user-facing operations.
    """
    if not supabase_user:
        raise RuntimeError("User Supabase client not initialized")
    
    if jwt_token:
        # Set the JWT token for RLS context
        # This ensures the user can only access their own data
        try:
            supabase_user.auth.set_session_from_token(jwt_token)
        except Exception as e:
            logging.warning(f"Failed to set JWT session: {e}")
    
    return supabase_user


def get_admin_client():
    """
    Get the admin Supabase client (service role).
    ⚠️  WARNING: This bypasses RLS! Only use for:
    - Webhook processing
    - System administration
    - Cross-user operations (with proper authorization)
    """
    if not supabase_admin:
        raise RuntimeError("Admin Supabase client not initialized")
    
    return supabase_admin


def init_livekit(app):
    """Initialize LiveKit client for interview functionality"""
    global livekit_client
    logger = app.logger if hasattr(app, "logger") else logging.getLogger("livekit")
    
    if not LIVEKIT_AVAILABLE:
        logger.warning("LiveKit not available. Mock interview features will be disabled.")
        return
    
    api_key = app.config.get('LIVEKIT_API_KEY')
    api_secret = app.config.get('LIVEKIT_API_SECRET')
    
    if not api_key or not api_secret:
        logger.warning("LiveKit credentials not configured. Mock interview features will be disabled.")
        return
    
    try:
        # Store credentials for later use
        app.config['LIVEKIT_CONFIGURED'] = True
        logger.info("LiveKit credentials configured successfully.")
    except Exception as e:
        logger.error(f"LiveKit initialization error: {e}", exc_info=True)
        
    app.extensions["livekit"] = True
