"""
Security Configuration and Utilities for Prepzo User Portal

This module contains security configurations, validation functions,
and security recommendations for production deployment.
"""

import re
import logging
from functools import wraps
from flask import request, jsonify, current_app

# Security Configuration
SECURITY_CONFIG = {
    # File upload security
    'MAX_FILE_SIZE': 10 * 1024 * 1024,  # 10MB
    'ALLOWED_FILE_TYPES': {
        'pdf': 'application/pdf',
        'doc': 'application/msword',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'txt': 'text/plain'
    },
    'MAX_FILENAME_LENGTH': 255,
    'MAX_COMMENT_LENGTH': 1000,
    
    # Rate limiting (requests per minute)
    'RATE_LIMITS': {
        'upload': 10,
        'api_calls': 100,
        'auth_attempts': 5
    },
    
    # Input validation
    'MAX_STRING_LENGTH': 1000,
    'ALLOWED_DOCUMENT_TYPES': ['resume', 'cover_letter', 'transcript', 'portfolio'],
    
    # CORS security
    'ALLOWED_ORIGINS': [
        r"https://prepzo-client-.*\.vercel\.app",
        r"http://localhost:.*",
        "https://prepzo.ai",
        "https://www.prepzo.ai", 
        "https://dashboard.prepzo.ai"
    ]
}

def validate_input_string(value, max_length=None, field_name="input"):
    """
    Validate and sanitize string input
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    
    # Basic length check
    max_len = max_length or SECURITY_CONFIG['MAX_STRING_LENGTH']
    if len(value) > max_len:
        raise ValueError(f"{field_name} exceeds maximum length of {max_len}")
    
    # Remove null bytes and control characters
    cleaned = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', value)
    return cleaned.strip()

def validate_document_type(doc_type):
    """
    Validate document type against allowed values
    """
    if doc_type not in SECURITY_CONFIG['ALLOWED_DOCUMENT_TYPES']:
        raise ValueError(f"Invalid document type. Allowed: {SECURITY_CONFIG['ALLOWED_DOCUMENT_TYPES']}")
    return doc_type

def validate_file_upload(file, allowed_types=None):
    """
    Validate uploaded file for security
    """
    if not file or file.filename == '':
        raise ValueError("No file provided")
    
    # Check file size
    file.seek(0, 2)  # Seek to end
    file_size = file.tell()
    file.seek(0)  # Reset to beginning
    
    if file_size > SECURITY_CONFIG['MAX_FILE_SIZE']:
        raise ValueError(f"File size exceeds {SECURITY_CONFIG['MAX_FILE_SIZE']} bytes")
    
    # Validate filename
    if len(file.filename) > SECURITY_CONFIG['MAX_FILENAME_LENGTH']:
        raise ValueError("Filename too long")
    
    # Check for path traversal attempts
    if '..' in file.filename or '/' in file.filename or '\\' in file.filename:
        raise ValueError("Invalid filename")
    
    # Validate file type if specified
    if allowed_types:
        file_ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
        if file_ext not in allowed_types:
            raise ValueError(f"File type not allowed. Allowed: {list(allowed_types.keys())}")
    
    return True

def secure_error_response(error_message, status_code=400, log_details=None):
    """
    Return a secure error response that doesn't leak sensitive information
    """
    # Log detailed error internally
    if log_details:
        current_app.logger.error(f"Security Error: {log_details}")
    
    # Return generic error to user
    safe_errors = {
        400: "Invalid request",
        401: "Authentication required", 
        403: "Access denied",
        404: "Not found",
        429: "Too many requests",
        500: "Internal server error"
    }
    
    safe_message = safe_errors.get(status_code, "An error occurred")
    
    return jsonify({"error": safe_message}), status_code

def log_security_event(event_type, user_id=None, details=None, severity="WARNING"):
    """
    Log security-related events for monitoring
    """
    log_data = {
        "event_type": event_type,
        "user_id": user_id,
        "ip_address": request.remote_addr,
        "user_agent": request.headers.get('User-Agent'),
        "timestamp": current_app.logger.handlers[0].formatter.formatTime(),
        "details": details
    }
    
    logger = current_app.logger
    log_message = f"SECURITY_EVENT: {event_type} - {details}"
    
    if severity == "CRITICAL":
        logger.critical(log_message, extra=log_data)
    elif severity == "ERROR":
        logger.error(log_message, extra=log_data)
    else:
        logger.warning(log_message, extra=log_data)

# Security Recommendations for Production
SECURITY_RECOMMENDATIONS = """
CRITICAL SECURITY ITEMS FOR PRODUCTION:

1. DATABASE SECURITY (CRITICAL):
   ✓ Implement Row Level Security (RLS) policies in Supabase
   ✓ Use anon key for client operations, service key only for admin
   ✓ Audit all database table permissions
   
2. AUTHENTICATION & AUTHORIZATION (CRITICAL):
   ✓ Implement proper JWT validation middleware
   ✓ Add role-based access control (RBAC)
   ✓ Set up session management and token refresh
   
3. API SECURITY (HIGH):
   ✓ Add rate limiting (consider redis-based)
   ✓ Implement request size limits
   ✓ Add API versioning and deprecation strategy
   
4. FILE UPLOAD SECURITY (HIGH):
   ✓ Virus scanning for uploaded files
   ✓ Content-type validation beyond MIME
   ✓ Sandboxed file processing
   
5. MONITORING & LOGGING (HIGH):
   ✓ Set up security event monitoring
   ✓ Implement audit trails for sensitive operations
   ✓ Add alerting for suspicious activities
   
6. INFRASTRUCTURE (MEDIUM):
   ✓ Configure proper CORS policies
   ✓ Set up WAF (Web Application Firewall)
   ✓ Enable HTTPS everywhere
   ✓ Implement proper secret rotation
   
7. DATA PROTECTION (MEDIUM):
   ✓ Encrypt sensitive data at rest
   ✓ Implement data retention policies
   ✓ Add GDPR compliance features (data export/deletion)

8. DEPENDENCY SECURITY (MEDIUM):
   ✓ Regular security updates for dependencies
   ✓ Vulnerability scanning in CI/CD
   ✓ Pin dependency versions
""" 