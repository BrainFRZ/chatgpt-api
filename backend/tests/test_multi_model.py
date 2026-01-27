"""
Comprehensive tests for multi-model support feature.

Tests cover:
1. Backend API endpoints (GET /api/models, /api-keys, POST /api/set-api-keys, /api/set-chat-model)
2. API key migration from legacy api_key.txt to api_keys.json
3. Provider unit tests (build_request, parse_response, calculate_cost, format_token_string)
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

# Import main module for patching
import main

# ============================================================
# Test Setup and Fixtures
# ============================================================

@pytest.fixture
def temp_data_dir(monkeypatch):
    """Create a temporary data directory for tests and patch main.DATA_DIR."""
    temp_dir = tempfile.mkdtemp()
    # Patch DATA_DIR in main module
    monkeypatch.setattr(main, 'DATA_DIR', Path(temp_dir))
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def client(temp_data_dir):
    """Create test client with isolated data directory."""
    # DATA_DIR is already patched by temp_data_dir fixture
    return TestClient(main.app)


@pytest.fixture
def test_user(temp_data_dir):
    """Create a test user with directories and sample chat."""
    username = "testuser"
    user_dir = Path(temp_data_dir) / username
    user_dir.mkdir(parents=True, exist_ok=True)

    # Create a sample chat (note: chat files are prefixed with "chat_")
    chat_data = {
        "messages": [
            {"id": "msg1", "role": "user", "content": "Hello"},
            {"id": "msg2", "role": "assistant", "content": "Hi there!", "parent_id": "msg1"}
        ],
        "stats": {
            "prompt_count": 1,
            "total_input_tokens": 10,
            "total_cached_tokens": 0,
            "total_output_tokens": 5,
            "total_reasoning_tokens": 0,
            "total_cost": 0.001,
            "last_accessed": "2025-01-01T00:00:00"
        },
        "current_leaf_id": "msg2"
    }
    # Chat files are stored as chat_{name}.json
    chat_path = user_dir / "chat_test_chat.json"
    with open(chat_path, 'w') as f:
        json.dump(chat_data, f)

    return username


# ============================================================
# 1. API Endpoint Tests
# ============================================================

class TestGetModels:
    """Tests for GET /api/models endpoint."""

    def test_returns_both_models(self, client):
        """Verify both GPT-5.2 and Claude Sonnet 4.5 are returned."""
        response = client.get("/api/models")
        assert response.status_code == 200

        models = response.json()
        assert len(models) == 2

        model_ids = [m["id"] for m in models]
        assert "gpt-5.2" in model_ids
        assert "claude-sonnet-4.5" in model_ids

    def test_gpt52_metadata(self, client):
        """Verify GPT-5.2 has correct pricing and context limits."""
        response = client.get("/api/models")
        models = {m["id"]: m for m in response.json()}

        gpt = models["gpt-5.2"]
        assert gpt["name"] == "GPT-5.2"
        assert gpt["pricing"]["input_new"] == 1.75
        assert gpt["pricing"]["input_cached"] == 0.175
        assert gpt["pricing"]["output"] == 14.0
        assert gpt["pricing"]["reasoning"] == 14.0
        assert gpt["context_limits"]["threshold"] == 275000
        assert gpt["context_limits"]["target"] == 225000

    def test_claude_metadata(self, client):
        """Verify Claude Sonnet 4.5 has correct pricing and context limits."""
        response = client.get("/api/models")
        models = {m["id"]: m for m in response.json()}

        claude = models["claude-sonnet-4.5"]
        assert claude["name"] == "Claude Sonnet 4.5"
        assert claude["pricing"]["input_new"] == 6.0
        assert claude["pricing"]["input_cached"] == 0.3
        assert claude["pricing"]["output"] == 15.0
        assert claude["pricing"]["reasoning"] == 15.0
        assert claude["context_limits"]["threshold"] == 180000
        assert claude["context_limits"]["target"] == 140000


class TestApiKeys:
    """Tests for API key management endpoints."""

    def test_set_openai_key_only(self, client, test_user, temp_data_dir):
        """Setting only OpenAI key should work."""
        response = client.post("/api/set-api-keys", json={
            "username": test_user,
            "openai_key": "sk-test-openai-key"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["has_openai"] is True
        assert data["has_anthropic"] is False

        # Verify file was created correctly
        keys_path = Path(temp_data_dir) / test_user / "api_keys.json"
        with open(keys_path) as f:
            saved_keys = json.load(f)
        assert saved_keys["openai"] == "sk-test-openai-key"

    def test_set_anthropic_key_only(self, client, test_user, temp_data_dir):
        """Setting only Anthropic key should work."""
        response = client.post("/api/set-api-keys", json={
            "username": test_user,
            "anthropic_key": "sk-ant-test-key"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["has_openai"] is False
        assert data["has_anthropic"] is True

    def test_set_both_keys(self, client, test_user, temp_data_dir):
        """Setting both keys should work."""
        response = client.post("/api/set-api-keys", json={
            "username": test_user,
            "openai_key": "sk-openai",
            "anthropic_key": "sk-ant"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["has_openai"] is True
        assert data["has_anthropic"] is True

    def test_set_empty_keys(self, client, test_user, temp_data_dir):
        """Setting empty keys should clear them."""
        # First set a key
        client.post("/api/set-api-keys", json={
            "username": test_user,
            "openai_key": "sk-test"
        })

        # Then clear it
        response = client.post("/api/set-api-keys", json={
            "username": test_user,
            "openai_key": ""
        })
        assert response.status_code == 200
        data = response.json()
        assert data["has_openai"] is False

    def test_get_api_keys_status(self, client, test_user, temp_data_dir):
        """GET /api/api-keys/{user} returns correct status."""
        # Set some keys first
        client.post("/api/set-api-keys", json={
            "username": test_user,
            "openai_key": "sk-test",
            "anthropic_key": "sk-ant"
        })

        response = client.get(f"/api/api-keys/{test_user}")
        assert response.status_code == 200
        data = response.json()
        assert data["has_openai"] is True
        assert data["has_anthropic"] is True

    def test_get_api_keys_user_not_found(self, client):
        """GET /api/api-keys for non-existent user returns 404."""
        response = client.get("/api/api-keys/nonexistent_user")
        assert response.status_code == 404


class TestSetChatModel:
    """Tests for POST /api/set-chat-model endpoint."""

    def test_set_valid_model_gpt(self, client, test_user, temp_data_dir):
        """Setting valid GPT model should work."""
        # First set OpenAI API key
        client.post("/api/set-api-keys", json={
            "username": test_user,
            "openai_key": "sk-test"
        })

        response = client.post("/api/set-chat-model", json={
            "username": test_user,
            "chat_name": "test_chat",
            "model": "gpt-5.2"
        })
        assert response.status_code == 200
        assert response.json()["model"] == "gpt-5.2"

        # Verify chat was updated (chat files are stored as chat_{name}.json)
        chat_path = Path(temp_data_dir) / test_user / "chat_test_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        assert chat_data["model"] == "gpt-5.2"

    def test_set_valid_model_claude(self, client, test_user, temp_data_dir):
        """Setting valid Claude model should work."""
        # First set Anthropic API key
        client.post("/api/set-api-keys", json={
            "username": test_user,
            "anthropic_key": "sk-ant-test"
        })

        response = client.post("/api/set-chat-model", json={
            "username": test_user,
            "chat_name": "test_chat",
            "model": "claude-sonnet-4.5"
        })
        assert response.status_code == 200
        assert response.json()["model"] == "claude-sonnet-4.5"

    def test_set_invalid_model(self, client, test_user):
        """Setting invalid model should return 400."""
        response = client.post("/api/set-chat-model", json={
            "username": test_user,
            "chat_name": "test_chat",
            "model": "invalid-model-xyz"
        })
        assert response.status_code == 400
        assert "Unknown model" in response.json()["detail"]

    def test_set_model_missing_api_key(self, client, test_user):
        """Setting Claude model without Anthropic key should return 400."""
        # User has no API keys set
        response = client.post("/api/set-chat-model", json={
            "username": test_user,
            "chat_name": "test_chat",
            "model": "claude-sonnet-4.5"
        })
        assert response.status_code == 400
        assert "API key" in response.json()["detail"]

    def test_set_model_chat_not_found(self, client, test_user):
        """Setting model on non-existent chat should return 404."""
        client.post("/api/set-api-keys", json={
            "username": test_user,
            "openai_key": "sk-test"
        })

        response = client.post("/api/set-chat-model", json={
            "username": test_user,
            "chat_name": "nonexistent_chat",
            "model": "gpt-5.2"
        })
        assert response.status_code == 404

    def test_model_switch_trims_to_target(self, client, test_user, temp_data_dir):
        """Switching models should trim context to target, not threshold.

        This ensures cache stability: trimming to target on switch means
        the first message won't cause an immediate re-trim on the second message.
        """
        # Create a chat with messages that have token counts
        # We'll create enough "tokens" to exceed Claude's target (140k) but stay under threshold (180k)
        chat_data = {
            "messages": [
                {"id": "sys", "role": "system", "content": "You are helpful.", "total_tokens": 100},
            ],
            "model": "gpt-5.2",
            "current_leaf_id": None,
            "stats": {}
        }

        # Add messages totaling ~150k tokens (between Claude's target=140k and threshold=180k)
        parent_id = "sys"
        for i in range(15):
            msg_id = f"msg{i}"
            chat_data["messages"].append({
                "id": msg_id,
                "parent_id": parent_id,
                "role": "user" if i % 2 == 0 else "assistant",
                "content": "x" * 1000,  # Content for token counting
                "total_tokens": 10000  # Each message is 10k tokens
            })
            parent_id = msg_id

        chat_data["current_leaf_id"] = parent_id

        # Write the chat file
        chat_path = Path(temp_data_dir) / test_user / "chat_token_test.json"
        with open(chat_path, 'w') as f:
            json.dump(chat_data, f)

        # Set up API key for Claude
        client.post("/api/set-api-keys", json={
            "username": test_user,
            "anthropic_key": "sk-ant-test"
        })

        # Mock the Anthropic provider's count_tokens to return the pre-set total_tokens
        # This ensures calculate_context_window uses our intended token counts
        from providers.anthropic_provider import AnthropicProvider
        original_count_tokens = AnthropicProvider.count_tokens

        def mock_count_tokens(self, text):
            # Return a fixed high count to ensure we're over the target
            return 10000  # Each call returns 10k tokens

        with patch.object(AnthropicProvider, 'count_tokens', mock_count_tokens):
            # Switch to Claude
            response = client.post("/api/set-chat-model", json={
                "username": test_user,
                "chat_name": "token_test",
                "model": "claude-sonnet-4.5"
            })

        assert response.status_code == 200
        result = response.json()

        # The context_start_index should reflect trimming to TARGET (140k), not THRESHOLD (180k)
        # With 16 messages at ~10k tokens each = 160k total
        # Trimming to 140k target means we should drop at least 2-3 messages
        # context_start_index > 1 means some messages are grayed out
        assert "context_start_index" in result
        # We expect trimming since 160k > 140k target
        assert result["context_start_index"] > 1, "Should trim to target (140k), not keep everything up to threshold (180k)"


class TestGetChatModel:
    """Tests for GET /api/chat/{user}/{chat} model field."""

    def test_chat_returns_model_field(self, client, test_user, temp_data_dir):
        """Chat response should include model field."""
        # Set model on the chat
        client.post("/api/set-api-keys", json={
            "username": test_user,
            "openai_key": "sk-test"
        })
        client.post("/api/set-chat-model", json={
            "username": test_user,
            "chat_name": "test_chat",
            "model": "gpt-5.2"
        })

        response = client.get(f"/api/chat/{test_user}/test_chat")
        assert response.status_code == 200
        data = response.json()
        assert "model" in data
        assert data["model"] == "gpt-5.2"

    def test_old_chat_defaults_to_gpt(self, client, test_user, temp_data_dir):
        """Old chat without model field should default to gpt-5.2."""
        # test_chat fixture doesn't have model field
        response = client.get(f"/api/chat/{test_user}/test_chat")
        assert response.status_code == 200
        data = response.json()
        assert data["model"] == "gpt-5.2"


class TestCreateChatWithModel:
    """Tests for creating chats with model selection."""

    def test_create_chat_with_model(self, client, test_user, temp_data_dir):
        """Creating chat with model should persist the model."""
        # Create chat with Claude model specified
        response = client.post("/api/create-chat", json={
            "username": test_user,
            "chat_name": "claude_chat",
            "model": "claude-sonnet-4.5"
        })
        assert response.status_code == 200

        # Verify the chat has the model set
        chat_path = Path(temp_data_dir) / test_user / "chat_claude_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        assert chat_data.get("model") == "claude-sonnet-4.5"

    def test_create_chat_without_model(self, client, test_user, temp_data_dir):
        """Creating chat without model should not set model field."""
        response = client.post("/api/create-chat", json={
            "username": test_user,
            "chat_name": "no_model_chat"
        })
        assert response.status_code == 200

        # Verify the chat does not have model field (will default on read)
        chat_path = Path(temp_data_dir) / test_user / "chat_no_model_chat.json"
        with open(chat_path) as f:
            chat_data = json.load(f)
        assert "model" not in chat_data


# ============================================================
# 2. API Key Migration Tests
# ============================================================

class TestApiKeyMigration:
    """Tests for auto-migration from legacy api_key.txt to api_keys.json."""

    def test_migration_from_legacy_file(self, temp_data_dir):
        """Loading keys should migrate from api_key.txt to api_keys.json."""
        username = "migrationuser"
        user_dir = Path(temp_data_dir) / username
        user_dir.mkdir(parents=True)

        # Create legacy api_key.txt
        legacy_key = "sk-legacy-openai-key-12345"
        (user_dir / "api_key.txt").write_text(legacy_key)

        # DATA_DIR is already patched by temp_data_dir fixture
        # Call load_api_keys directly
        keys = main.load_api_keys(username)

        # Verify migration
        assert keys["openai"] == legacy_key
        assert keys.get("anthropic", "") == ""

        # Verify api_keys.json was created
        json_path = user_dir / "api_keys.json"
        assert json_path.exists()
        with open(json_path) as f:
            saved = json.load(f)
        assert saved["openai"] == legacy_key

    def test_no_migration_when_json_exists(self, temp_data_dir):
        """If api_keys.json exists, should not read from api_key.txt."""
        username = "nomigrationuser"
        user_dir = Path(temp_data_dir) / username
        user_dir.mkdir(parents=True)

        # Create both files
        (user_dir / "api_key.txt").write_text("legacy-key")
        (user_dir / "api_keys.json").write_text(json.dumps({
            "openai": "json-key",
            "anthropic": "ant-key"
        }))

        # DATA_DIR is already patched by temp_data_dir fixture
        keys = main.load_api_keys(username)

        # Should get JSON values, not legacy
        assert keys["openai"] == "json-key"
        assert keys["anthropic"] == "ant-key"


# ============================================================
# 3. Provider Unit Tests
# ============================================================

class TestOpenAIProvider:
    """Unit tests for OpenAI provider."""

    def test_model_properties(self):
        """Test provider properties are correct."""
        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()

        assert provider.model_id == "gpt-5.2"
        assert provider.display_name == "GPT-5.2"
        assert provider.pricing.input_new == 1.75
        assert provider.pricing.input_cached == 0.175
        assert provider.pricing.output == 14.0
        assert provider.pricing.reasoning == 14.0
        assert provider.context_limits.threshold == 275000
        assert provider.context_limits.target == 225000

    def test_build_request_basic(self):
        """Test build_request creates correct structure."""
        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"}
        ]

        params = provider.build_request(
            messages=messages,
            username="testuser",
            project="myproject",
            chat_name="chat1",
            is_free_chat=False
        )

        assert params["model"] == "gpt-5.2"
        assert params["input"] == messages
        assert params["store"] is False
        assert params["prompt_cache_retention"] == "24h"
        assert "testuser" in params["prompt_cache_key"]
        assert "myproject" in params["prompt_cache_key"]
        assert "reasoning" in params
        assert params["reasoning"]["effort"] == "medium"

    def test_build_request_free_chat_limits_tokens(self):
        """Free chats should have max_output_tokens limit."""
        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()

        params = provider.build_request(
            messages=[{"role": "user", "content": "Hi"}],
            username="user",
            project=None,
            chat_name="chat",
            is_free_chat=True
        )

        assert params["max_output_tokens"] == 1200

    def test_build_request_project_chat_no_limit(self):
        """Project chats should not have output token limit."""
        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()

        params = provider.build_request(
            messages=[{"role": "user", "content": "Hi"}],
            username="user",
            project="project",
            chat_name="chat",
            is_free_chat=False
        )

        assert "max_output_tokens" not in params

    def test_calculate_cost(self):
        """Test cost calculation uses correct pricing."""
        from providers.openai_provider import OpenAIProvider
        from providers import ParsedResponse
        provider = OpenAIProvider()

        parsed = ParsedResponse(
            content="Hello",
            reasoning="Thinking...",
            input_tokens=1000,
            cached_tokens=800,
            output_tokens=100,
            reasoning_tokens=50
        )

        # Cost = (200 new * 1.75 + 800 cached * 0.175 + 100 output * 14 + 50 reasoning * 14) / 1M
        expected = (200 * 1.75 + 800 * 0.175 + 100 * 14.0 + 50 * 14.0) / 1_000_000
        cost = provider.calculate_cost(parsed)

        assert abs(cost - expected) < 0.0000001

    def test_format_token_string(self):
        """Test token string format is correct."""
        from providers.openai_provider import OpenAIProvider
        from providers import ParsedResponse
        provider = OpenAIProvider()

        parsed = ParsedResponse(
            content="Hello",
            reasoning=None,
            input_tokens=1000,
            cached_tokens=800,
            output_tokens=100,
            reasoning_tokens=50
        )

        result = provider.format_token_string(parsed)
        # new_input = 1000 - 800 = 200
        # total = 1000 + 100 + 50 = 1150
        assert result == "I:200 C:800 O:100 R:50 T:1150"

    def test_count_tokens(self):
        """Test token counting with tiktoken."""
        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()

        # Simple test - "Hello" should be 1 token
        count = provider.count_tokens("Hello")
        assert count >= 1

        # Longer text should have more tokens
        long_text = "This is a longer piece of text that should have multiple tokens."
        count2 = provider.count_tokens(long_text)
        assert count2 > count


class TestAnthropicProvider:
    """Unit tests for Anthropic provider."""

    def test_model_properties(self):
        """Test provider properties are correct."""
        from providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider()

        assert provider.model_id == "claude-sonnet-4.5"
        assert provider.display_name == "Claude Sonnet 4.5"
        assert provider.pricing.input_new == 6.0
        assert provider.pricing.input_cached == 0.3
        assert provider.pricing.output == 15.0
        assert provider.pricing.reasoning == 15.0
        assert provider.context_limits.threshold == 180000
        assert provider.context_limits.target == 140000

    def test_build_request_structure(self):
        """Test build_request creates correct Anthropic structure."""
        from providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider()

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"}
        ]

        params = provider.build_request(
            messages=messages,
            username="testuser",
            project="myproject",
            chat_name="chat1",
            is_free_chat=False
        )

        assert params["model"] == "claude-sonnet-4-5-20250929"
        assert params["max_tokens"] == 16000
        assert "system" in params
        assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert "thinking" in params
        assert params["thinking"]["type"] == "enabled"
        assert params["thinking"]["budget_tokens"] == 3000

    def test_build_request_separates_system(self):
        """System message should be extracted to separate field."""
        from providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider()

        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"}
        ]

        params = provider.build_request(
            messages=messages,
            username="user",
            project=None,
            chat_name="chat",
            is_free_chat=False
        )

        # System should be separate
        assert params["system"][0]["text"] == "System prompt"

        # Messages should not include system
        assert len(params["messages"]) == 1
        assert params["messages"][0]["role"] == "user"

    def test_cache_breakpoint_placement(self):
        """Test cache breakpoint is placed on second-to-last assistant message."""
        from providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider()

        messages = [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "2"},
            {"role": "assistant", "content": "A2"},  # This should get cache_control
            {"role": "user", "content": "3"},
            {"role": "assistant", "content": "A3"},
        ]

        params = provider.build_request(
            messages=messages,
            username="user",
            project=None,
            chat_name="chat",
            is_free_chat=False
        )

        # Find which message has cache_control
        cache_controlled = [
            i for i, m in enumerate(params["messages"])
            if isinstance(m.get("content"), list) and
            any(c.get("cache_control") for c in m["content"] if isinstance(c, dict))
        ]

        # Should be the second-to-last assistant (index 3 in this case)
        assert 3 in cache_controlled

    def test_calculate_cost(self):
        """Test cost calculation uses Claude pricing."""
        from providers.anthropic_provider import AnthropicProvider
        from providers import ParsedResponse
        provider = AnthropicProvider()

        parsed = ParsedResponse(
            content="Hello",
            reasoning="Extended thinking...",
            input_tokens=1000,
            cached_tokens=600,
            output_tokens=100,
            reasoning_tokens=200
        )

        # Cost = (400 new * 6.0 + 600 cached * 0.3 + 100 output * 15 + 200 reasoning * 15) / 1M
        expected = (400 * 6.0 + 600 * 0.3 + 100 * 15.0 + 200 * 15.0) / 1_000_000
        cost = provider.calculate_cost(parsed)

        assert abs(cost - expected) < 0.0000001

    def test_format_token_string(self):
        """Test token string format is correct for Claude."""
        from providers.anthropic_provider import AnthropicProvider
        from providers import ParsedResponse
        provider = AnthropicProvider()

        parsed = ParsedResponse(
            content="Response",
            reasoning="Thinking",
            input_tokens=500,
            cached_tokens=300,
            output_tokens=50,
            reasoning_tokens=100
        )

        result = provider.format_token_string(parsed)
        # new_input = 500 - 300 = 200
        # total = 500 + 50 + 100 = 650
        assert result == "I:200 C:300 O:50 R:100 T:650"


class TestProviderRegistry:
    """Tests for the provider registry."""

    def test_both_providers_registered(self):
        """Both providers should be available in registry."""
        from providers import ProviderRegistry

        gpt = ProviderRegistry.get("gpt-5.2")
        claude = ProviderRegistry.get("claude-sonnet-4.5")

        assert gpt is not None
        assert claude is not None

    def test_get_default_returns_gpt(self):
        """Default provider should be GPT-5.2."""
        from providers import ProviderRegistry

        default = ProviderRegistry.get_default()
        assert default.model_id == "gpt-5.2"

    def test_list_models_returns_metadata(self):
        """list_models should return metadata for all providers."""
        from providers import ProviderRegistry

        models = ProviderRegistry.list_models()
        assert len(models) == 2

        model_ids = [m["id"] for m in models]
        assert "gpt-5.2" in model_ids
        assert "claude-sonnet-4.5" in model_ids

    def test_get_required_api_key(self):
        """get_required_api_key should return correct provider."""
        from providers import ProviderRegistry

        assert ProviderRegistry.get_required_api_key("gpt-5.2") == "openai"
        assert ProviderRegistry.get_required_api_key("claude-sonnet-4.5") == "anthropic"


class TestAddUpdatesToMessages:
    """Tests for the add_updates_to_messages functions."""

    def test_openai_adds_separate_message(self):
        """OpenAI version should add updates as separate user message."""
        from providers.openai_provider import add_updates_to_messages

        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"}
        ]

        result = add_updates_to_messages(messages, "Some updates")

        assert len(result) == 3
        assert result[-1]["role"] == "user"
        assert "CONTEXT UPDATES" in result[-1]["content"]
        assert "Some updates" in result[-1]["content"]

    def test_openai_empty_updates_unchanged(self):
        """Empty updates should not modify messages."""
        from providers.openai_provider import add_updates_to_messages

        messages = [{"role": "user", "content": "Hello"}]
        result = add_updates_to_messages(messages, "   ")

        assert result == messages

    def test_anthropic_appends_to_last_user(self):
        """Anthropic version should append to last user message."""
        from providers.anthropic_provider import add_updates_to_messages

        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "How are you?"}
        ]

        result = add_updates_to_messages(messages, "Update info")

        # Same number of messages
        assert len(result) == 3
        # Last user message should have updates appended
        assert "How are you?" in result[-1]["content"]
        assert "CONTEXT UPDATES" in result[-1]["content"]
        assert "Update info" in result[-1]["content"]

    def test_anthropic_empty_updates_unchanged(self):
        """Empty updates should not modify messages."""
        from providers.anthropic_provider import add_updates_to_messages

        messages = [{"role": "user", "content": "Hello"}]
        result = add_updates_to_messages(messages, "")

        assert result == messages


# ============================================================
# Run tests
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
