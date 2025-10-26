import time
from flask import current_app
from app import extensions

_plan_cache = {}  # key: 'price:{price_id}' or 'id:{plan_id}', value: {'data': value, 'ts': timestamp}
TTL_SECONDS = 600  # 10 min default

def _get_ttl():
    try:
        return current_app.config.get('PLAN_CACHE_TTL_SECONDS', TTL_SECONDS)
    except:
        return TTL_SECONDS

def get_plan_id_by_price(price_id):
    key = f'price:{price_id}'
    now = time.time()
    ttl = _get_ttl()
    
    if key in _plan_cache and now - _plan_cache[key]['ts'] < ttl:
        return _plan_cache[key]['data']
    
    supabase = extensions.supabase
    res = supabase.table('subscription_plans').select('id').eq('stripe_price_id', price_id).single().execute()
    if res.data:
        plan_id = res.data['id']
        _plan_cache[key] = {'data': plan_id, 'ts': now}
        return plan_id
    return None

def get_plan_by_id(plan_id):
    key = f'id:{plan_id}'
    now = time.time()
    ttl = _get_ttl()
    
    if key in _plan_cache and now - _plan_cache[key]['ts'] < ttl:
        return _plan_cache[key]['data']
    
    supabase = extensions.supabase
    res = supabase.table('subscription_plans').select('*').eq('id', plan_id).single().execute()
    if res.data:
        plan = res.data
        _plan_cache[key] = {'data': plan, 'ts': now}
        return plan
    return None

def invalidate_cache():
    _plan_cache.clear()

