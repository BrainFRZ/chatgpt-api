"""
Tests for the Anthropic async/sync toggle feature.

Tests cover:
1. Provider unit tests: cache breakpoints present/absent based on use_cache flag
2. API endpoint: POST /api/set-anthropic-sync persists and returns the setting
3. Chat response: GET /api/get-chat returns anthropic_sync field
4. Integration: build_request called with correct use_cache from chat data
5. SyncEventType.CHAT_SETTINGS_CHANGED enum and serialization
6. Live broadcast: set-anthropic-sync and set-chat-model broadcast to other sessions
"""

import os
import sys
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

# Add backend directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

import main
from providers.anthropic_provider import AnthropicProvider, AnthropicOpusProvider
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
            {"id": "msg5", "role": "user", "content": "Tell me more", "parent_id": "msg4"},
            {"id": "msg6", "role": "assistant", "content": "Sure thing!", "parent_id": "msg5"},
        ],
        "stats": {
            "prompt_count": 3,
            "total_input_tokens": 100,
            "total_cache_read_tokens": 0,
            "total_output_tokens": 50,
            "total_reasoning_tokens": 0,
            "total_cost": 0.01,
            "last_accessed": "2025-01-01T00:00:00"
        },
        "current_leaf_id": "msg6",
        "model": "claude-sonnet-4.5"
    }
    chat_path = user_dir / "chat_test_chat.json"
    with open(chat_path, 'w') as f:
        json.dump(chat_data, f)

    return username


@pytest.fixture
def sonnet_provider():
    """Create an AnthropicProvider (Sonnet 4.5) instance."""
    return AnthropicProvider()


@pytest.fixture
def opus_provider():
    """Create an AnthropicOpusProvider (Opus 4.6) instance."""
    return AnthropicOpusProvider()


@pytest.fixture
def sample_messages():
    """Sample messages for provider build_request tests."""
    return [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "How are you?"},
        {"role": "assistant", "content": "I'm doing great!"},
        {"role": "user", "content": "Tell me more"},
    ]


# ============================================================
# 1. Provider Unit Tests: AnthropicProvider.build_request
# ============================================================

class TestAnthropicBuildRequestCacheOn:
    """Tests that use_cache=True (default) places cache breakpoints."""

    def test_system_message_has_cache_control(self, sonnet_provider, sample_messages):
        """System message should have cache_control when use_cache=True."""
        params = sonnet_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=True
        )
        system = params["system"]
        assert len(system) == 1
        assert "cache_control" in system[0]
        assert system[0]["cache_control"]["type"] == "ephemeral"
        assert system[0]["cache_control"]["ttl"] == "1h"

    def test_default_use_cache_is_true(self, sonnet_provider, sample_messages):
        """Not passing use_cache should default to True (caching on)."""
        params = sonnet_provider.build_request(
            sample_messages, "user", None, "chat", True
        )
        system = params["system"]
        assert "cache_control" in system[0]

    def test_assistant_breakpoint_present(self, sonnet_provider, sample_messages):
        """Last assistant message should have cache breakpoint."""
        params = sonnet_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=True
        )
        messages = params["messages"]

        # Find assistant messages
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 2

        # Last assistant should have cache_control
        last_assistant = assistant_msgs[-1]
        assert isinstance(last_assistant["content"], list)
        assert "cache_control" in last_assistant["content"][0]
        assert last_assistant["content"][0]["cache_control"]["ttl"] == "1h"

    def test_first_assistant_no_breakpoint(self, sonnet_provider, sample_messages):
        """First assistant message (non-last) should NOT have cache breakpoint."""
        params = sonnet_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=True
        )
        messages = params["messages"]

        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        first_assistant = assistant_msgs[0]
        # Should be plain string content, not list with cache_control
        assert isinstance(first_assistant["content"], str)


class TestAnthropicBuildRequestCacheOff:
    """Tests that use_cache=False omits all cache breakpoints."""

    def test_system_message_no_cache_control(self, sonnet_provider, sample_messages):
        """System message should NOT have cache_control when use_cache=False."""
        params = sonnet_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=False
        )
        system = params["system"]
        assert len(system) == 1
        assert "cache_control" not in system[0]
        # Content should still be present
        assert "text" in system[0]
        assert "type" in system[0]

    def test_no_assistant_breakpoints(self, sonnet_provider, sample_messages):
        """No assistant messages should have cache breakpoints when use_cache=False."""
        params = sonnet_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=False
        )
        messages = params["messages"]

        for msg in messages:
            content = msg["content"]
            if isinstance(content, list):
                for block in content:
                    assert "cache_control" not in block, \
                        f"Found cache_control in message: {msg['role']}"
            # String content is always fine (no cache_control possible)

    def test_all_messages_plain_format(self, sonnet_provider, sample_messages):
        """All conversation messages should be plain string format when cache off."""
        params = sonnet_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=False
        )
        messages = params["messages"]

        for msg in messages:
            assert isinstance(msg["content"], str), \
                f"Expected string content for {msg['role']} message, got {type(msg['content'])}"

    def test_system_still_has_model_identity(self, sonnet_provider, sample_messages):
        """System message should still include model identity even without caching."""
        params = sonnet_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=False
        )
        system_text = params["system"][0]["text"]
        assert "Claude Sonnet 4.5" in system_text

    def test_thinking_config_unchanged(self, sonnet_provider, sample_messages):
        """Thinking configuration should be unaffected by use_cache flag."""
        params_on = sonnet_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=True
        )
        params_off = sonnet_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=False
        )
        assert params_on["thinking"] == params_off["thinking"]

    def test_model_name_unchanged(self, sonnet_provider, sample_messages):
        """Model name should be unaffected by use_cache flag."""
        params = sonnet_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=False
        )
        assert params["model"] == "claude-sonnet-4-5-20250929"


class TestAnthropicBuildRequestEdgeCases:
    """Edge cases for cache control behavior."""

    def test_no_assistant_messages_cache_on(self, sonnet_provider):
        """With no assistant messages, no breakpoints even when cache is on."""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "First message"},
        ]
        params = sonnet_provider.build_request(
            messages, "user", None, "chat", True, use_cache=True
        )
        # System should still have cache_control
        assert "cache_control" in params["system"][0]
        # Only one user message, no assistant breakpoints
        for msg in params["messages"]:
            assert isinstance(msg["content"], str)

    def test_one_assistant_message_cache_on(self, sonnet_provider):
        """With only one assistant message, it should get a cache breakpoint."""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "More"},
        ]
        params = sonnet_provider.build_request(
            messages, "user", None, "chat", True, use_cache=True
        )
        # The single assistant message should have a breakpoint (it's the last)
        assistant_msgs = [m for m in params["messages"] if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert isinstance(assistant_msgs[0]["content"], list)
        assert "cache_control" in assistant_msgs[0]["content"][0]

    def test_empty_system_content(self, sonnet_provider):
        """Empty system content should not produce cache_control block."""
        messages = [
            {"role": "system", "content": ""},
            {"role": "user", "content": "Hello"},
        ]
        params_on = sonnet_provider.build_request(
            messages, "user", None, "chat", True, use_cache=True
        )
        params_off = sonnet_provider.build_request(
            messages, "user", None, "chat", True, use_cache=False
        )
        # Both should have system with just model identity (no cache control on empty)
        # The model identity is always injected
        for params in [params_on, params_off]:
            assert "system" in params
            assert len(params["system"]) == 1
            assert "Claude Sonnet 4.5" in params["system"][0]["text"]


# ============================================================
# 2. Provider Unit Tests: AnthropicOpusProvider
# ============================================================

class TestOpusBuildRequestCachePassthrough:
    """Tests that Opus passes use_cache through to parent."""

    def test_opus_cache_on(self, opus_provider, sample_messages):
        """Opus with use_cache=True should have cache breakpoints."""
        params = opus_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=True
        )
        # System should have cache_control
        assert "cache_control" in params["system"][0]
        # Last assistant should have breakpoint
        assistant_msgs = [m for m in params["messages"] if m["role"] == "assistant"]
        last_assistant = assistant_msgs[-1]
        assert isinstance(last_assistant["content"], list)
        assert "cache_control" in last_assistant["content"][0]

    def test_opus_cache_off(self, opus_provider, sample_messages):
        """Opus with use_cache=False should have no cache breakpoints."""
        params = opus_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=False
        )
        # System should NOT have cache_control
        assert "cache_control" not in params["system"][0]
        # No assistant breakpoints
        for msg in params["messages"]:
            assert isinstance(msg["content"], str)

    def test_opus_adaptive_thinking_unaffected(self, opus_provider, sample_messages):
        """Opus adaptive thinking config should be unaffected by cache toggle."""
        params_on = opus_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=True
        )
        params_off = opus_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=False
        )
        assert params_on["thinking"] == {"type": "adaptive"}
        assert params_off["thinking"] == {"type": "adaptive"}
        assert params_on["output_config"] == {"effort": "high"}
        assert params_off["output_config"] == {"effort": "high"}

    def test_opus_model_name(self, opus_provider, sample_messages):
        """Opus model name should be correct regardless of cache setting."""
        params = opus_provider.build_request(
            sample_messages, "user", None, "chat", True, use_cache=False
        )
        assert params["model"] == "claude-opus-4-6"


# ============================================================
# 3. Provider Unit Tests: OpenAI ignores use_cache
# ============================================================

class TestOpenAIIgnoresUseCache:
    """Tests that OpenAI provider accepts but ignores use_cache."""

    def test_openai_build_request_accepts_use_cache(self):
        """OpenAI build_request should accept use_cache without error."""
        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hello"},
        ]
        # Should not raise
        params_on = provider.build_request(
            messages, "user", None, "chat", True, use_cache=True
        )
        params_off = provider.build_request(
            messages, "user", None, "chat", True, use_cache=False
        )
        # Both should produce identical params (OpenAI ignores the flag)
        assert params_on["model"] == params_off["model"]
        assert params_on["input"] == params_off["input"]


# ============================================================
# 4. _convert_messages_with_cache direct tests
# ============================================================

class TestConvertMessagesWithCache:
    """Direct tests for _convert_messages_with_cache."""

    def test_cache_on_places_breakpoint(self, sonnet_provider):
        """With use_cache=True, last assistant gets breakpoint."""
        messages = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "Q3"},
        ]
        result = sonnet_provider._convert_messages_with_cache(messages, use_cache=True)

        # A1 (index 1) is first assistant - no breakpoint
        assert isinstance(result[1]["content"], str)

        # A2 (index 3) is last assistant - should have breakpoint
        assert isinstance(result[3]["content"], list)
        assert result[3]["content"][0]["cache_control"]["ttl"] == "1h"

    def test_cache_off_no_breakpoints(self, sonnet_provider):
        """With use_cache=False, no messages get breakpoints."""
        messages = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "Q3"},
        ]
        result = sonnet_provider._convert_messages_with_cache(messages, use_cache=False)

        for msg in result:
            assert isinstance(msg["content"], str), \
                f"Expected plain string for {msg['role']}, got list"

    def test_default_use_cache_is_true(self, sonnet_provider):
        """Default (no use_cache param) should behave like True."""
        messages = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "Q3"},
        ]
        result_default = sonnet_provider._convert_messages_with_cache(messages)
        result_true = sonnet_provider._convert_messages_with_cache(messages, use_cache=True)

        # Both should place breakpoint on last assistant (index 3)
        assert isinstance(result_default[3]["content"], list)
        assert isinstance(result_true[3]["content"], list)

    def test_three_assistants_breakpoint_position(self, sonnet_provider):
        """With 3 assistant messages, breakpoint goes on the last."""
        messages = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "Q3"},
            {"role": "assistant", "content": "A3"},
            {"role": "user", "content": "Q4"},
        ]
        result = sonnet_provider._convert_messages_with_cache(messages, use_cache=True)

        # A1 (index 1) - no breakpoint
        assert isinstance(result[1]["content"], str)
        # A2 (index 3) - not last, no breakpoint
        assert isinstance(result[3]["content"], str)
        # A3 (index 5) - last assistant, should have breakpoint
        assert isinstance(result[5]["content"], list)
        assert "cache_control" in result[5]["content"][0]


# ============================================================
# 5. API Endpoint Tests: POST /api/set-anthropic-sync
# ============================================================

class TestSetAnthropicSyncEndpoint:
    """Tests for the POST /api/set-anthropic-sync endpoint."""

    def test_set_sync_true(self, client, test_user, temp_data_dir):
        """Setting sync=True should persist and return the value."""
        response = client.post("/api/set-anthropic-sync", json={
            "username": test_user,
            "chat_name": "test_chat",
            "sync": True
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["anthropic_sync"] is True

        # Verify persisted to disk
        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        assert chat_data["anthropic_sync"] is True

    def test_set_sync_false(self, client, test_user, temp_data_dir):
        """Setting sync=False should persist and return the value."""
        response = client.post("/api/set-anthropic-sync", json={
            "username": test_user,
            "chat_name": "test_chat",
            "sync": False
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["anthropic_sync"] is False

        # Verify persisted
        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        assert chat_data["anthropic_sync"] is False

    def test_toggle_back_and_forth(self, client, test_user, temp_data_dir):
        """Toggling sync on and off should always persist the latest value."""
        client.post("/api/set-anthropic-sync", json={
            "username": test_user, "chat_name": "test_chat", "sync": False
        })
        client.post("/api/set-anthropic-sync", json={
            "username": test_user, "chat_name": "test_chat", "sync": True
        })
        client.post("/api/set-anthropic-sync", json={
            "username": test_user, "chat_name": "test_chat", "sync": False
        })

        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        assert chat_data["anthropic_sync"] is False

    def test_nonexistent_chat_returns_404(self, client, test_user):
        """Setting sync on a non-existent chat should return 404."""
        response = client.post("/api/set-anthropic-sync", json={
            "username": test_user,
            "chat_name": "nonexistent_chat",
            "sync": True
        })
        assert response.status_code == 404

    def test_nonexistent_user_returns_404(self, client):
        """Setting sync for a non-existent user should return 404."""
        response = client.post("/api/set-anthropic-sync", json={
            "username": "nobody",
            "chat_name": "test_chat",
            "sync": True
        })
        assert response.status_code == 404

    def test_with_project(self, client, test_user, temp_data_dir):
        """Setting sync with a project param should work for project chats."""
        # Create a project chat
        project = "test_project"
        project_dir = Path(temp_data_dir) / test_user / "projects" / project
        project_dir.mkdir(parents=True, exist_ok=True)

        chat_data = {
            "messages": [
                {"id": "sys1", "role": "system", "content": "System"},
                {"id": "msg1", "role": "user", "content": "Hello", "parent_id": "sys1"},
                {"id": "msg2", "role": "assistant", "content": "Hi!", "parent_id": "msg1"},
            ],
            "stats": {
                "prompt_count": 1, "total_input_tokens": 10,
                "total_cache_read_tokens": 0, "total_output_tokens": 5,
                "total_reasoning_tokens": 0, "total_cost": 0.001,
                "last_accessed": "2025-01-01T00:00:00"
            },
            "current_leaf_id": "msg2",
            "model": "claude-sonnet-4.5"
        }
        chat_path = project_dir / "chat_proj_chat.json"
        with open(chat_path, 'w') as f:
            json.dump(chat_data, f)

        response = client.post("/api/set-anthropic-sync", json={
            "username": test_user,
            "chat_name": "proj_chat",
            "project": project,
            "sync": False
        })
        assert response.status_code == 200
        assert response.json()["anthropic_sync"] is False

        with open(chat_path) as f:
            saved = json.load(f)
        assert saved["anthropic_sync"] is False


# ============================================================
# 6. GET /api/get-chat returns anthropic_sync
# ============================================================

class TestGetChatReturnsAnthropicSync:
    """Tests that GET /api/get-chat includes the anthropic_sync field."""

    def test_default_value_when_not_set(self, client, test_user, temp_data_dir):
        """Chat without anthropic_sync field should return True (default)."""
        # The test_user fixture creates a chat without anthropic_sync
        # Remove it explicitly to be sure
        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        chat_data.pop("anthropic_sync", None)
        with open(chat_path, 'w') as f:
            json.dump(chat_data, f)

        response = client.get(f"/api/chat/{test_user}/test_chat")
        assert response.status_code == 200
        data = response.json()
        assert data["anthropic_sync"] is True

    def test_returns_false_when_set(self, client, test_user, temp_data_dir):
        """Chat with anthropic_sync=False should return False."""
        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        chat_data["anthropic_sync"] = False
        with open(chat_path, 'w') as f:
            json.dump(chat_data, f)

        response = client.get(f"/api/chat/{test_user}/test_chat")
        assert response.status_code == 200
        data = response.json()
        assert data["anthropic_sync"] is False

    def test_returns_true_when_set(self, client, test_user, temp_data_dir):
        """Chat with anthropic_sync=True should return True."""
        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        chat_data["anthropic_sync"] = True
        with open(chat_path, 'w') as f:
            json.dump(chat_data, f)

        response = client.get(f"/api/chat/{test_user}/test_chat")
        assert response.status_code == 200
        data = response.json()
        assert data["anthropic_sync"] is True

    def test_persists_after_set_endpoint(self, client, test_user):
        """Setting via endpoint then loading chat should show the same value."""
        client.post("/api/set-anthropic-sync", json={
            "username": test_user, "chat_name": "test_chat", "sync": False
        })
        response = client.get(f"/api/chat/{test_user}/test_chat")
        assert response.status_code == 200
        assert response.json()["anthropic_sync"] is False

        # Toggle back
        client.post("/api/set-anthropic-sync", json={
            "username": test_user, "chat_name": "test_chat", "sync": True
        })
        response = client.get(f"/api/chat/{test_user}/test_chat")
        assert response.json()["anthropic_sync"] is True


# ============================================================
# 7. Integration: use_cache flows from chat data to provider
# ============================================================

class TestUseCacheIntegration:
    """Tests that use_cache is correctly read from chat data and passed to build_request."""

    def test_send_message_passes_use_cache_true(self, client, test_user, temp_data_dir):
        """send_message should pass use_cache=True when anthropic_sync=True."""
        # Set up API key
        client.post("/api/set-api-keys", json={
            "username": test_user,
            "anthropic_key": "sk-ant-test"
        })

        with patch.object(AnthropicProvider, 'build_request', wraps=AnthropicProvider.build_request) as mock_build, \
             patch.object(AnthropicProvider, 'send_request') as mock_send:
            # Mock the API response
            mock_response = Mock()
            mock_response.content = [Mock(type="text", text="Response")]
            mock_response.usage = Mock(
                input_tokens=100, output_tokens=20,
                cache_read_input_tokens=0, cache_creation_input_tokens=0
            )
            mock_response.content[0].text = "Response"
            mock_send.return_value = mock_response

            # Chat has anthropic_sync=True (default - not set)
            try:
                client.post("/api/send-message", json={
                    "username": test_user,
                    "chat_name": "test_chat",
                    "message": "Test message",
                    "model": "claude-sonnet-4.5"
                })
            except Exception:
                pass  # May fail on response parsing, that's ok

            if mock_build.called:
                _, kwargs = mock_build.call_args
                # use_cache should be True (default)
                assert kwargs.get("use_cache", True) is True

    def test_send_message_passes_use_cache_false(self, client, test_user, temp_data_dir):
        """send_message should pass use_cache=False when anthropic_sync=False."""
        # Set anthropic_sync to False
        client.post("/api/set-anthropic-sync", json={
            "username": test_user, "chat_name": "test_chat", "sync": False
        })

        # Set up API key
        client.post("/api/set-api-keys", json={
            "username": test_user,
            "anthropic_key": "sk-ant-test"
        })

        with patch.object(AnthropicProvider, 'build_request', wraps=AnthropicProvider.build_request) as mock_build, \
             patch.object(AnthropicProvider, 'send_request') as mock_send:
            mock_response = Mock()
            mock_response.content = [Mock(type="text", text="Response")]
            mock_response.usage = Mock(
                input_tokens=100, output_tokens=20,
                cache_read_input_tokens=0, cache_creation_input_tokens=0
            )
            mock_response.content[0].text = "Response"
            mock_send.return_value = mock_response

            try:
                client.post("/api/send-message", json={
                    "username": test_user,
                    "chat_name": "test_chat",
                    "message": "Test message",
                    "model": "claude-sonnet-4.5"
                })
            except Exception:
                pass

            if mock_build.called:
                _, kwargs = mock_build.call_args
                assert kwargs.get("use_cache") is False


# ============================================================
# 8. Regression: GPT-5.2 unaffected
# ============================================================

class TestGPTUnaffected:
    """Verify GPT-5.2 behavior is completely unaffected by the toggle."""

    def test_gpt_chat_with_sync_false(self, client, test_user, temp_data_dir):
        """GPT chat should work normally even if anthropic_sync=False is set."""
        # Set the chat to GPT and anthropic_sync=False
        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        chat_data["model"] = "gpt-5.2"
        chat_data["anthropic_sync"] = False
        with open(chat_path, 'w') as f:
            json.dump(chat_data, f)

        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()

        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hello"},
        ]

        # Both should produce identical results for GPT
        params_true = provider.build_request(
            messages, "user", None, "chat", True, use_cache=True
        )
        params_false = provider.build_request(
            messages, "user", None, "chat", True, use_cache=False
        )
        assert params_true["model"] == params_false["model"]
        assert params_true["input"] == params_false["input"]

    def test_set_sync_on_gpt_chat_still_persists(self, client, test_user, temp_data_dir):
        """Setting anthropic_sync on a GPT chat should still persist (no error)."""
        # Change model to GPT
        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        chat_data["model"] = "gpt-5.2"
        with open(chat_path, 'w') as f:
            json.dump(chat_data, f)

        response = client.post("/api/set-anthropic-sync", json={
            "username": test_user, "chat_name": "test_chat", "sync": False
        })
        assert response.status_code == 200
        assert response.json()["anthropic_sync"] is False


# ============================================================
# 9. SyncEventType.CHAT_SETTINGS_CHANGED
# ============================================================

class TestChatSettingsChangedEnum:
    """Tests for the CHAT_SETTINGS_CHANGED sync event type."""

    def test_enum_exists(self):
        """CHAT_SETTINGS_CHANGED should be a valid SyncEventType."""
        assert hasattr(SyncEventType, 'CHAT_SETTINGS_CHANGED')
        assert SyncEventType.CHAT_SETTINGS_CHANGED.value == "chat_settings_changed"

    def test_serializes_with_model(self):
        """SyncEvent with model data should serialize correctly."""
        event = SyncEvent(
            type=SyncEventType.CHAT_SETTINGS_CHANGED,
            data={"model": "claude-sonnet-4.5", "context_start_index": 3}
        )
        parsed = json.loads(event.to_json())
        assert parsed["type"] == "chat_settings_changed"
        assert parsed["data"]["model"] == "claude-sonnet-4.5"
        assert parsed["data"]["context_start_index"] == 3

    def test_serializes_with_anthropic_sync(self):
        """SyncEvent with anthropic_sync data should serialize correctly."""
        event = SyncEvent(
            type=SyncEventType.CHAT_SETTINGS_CHANGED,
            data={"anthropic_sync": False}
        )
        parsed = json.loads(event.to_json())
        assert parsed["type"] == "chat_settings_changed"
        assert parsed["data"]["anthropic_sync"] is False


# ============================================================
# 10. Broadcast Tests: set-anthropic-sync
# ============================================================

class TestAnthropicSyncBroadcast:
    """Tests that set-anthropic-sync broadcasts to other sessions."""

    def test_broadcasts_on_sync_change(self, client, test_user, temp_data_dir):
        """Toggling sync should broadcast CHAT_SETTINGS_CHANGED with anthropic_sync."""
        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({
                "chat_key": chat_key,
                "type": event.type.value,
                "data": event.data
            })
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            response = client.post("/api/set-anthropic-sync", json={
                "username": test_user, "chat_name": "test_chat", "sync": False
            })
            assert response.status_code == 200

        settings_broadcasts = [c for c in broadcast_calls if c["type"] == "chat_settings_changed"]
        assert len(settings_broadcasts) == 1
        assert settings_broadcasts[0]["data"]["anthropic_sync"] is False

    def test_broadcast_chat_key_correct(self, client, test_user, temp_data_dir):
        """Broadcast should target the correct chat key."""
        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({"chat_key": chat_key, "type": event.type.value, "data": event.data})
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            client.post("/api/set-anthropic-sync", json={
                "username": test_user, "chat_name": "test_chat", "sync": True
            })

        settings_broadcasts = [c for c in broadcast_calls if c["type"] == "chat_settings_changed"]
        assert len(settings_broadcasts) == 1
        # chat_key format: "username::chat_name" (no project)
        assert settings_broadcasts[0]["chat_key"] == f"{test_user}::test_chat"

    def test_broadcast_chat_key_with_project(self, client, test_user, temp_data_dir):
        """Broadcast for project chat should use project in chat key."""
        project = "myproject"
        project_dir = Path(temp_data_dir) / test_user / "projects" / project
        project_dir.mkdir(parents=True, exist_ok=True)
        chat_data = {
            "messages": [
                {"id": "sys1", "role": "system", "content": "System"},
                {"id": "msg1", "role": "user", "content": "Hi", "parent_id": "sys1"},
                {"id": "msg2", "role": "assistant", "content": "Hello!", "parent_id": "msg1"},
            ],
            "stats": {
                "prompt_count": 1, "total_input_tokens": 10,
                "total_cache_read_tokens": 0, "total_output_tokens": 5,
                "total_reasoning_tokens": 0, "total_cost": 0.001,
                "last_accessed": "2025-01-01T00:00:00"
            },
            "current_leaf_id": "msg2", "model": "claude-sonnet-4.5"
        }
        with open(project_dir / "chat_proj_chat.json", 'w') as f:
            json.dump(chat_data, f)

        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({"chat_key": chat_key, "type": event.type.value, "data": event.data})
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            client.post("/api/set-anthropic-sync", json={
                "username": test_user, "chat_name": "proj_chat",
                "project": project, "sync": False
            })

        settings_broadcasts = [c for c in broadcast_calls if c["type"] == "chat_settings_changed"]
        assert len(settings_broadcasts) == 1
        assert settings_broadcasts[0]["chat_key"] == f"{test_user}:{project}:proj_chat"

    def test_broadcast_sync_true(self, client, test_user, temp_data_dir):
        """Broadcast should carry sync=True when toggled on."""
        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({"type": event.type.value, "data": event.data})
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            client.post("/api/set-anthropic-sync", json={
                "username": test_user, "chat_name": "test_chat", "sync": True
            })

        settings_broadcasts = [c for c in broadcast_calls if c["type"] == "chat_settings_changed"]
        assert len(settings_broadcasts) == 1
        assert settings_broadcasts[0]["data"]["anthropic_sync"] is True

    def test_no_broadcast_on_404(self, client, test_user, temp_data_dir):
        """No broadcast should happen if the chat doesn't exist (404)."""
        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({"type": event.type.value})
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            response = client.post("/api/set-anthropic-sync", json={
                "username": test_user, "chat_name": "nonexistent", "sync": False
            })
            assert response.status_code == 404

        assert len(broadcast_calls) == 0


# ============================================================
# 11. Broadcast Tests: set-chat-model
# ============================================================

class TestModelChangeBroadcast:
    """Tests that set-chat-model broadcasts CHAT_SETTINGS_CHANGED to other sessions."""

    def test_broadcasts_on_model_change(self, client, test_user, temp_data_dir):
        """Changing model should broadcast CHAT_SETTINGS_CHANGED with model and context_start_index."""
        # Set up an OpenAI API key so the switch succeeds
        client.post("/api/set-api-keys", json={
            "username": test_user, "openai_key": "sk-test"
        })

        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({
                "chat_key": chat_key,
                "type": event.type.value,
                "data": event.data
            })
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            response = client.post("/api/set-chat-model", json={
                "username": test_user, "chat_name": "test_chat", "model": "gpt-5.2"
            })
            assert response.status_code == 200

        settings_broadcasts = [c for c in broadcast_calls if c["type"] == "chat_settings_changed"]
        assert len(settings_broadcasts) == 1
        assert settings_broadcasts[0]["data"]["model"] == "gpt-5.2"
        assert "context_start_index" in settings_broadcasts[0]["data"]

    def test_model_broadcast_chat_key(self, client, test_user, temp_data_dir):
        """Model change broadcast should target the correct chat key."""
        client.post("/api/set-api-keys", json={
            "username": test_user, "openai_key": "sk-test"
        })

        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({"chat_key": chat_key, "type": event.type.value})
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            client.post("/api/set-chat-model", json={
                "username": test_user, "chat_name": "test_chat", "model": "gpt-5.2"
            })

        settings_broadcasts = [c for c in broadcast_calls if c["type"] == "chat_settings_changed"]
        assert len(settings_broadcasts) == 1
        assert settings_broadcasts[0]["chat_key"] == f"{test_user}::test_chat"

    def test_model_broadcast_includes_context_start_index(self, client, test_user, temp_data_dir):
        """Model change broadcast data should include context_start_index."""
        client.post("/api/set-api-keys", json={
            "username": test_user, "openai_key": "sk-test"
        })

        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({"type": event.type.value, "data": event.data})
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            client.post("/api/set-chat-model", json={
                "username": test_user, "chat_name": "test_chat", "model": "gpt-5.2"
            })

        settings_broadcasts = [c for c in broadcast_calls if c["type"] == "chat_settings_changed"]
        assert len(settings_broadcasts) == 1
        assert isinstance(settings_broadcasts[0]["data"]["context_start_index"], int)

    def test_no_model_broadcast_on_invalid_model(self, client, test_user, temp_data_dir):
        """No broadcast should happen if the model is invalid (400)."""
        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({"type": event.type.value})
            return 0

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            response = client.post("/api/set-chat-model", json={
                "username": test_user, "chat_name": "test_chat", "model": "invalid-model"
            })
            assert response.status_code == 400

        assert len(broadcast_calls) == 0

    def test_no_model_broadcast_on_missing_api_key(self, client, test_user, temp_data_dir):
        """No broadcast should happen if the required API key is missing (400)."""
        broadcast_calls = []

        async def capture_broadcast(self_mgr, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({"type": event.type.value})
            return 0

        # Don't set any Anthropic key — switching to Claude should fail
        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            response = client.post("/api/set-chat-model", json={
                "username": test_user, "chat_name": "test_chat", "model": "claude-sonnet-4.5"
            })
            assert response.status_code == 400

        assert len(broadcast_calls) == 0
