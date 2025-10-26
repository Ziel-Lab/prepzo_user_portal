from datetime import date, datetime
from app import extensions
from .helpers import get_anniversary_period, get_user_display_name

def ensure_current_usage_record(user_id, plan_id, period_start, period_end, display_name):
    supabase = extensions.supabase
    initial_usage = {
        'user_id': user_id,
        'plan_id': plan_id,
        'display_name': display_name,
        'period_start': str(period_start),
        'period_end': str(period_end)
    }
    new_usage_res = supabase.table('feature_usage').upsert(
        initial_usage,
        on_conflict='user_id',
        returning='representation'
    ).execute()
    return new_usage_res.data[0] if new_usage_res.data else None

def rollover_if_needed(user_id, current_plan_id, user_created_at, today):
    supabase = extensions.supabase
    usage_res = supabase.table('feature_usage') \
        .select('*') \
        .eq('user_id', user_id) \
        .order('period_end', desc=True) \
        .limit(1) \
        .maybe_single() \
        .execute()
    
    usage_record = usage_res.data
    if not usage_record or today > datetime.strptime(usage_record['period_end'], '%Y-%m-%d').date():
        period_start, period_end = get_anniversary_period(user_created_at, today)
        display_name = get_user_display_name(extensions.supabase.auth.admin.get_user_by_id(user_id).user)
        
        new_usage = ensure_current_usage_record(user_id, current_plan_id, period_start, period_end, display_name)
        
        if usage_record:
            update_payload = {}
            for key, value in usage_record.items():
                if key.endswith('_lifetime_count'):
                    update_payload[key] = value or 0
                    period_key = key.replace('_lifetime_count', '_period_count')
                    update_payload[period_key] = 0
            supabase.table('feature_usage').update(update_payload).eq('id', new_usage['id']).execute()
        
        return new_usage
    return usage_record

def apply_plan_change_for_current_period(user_id, new_plan_id):
    supabase = extensions.supabase
    usage_res = supabase.table('feature_usage') \
        .select('*') \
        .eq('user_id', user_id) \
        .order('period_end', desc=True) \
        .limit(1) \
        .maybe_single() \
        .execute()
    
    usage_record = usage_res.data
    if usage_record and usage_record['plan_id'] != new_plan_id:
        supabase.table('feature_usage') \
            .update({'plan_id': new_plan_id}) \
            .eq('user_id', user_id) \
            .eq('period_start', usage_record['period_start']) \
            .execute()
