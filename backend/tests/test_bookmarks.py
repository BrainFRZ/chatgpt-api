"""
Comprehensive tests for the message bookmark feature.

Covers:
1. SyncEventType.BOOKMARK_UPDATED enum and serialization
2. POST /api/set-bookmark endpoint — add, update, remove bookmarks
3. Persistence — bookmarks saved to and loaded from disk
4. Error handling — 404 on missing chat, missing message
5. Broadcast — sync event fires with correct chat key and data
6. Project chats — bookmarks work for project-scoped chats
7. Edge cases — whitespace trimming, empty string removal, idempotent removal
8. GET /api/get-chat — bookmarks present in message data returned to client
"""

import os
import sys
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

# Add backend directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

import main
from sync_manager import SyncEventType, SyncEvent, SyncManager


# ============================================================
# Test Setup and Fixtures
# ============================================================

@pytest.fixture
def temp_data_dir(monkeypatch):
    """Create a temporary data directory for tests and patch main.DATA_DIR."""
    temp_dir = tempfile.mkdtemp()
    monkeypatch.setattr(main, 'DATA_DIR', Path(temp_dir))
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def client(temp_data_dir):
    """Create test client with isolated data directory."""
    return TestClient(main.app)


@pytest.fixture
def test_user(temp_data_dir):
    """Create a test user with directories and a sample chat."""
    username = "testuser"
    user_dir = Path(temp_data_dir) / username
    user_dir.mkdir(parents=True, exist_ok=True)

    chat_data = {
        "messages": [
            {"id": "sys1", "role": "system", "content": "You are a helpful assistant."},
            {"id": "msg1", "role": "user", "content": "Hello", "parent_id": "sys1"},
            {"id": "msg2", "role": "assistant", "content": "Hi there!", "parent_id": "msg1"},
            {"id": "msg3", "role": "user", "content": "How are you?", "parent_id": "msg2"},
            {"id": "msg4", "role": "assistant", "content": "I'm doing great!", "parent_id": "msg3"},
        ],
        "stats": {
            "prompt_count": 2,
            "total_input_tokens": 80,
            "total_cache_read_tokens": 0,
            "total_output_tokens": 40,
            "total_reasoning_tokens": 0,
            "total_cost": 0.005,
            "last_accessed": "2025-01-01T00:00:00"
        },
        "current_leaf_id": "msg4",
        "model": "gpt-5.2"
    }
    chat_path = user_dir / "chat_test_chat.json"
    with open(chat_path, 'w') as f:
        json.dump(chat_data, f)

    return username


@pytest.fixture
def test_project_chat(temp_data_dir, test_user):
    """Create a project chat for the test user."""
    project = "test_project"
    project_dir = Path(temp_data_dir) / test_user / "projects" / project
    project_dir.mkdir(parents=True, exist_ok=True)

    chat_data = {
        "messages": [
            {"id": "psys1", "role": "system", "content": "Project assistant."},
            {"id": "pmsg1", "role": "user", "content": "Start scene", "parent_id": "psys1"},
            {"id": "pmsg2", "role": "assistant", "content": "The scene begins...", "parent_id": "pmsg1"},
            {"id": "pmsg3", "role": "user", "content": "Continue", "parent_id": "pmsg2"},
            {"id": "pmsg4", "role": "assistant", "content": "The story continues...", "parent_id": "pmsg3"},
        ],
        "stats": {
            "prompt_count": 2,
            "total_input_tokens": 60,
            "total_cache_read_tokens": 0,
            "total_output_tokens": 30,
            "total_reasoning_tokens": 0,
            "total_cost": 0.003,
            "last_accessed": "2025-01-01T00:00:00"
        },
        "current_leaf_id": "pmsg4",
        "model": "gpt-5.2"
    }
    chat_path = project_dir / "chat_proj_chat.json"
    with open(chat_path, 'w') as f:
        json.dump(chat_data, f)

    return project


# ============================================================
# 1. SyncEventType.BOOKMARK_UPDATED enum
# ============================================================

class TestBookmarkUpdatedEnum:
    """Tests for the BOOKMARK_UPDATED sync event type."""

    def test_enum_exists(self):
        """BOOKMARK_UPDATED should be a valid SyncEventType."""
        assert hasattr(SyncEventType, 'BOOKMARK_UPDATED')
        assert SyncEventType.BOOKMARK_UPDATED.value == "bookmark_updated"

    def test_serializes_with_bookmark_text(self):
        """SyncEvent with bookmark data should serialize correctly."""
        event = SyncEvent(
            type=SyncEventType.BOOKMARK_UPDATED,
            data={"message_id": "msg1", "bookmark": "Scene start"}
        )
        parsed = json.loads(event.to_json())
        assert parsed["type"] == "bookmark_updated"
        assert parsed["data"]["message_id"] == "msg1"
        assert parsed["data"]["bookmark"] == "Scene start"

    def test_serializes_with_empty_bookmark(self):
        """SyncEvent with empty bookmark (removal) should serialize correctly."""
        event = SyncEvent(
            type=SyncEventType.BOOKMARK_UPDATED,
            data={"message_id": "msg1", "bookmark": ""}
        )
        parsed = json.loads(event.to_json())
        assert parsed["type"] == "bookmark_updated"
        assert parsed["data"]["bookmark"] == ""

    def test_enum_is_distinct(self):
        """BOOKMARK_UPDATED should have a unique value among all event types."""
        values = [e.value for e in SyncEventType]
        assert values.count("bookmark_updated") == 1


# ============================================================
# 2. POST /api/set-bookmark — Add bookmark
# ============================================================

class TestSetBookmarkAdd:
    """Tests for adding bookmarks via POST /api/set-bookmark."""

    def test_add_bookmark_to_user_message(self, client, test_user, temp_data_dir):
        """Adding a bookmark to a user message should persist it."""
        response = client.post("/api/set-bookmark", json={
            "username": test_user,
            "chat_name": "test_chat",
            "message_id": "msg1",
            "bookmark": "Scene start"
        })
        assert response.status_code == 200
        assert response.json()["success"] is True

        # Verify persisted to disk
        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        msg = next(m for m in chat_data["messages"] if m["id"] == "msg1")
        assert msg["bookmark"] == "Scene start"

    def test_add_bookmark_to_assistant_message(self, client, test_user, temp_data_dir):
        """Adding a bookmark to an assistant message should also work."""
        response = client.post("/api/set-bookmark", json={
            "username": test_user,
            "chat_name": "test_chat",
            "message_id": "msg2",
            "bookmark": "Key moment"
        })
        assert response.status_code == 200

        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        msg = next(m for m in chat_data["messages"] if m["id"] == "msg2")
        assert msg["bookmark"] == "Key moment"

    def test_add_bookmark_to_system_message(self, client, test_user, temp_data_dir):
        """Bookmarking the system message should also work (no restriction)."""
        response = client.post("/api/set-bookmark", json={
            "username": test_user,
            "chat_name": "test_chat",
            "message_id": "sys1",
            "bookmark": "System"
        })
        assert response.status_code == 200

        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        msg = next(m for m in chat_data["messages"] if m["id"] == "sys1")
        assert msg["bookmark"] == "System"

    def test_response_format(self, client, test_user):
        """Response should be JSON with success: true."""
        response = client.post("/api/set-bookmark", json={
            "username": test_user,
            "chat_name": "test_chat",
            "message_id": "msg1",
            "bookmark": "test"
        })
        data = response.json()
        assert "success" in data
        assert data["success"] is True


# ============================================================
# 3. POST /api/set-bookmark — Update bookmark
# ============================================================

class TestSetBookmarkUpdate:
    """Tests for updating existing bookmarks."""

    def test_update_existing_bookmark(self, client, test_user, temp_data_dir):
        """Updating an existing bookmark should overwrite the old value."""
        # Add initial
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": "Original"
        })
        # Update
        response = client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": "Updated"
        })
        assert response.status_code == 200

        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        msg = next(m for m in chat_data["messages"] if m["id"] == "msg1")
        assert msg["bookmark"] == "Updated"

    def test_multiple_messages_bookmarked(self, client, test_user, temp_data_dir):
        """Multiple messages can have bookmarks simultaneously."""
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": "First"
        })
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg3", "bookmark": "Second"
        })

        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)

        bookmarked = [m for m in chat_data["messages"] if "bookmark" in m]
        assert len(bookmarked) == 2
        assert next(m for m in bookmarked if m["id"] == "msg1")["bookmark"] == "First"
        assert next(m for m in bookmarked if m["id"] == "msg3")["bookmark"] == "Second"


# ============================================================
# 4. POST /api/set-bookmark — Remove bookmark
# ============================================================

class TestSetBookmarkRemove:
    """Tests for removing bookmarks (empty string)."""

    def test_remove_bookmark_with_empty_string(self, client, test_user, temp_data_dir):
        """Sending empty bookmark string should remove the bookmark field."""
        # Add first
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": "To be removed"
        })
        # Remove
        response = client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": ""
        })
        assert response.status_code == 200

        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        msg = next(m for m in chat_data["messages"] if m["id"] == "msg1")
        assert "bookmark" not in msg

    def test_remove_bookmark_with_whitespace_only(self, client, test_user, temp_data_dir):
        """Sending whitespace-only bookmark should also remove it."""
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": "Existing"
        })
        response = client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": "   "
        })
        assert response.status_code == 200

        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        msg = next(m for m in chat_data["messages"] if m["id"] == "msg1")
        assert "bookmark" not in msg

    def test_remove_nonexistent_bookmark_is_noop(self, client, test_user, temp_data_dir):
        """Removing a bookmark that doesn't exist should succeed silently."""
        response = client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": ""
        })
        assert response.status_code == 200

        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        msg = next(m for m in chat_data["messages"] if m["id"] == "msg1")
        assert "bookmark" not in msg


# ============================================================
# 5. Whitespace trimming
# ============================================================

class TestBookmarkWhitespaceTrimming:
    """Tests that bookmark text is trimmed on save."""

    def test_leading_trailing_whitespace_trimmed(self, client, test_user, temp_data_dir):
        """Bookmark text should be trimmed of leading/trailing whitespace."""
        response = client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": "  Scene start  "
        })
        assert response.status_code == 200

        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        msg = next(m for m in chat_data["messages"] if m["id"] == "msg1")
        assert msg["bookmark"] == "Scene start"

    def test_internal_whitespace_preserved(self, client, test_user, temp_data_dir):
        """Internal whitespace in bookmark text should be preserved."""
        response = client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": "Scene  start\nAct 2"
        })
        assert response.status_code == 200

        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        msg = next(m for m in chat_data["messages"] if m["id"] == "msg1")
        assert msg["bookmark"] == "Scene  start\nAct 2"


# ============================================================
# 6. Error handling — 404s
# ============================================================

class TestSetBookmarkErrors:
    """Tests for error cases."""

    def test_nonexistent_chat_returns_404(self, client, test_user):
        """Setting bookmark on a non-existent chat should return 404."""
        response = client.post("/api/set-bookmark", json={
            "username": test_user,
            "chat_name": "nonexistent_chat",
            "message_id": "msg1",
            "bookmark": "test"
        })
        assert response.status_code == 404
        assert "Chat not found" in response.json()["detail"]

    def test_nonexistent_user_returns_404(self, client):
        """Setting bookmark for a non-existent user should return 404."""
        response = client.post("/api/set-bookmark", json={
            "username": "nobody",
            "chat_name": "test_chat",
            "message_id": "msg1",
            "bookmark": "test"
        })
        assert response.status_code == 404

    def test_nonexistent_message_returns_404(self, client, test_user):
        """Setting bookmark on a non-existent message ID should return 404."""
        response = client.post("/api/set-bookmark", json={
            "username": test_user,
            "chat_name": "test_chat",
            "message_id": "nonexistent_msg",
            "bookmark": "test"
        })
        assert response.status_code == 404
        assert "Message not found" in response.json()["detail"]

    def test_missing_required_fields_returns_422(self, client, test_user):
        """Missing required fields should return 422 validation error."""
        # Missing message_id
        response = client.post("/api/set-bookmark", json={
            "username": test_user,
            "chat_name": "test_chat",
            "bookmark": "test"
        })
        assert response.status_code == 422

        # Missing bookmark
        response = client.post("/api/set-bookmark", json={
            "username": test_user,
            "chat_name": "test_chat",
            "message_id": "msg1"
        })
        assert response.status_code == 422


# ============================================================
# 7. Project chat bookmarks
# ============================================================

class TestProjectChatBookmarks:
    """Tests for bookmarks in project-scoped chats."""

    def test_add_bookmark_in_project_chat(self, client, test_user, test_project_chat, temp_data_dir):
        """Adding a bookmark in a project chat should persist."""
        response = client.post("/api/set-bookmark", json={
            "username": test_user,
            "chat_name": "proj_chat",
            "project": test_project_chat,
            "message_id": "pmsg1",
            "bookmark": "Scene start"
        })
        assert response.status_code == 200

        chat_path = Path(temp_data_dir) / test_user / "projects" / test_project_chat / "chat_proj_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        msg = next(m for m in chat_data["messages"] if m["id"] == "pmsg1")
        assert msg["bookmark"] == "Scene start"

    def test_remove_bookmark_in_project_chat(self, client, test_user, test_project_chat, temp_data_dir):
        """Removing a bookmark from a project chat should work."""
        # Add
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "proj_chat",
            "project": test_project_chat,
            "message_id": "pmsg2", "bookmark": "To remove"
        })
        # Remove
        response = client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "proj_chat",
            "project": test_project_chat,
            "message_id": "pmsg2", "bookmark": ""
        })
        assert response.status_code == 200

        chat_path = Path(temp_data_dir) / test_user / "projects" / test_project_chat / "chat_proj_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        msg = next(m for m in chat_data["messages"] if m["id"] == "pmsg2")
        assert "bookmark" not in msg

    def test_nonexistent_project_chat_returns_404(self, client, test_user, test_project_chat):
        """Setting bookmark on a non-existent project chat should return 404."""
        response = client.post("/api/set-bookmark", json={
            "username": test_user,
            "chat_name": "nonexistent_chat",
            "project": test_project_chat,
            "message_id": "pmsg1",
            "bookmark": "test"
        })
        assert response.status_code == 404


# ============================================================
# 8. Broadcast tests
# ============================================================

class TestBookmarkBroadcast:
    """Tests that set-bookmark broadcasts BOOKMARK_UPDATED to other sessions."""

    def test_broadcasts_on_add(self, client, test_user, temp_data_dir):
        """Adding a bookmark should broadcast BOOKMARK_UPDATED."""
        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({
                "chat_key": chat_key,
                "type": event.type.value,
                "data": event.data
            })
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            response = client.post("/api/set-bookmark", json={
                "username": test_user, "chat_name": "test_chat",
                "message_id": "msg1", "bookmark": "Important"
            })
            assert response.status_code == 200

        bookmark_broadcasts = [c for c in broadcast_calls if c["type"] == "bookmark_updated"]
        assert len(bookmark_broadcasts) == 1
        assert bookmark_broadcasts[0]["data"]["message_id"] == "msg1"
        assert bookmark_broadcasts[0]["data"]["bookmark"] == "Important"

    def test_broadcasts_on_remove(self, client, test_user, temp_data_dir):
        """Removing a bookmark should broadcast BOOKMARK_UPDATED with empty string."""
        # Add first
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": "Existing"
        })

        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({
                "type": event.type.value,
                "data": event.data
            })
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            client.post("/api/set-bookmark", json={
                "username": test_user, "chat_name": "test_chat",
                "message_id": "msg1", "bookmark": ""
            })

        bookmark_broadcasts = [c for c in broadcast_calls if c["type"] == "bookmark_updated"]
        assert len(bookmark_broadcasts) == 1
        assert bookmark_broadcasts[0]["data"]["message_id"] == "msg1"
        assert bookmark_broadcasts[0]["data"]["bookmark"] == ""

    def test_broadcast_chat_key_no_project(self, client, test_user, temp_data_dir):
        """Broadcast chat key should be correct for non-project chat."""
        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({"chat_key": chat_key, "type": event.type.value})
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            client.post("/api/set-bookmark", json={
                "username": test_user, "chat_name": "test_chat",
                "message_id": "msg1", "bookmark": "test"
            })

        bookmark_broadcasts = [c for c in broadcast_calls if c["type"] == "bookmark_updated"]
        assert len(bookmark_broadcasts) == 1
        assert bookmark_broadcasts[0]["chat_key"] == f"{test_user}::test_chat"

    def test_broadcast_chat_key_with_project(self, client, test_user, test_project_chat, temp_data_dir):
        """Broadcast chat key should include project for project chats."""
        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({"chat_key": chat_key, "type": event.type.value})
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            client.post("/api/set-bookmark", json={
                "username": test_user, "chat_name": "proj_chat",
                "project": test_project_chat,
                "message_id": "pmsg1", "bookmark": "test"
            })

        bookmark_broadcasts = [c for c in broadcast_calls if c["type"] == "bookmark_updated"]
        assert len(bookmark_broadcasts) == 1
        assert bookmark_broadcasts[0]["chat_key"] == f"{test_user}:{test_project_chat}:proj_chat"

    def test_no_broadcast_on_404_chat(self, client, test_user, temp_data_dir):
        """No broadcast should happen if the chat doesn't exist."""
        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({"type": event.type.value})
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            response = client.post("/api/set-bookmark", json={
                "username": test_user, "chat_name": "nonexistent",
                "message_id": "msg1", "bookmark": "test"
            })
            assert response.status_code == 404

        assert len(broadcast_calls) == 0

    def test_no_broadcast_on_404_message(self, client, test_user, temp_data_dir):
        """No broadcast should happen if the message doesn't exist."""
        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({"type": event.type.value})
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            response = client.post("/api/set-bookmark", json={
                "username": test_user, "chat_name": "test_chat",
                "message_id": "nonexistent_msg", "bookmark": "test"
            })
            assert response.status_code == 404

        assert len(broadcast_calls) == 0

    def test_broadcast_data_trimmed(self, client, test_user, temp_data_dir):
        """Broadcast bookmark data should be trimmed."""
        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({"type": event.type.value, "data": event.data})
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            client.post("/api/set-bookmark", json={
                "username": test_user, "chat_name": "test_chat",
                "message_id": "msg1", "bookmark": "  Padded  "
            })

        bookmark_broadcasts = [c for c in broadcast_calls if c["type"] == "bookmark_updated"]
        assert len(bookmark_broadcasts) == 1
        assert bookmark_broadcasts[0]["data"]["bookmark"] == "Padded"


# ============================================================
# 9. GET /api/get-chat returns bookmarks
# ============================================================

class TestGetChatReturnsBookmarks:
    """Tests that GET /api/get-chat includes bookmark fields on messages."""

    def test_bookmark_in_get_chat_response(self, client, test_user, temp_data_dir):
        """After adding a bookmark, GET should return it on the message."""
        # Add bookmark
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": "Chapter 1"
        })

        response = client.get(f"/api/chat/{test_user}/test_chat")
        assert response.status_code == 200
        data = response.json()

        # Find the bookmarked message
        messages = data["messages"]
        msg1 = next(m for m in messages if m.get("id") == "msg1")
        assert msg1["bookmark"] == "Chapter 1"

    def test_no_bookmark_field_when_absent(self, client, test_user):
        """Messages without bookmarks should not have the bookmark field."""
        response = client.get(f"/api/chat/{test_user}/test_chat")
        assert response.status_code == 200
        data = response.json()

        for msg in data["messages"]:
            assert "bookmark" not in msg or msg.get("bookmark") is None or msg.get("bookmark") == ""

    def test_removed_bookmark_not_in_response(self, client, test_user, temp_data_dir):
        """After removing a bookmark, GET should return null/None for the field."""
        # Add then remove
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": "Temp"
        })
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": ""
        })

        response = client.get(f"/api/chat/{test_user}/test_chat")
        data = response.json()
        msg1 = next(m for m in data["messages"] if m.get("id") == "msg1")
        # Pydantic serializes Optional fields as None; the bookmark key removed from raw JSON
        assert msg1.get("bookmark") is None


# ============================================================
# 10. Username case normalization
# ============================================================

class TestBookmarkUsernameCaseNormalization:
    """Tests that username is normalized to lowercase."""

    def test_uppercase_username_works(self, client, test_user, temp_data_dir):
        """Username should be case-insensitive (lowered internally)."""
        response = client.post("/api/set-bookmark", json={
            "username": "TestUser",  # Mixed case
            "chat_name": "test_chat",
            "message_id": "msg1",
            "bookmark": "Case test"
        })
        assert response.status_code == 200

        # Should persist to the lowercase user directory
        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        msg = next(m for m in chat_data["messages"] if m["id"] == "msg1")
        assert msg["bookmark"] == "Case test"


# ============================================================
# 11. Other messages remain unaffected
# ============================================================

class TestBookmarkIsolation:
    """Tests that bookmarking one message doesn't affect others."""

    def test_other_messages_unaffected(self, client, test_user, temp_data_dir):
        """Adding/removing a bookmark should not modify other messages."""
        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"

        # Read original state
        with open(chat_path) as f:
            original = json.load(f)
        original_msg2 = next(m for m in original["messages"] if m["id"] == "msg2")
        original_msg3 = next(m for m in original["messages"] if m["id"] == "msg3")

        # Add bookmark to msg1
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": "Bookmark here"
        })

        # Verify other messages unchanged
        with open(chat_path) as f:
            after = json.load(f)
        after_msg2 = next(m for m in after["messages"] if m["id"] == "msg2")
        after_msg3 = next(m for m in after["messages"] if m["id"] == "msg3")

        assert after_msg2 == original_msg2
        assert after_msg3 == original_msg3

    def test_remove_doesnt_affect_other_bookmarks(self, client, test_user, temp_data_dir):
        """Removing one bookmark should not affect another."""
        # Add bookmarks to two messages
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": "First"
        })
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg3", "bookmark": "Second"
        })

        # Remove only msg1's bookmark
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": ""
        })

        # Verify msg3's bookmark is still there
        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        msg1 = next(m for m in chat_data["messages"] if m["id"] == "msg1")
        msg3 = next(m for m in chat_data["messages"] if m["id"] == "msg3")
        assert "bookmark" not in msg1
        assert msg3["bookmark"] == "Second"


# ============================================================
# 12. Searchability in raw JSON
# ============================================================

class TestBookmarkSearchable:
    """Tests that bookmarks are searchable in raw chat JSON (Ctrl+F)."""

    def test_bookmark_text_in_raw_json(self, client, test_user, temp_data_dir):
        """Bookmark text should be plaintext-searchable in the JSON file."""
        unique_phrase = "UNIQUE_SCENE_MARKER_12345"
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": unique_phrase
        })

        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        raw_content = chat_path.read_text()
        assert unique_phrase in raw_content

    def test_removed_bookmark_not_in_raw_json(self, client, test_user, temp_data_dir):
        """After removal, bookmark text should not appear in raw JSON."""
        unique_phrase = "REMOVED_MARKER_67890"
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": unique_phrase
        })
        client.post("/api/set-bookmark", json={
            "username": test_user, "chat_name": "test_chat",
            "message_id": "msg1", "bookmark": ""
        })

        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        raw_content = chat_path.read_text()
        assert unique_phrase not in raw_content
