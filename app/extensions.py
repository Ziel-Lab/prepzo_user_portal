from supabase import create_client

supabase = None

def init_supabase(app_config):
    global supabase
    SUPABASE_URL = app_config.get("SUPABASE_URL")
    SUPABASE_KEY = app_config.get("SUPABASE_SERVICE_ROLE_KEY")

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Error: SUPABASE_URL or SUPABASE_KEY is missing. Supabase client NOT initialized.")
        return

    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        if supabase:
            print("Supabase client initialized successfully.")
        else:
            print("Supabase client initialization returned None.")
    except Exception as e:
        print(f"Error during Supabase client initialization: {e}")
        supabase = None # Ensure supabase is None if an error occurs
