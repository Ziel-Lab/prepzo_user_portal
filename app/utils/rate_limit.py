from functools import wraps
from collections import defaultdict
import time
import logging
from flask import jsonify, g

logger = logging.getLogger(__name__)

# Simple in-memory rate limiter store
rate_limit_store = defaultdict(list)

def simple_rate_limit(max_requests=10, window_seconds=60):
    """
    Simple rate limiter using in-memory storage
    
    Args:
        max_requests: Maximum number of requests allowed in the time window
        window_seconds: Time window in seconds
    
    Returns:
        Decorator function that enforces rate limiting
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user_id = g.user.id
            current_time = time.time()
            
            # Clean old requests outside window
            rate_limit_store[user_id] = [
                req_time for req_time in rate_limit_store[user_id] 
                if current_time - req_time < window_seconds
            ]
            
            # Check if user exceeds limit
            if len(rate_limit_store[user_id]) >= max_requests:
                logger.warning(f"Rate limit exceeded for user {user_id[:8]}*** on {f.__name__}")
                return jsonify({
                    'error': 'Too many requests. Please wait a moment.',
                    'retry_after': window_seconds,
                    'limit': max_requests,
                    'window': window_seconds
                }), 429
            
            # Add current request
            rate_limit_store[user_id].append(current_time)
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def get_rate_limit_status(user_id, max_requests=10, window_seconds=60):
    """
    Get current rate limit status for a user
    
    Args:
        user_id: User identifier
        max_requests: Maximum requests allowed
        window_seconds: Time window in seconds
    
    Returns:
        dict: Rate limit status information
    """
    current_time = time.time()
    
    # Clean old requests
    rate_limit_store[user_id] = [
        req_time for req_time in rate_limit_store[user_id] 
        if current_time - req_time < window_seconds
    ]
    
    current_requests = len(rate_limit_store[user_id])
    remaining_requests = max(0, max_requests - current_requests)
    
    return {
        'requests_made': current_requests,
        'requests_remaining': remaining_requests,
        'window_seconds': window_seconds,
        'reset_time': current_time + window_seconds if current_requests > 0 else None
    }

def clear_rate_limit_for_user(user_id):
    """
    Clear rate limit data for a specific user (useful for testing/admin)
    
    Args:
        user_id: User identifier to clear
    """
    if user_id in rate_limit_store:
        del rate_limit_store[user_id]
        logger.info(f"Cleared rate limit data for user {user_id[:8]}***")

def get_rate_limit_stats():
    """
    Get overall rate limiting statistics (for monitoring)
    
    Returns:
        dict: Overall rate limiting statistics
    """
    current_time = time.time()
    total_users = len(rate_limit_store)
    active_users = 0
    total_requests = 0
    
    for user_id, requests in rate_limit_store.items():
        # Count recent requests (last 60 seconds)
        recent_requests = [req for req in requests if current_time - req < 60]
        if recent_requests:
            active_users += 1
            total_requests += len(recent_requests)
    
    return {
        'total_tracked_users': total_users,
        'active_users_last_60s': active_users,
        'total_requests_last_60s': total_requests,
        'timestamp': current_time
    }
