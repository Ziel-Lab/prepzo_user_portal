# transcription.py - Mock Interview Transcription Module
from flask import current_app
from datetime import datetime, timezone
from app import extensions
from postgrest.exceptions import APIError
import json
from typing import List, Dict, Optional, Any
import uuid

class TranscriptionHandler:
    """Handles transcription processing for mock interview sessions"""
    
    def __init__(self):
        self.supabase = extensions.supabase
    
    def create_session(self, user_id: str, interview_type: str = "general") -> str:
        """Create a new mock interview session"""
        try:
            session_id = str(uuid.uuid4())
            session_data = {
                'id': session_id,
                'user_id': user_id,
                'interview_type': interview_type,
                'status': 'active',
                'started_at': datetime.now(timezone.utc).isoformat(),
                'created_at': datetime.now(timezone.utc).isoformat()
            }
            
            result = self.supabase.table('mock_interview_sessions').insert(session_data).execute()
            current_app.logger.info(f"Created mock interview session {session_id} for user {user_id}")
            return session_id
            
        except Exception as e:
            current_app.logger.error(f"Failed to create mock interview session: {e}")
            raise
    
    def add_transcription(self, session_id: str, speaker: str, text: str, 
                         timestamp: Optional[float] = None, metadata: Optional[Dict] = None) -> str:
        """Add a transcription entry to a session"""
        try:
            transcription_id = str(uuid.uuid4())
            transcription_data = {
                'id': transcription_id,
                'session_id': session_id,
                'speaker': speaker,  # 'user' or 'agent'
                'text': text,
                'timestamp': timestamp or datetime.now(timezone.utc).timestamp(),
                'metadata': json.dumps(metadata) if metadata else None,
                'created_at': datetime.now(timezone.utc).isoformat()
            }
            
            result = self.supabase.table('mock_interview_transcriptions').insert(transcription_data).execute()
            current_app.logger.info(f"Added transcription {transcription_id} for session {session_id}")
            return transcription_id
            
        except Exception as e:
            current_app.logger.error(f"Failed to add transcription: {e}")
            raise
    
    def process_user_audio(self, session_id: str, audio_data: bytes, 
                          audio_format: str = "wav") -> str:
        """Process user audio and return transcription"""
        try:
            # TODO: Integrate with speech-to-text service (OpenAI Whisper, Google Speech-to-Text, etc.)
            # For now, return placeholder
            text = "[User audio transcription placeholder]"
            
            # Add transcription to database
            transcription_id = self.add_transcription(
                session_id=session_id,
                speaker="user",
                text=text,
                metadata={"audio_format": audio_format, "audio_length": len(audio_data)}
            )
            
            return text
            
        except Exception as e:
            current_app.logger.error(f"Failed to process user audio: {e}")
            raise
    
    def process_agent_response(self, session_id: str, response_text: str, 
                              question_type: str = "general") -> str:
        """Process and store agent response"""
        try:
            transcription_id = self.add_transcription(
                session_id=session_id,
                speaker="agent",
                text=response_text,
                metadata={"question_type": question_type}
            )
            
            return transcription_id
            
        except Exception as e:
            current_app.logger.error(f"Failed to process agent response: {e}")
            raise
    
    def end_session(self, session_id: str) -> Dict[str, Any]:
        """End a mock interview session and generate summary"""
        try:
            # Update session status
            self.supabase.table('mock_interview_sessions').update({
                'status': 'completed',
                'ended_at': datetime.now(timezone.utc).isoformat(),
                'updated_at': datetime.now(timezone.utc).isoformat()
            }).eq('id', session_id).execute()
            
            # Generate session summary
            summary = self.generate_session_summary(session_id)
            
            # Store summary
            self.supabase.table('mock_interview_sessions').update({
                'summary': json.dumps(summary)
            }).eq('id', session_id).execute()
            
            current_app.logger.info(f"Ended mock interview session {session_id}")
            return summary
            
        except Exception as e:
            current_app.logger.error(f"Failed to end session: {e}")
            raise
    
    def generate_session_summary(self, session_id: str) -> Dict[str, Any]:
        """Generate summary for a completed session"""
        try:
            # Get all transcriptions for the session
            transcriptions = self.get_session_transcriptions(session_id)
            
            if not transcriptions:
                return {"message": "No transcriptions found", "total_exchanges": 0}
            
            user_messages = [t for t in transcriptions if t['speaker'] == 'user']
            agent_messages = [t for t in transcriptions if t['speaker'] == 'agent']
            
            summary = {
                "session_id": session_id,
                "total_exchanges": len(transcriptions),
                "user_messages": len(user_messages),
                "agent_messages": len(agent_messages),
                "duration_minutes": self._calculate_session_duration(transcriptions),
                "key_topics": self._extract_key_topics(transcriptions),
                "performance_notes": self._generate_performance_notes(user_messages),
                "generated_at": datetime.now(timezone.utc).isoformat()
            }
            
            return summary
            
        except Exception as e:
            current_app.logger.error(f"Failed to generate session summary: {e}")
            return {"error": str(e)}
    
    def get_session_transcriptions(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all transcriptions for a session"""
        try:
            result = self.supabase.table('mock_interview_transcriptions').select('*')\
                .eq('session_id', session_id)\
                .order('timestamp', desc=False)\
                .execute()
            
            return result.data if result.data else []
            
        except Exception as e:
            current_app.logger.error(f"Failed to get session transcriptions: {e}")
            return []
    
    def _calculate_session_duration(self, transcriptions: List[Dict]) -> float:
        """Calculate session duration in minutes"""
        if not transcriptions or len(transcriptions) < 2:
            return 0.0
        
        start_time = min(t['timestamp'] for t in transcriptions)
        end_time = max(t['timestamp'] for t in transcriptions)
        duration_seconds = end_time - start_time
        return round(duration_seconds / 60, 2)
    
    def _extract_key_topics(self, transcriptions: List[Dict]) -> List[str]:
        """Extract key topics from transcriptions"""
        # TODO: Implement NLP-based topic extraction
        # For now, return placeholder topics
        return ["Technical Skills", "Communication", "Problem Solving"]
    
    def _generate_performance_notes(self, user_messages: List[Dict]) -> List[str]:
        """Generate performance feedback notes"""
        # TODO: Implement AI-based performance analysis
        # For now, return basic feedback
        notes = []
        
        if len(user_messages) == 0:
            notes.append("No user responses detected")
        elif len(user_messages) < 3:
            notes.append("Consider providing more detailed responses")
        else:
            notes.append("Good engagement throughout the interview")
        
        return notes

def get_user_transcriptions(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Get transcriptions for a specific user across all sessions"""
    try:
        supabase = extensions.supabase
        
        # Get user's mock interview sessions
        sessions_result = supabase.table('mock_interview_sessions').select('id')\
            .eq('user_id', user_id)\
            .order('created_at', desc=True)\
            .limit(limit)\
            .execute()
        
        if not sessions_result.data:
            return []
        
        session_ids = [session['id'] for session in sessions_result.data]
        
        # Get transcriptions for these sessions
        transcriptions_result = supabase.table('mock_interview_transcriptions').select('*')\
            .in_('session_id', session_ids)\
            .eq('speaker', 'user')\
            .order('created_at', desc=True)\
            .execute()
        
        return transcriptions_result.data if transcriptions_result.data else []
        
    except Exception as e:
        current_app.logger.error(f"Failed to get user transcriptions: {e}")
        return []

def get_session_summary(session_id: str) -> Dict[str, Any]:
    """Get summary for a specific session"""
    try:
        supabase = extensions.supabase
        
        # Get session with summary
        result = supabase.table('mock_interview_sessions').select('*')\
            .eq('id', session_id)\
            .single()\
            .execute()
        
        if not result.data:
            return {"error": "Session not found"}
        
        session_data = result.data
        
        # Parse summary if it exists
        if session_data.get('summary'):
            try:
                summary = json.loads(session_data['summary'])
            except json.JSONDecodeError:
                summary = {"error": "Invalid summary format"}
        else:
            # Generate summary if not exists
            handler = TranscriptionHandler()
            summary = handler.generate_session_summary(session_id)
        
        # Add session metadata
        summary.update({
            "session_info": {
                "id": session_data['id'],
                "user_id": session_data['user_id'],
                "interview_type": session_data.get('interview_type', 'general'),
                "status": session_data['status'],
                "started_at": session_data['started_at'],
                "ended_at": session_data.get('ended_at'),
                "created_at": session_data['created_at']
            }
        })
        
        return summary
        
    except Exception as e:
        current_app.logger.error(f"Failed to get session summary: {e}")
        return {"error": str(e)}

def get_agent_transcriptions(session_id: str) -> List[Dict[str, Any]]:
    """Get agent transcriptions for a specific session"""
    try:
        supabase = extensions.supabase
        
        result = supabase.table('mock_interview_transcriptions').select('*')\
            .eq('session_id', session_id)\
            .eq('speaker', 'agent')\
            .order('timestamp', desc=False)\
            .execute()
        
        return result.data if result.data else []
        
    except Exception as e:
        current_app.logger.error(f"Failed to get agent transcriptions: {e}")
        return []

def get_user_sessions(user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Get mock interview sessions for a user"""
    try:
        supabase = extensions.supabase
        
        result = supabase.table('mock_interview_sessions').select('*')\
            .eq('user_id', user_id)\
            .order('created_at', desc=True)\
            .limit(limit)\
            .execute()
        
        return result.data if result.data else []
        
    except Exception as e:
        current_app.logger.error(f"Failed to get user sessions: {e}")
        return [] 