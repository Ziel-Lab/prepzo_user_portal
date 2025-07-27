"""
LiveKit API helpers for mock interview functionality
"""

import asyncio
import logging
import requests
import PyPDF2
import io
from flask import current_app

try:
    from livekit import api
    from livekit.api import AccessToken, VideoGrants
    LIVEKIT_AVAILABLE = True
except ImportError:
    api = None
    AccessToken = None
    VideoGrants = None
    LIVEKIT_AVAILABLE = False

logger = logging.getLogger(__name__)

def get_livekit_api():
    """Get initialized LiveKit API client"""
    if not LIVEKIT_AVAILABLE:
        return None
    
    livekit_api_key = current_app.config.get('LIVEKIT_API_KEY')
    livekit_api_secret = current_app.config.get('LIVEKIT_API_SECRET')
    livekit_url = current_app.config.get('LIVEKIT_URL')
    
    if not all([livekit_api_key, livekit_api_secret, livekit_url]):
        return None
    
    try:
        return api.LiveKitAPI(
            url=livekit_url,
            api_key=livekit_api_key,
            api_secret=livekit_api_secret
        )
    except Exception as e:
        logger.error(f"Failed to create LiveKit API client: {e}")
        return None

async def create_interview_room(room_name):
    """Create a new LiveKit room for interview"""
    if not LIVEKIT_AVAILABLE:
        logger.error("LiveKit is not available. Please install: pip install livekit")
        return None
        
    try:
        livekit_api_key = current_app.config.get('LIVEKIT_API_KEY')
        livekit_api_secret = current_app.config.get('LIVEKIT_API_SECRET')
        livekit_url = current_app.config.get('LIVEKIT_URL')
        
        if not all([livekit_api_key, livekit_api_secret, livekit_url]):
            logger.error("Missing LiveKit configuration")
            return None
        
        # Get LiveKit API client
        lkapi = get_livekit_api()
        if not lkapi:
            logger.error("LiveKit API client not available")
            return None
        
        # Create room using current API
        room_options = api.CreateRoomRequest(
            name=room_name,
            empty_timeout=10 * 60,  # 10 minutes
            max_participants=2,  # User + AI agent
            metadata=""
        )
        
        room = await lkapi.room.create_room(room_options)
        logger.info(f"Created LiveKit room: {room_name}")
        await lkapi.aclose()
        return room
        
    except Exception as e:
        logger.error(f"Error creating LiveKit room: {str(e)}")
        return None

async def get_room_token(room_name, user_id, participant_name=None):
    """Generate access token for a LiveKit room"""
    if not LIVEKIT_AVAILABLE:
        logger.error("LiveKit is not available. Please install: pip install livekit")
        return None
        
    try:
        livekit_api_key = current_app.config.get('LIVEKIT_API_KEY')
        livekit_api_secret = current_app.config.get('LIVEKIT_API_SECRET')
        
        if not all([livekit_api_key, livekit_api_secret]):
            logger.error("Missing LiveKit API credentials")
            return None
        
        # Create access token
        token = AccessToken(livekit_api_key, livekit_api_secret) \
            .with_identity(participant_name or f"user_{user_id}") \
            .with_name(participant_name or f"User {user_id}") \
            .with_grants(VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )) \
            .to_jwt()
        
        logger.info(f"Generated token for user {user_id} in room {room_name}")
        return token
        
    except Exception as e:
        logger.error(f"Error generating room token: {str(e)}")
        return None

async def delete_room(room_name):
    """Delete a LiveKit room"""
    if not LIVEKIT_AVAILABLE:
        logger.error("LiveKit is not available. Please install: pip install livekit")
        return False
        
    try:
        livekit_api_key = current_app.config.get('LIVEKIT_API_KEY')
        livekit_api_secret = current_app.config.get('LIVEKIT_API_SECRET')
        livekit_url = current_app.config.get('LIVEKIT_URL')
        
        if not all([livekit_api_key, livekit_api_secret, livekit_url]):
            logger.error("Missing LiveKit configuration")
            return False
        
        lkapi = get_livekit_api()
        if not lkapi:
            logger.error("LiveKit API client not available")
            return False
        
        delete_request = api.DeleteRoomRequest(room=room_name)
        await lkapi.room.delete_room(delete_request)
        logger.info(f"Deleted LiveKit room: {room_name}")
        await lkapi.aclose()
        return True
        
    except Exception as e:
        logger.error(f"Error deleting LiveKit room: {str(e)}")
        return False

async def get_room_participants(room_name):
    """Get list of participants in a room"""
    if not LIVEKIT_AVAILABLE:
        logger.error("LiveKit is not available. Please install: pip install livekit")
        return []
        
    try:
        livekit_api_key = current_app.config.get('LIVEKIT_API_KEY')
        livekit_api_secret = current_app.config.get('LIVEKIT_API_SECRET')
        livekit_url = current_app.config.get('LIVEKIT_URL')
        
        if not all([livekit_api_key, livekit_api_secret, livekit_url]):
            logger.error("Missing LiveKit configuration")
            return []
        
        lkapi = get_livekit_api()
        if not lkapi:
            logger.error("LiveKit API client not available")
            return []
        
        list_request = api.ListParticipantsRequest(room=room_name)
        participants = await lkapi.room.list_participants(list_request)
        await lkapi.aclose()
        
        return participants.participants
        
    except Exception as e:
        logger.error(f"Error getting room participants: {str(e)}")
        return []

def get_agent_token(room_name):
    """Generate token for AI agent"""
    if not LIVEKIT_AVAILABLE:
        logger.error("LiveKit is not available. Please install: pip install livekit")
        return None
        
    try:
        livekit_api_key = current_app.config.get('LIVEKIT_API_KEY')
        livekit_api_secret = current_app.config.get('LIVEKIT_API_SECRET')
        
        if not all([livekit_api_key, livekit_api_secret]):
            logger.error("Missing LiveKit API credentials")
            return None
        
        # Create access token for AI agent
        token = AccessToken(livekit_api_key, livekit_api_secret) \
            .with_identity("interview_agent") \
            .with_name("Interview AI") \
            .with_grants(VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
                agent=True,
            )) \
            .to_jwt()
        
        return token
        
    except Exception as e:
        logger.error(f"Error generating agent token: {str(e)}")
        return None

async def get_document_content(document_url):
    """Fetch and extract content from a document URL"""
    try:
        if not document_url:
            logger.warning("Empty document URL provided")
            return None
            
        logger.info(f"Fetching document content from: {document_url[:100]}...")
        
        # Add headers to mimic a browser request
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain,*/*'
        }
        
        response = requests.get(document_url, timeout=30, headers=headers)
        response.raise_for_status()
        
        content_type = response.headers.get('content-type', '').lower()
        file_content = response.content
        
        logger.info(f"Downloaded {len(file_content)} bytes, content-type: {content_type}")
        
        if 'pdf' in content_type or document_url.lower().endswith('.pdf'):
            content = extract_pdf_content(file_content)
            if content:
                logger.info(f"Successfully extracted {len(content)} characters from PDF")
                return content
            else:
                logger.warning("Failed to extract content from PDF")
                return None
                
        elif any(term in content_type for term in ['text', 'plain']) or document_url.lower().endswith('.txt'):
            content = file_content.decode('utf-8', errors='ignore')
            logger.info(f"Successfully extracted {len(content)} characters from text file")
            return content
            
        elif 'msword' in content_type or document_url.lower().endswith(('.doc', '.docx')):
            logger.warning("Word document processing not yet implemented")
            return None
            
        else:
            logger.warning(f"Unsupported content type: {content_type}")
            # Try to decode as text anyway
            try:
                content = file_content.decode('utf-8', errors='ignore')
                if len(content.strip()) > 0:
                    logger.info(f"Successfully decoded unknown format as text: {len(content)} characters")
                    return content
            except Exception:
                pass
            return None
            
    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching document from: {document_url}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error fetching document: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error fetching document content: {str(e)}")
        return None

def extract_pdf_content(pdf_bytes):
    """Extract text from PDF bytes"""
    try:
        if not pdf_bytes:
            logger.warning("Empty PDF bytes provided")
            return None
            
        pdf_file = io.BytesIO(pdf_bytes)
        reader = PyPDF2.PdfReader(pdf_file)
        
        if len(reader.pages) == 0:
            logger.warning("PDF has no pages")
            return None
        
        text = ""
        pages_processed = 0
        
        for page_num, page in enumerate(reader.pages):
            try:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
                    pages_processed += 1
                else:
                    logger.warning(f"No text extracted from page {page_num + 1}")
            except Exception as e:
                logger.warning(f"Error extracting text from page {page_num + 1}: {e}")
                continue
        
        extracted_text = text.strip()
        
        if extracted_text:
            logger.info(f"Successfully extracted text from {pages_processed}/{len(reader.pages)} pages, "
                       f"{len(extracted_text)} characters total")
            return extracted_text
        else:
            logger.warning("No text could be extracted from PDF")
            return None
        
    except Exception as e:
        logger.error(f"Error extracting PDF: {str(e)}")
        return None 