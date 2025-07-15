"""
LiveKit Agent Tools for Mock Interview Resume Processing
"""

import logging
import json
from typing import Dict, Any, Optional

# Handle imports for both standalone and Flask app usage
try:
    from flask import current_app
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False
    current_app = None

# Handle relative imports
try:
    from .api import get_document_content, extract_pdf_content
    from app import extensions
    from app.extensions import get_admin_client
except ImportError:
    try:
        from api import get_document_content, extract_pdf_content
    except ImportError:
        # Define minimal fallbacks if api module not available
        async def get_document_content(url):
            return None
        def extract_pdf_content(content):
            return None

# LiveKit agents imports
try:
    from livekit.agents import function_tool, RunContext
    LIVEKIT_AGENTS_AVAILABLE = True
except ImportError:
    LIVEKIT_AGENTS_AVAILABLE = False
    # Create a dummy decorator for development
    def function_tool():
        def decorator(func):
            return func
        return decorator

logger = logging.getLogger(__name__)

@function_tool()
async def load_user_resume(
    context: RunContext,
    user_id: str,
    document_type: str = "resume"
) -> Dict[str, Any]:
    """Load the user's resume from Supabase user_documents table and process it.
    
    Args:
        user_id: The ID of the user whose resume to load
        document_type: Type of document to load (default: "resume")
    
    Returns:
        A dictionary containing the resume content and metadata
    """
    try:
        if not FLASK_AVAILABLE:
            logger.error("Flask not available - cannot access Supabase")
            return {"error": "Service not available", "content": None}
        
        # Get admin client for database access
        try:
            admin_supabase = get_admin_client()
        except Exception as e:
            logger.error(f"Failed to get admin Supabase client: {e}")
            return {"error": "Database connection failed", "content": None}
        
        # Query user documents for resume
        try:
            response = admin_supabase.table("user_documents") \
                .select("id, document_name, document_type, document_url, created_at, document_comments") \
                .eq("uid", user_id) \
                .eq("document_type", document_type) \
                .order("created_at", desc=True) \
                .limit(1) \
                .execute()
            
            if not response.data:
                logger.info(f"No {document_type} found for user {user_id}")
                return {
                    "error": f"No {document_type} found",
                    "content": None,
                    "has_resume": False
                }
            
            resume_doc = response.data[0]
            logger.info(f"Found {document_type} for user {user_id}: {resume_doc['document_name']}")
            
        except Exception as e:
            logger.error(f"Database query failed: {e}")
            return {"error": "Failed to query documents", "content": None}
        
        # Get resume content from URL
        resume_content = None
        document_url = resume_doc.get('document_url')
        
        if document_url:
            try:
                resume_content = await get_document_content(document_url)
                if resume_content:
                    logger.info(f"Successfully extracted content from {document_type}")
                else:
                    logger.warning(f"Failed to extract content from {document_type}")
            except Exception as e:
                logger.error(f"Error extracting resume content: {e}")
        
        return {
            "success": True,
            "content": resume_content,
            "metadata": {
                "document_name": resume_doc.get('document_name'),
                "document_type": resume_doc.get('document_type'),
                "created_at": resume_doc.get('created_at'),
                "comments": resume_doc.get('document_comments'),
                "has_content": bool(resume_content)
            },
            "has_resume": True
        }
        
    except Exception as e:
        logger.error(f"Unexpected error in load_user_resume: {e}")
        return {
            "error": f"Unexpected error: {str(e)}",
            "content": None,
            "has_resume": False
        }

@function_tool()
async def load_job_description(
    context: RunContext,
    user_id: str,
    document_type: str = "job_description"
) -> Dict[str, Any]:
    """Load the user's job description from Supabase user_documents table.
    
    Args:
        user_id: The ID of the user whose job description to load
        document_type: Type of document to load (default: "job_description")
    
    Returns:
        A dictionary containing the job description content and metadata
    """
    try:
        if not FLASK_AVAILABLE:
            logger.error("Flask not available - cannot access Supabase")
            return {"error": "Service not available", "content": None}
        
        # Get admin client for database access
        try:
            admin_supabase = get_admin_client()
        except Exception as e:
            logger.error(f"Failed to get admin Supabase client: {e}")
            return {"error": "Database connection failed", "content": None}
        
        # Query user documents for job description
        try:
            response = admin_supabase.table("user_documents") \
                .select("id, document_name, document_type, document_url, created_at, document_comments") \
                .eq("uid", user_id) \
                .eq("document_type", document_type) \
                .order("created_at", desc=True) \
                .limit(1) \
                .execute()
            
            if not response.data:
                logger.info(f"No {document_type} found for user {user_id}")
                return {
                    "error": f"No {document_type} found",
                    "content": None,
                    "has_job_description": False
                }
            
            job_doc = response.data[0]
            logger.info(f"Found {document_type} for user {user_id}: {job_doc['document_name']}")
            
        except Exception as e:
            logger.error(f"Database query failed: {e}")
            return {"error": "Failed to query documents", "content": None}
        
        # Get job description content from URL
        job_content = None
        document_url = job_doc.get('document_url')
        
        if document_url:
            try:
                job_content = await get_document_content(document_url)
                if job_content:
                    logger.info(f"Successfully extracted content from {document_type}")
                else:
                    logger.warning(f"Failed to extract content from {document_type}")
            except Exception as e:
                logger.error(f"Error extracting job description content: {e}")
        
        return {
            "success": True,
            "content": job_content,
            "metadata": {
                "document_name": job_doc.get('document_name'),
                "document_type": job_doc.get('document_type'),
                "created_at": job_doc.get('created_at'),
                "comments": job_doc.get('document_comments'),
                "has_content": bool(job_content)
            },
            "has_job_description": True
        }
        
    except Exception as e:
        logger.error(f"Unexpected error in load_job_description: {e}")
        return {
            "error": f"Unexpected error: {str(e)}",
            "content": None,
            "has_job_description": False
        }

@function_tool()
async def prepare_interview_context(
    context: RunContext,
    user_id: str,
    session_id: str
) -> Dict[str, Any]:
    """Prepare complete interview context by loading resume and job description.
    
    Args:
        user_id: The ID of the user
        session_id: The interview session ID
    
    Returns:
        A dictionary containing complete interview context
    """
    try:
        logger.info(f"Preparing interview context for user {user_id}, session {session_id}")
        
        # Load resume
        resume_result = await load_user_resume(context, user_id, "resume")
        
        # Load job description
        job_result = await load_job_description(context, user_id, "job_description")
        
        # Prepare enhanced context
        interview_context = {
            "session_id": session_id,
            "user_id": user_id,
            "resume_text": resume_result.get("content"),
            "job_description": job_result.get("content"),
            "has_resume": resume_result.get("has_resume", False),
            "has_job_description": job_result.get("has_job_description", False),
            "resume_metadata": resume_result.get("metadata"),
            "job_metadata": job_result.get("metadata"),
            "preparation_timestamp": context.function_call.id if hasattr(context, 'function_call') else None
        }
        
        # Generate context summary for the agent
        context_summary = []
        
        if interview_context["has_resume"]:
            resume_name = resume_result.get("metadata", {}).get("document_name", "Resume")
            context_summary.append(f"✓ Resume loaded: {resume_name}")
            
        if interview_context["has_job_description"]:
            job_name = job_result.get("metadata", {}).get("document_name", "Job Description")
            context_summary.append(f"✓ Job description loaded: {job_name}")
        
        if not context_summary:
            context_summary.append("ℹ No resume or job description available - conducting general interview")
        
        logger.info(f"Interview context prepared: {'; '.join(context_summary)}")
        
        return {
            "success": True,
            "context": interview_context,
            "summary": "; ".join(context_summary),
            "ready_for_interview": True
        }
        
    except Exception as e:
        logger.error(f"Error preparing interview context: {e}")
        return {
            "success": False,
            "error": f"Failed to prepare context: {str(e)}",
            "context": {"session_id": session_id, "user_id": user_id},
            "ready_for_interview": False
        }

# Helper function for non-tool usage (direct import)
async def get_user_interview_context(user_id: str, session_id: str = None) -> Dict[str, Any]:
    """
    Direct function to get user interview context without using LiveKit tools.
    This can be called directly from agent.py during initialization.
    """
    try:
        if not FLASK_AVAILABLE:
            logger.error("Flask not available - cannot access Supabase")
            return {"error": "Service not available"}
        
        admin_supabase = get_admin_client()
        
        # Get resume
        resume_response = admin_supabase.table("user_documents") \
            .select("*") \
            .eq("uid", user_id) \
            .eq("document_type", "resume") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        
        # Get job description
        job_response = admin_supabase.table("user_documents") \
            .select("*") \
            .eq("uid", user_id) \
            .eq("document_type", "job_description") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        
        context = {
            "user_id": user_id,
            "session_id": session_id,
            "resume_text": None,
            "job_description": None,
            "has_resume": False,
            "has_job_description": False
        }
        
        # Process resume
        if resume_response.data:
            resume_doc = resume_response.data[0]
            try:
                resume_content = await get_document_content(resume_doc.get('document_url'))
                if resume_content:
                    context["resume_text"] = resume_content
                    context["has_resume"] = True
                    logger.info(f"Loaded resume for user {user_id}")
            except Exception as e:
                logger.error(f"Error loading resume content: {e}")
        
        # Process job description
        if job_response.data:
            job_doc = job_response.data[0]
            try:
                job_content = await get_document_content(job_doc.get('document_url'))
                if job_content:
                    context["job_description"] = job_content
                    context["has_job_description"] = True
                    logger.info(f"Loaded job description for user {user_id}")
            except Exception as e:
                logger.error(f"Error loading job description content: {e}")
        
        return context
        
    except Exception as e:
        logger.error(f"Error getting user interview context: {e}")
        return {"error": str(e), "user_id": user_id}

# Export tools for easy import
INTERVIEW_TOOLS = [
    load_user_resume,
    load_job_description,
    prepare_interview_context
]
