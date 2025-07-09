"""
LiveKit API helpers for mock interview functionality
"""

import asyncio
import logging
import requests
import PyPDF2
import io
from flask import current_app

# LiveKit imports - handle if not installed
try:
    from livekit import api
    from livekit.api import AccessToken, VideoGrants
    LIVEKIT_AVAILABLE = True
except ImportError:
    api = None
    AccessToken = None
    VideoGrants = None
    LIVEKIT_AVAILABLE = False

# Import RoomService-related functions lazily to avoid import errors
def get_room_service_client():
    """Get RoomServiceClient class dynamically to avoid import errors"""
    if not LIVEKIT_AVAILABLE:
        return None
    try:
        # Try different possible names for the room service
        if hasattr(api, 'RoomServiceClient'):
            return api.RoomServiceClient
        elif hasattr(api, 'RoomService'):
            return api.RoomService
        else:
            return None
    except Exception:
        return None

logger = logging.getLogger(__name__)

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
        
        # Create room using lazy loading
        RoomServiceClient = get_room_service_client()
        if not RoomServiceClient:
            logger.error("RoomServiceClient not available")
            return None
            
        room_service = RoomServiceClient(
            livekit_url,
            livekit_api_key,
            livekit_api_secret,
        )
        
        # Use proper request object or dict based on API
        try:
            from livekit.api import CreateRoomRequest
            room_options = CreateRoomRequest(
                name=room_name,
                empty_timeout=10 * 60,  # 10 minutes
                max_participants=2,  # User + AI agent
                metadata=""
            )
        except ImportError:
            # Fallback to dict if request objects not available
            room_options = {
                'name': room_name,
                'empty_timeout': 10 * 60,
                'max_participants': 2,
                'metadata': ""
            }
        
        room = await room_service.create_room(room_options)
        logger.info(f"Created LiveKit room: {room_name}")
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
        
        RoomServiceClient = get_room_service_client()
        if not RoomServiceClient:
            logger.error("RoomServiceClient not available")
            return False
            
        room_service = RoomServiceClient(
            livekit_url,
            livekit_api_key,
            livekit_api_secret,
        )
        
        # Use proper request object or dict based on API
        try:
            from livekit.api import DeleteRoomRequest
            delete_request = DeleteRoomRequest(room=room_name)
        except ImportError:
            delete_request = {'room': room_name}
            
        await room_service.delete_room(delete_request)
        logger.info(f"Deleted LiveKit room: {room_name}")
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
        
        RoomServiceClient = get_room_service_client()
        if not RoomServiceClient:
            logger.error("RoomServiceClient not available")
            return []
            
        room_service = RoomServiceClient(
            livekit_url,
            livekit_api_key,
            livekit_api_secret,
        )
        
        # Use proper request object or dict based on API
        try:
            from livekit.api import ListParticipantsRequest
            list_request = ListParticipantsRequest(room=room_name)
        except ImportError:
            list_request = {'room': room_name}
            
        participants = await room_service.list_participants(list_request)
        
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
        response = requests.get(document_url, timeout=30)
        response.raise_for_status()
        
        content_type = response.headers.get('content-type', '').lower()
        file_content = response.content
        
        if 'pdf' in content_type:
            return extract_pdf_content(file_content)
        elif 'text' in content_type:
            return file_content.decode('utf-8')
        else:
            logger.warning(f"Unsupported content type: {content_type}")
            return None
            
    except Exception as e:
        logger.error(f"Error fetching document content: {str(e)}")
        return None

def extract_pdf_content(pdf_bytes):
    """Extract text from PDF bytes"""
    try:
        pdf_file = io.BytesIO(pdf_bytes)
        reader = PyPDF2.PdfReader(pdf_file)
        
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        
        return text.strip()
        
    except Exception as e:
        logger.error(f"Error extracting PDF: {str(e)}")
        return None 