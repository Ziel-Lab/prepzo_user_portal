from functools import wraps
import time
import logging

logger = logging.getLogger(__name__)

def monitor_performance(endpoint_name):
    """
    Performance monitoring decorator for API endpoints
    
    Args:
        endpoint_name: Human-readable name for the endpoint (for logging)
    
    Returns:
        Decorator function that monitors performance and logs metrics
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            start_time = time.time()
            
            # Get user ID safely
            user_id = "unknown"
            try:
                from flask import g
                if hasattr(g, 'user') and g.user:
                    user_id = g.user.id[:8] + '***'
            except Exception:
                pass
            
            try:
                result = f(*args, **kwargs)
                duration = time.time() - start_time
                
                # Log performance metrics
                logger.info(f"PERF: {endpoint_name} | User: {user_id} | Duration: {duration:.3f}s | Status: SUCCESS")
                
                # Alert on slow responses
                if duration > 2.0:
                    logger.warning(f"SLOW_RESPONSE: {endpoint_name} took {duration:.3f}s for user {user_id}")
                
                # Alert on very slow responses
                if duration > 5.0:
                    logger.critical(f"CRITICAL_SLOW: {endpoint_name} took {duration:.3f}s for user {user_id}")
                
                # Add performance header for debugging
                if hasattr(result, 'headers'):
                    result.headers['X-Response-Time'] = f"{duration:.3f}s"
                    result.headers['X-Endpoint'] = endpoint_name
                
                return result
                
            except Exception as e:
                duration = time.time() - start_time
                error_msg = str(e)[:100]
                logger.error(f"PERF: {endpoint_name} | User: {user_id} | Duration: {duration:.3f}s | Status: ERROR | Error: {error_msg}")
                
                # Alert on endpoint errors
                alert_on_performance_issue(endpoint_name, duration, user_id, error=error_msg)
                raise
                
        return decorated_function
    return decorator

def alert_on_performance_issue(endpoint, duration, user_id, error=None):
    """
    Alert on performance issues
    
    Args:
        endpoint: Endpoint name
        duration: Response time in seconds
        user_id: User identifier
        error: Error message if any
    """
    if duration > 3.0:
        logger.critical(f"CRITICAL_SLOW: {endpoint} took {duration:.3f}s for user {user_id}")
        # Future: Could integrate with external alerting service here
    
    if error:
        logger.error(f"ENDPOINT_ERROR: {endpoint} failed for user {user_id}: {error}")
        # Future: Could send error alerts to monitoring service

def log_endpoint_usage(endpoint_name, user_id, method="GET", additional_data=None):
    """
    Log endpoint usage for analytics
    
    Args:
        endpoint_name: Name of the endpoint
        user_id: User identifier
        method: HTTP method
        additional_data: Any additional data to log
    """
    log_data = {
        'endpoint': endpoint_name,
        'user': user_id[:8] + '***' if user_id else 'unknown',
        'method': method,
        'timestamp': time.time()
    }
    
    if additional_data:
        log_data.update(additional_data)
    
    logger.info(f"USAGE: {log_data}")

class PerformanceTracker:
    """
    Simple performance tracking utility
    """
    def __init__(self):
        self.metrics = {}
    
    def record_request(self, endpoint, duration, success=True):
        """Record a request performance metric"""
        if endpoint not in self.metrics:
            self.metrics[endpoint] = {
                'total_requests': 0,
                'total_duration': 0,
                'success_count': 0,
                'error_count': 0,
                'avg_duration': 0,
                'max_duration': 0,
                'min_duration': float('inf')
            }
        
        metric = self.metrics[endpoint]
        metric['total_requests'] += 1
        metric['total_duration'] += duration
        
        if success:
            metric['success_count'] += 1
        else:
            metric['error_count'] += 1
        
        metric['avg_duration'] = metric['total_duration'] / metric['total_requests']
        metric['max_duration'] = max(metric['max_duration'], duration)
        metric['min_duration'] = min(metric['min_duration'], duration)
    
    def get_metrics(self, endpoint=None):
        """Get performance metrics"""
        if endpoint:
            return self.metrics.get(endpoint, {})
        return self.metrics
    
    def reset_metrics(self):
        """Reset all metrics"""
        self.metrics = {}

# Global performance tracker instance
performance_tracker = PerformanceTracker()
