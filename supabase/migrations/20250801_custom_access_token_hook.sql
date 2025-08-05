-- Custom Access Token Hook for Enhanced JWT Handling
-- This hook is called every time Supabase creates a new JWT
-- It adds custom claims that help with token refresh and user context

CREATE OR REPLACE FUNCTION public.custom_access_token_hook(event jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  user_id uuid;
  user_email text;
  user_metadata jsonb;
  subscription_plan_id integer;
  claims jsonb;
  custom_claims jsonb;
BEGIN
  -- Extract user information from the event
  user_id := (event->'user'->>'id')::uuid;
  user_email := event->'user'->>'email';
  user_metadata := event->'user'->'user_metadata';
  
  -- Initialize custom claims object
  custom_claims := '{}'::jsonb;
  
  -- Add basic user context claims
  custom_claims := jsonb_set(custom_claims, '{user_id}', to_jsonb(user_id));
  custom_claims := jsonb_set(custom_claims, '{email}', to_jsonb(user_email));
  
  -- Add user display name from metadata
  IF user_metadata ? 'full_name' AND user_metadata->>'full_name' IS NOT NULL THEN
    custom_claims := jsonb_set(custom_claims, '{display_name}', user_metadata->'full_name');
  ELSIF user_metadata ? 'name' AND user_metadata->>'name' IS NOT NULL THEN
    custom_claims := jsonb_set(custom_claims, '{display_name}', user_metadata->'name');
  ELSE
    custom_claims := jsonb_set(custom_claims, '{display_name}', to_jsonb(user_email));
  END IF;
  
  -- Add subscription plan information (if available)
  BEGIN
    SELECT plan_id INTO subscription_plan_id
    FROM user_subscriptions 
    WHERE user_id = (event->'user'->>'id')::uuid 
    LIMIT 1;
    
    IF subscription_plan_id IS NOT NULL THEN
      custom_claims := jsonb_set(custom_claims, '{subscription_plan_id}', to_jsonb(subscription_plan_id));
    ELSE
      custom_claims := jsonb_set(custom_claims, '{subscription_plan_id}', to_jsonb(1)); -- Default to Free plan
    END IF;
  EXCEPTION
    WHEN OTHERS THEN
      -- If subscription lookup fails, default to Free plan
      custom_claims := jsonb_set(custom_claims, '{subscription_plan_id}', to_jsonb(1));
  END;
  
  -- Add token metadata for refresh tracking
  custom_claims := jsonb_set(custom_claims, '{token_version}', to_jsonb('v2.0'));
  custom_claims := jsonb_set(custom_claims, '{issued_at}', to_jsonb(extract(epoch from now())::integer));
  custom_claims := jsonb_set(custom_claims, '{refresh_enabled}', to_jsonb(true));
  
  -- Add custom claims to the event's claims
  claims := event->'claims';
  IF claims IS NULL THEN
    claims := '{}'::jsonb;
  END IF;
  
  -- Merge our custom claims
  claims := claims || custom_claims;
  
  -- Update the event with our custom claims
  event := jsonb_set(event, '{claims}', claims);
  
  -- Log successful hook execution (optional, for debugging)
  RAISE LOG 'Custom Access Token Hook executed for user: %', user_id;
  
  RETURN event;
  
EXCEPTION
  WHEN OTHERS THEN
    -- Log error but don't fail the authentication
    RAISE LOG 'Custom Access Token Hook error: %', SQLERRM;
    -- Return original event unchanged if there's an error
    RETURN event;
END;
$$;

-- Grant necessary permissions
GRANT EXECUTE ON FUNCTION public.custom_access_token_hook(jsonb) TO supabase_auth_admin;
GRANT USAGE ON SCHEMA public TO supabase_auth_admin;

-- Revoke permissions from other roles for security
REVOKE EXECUTE ON FUNCTION public.custom_access_token_hook(jsonb) FROM authenticated, anon, public;

-- Add comment for documentation
COMMENT ON FUNCTION public.custom_access_token_hook(jsonb) IS 
'Custom Access Token Hook that adds enhanced claims to JWTs for better token refresh and user context handling';