from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

class MockInterviewPagination:
    """Production-ready pagination for mock interview sessions"""
    
    DEFAULT_PAGE_SIZE = 10
    MAX_PAGE_SIZE = 50
    
    @staticmethod
    def paginate_user_sessions(admin_client, user_id: str, request_args: Dict) -> Tuple[List[Dict], Dict]:
        """
        Get paginated user sessions - FIXED and production ready
        
        Returns: (sessions_list, pagination_metadata)
        """
        try:
            # Parse page size
            try:
                page_size = int(request_args.get('limit', MockInterviewPagination.DEFAULT_PAGE_SIZE))
                page_size = min(max(1, page_size), MockInterviewPagination.MAX_PAGE_SIZE)
            except (ValueError, TypeError):
                page_size = MockInterviewPagination.DEFAULT_PAGE_SIZE
            
            # Get cursor for pagination
            cursor = request_args.get('cursor')
            
            # Build base query
            query = admin_client.table('mock_interview')\
                .select('*')\
                .eq('user_id', user_id)
            
            # Apply cursor filter if provided (for pagination)
            if cursor:
                try:
                    # For descending order (newest first), get records older than cursor
                    query = query.lt('created_at', cursor)
                except Exception:
                    # Invalid cursor - ignore and start from beginning
                    pass
            
            # Always order by created_at descending (newest first)
            query = query.order('created_at', desc=True)
            
            # Request one extra record to check if there are more pages
            query = query.limit(page_size + 1)
            
            # Execute the query
            result = query.execute()
            raw_sessions = result.data or []
            
            # Determine if there are more pages
            has_more = len(raw_sessions) > page_size
            
            # Get the actual sessions to return (trim the extra one if we got it)
            sessions = raw_sessions[:page_size] if has_more else raw_sessions
            
            # Get next cursor from the last session
            next_cursor = None
            if has_more and sessions:
                next_cursor = sessions[-1]['created_at']
            
            # Build pagination metadata
            pagination_metadata = {
                'page_size': page_size,
                'current_count': len(sessions),
                'has_more': has_more,
                'cursor_column': 'created_at'
            }
            
            if next_cursor:
                pagination_metadata['next_cursor'] = next_cursor
            
            if cursor:
                pagination_metadata['current_cursor'] = cursor
            
            return sessions, pagination_metadata
            
        except Exception as e:
            logger.error(f"Pagination error for user {user_id[:8]}***: {str(e)}")
            # Return safe empty result on error
            return [], {
                'page_size': MockInterviewPagination.DEFAULT_PAGE_SIZE,
                'current_count': 0,
                'has_more': False,
                'cursor_column': 'created_at',
                'error': True
            }