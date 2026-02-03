"""
Real-time sync manager for multi-instance chat synchronization.

Handles WebSocket connection tracking and event broadcasting
to enable multiple browser instances to stay in sync.
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional
import asyncio
import json
import uuid
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class SyncEventType(Enum):
    """Event types for real-time chat synchronization."""
    USER_MESSAGE_ADDED = "user_message_added"
    STREAM_START = "stream_start"
    STREAM_CONTENT = "stream_content"
    STREAM_THINKING = "stream_thinking"
    STREAM_DONE = "stream_done"
    STREAM_ERROR = "stream_error"
    MESSAGE_EDITED = "message_edited"
    BRANCH_SWITCHED = "branch_switched"
    CLIENT_JOINED = "client_joined"
    CLIENT_LEFT = "client_left"
    # User-level events (chat list changes)
    CHAT_CREATED = "chat_created"
    CHAT_DELETED = "chat_deleted"


@dataclass
class SyncEvent:
    """A sync event to be broadcast to connected clients."""
    type: SyncEventType
    data: dict

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type.value,
            "data": self.data
        })


@dataclass
class Connection:
    """Represents a WebSocket connection."""
    id: str
    websocket: WebSocket


class SyncManager:
    """
    Manages WebSocket connections and broadcasts sync events.

    Thread-safe for concurrent connection/disconnection and broadcasting.
    """

    def __init__(self):
        # {chat_key: {connection_id: Connection}}
        self._connections: dict[str, dict[str, Connection]] = {}
        # {username: {connection_id: Connection}} - for user-level events
        self._user_connections: dict[str, dict[str, Connection]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def make_chat_key(username: str, project: Optional[str], chat_name: str) -> str:
        """Generate a unique key for a chat."""
        if project:
            return f"{username}:{project}:{chat_name}"
        return f"{username}::{chat_name}"

    async def register_connection(self, chat_key: str, username: str, websocket: WebSocket) -> str:
        """
        Register a new WebSocket connection for a chat and user.

        Returns the connection ID.
        """
        connection_id = str(uuid.uuid4())
        connection = Connection(id=connection_id, websocket=websocket)

        async with self._lock:
            # Register at chat level
            if chat_key not in self._connections:
                self._connections[chat_key] = {}
            self._connections[chat_key][connection_id] = connection
            count = len(self._connections[chat_key])

            # Register at user level (for user-wide events like chat created/deleted)
            if username not in self._user_connections:
                self._user_connections[username] = {}
            self._user_connections[username][connection_id] = connection

        logger.info(f"Registered connection {connection_id} for chat {chat_key}, total: {count}")
        return connection_id

    async def unregister_connection(self, chat_key: str, username: str, connection_id: str) -> int:
        """
        Unregister a WebSocket connection from both chat and user levels.

        Returns the remaining connection count for the chat.
        """
        async with self._lock:
            # Unregister from chat level
            if chat_key in self._connections:
                self._connections[chat_key].pop(connection_id, None)
                count = len(self._connections[chat_key])
                if count == 0:
                    del self._connections[chat_key]
                    logger.info(f"Removed empty chat_key {chat_key}")
                else:
                    logger.info(f"Unregistered connection {connection_id} from {chat_key}, remaining: {count}")
            else:
                count = 0

            # Unregister from user level
            if username in self._user_connections:
                self._user_connections[username].pop(connection_id, None)
                if len(self._user_connections[username]) == 0:
                    del self._user_connections[username]

            return count

    async def get_connection_count(self, chat_key: str) -> int:
        """Get the number of active connections for a chat."""
        async with self._lock:
            if chat_key in self._connections:
                return len(self._connections[chat_key])
            return 0

    async def broadcast_to_chat(
        self,
        chat_key: str,
        event: SyncEvent,
        exclude_connection_id: Optional[str] = None
    ) -> int:
        """
        Broadcast an event to all connections viewing a chat.

        Args:
            chat_key: The chat to broadcast to
            event: The event to broadcast
            exclude_connection_id: Optional connection ID to exclude (e.g., the sender)

        Returns:
            Number of clients the event was sent to
        """
        async with self._lock:
            if chat_key not in self._connections:
                return 0
            connections = list(self._connections[chat_key].values())

        if not connections:
            return 0

        message = event.to_json()
        sent_count = 0
        failed_connections = []

        for conn in connections:
            if exclude_connection_id and conn.id == exclude_connection_id:
                continue
            try:
                await conn.websocket.send_text(message)
                sent_count += 1
            except Exception as e:
                logger.warning(f"Failed to send to connection {conn.id}: {e}")
                failed_connections.append(conn.id)

        # Clean up failed connections
        if failed_connections:
            async with self._lock:
                if chat_key in self._connections:
                    for conn_id in failed_connections:
                        self._connections[chat_key].pop(conn_id, None)

        return sent_count

    async def broadcast_to_user(
        self,
        username: str,
        event: SyncEvent,
        exclude_connection_id: Optional[str] = None
    ) -> int:
        """
        Broadcast an event to all connections for a user (across all their chats).

        Args:
            username: The user to broadcast to
            event: The event to broadcast
            exclude_connection_id: Optional connection ID to exclude

        Returns:
            Number of clients the event was sent to
        """
        async with self._lock:
            if username not in self._user_connections:
                return 0
            connections = list(self._user_connections[username].values())

        if not connections:
            return 0

        message = event.to_json()
        sent_count = 0
        failed_connections = []

        for conn in connections:
            if exclude_connection_id and conn.id == exclude_connection_id:
                continue
            try:
                await conn.websocket.send_text(message)
                sent_count += 1
            except Exception as e:
                logger.warning(f"Failed to send to user connection {conn.id}: {e}")
                failed_connections.append(conn.id)

        # Clean up failed connections
        if failed_connections:
            async with self._lock:
                if username in self._user_connections:
                    for conn_id in failed_connections:
                        self._user_connections[username].pop(conn_id, None)

        return sent_count


# Global sync manager instance
sync_manager = SyncManager()
