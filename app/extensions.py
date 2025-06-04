from supabase import create_client
import os

supabase = None

def init_supabase():
    global supabase
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    
    print(f"Attempting to initialize Supabase...")
    print(f"SUPABASE_URL: {SUPABASE_URL}")
    # Be careful printing sensitive keys in production logs, ok for local debugging
    print(f"SUPABASE_KEY is set: {bool(SUPABASE_KEY)}") 

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
