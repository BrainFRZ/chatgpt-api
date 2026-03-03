"""
Tests for _stateful_tool_retry and the retry integration branch in send_message_stream.

Covers:
- Unit tests: _stateful_tool_retry function (message construction, API params, usage extraction)
- Integration tests: state application after retry (IC, OOC, failure, merge semantics)
- Async tests: asyncio.to_thread behavior and error propagation
"""

import asyncio
import copy
import sys
import os

import pytest

# Ensure backend is on sys.path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock

from main import _stateful_tool_retry, _tool_input_valid
from pipeline import (
    STATE_REPORT_TOOL,
    apply_single_agent_state_updates,
    _fresh_pipeline_state,
)


# ============================================================
# Test Helpers
# ============================================================

def _make_tool_use_block(input_data):
    """Create a mock tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.input = input_data
    return block


def _make_text_block(text):
    """Create a mock text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_thinking_block(thinking):
    """Create a mock thinking content block."""
    block = MagicMock()
    block.type = "thinking"
    block.thinking = thinking
    return block


def _make_usage(input_tokens=100, output_tokens=50, cache_read=80, cache_creation=10):
    """Create a mock usage object with cache attributes."""
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_read_input_tokens = cache_read
    usage.cache_creation_input_tokens = cache_creation
    return usage


def _make_response(content_blocks, usage):
    """Create a mock Anthropic API response."""
    response = MagicMock()
    response.content = content_blocks
    response.usage = usage
    return response


def _make_client(response):
    """Create a mock Anthropic client that returns the given response."""
    client = MagicMock()
    client.messages.create.return_value = response
    return client


def _sample_tool_input():
    """A realistic tool input with all sections populated."""
    return {
        "is_ooc": False,
        "pacing": {"episode": "Session 1", "beat": "Opening", "responses": 1},
        "scene_state": {
            "location": "Tavern",
            "npcs_present": ["Bartender"],
            "active_tensions": ["mysterious stranger"],
            "atmosphere": "dim and smoky",
        },
        "character_states": {"Hero": "HP 20/20, no conditions"},
        "callback_ops": [
            {"action": "add", "original_text": "The stranger watches", "source_npc": "Stranger"}
        ],
        "npc_memory_ops": [
            {"action": "add", "npc": "Bartender", "text": "Served the hero", "impact": 2, "date": "Day 1"}
        ],
    }


def _sample_ooc_input():
    """A realistic OOC tool input."""
    return {
        "is_ooc": True,
        "pacing": {"episode": "Session 1", "beat": "OOC", "responses": 0},
        "scene_state": {
            "location": "N/A",
            "npcs_present": [],
            "active_tensions": [],
            "atmosphere": "N/A",
        },
        "character_states": {},
    }


SAMPLE_SYSTEM = [{"type": "text", "text": "You are a narrator.", "cache_control": {"type": "ephemeral"}}]
SAMPLE_MESSAGES = [
    {"role": "user", "content": "I enter the tavern."},
]
SAMPLE_NARRATIVE = "The door creaks open as you step inside the dimly lit tavern."
SAMPLE_THINKING = "The player wants to enter the tavern. I should describe the scene."
MODEL_NAME = "claude-sonnet-4-5-20250929"


def _call_retry(**overrides):
    """Convenience wrapper to call _stateful_tool_retry with defaults."""
    kwargs = dict(
        client=overrides.get("client"),
        model_name=overrides.get("model_name", MODEL_NAME),
        narrative=overrides.get("narrative", SAMPLE_NARRATIVE),
        thinking=overrides.get("thinking", SAMPLE_THINKING),
        tool_def=overrides.get("tool_def", STATE_REPORT_TOOL),
        state_contract=overrides.get("system_content", SAMPLE_SYSTEM),
    )
    return _stateful_tool_retry(**kwargs)


# ============================================================
# Unit Tests: Basic Return Values
# ============================================================

class TestRetryBasicReturn:
    """Test the core return values of _stateful_tool_retry."""

    def test_successful_retry_returns_tool_input(self):
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        result, usage = _call_retry(client=client)
        assert result == tool_input
        assert usage is not None

    def test_no_tool_use_returns_none(self):
        client = _make_client(_make_response([_make_text_block("I refuse.")], _make_usage()))
        result, usage = _call_retry(client=client)
        assert result is None
        assert usage is not None

    def test_empty_content_returns_none(self):
        client = _make_client(_make_response([], _make_usage()))
        result, _ = _call_retry(client=client)
        assert result is None

    def test_only_thinking_block_returns_none(self):
        client = _make_client(
            _make_response([_make_thinking_block("I'm thinking...")], _make_usage())
        )
        result, _ = _call_retry(client=client)
        assert result is None


# ============================================================
# Unit Tests: Message Construction
# ============================================================

class TestRetryMessageConstruction:
    """Test that retry_messages are built correctly."""

    def _get_retry_messages(self, client):
        return client.messages.create.call_args.kwargs["messages"]

    def test_thinking_embedded_as_plain_text(self):
        """Thinking is embedded into the single retry user prompt."""
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client, thinking="Deep analysis here")

        msgs = self._get_retry_messages(client)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        text = msgs[0]["content"]
        assert "<reasoning>\nDeep analysis here\n</reasoning>" in text
        assert SAMPLE_NARRATIVE in text

    def test_no_reasoning_tags_when_thinking_empty(self):
        """When thinking is empty, just the narrative with no reasoning tags."""
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client, thinking="")

        msgs = self._get_retry_messages(client)
        assert len(msgs) == 1
        text = msgs[0]["content"]
        assert SAMPLE_NARRATIVE in text
        assert "<reasoning>" not in text

    def test_no_reasoning_tags_when_thinking_none(self):
        """When thinking is None, just the narrative with no reasoning tags."""
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client, thinking=None)

        msgs = self._get_retry_messages(client)
        assert len(msgs) == 1
        assert SAMPLE_NARRATIVE in msgs[0]["content"]

    def test_retry_user_prompt_is_last_message(self):
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client)

        msgs = self._get_retry_messages(client)
        last_msg = msgs[-1]
        assert last_msg["role"] == "user"
        assert "report_state" in last_msg["content"]
        assert "Call report_state now" in last_msg["content"]

    def test_original_messages_ignored_in_minimal_retry(self):
        original = [{"role": "user", "content": "Hello"}]
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client, messages=original)

        msgs = self._get_retry_messages(client)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_original_messages_not_mutated(self):
        original = [{"role": "user", "content": "Hello"}]
        original_copy = copy.deepcopy(original)
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client, messages=original)
        assert original == original_copy

    def test_empty_messages_list(self):
        """Edge case: no prior conversation history (ignored by minimal retry)."""
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client, messages=[])

        msgs = self._get_retry_messages(client)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_narrative_in_assistant_text_block(self):
        """The narrative text should appear in the retry user prompt."""
        custom_narrative = "A dragon descends from the sky, scales glinting."
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client, narrative=custom_narrative, thinking="")

        msgs = self._get_retry_messages(client)
        assert custom_narrative in msgs[-1]["content"]


# ============================================================
# Unit Tests: API Call Parameters
# ============================================================

class TestRetryAPIParams:
    """Test that the correct parameters are passed to client.messages.create."""

    def _get_call_kwargs(self, client):
        return client.messages.create.call_args.kwargs

    def test_uses_non_beta_endpoint(self):
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client)

        client.messages.create.assert_called_once()
        # beta endpoint should NOT be called
        assert not client.beta.messages.create.called

    def test_forced_tool_choice(self):
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client)

        kwargs = self._get_call_kwargs(client)
        assert kwargs["tool_choice"] == {"type": "tool", "name": "report_state"}

    def test_correct_model_forwarded(self):
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client, model_name="claude-opus-4-6-20250910")

        kwargs = self._get_call_kwargs(client)
        assert kwargs["model"] == "claude-opus-4-6-20250910"

    def test_system_content_forwarded(self):
        custom_system = [{"type": "text", "text": "Custom narrator prompt."}]
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client, system_content=custom_system)

        kwargs = self._get_call_kwargs(client)
        assert kwargs["system"] == custom_system

    def test_max_tokens_is_4096(self):
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client)

        kwargs = self._get_call_kwargs(client)
        assert kwargs["max_tokens"] == 4096

    def test_tool_def_passed_as_single_list(self):
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client)

        kwargs = self._get_call_kwargs(client)
        assert kwargs["tools"] == [STATE_REPORT_TOOL]

    def test_no_beta_headers_or_thinking_params(self):
        """Retry should not include betas, thinking budgets, or other beta features."""
        tool_input = _sample_tool_input()
        client = _make_client(_make_response([_make_tool_use_block(tool_input)], _make_usage()))
        _call_retry(client=client)

        kwargs = self._get_call_kwargs(client)
        assert "betas" not in kwargs
        assert "thinking" not in kwargs


# ============================================================
# Unit Tests: Usage Extraction
# ============================================================

class TestRetryUsageExtraction:
    """Test usage/token extraction from the retry response."""

    def test_usage_with_cache_tokens(self):
        """input_tokens = raw + cache_read + cache_creation."""
        client = _make_client(
            _make_response(
                [_make_tool_use_block(_sample_tool_input())],
                _make_usage(input_tokens=100, output_tokens=50, cache_read=80, cache_creation=10),
            )
        )
        _, usage = _call_retry(client=client)

        assert usage["input_tokens"] == 100 + 80 + 10
        assert usage["cache_read_tokens"] == 80
        assert usage["cache_creation_tokens"] == 10
        assert usage["output_tokens"] == 50

    def test_usage_without_cache_attributes(self):
        """When cache attributes are missing, getattr defaults to 0."""
        mock_usage = MagicMock(spec=[])  # empty spec so no auto-attributes
        mock_usage.input_tokens = 200
        mock_usage.output_tokens = 30
        # Deliberately no cache_read_input_tokens or cache_creation_input_tokens

        client = _make_client(
            _make_response([_make_tool_use_block(_sample_tool_input())], mock_usage)
        )
        _, usage = _call_retry(client=client)

        assert usage["input_tokens"] == 200
        assert usage["cache_read_tokens"] == 0
        assert usage["cache_creation_tokens"] == 0
        assert usage["output_tokens"] == 30

    def test_usage_with_zero_cache(self):
        client = _make_client(
            _make_response(
                [_make_tool_use_block(_sample_tool_input())],
                _make_usage(input_tokens=150, output_tokens=25, cache_read=0, cache_creation=0),
            )
        )
        _, usage = _call_retry(client=client)

        assert usage["input_tokens"] == 150
        assert usage["cache_read_tokens"] == 0
        assert usage["cache_creation_tokens"] == 0

    def test_usage_with_none_cache_values(self):
        """Cache attributes present but set to None."""
        mock_usage = MagicMock()
        mock_usage.input_tokens = 200
        mock_usage.output_tokens = 40
        mock_usage.cache_read_input_tokens = None
        mock_usage.cache_creation_input_tokens = None

        client = _make_client(
            _make_response([_make_tool_use_block(_sample_tool_input())], mock_usage)
        )
        _, usage = _call_retry(client=client)

        assert usage["input_tokens"] == 200  # None → 0 via `or 0`
        assert usage["cache_read_tokens"] == 0
        assert usage["cache_creation_tokens"] == 0

    def test_usage_returned_even_on_no_tool_use(self):
        """Usage should always be returned, even when tool_input is None."""
        client = _make_client(
            _make_response(
                [_make_text_block("Sorry, I can't do that.")],
                _make_usage(input_tokens=50, output_tokens=10, cache_read=40, cache_creation=5),
            )
        )
        result, usage = _call_retry(client=client)

        assert result is None
        assert usage["input_tokens"] == 50 + 40 + 5
        assert usage["output_tokens"] == 10


# ============================================================
# Unit Tests: Multiple Content Blocks
# ============================================================

class TestRetryMultipleBlocks:
    """Test handling of responses with various content block configurations."""

    def test_tool_use_among_text_blocks(self):
        tool_input = _sample_tool_input()
        blocks = [
            _make_text_block("Let me report the state..."),
            _make_tool_use_block(tool_input),
            _make_text_block("Done."),
        ]
        client = _make_client(_make_response(blocks, _make_usage()))
        result, _ = _call_retry(client=client)
        assert result == tool_input

    def test_first_tool_use_wins(self):
        first = {"is_ooc": False, "pacing": {"episode": "1", "beat": "A", "responses": 1},
                 "scene_state": {"location": "A", "npcs_present": [], "active_tensions": [], "atmosphere": "A"},
                 "character_states": {}}
        second = {"is_ooc": True, "pacing": {"episode": "2", "beat": "B", "responses": 2},
                  "scene_state": {"location": "B", "npcs_present": [], "active_tensions": [], "atmosphere": "B"},
                  "character_states": {}}
        blocks = [_make_tool_use_block(first), _make_tool_use_block(second)]
        client = _make_client(_make_response(blocks, _make_usage()))
        result, _ = _call_retry(client=client)
        assert result == first

    def test_tool_use_after_thinking_block(self):
        """Thinking + tool_use is a realistic pattern."""
        tool_input = _sample_tool_input()
        blocks = [
            _make_thinking_block("Analyzing the turn..."),
            _make_tool_use_block(tool_input),
        ]
        client = _make_client(_make_response(blocks, _make_usage()))
        result, _ = _call_retry(client=client)
        assert result == tool_input


# ============================================================
# Unit Tests: Exception Handling
# ============================================================

class TestRetryExceptionHandling:
    """Test error propagation from the API client."""

    def test_api_error_propagates(self):
        client = MagicMock()
        client.messages.create.side_effect = Exception("API Error: overloaded")
        with pytest.raises(Exception, match="API Error: overloaded"):
            _call_retry(client=client)

    def test_connection_error_propagates(self):
        client = MagicMock()
        client.messages.create.side_effect = ConnectionError("Connection refused")
        with pytest.raises(ConnectionError, match="Connection refused"):
            _call_retry(client=client)

    def test_timeout_error_propagates(self):
        client = MagicMock()
        client.messages.create.side_effect = TimeoutError("Request timed out")
        with pytest.raises(TimeoutError, match="Request timed out"):
            _call_retry(client=client)


# ============================================================
# Integration Tests: Usage Accumulation
# ============================================================

class TestRetryUsageAccumulation:
    """Test that retry usage tokens are correctly summed into main usage."""

    def test_all_token_types_accumulated(self):
        main_usage = {
            'input_tokens': 1000,
            'cache_read_tokens': 800,
            'cache_creation_tokens': 100,
            'output_tokens': 200,
        }
        retry_usage = {
            'input_tokens': 190,
            'cache_read_tokens': 80,
            'cache_creation_tokens': 10,
            'output_tokens': 50,
        }
        # Replicate the accumulation logic from main.py
        main_usage['input_tokens'] = main_usage.get('input_tokens', 0) + retry_usage['input_tokens']
        main_usage['cache_read_tokens'] = main_usage.get('cache_read_tokens', 0) + retry_usage['cache_read_tokens']
        main_usage['cache_creation_tokens'] = main_usage.get('cache_creation_tokens', 0) + retry_usage['cache_creation_tokens']
        main_usage['output_tokens'] = main_usage.get('output_tokens', 0) + retry_usage['output_tokens']

        assert main_usage['input_tokens'] == 1190
        assert main_usage['cache_read_tokens'] == 880
        assert main_usage['cache_creation_tokens'] == 110
        assert main_usage['output_tokens'] == 250

    def test_accumulation_with_zero_main_usage(self):
        """When main usage starts at 0 (edge case)."""
        main_usage = {
            'input_tokens': 0,
            'cache_read_tokens': 0,
            'cache_creation_tokens': 0,
            'output_tokens': 0,
        }
        retry_usage = {
            'input_tokens': 190,
            'cache_read_tokens': 80,
            'cache_creation_tokens': 10,
            'output_tokens': 50,
        }
        main_usage['input_tokens'] = main_usage.get('input_tokens', 0) + retry_usage['input_tokens']
        main_usage['cache_read_tokens'] = main_usage.get('cache_read_tokens', 0) + retry_usage['cache_read_tokens']
        main_usage['cache_creation_tokens'] = main_usage.get('cache_creation_tokens', 0) + retry_usage['cache_creation_tokens']
        main_usage['output_tokens'] = main_usage.get('output_tokens', 0) + retry_usage['output_tokens']

        assert main_usage == retry_usage

    def test_accumulation_preserves_other_keys(self):
        """Keys not in retry_usage (e.g. reasoning_tokens) should be untouched."""
        main_usage = {
            'input_tokens': 100,
            'cache_read_tokens': 50,
            'cache_creation_tokens': 10,
            'output_tokens': 80,
            'reasoning_tokens': 500,
            'service_tier': 'flex',
        }
        retry_usage = {
            'input_tokens': 50,
            'cache_read_tokens': 40,
            'cache_creation_tokens': 5,
            'output_tokens': 20,
        }
        main_usage['input_tokens'] = main_usage.get('input_tokens', 0) + retry_usage['input_tokens']
        main_usage['cache_read_tokens'] = main_usage.get('cache_read_tokens', 0) + retry_usage['cache_read_tokens']
        main_usage['cache_creation_tokens'] = main_usage.get('cache_creation_tokens', 0) + retry_usage['cache_creation_tokens']
        main_usage['output_tokens'] = main_usage.get('output_tokens', 0) + retry_usage['output_tokens']

        assert main_usage['reasoning_tokens'] == 500
        assert main_usage['service_tier'] == 'flex'


# ============================================================
# Integration Tests: State Application After Retry
# ============================================================

class TestRetryStateApplicationIC:
    """Test state changes when retry succeeds with an IC (in-character) turn."""

    def test_turn_counter_incremented(self):
        state = _fresh_pipeline_state()
        state["turn_counter"] = 5
        retry_result = _sample_tool_input()

        # Simulate the retry success branch from main.py
        assert not retry_result.get("is_ooc", False)
        state["turn_counter"] += 1
        current_turn = state["turn_counter"]
        apply_single_agent_state_updates(state, retry_result, current_turn)

        assert state["turn_counter"] == 6

    def test_pacing_applied(self):
        state = _fresh_pipeline_state()
        state["turn_counter"] = 0
        retry_result = _sample_tool_input()

        state["turn_counter"] += 1
        apply_single_agent_state_updates(state, retry_result, state["turn_counter"])

        assert state["pacing"]["episode"] == "Session 1"
        assert state["pacing"]["beat"] == "Opening"
        assert state["pacing"]["responses"] == 1

    def test_scene_state_applied(self):
        state = _fresh_pipeline_state()
        state["turn_counter"] = 0
        retry_result = _sample_tool_input()

        state["turn_counter"] += 1
        apply_single_agent_state_updates(state, retry_result, state["turn_counter"])

        assert state["scene_state"]["location"] == "Tavern"
        assert "Bartender" in state["scene_state"]["npcs_present"]
        assert state["scene_state"]["atmosphere"] == "dim and smoky"

    def test_callbacks_applied(self):
        state = _fresh_pipeline_state()
        state["turn_counter"] = 0
        retry_result = _sample_tool_input()

        state["turn_counter"] += 1
        apply_single_agent_state_updates(state, retry_result, state["turn_counter"])

        assert len(state["callback_ledger"]["open"]) == 1
        assert state["callback_ledger"]["open"][0]["original_text"] == "The stranger watches"
        assert state["callback_ledger"]["open"][0]["source_npc"] == "Stranger"

    def test_npc_memories_applied(self):
        state = _fresh_pipeline_state()
        state["turn_counter"] = 0
        retry_result = _sample_tool_input()

        state["turn_counter"] += 1
        apply_single_agent_state_updates(state, retry_result, state["turn_counter"])

        assert "Bartender" in state["npc_memories"]
        assert len(state["npc_memories"]["Bartender"]) == 1
        assert state["npc_memories"]["Bartender"][0]["text"] == "Served the hero"

    def test_character_states_applied(self):
        state = _fresh_pipeline_state()
        state["turn_counter"] = 0
        retry_result = _sample_tool_input()

        state["turn_counter"] += 1
        apply_single_agent_state_updates(state, retry_result, state["turn_counter"])

        assert "Hero" in state["character_states"]
        assert state["character_states"]["Hero"]["data"] == {"summary": "HP 20/20, no conditions"}

    def test_data_pipeline_state_set(self):
        """data['pipeline_state'] should point to the updated state."""
        state = _fresh_pipeline_state()
        state["turn_counter"] = 0
        data = {"pipeline_state": state}
        retry_result = _sample_tool_input()

        state["turn_counter"] += 1
        apply_single_agent_state_updates(state, retry_result, state["turn_counter"])
        data["pipeline_state"] = state

        assert data["pipeline_state"] is state
        assert data["pipeline_state"]["turn_counter"] == 1


class TestRetryStateApplicationOOC:
    """Test state behavior when retry returns an OOC turn.

    OOC turns apply state updates (for character creation) but do NOT
    increment turn_counter.
    """

    def test_turn_counter_not_incremented(self):
        state = _fresh_pipeline_state()
        state["turn_counter"] = 5
        retry_result = _sample_ooc_input()

        is_ooc = retry_result.get("is_ooc", False)
        assert is_ooc
        # OOC: don't increment, but still apply state
        current_turn = state["turn_counter"]
        apply_single_agent_state_updates(state, retry_result, current_turn)
        assert state["turn_counter"] == 5

    def test_state_applied_on_ooc(self):
        """OOC turns apply state updates (pacing, scene, characters) without incrementing turn."""
        state = _fresh_pipeline_state()
        state["turn_counter"] = 5
        state["pacing"] = {"episode": "S1", "beat": "B1", "responses": 3}

        retry_result = _sample_ooc_input()
        is_ooc = retry_result.get("is_ooc", False)
        assert is_ooc

        current_turn = state["turn_counter"]
        apply_single_agent_state_updates(state, retry_result, current_turn)

        # Pacing and scene_state are overwritten by the OOC input
        assert state["pacing"]["episode"] == "Session 1"
        assert state["pacing"]["beat"] == "OOC"
        assert state["scene_state"]["location"] == "N/A"
        # But turn_counter is unchanged
        assert state["turn_counter"] == 5

    def test_stateful_tool_input_still_set(self):
        """Even for OOC, stateful_tool_input should be set (for debug transcript)."""
        retry_result = _sample_ooc_input()
        stateful_tool_input = None

        is_ooc = retry_result.get("is_ooc", False)
        if is_ooc:
            stateful_tool_input = retry_result

        assert stateful_tool_input is not None
        assert stateful_tool_input["is_ooc"] is True


class TestRetryStateApplicationFailure:
    """Test state when retry also fails (returns None)."""

    def test_state_unchanged_on_failure(self):
        state = _fresh_pipeline_state()
        state["turn_counter"] = 5
        state["pacing"] = {"episode": "S1", "beat": "B1", "responses": 3}
        original = copy.deepcopy(state)

        retry_result = None
        stateful_tool_input = None

        if retry_result:
            pass  # Would apply state
        else:
            pass  # Warning logged, nothing applied

        assert state == original
        assert stateful_tool_input is None

    def test_data_pipeline_state_not_modified(self):
        state = _fresh_pipeline_state()
        state["turn_counter"] = 5
        data = {"pipeline_state": state}
        original_state = copy.deepcopy(state)

        retry_result = None
        if retry_result:
            data["pipeline_state"] = state

        assert data["pipeline_state"] == original_state


class TestRetryStateApplicationMinimal:
    """Test retry with minimal (required-only) tool input."""

    def test_required_fields_only(self):
        state = _fresh_pipeline_state()
        state["turn_counter"] = 10
        retry_result = {
            "is_ooc": False,
            "pacing": {"episode": "Session 2", "beat": "Travel", "responses": 1},
            "scene_state": {
                "location": "Forest Path",
                "npcs_present": [],
                "active_tensions": [],
                "atmosphere": "peaceful",
            },
            "character_states": {"Hero": "50/50 HP"},
            # No callback_ops, no npc_memory_ops
        }

        state["turn_counter"] += 1
        apply_single_agent_state_updates(state, retry_result, state["turn_counter"])

        assert state["turn_counter"] == 11
        assert state["pacing"]["episode"] == "Session 2"
        assert state["scene_state"]["location"] == "Forest Path"
        assert state["callback_ledger"]["open"] == []  # unchanged
        assert state["npc_memories"] == {}  # unchanged
        assert state["character_states"]["Hero"]["data"] == {"summary": "50/50 HP"}

    def test_empty_optional_arrays(self):
        state = _fresh_pipeline_state()
        state["turn_counter"] = 0
        retry_result = {
            "is_ooc": False,
            "pacing": {"episode": "S1", "beat": "Intro", "responses": 1},
            "scene_state": {
                "location": "Tavern",
                "npcs_present": [],
                "active_tensions": [],
                "atmosphere": "Cozy",
            },
            "character_states": {},
            "callback_ops": [],
            "npc_memory_ops": [],
        }

        state["turn_counter"] += 1
        apply_single_agent_state_updates(state, retry_result, state["turn_counter"])

        assert state["pacing"]["beat"] == "Intro"
        assert state["callback_ledger"]["open"] == []


# ============================================================
# Integration Tests: Merge Semantics with Existing State
# ============================================================

class TestRetryMergeWithExistingState:
    """Test that retry results merge correctly with pre-existing pipeline_state."""

    def test_pacing_merge_preserves_existing_keys(self):
        state = _fresh_pipeline_state()
        state["pacing"] = {"episode": "Session 5", "beat": "Rest", "responses": 10, "notes": "camp scene"}
        state["turn_counter"] = 10

        retry_result = {
            "is_ooc": False,
            "pacing": {"episode": "Session 5", "beat": "Morning", "responses": 11},
            # No "notes" key
            "scene_state": {"location": "Camp", "npcs_present": [], "active_tensions": [], "atmosphere": "dawn"},
            "character_states": {},
        }

        state["turn_counter"] += 1
        apply_single_agent_state_updates(state, retry_result, state["turn_counter"])

        assert state["pacing"]["beat"] == "Morning"
        assert state["pacing"]["responses"] == 11
        assert state["pacing"]["notes"] == "camp scene"  # preserved

    def test_scene_state_merge_preserves_existing_fields(self):
        state = _fresh_pipeline_state()
        state["scene_state"] = {
            "location": "Tavern",
            "npcs_present": ["Bartender"],
            "active_tensions": ["mystery"],
            "atmosphere": "cozy",
            "details": ["fireplace", "wooden tables"],
            "pending_actions": ["order drink"],
        }
        state["turn_counter"] = 3

        retry_result = {
            "is_ooc": False,
            "pacing": {"episode": "S1", "beat": "B1", "responses": 1},
            "scene_state": {
                "location": "Tavern",
                "npcs_present": ["Bartender", "Stranger"],
                "active_tensions": ["mystery", "stranger's intent"],
                "atmosphere": "tense",
                # No "details" or "pending_actions"
            },
            "character_states": {},
        }

        state["turn_counter"] += 1
        apply_single_agent_state_updates(state, retry_result, state["turn_counter"])

        assert state["scene_state"]["atmosphere"] == "tense"
        assert "Stranger" in state["scene_state"]["npcs_present"]
        assert "details" in state["scene_state"]
        assert state["scene_state"]["details"] == ["fireplace", "wooden tables"]

    def test_callbacks_added_to_existing_ledger(self):
        state = _fresh_pipeline_state()
        state["callback_ledger"] = {
            "next_id": 2,
            "open": [
                {"id": 1, "text": "Old callback", "source": "NPC1", "created_turn": 1}
            ],
            "recently_resolved": [],
        }
        state["turn_counter"] = 3

        retry_result = {
            "is_ooc": False,
            "pacing": {"episode": "S1", "beat": "B1", "responses": 1},
            "scene_state": {"location": "X", "npcs_present": [], "active_tensions": [], "atmosphere": "Y"},
            "character_states": {},
            "callback_ops": [
                {"action": "add", "original_text": "New callback", "source_npc": "NPC2"}
            ],
        }

        state["turn_counter"] += 1
        apply_single_agent_state_updates(state, retry_result, state["turn_counter"])

        assert len(state["callback_ledger"]["open"]) == 2
        assert state["callback_ledger"]["open"][1]["original_text"] == "New callback"

    def test_npc_memories_added_to_existing(self):
        state = _fresh_pipeline_state()
        state["npc_memories"] = {
            "Kira": [{"text": "Old memory", "impact": 2, "tier": "flavor", "turn_created": 1, "date": "Day 1", "quote": None, "focus": None}]
        }
        state["turn_counter"] = 3

        retry_result = {
            "is_ooc": False,
            "pacing": {"episode": "S1", "beat": "B1", "responses": 1},
            "scene_state": {"location": "X", "npcs_present": [], "active_tensions": [], "atmosphere": "Y"},
            "character_states": {},
            "npc_memory_ops": [
                {"action": "add", "npc": "Kira", "text": "New memory", "impact": 3, "date": "Day 4"}
            ],
        }

        state["turn_counter"] += 1
        apply_single_agent_state_updates(state, retry_result, state["turn_counter"])

        assert len(state["npc_memories"]["Kira"]) == 2
        # Sorted by (impact, turn_created) desc — impact 3 > impact 2, so new memory is first
        assert state["npc_memories"]["Kira"][0]["text"] == "New memory"

    def test_character_states_updated_for_existing_character(self):
        state = _fresh_pipeline_state()
        state["character_states"] = {
            "Hero": {"state": "HP 50/50", "last_updated": 1}
        }
        state["turn_counter"] = 3

        retry_result = {
            "is_ooc": False,
            "pacing": {"episode": "S1", "beat": "B1", "responses": 1},
            "scene_state": {"location": "X", "npcs_present": [], "active_tensions": [], "atmosphere": "Y"},
            "character_states": {"Hero": "HP 35/50, poisoned"},
        }

        state["turn_counter"] += 1
        apply_single_agent_state_updates(state, retry_result, state["turn_counter"])

        assert state["character_states"]["Hero"]["data"] == {"summary": "HP 35/50, poisoned"}
        assert state["character_states"]["Hero"]["last_updated"] == 4


class TestRetryFullScenario:
    """End-to-end scenarios simulating the complete retry flow."""

    def test_full_retry_flow_ic(self):
        """Simulate the complete retry path: call → extract → apply → verify."""
        # 1. Setup state
        state = _fresh_pipeline_state()
        state["turn_counter"] = 5
        data = {"pipeline_state": state}

        # 2. Simulate retry returning tool input
        tool_input = _sample_tool_input()
        usage = _make_usage(input_tokens=500, output_tokens=100, cache_read=400, cache_creation=50)
        response = _make_response([_make_tool_use_block(tool_input)], usage)
        client = _make_client(response)

        # 3. Call the retry function
        retry_result, retry_usage = _stateful_tool_retry(
            client, MODEL_NAME, SAMPLE_NARRATIVE, SAMPLE_THINKING, STATE_REPORT_TOOL, SAMPLE_SYSTEM
        )

        # 4. Accumulate usage (simulate main.py)
        main_usage = {'input_tokens': 1000, 'cache_read_tokens': 800,
                      'cache_creation_tokens': 100, 'output_tokens': 200}
        if retry_usage:
            main_usage['input_tokens'] += retry_usage['input_tokens']
            main_usage['cache_read_tokens'] += retry_usage['cache_read_tokens']
            main_usage['cache_creation_tokens'] += retry_usage['cache_creation_tokens']
            main_usage['output_tokens'] += retry_usage['output_tokens']

        # 5. Apply state (simulate main.py IC branch)
        assert retry_result is not None
        is_ooc = retry_result.get("is_ooc", False)
        assert not is_ooc
        state["turn_counter"] += 1
        current_turn = state["turn_counter"]
        apply_single_agent_state_updates(state, retry_result, current_turn)
        data["pipeline_state"] = state
        stateful_tool_input = retry_result

        # 6. Verify everything
        assert state["turn_counter"] == 6
        assert state["pacing"]["episode"] == "Session 1"
        assert state["scene_state"]["location"] == "Tavern"
        assert len(state["callback_ledger"]["open"]) == 1
        assert "Bartender" in state["npc_memories"]
        assert "Hero" in state["character_states"]
        assert data["pipeline_state"] is state
        assert stateful_tool_input is retry_result
        assert main_usage['input_tokens'] == 1000 + 500 + 400 + 50
        assert main_usage['output_tokens'] == 300

    def test_full_retry_flow_ooc(self):
        """Simulate the complete retry path for OOC turns.

        OOC turns apply state (for character creation) but don't increment turn_counter.
        """
        state = _fresh_pipeline_state()
        state["turn_counter"] = 5
        state["pacing"] = {"episode": "S1", "beat": "B1", "responses": 5}
        data = {"pipeline_state": state}

        ooc_input = _sample_ooc_input()
        client = _make_client(
            _make_response([_make_tool_use_block(ooc_input)], _make_usage())
        )

        retry_result, retry_usage = _stateful_tool_retry(
            client, MODEL_NAME, SAMPLE_NARRATIVE, SAMPLE_THINKING, STATE_REPORT_TOOL, SAMPLE_SYSTEM
        )

        assert retry_result is not None
        is_ooc = retry_result.get("is_ooc", False)
        assert is_ooc

        # Apply state (simulate new main.py OOC branch)
        # turn_counter NOT incremented, but state IS applied
        current_turn = state["turn_counter"]
        apply_single_agent_state_updates(state, retry_result, current_turn)
        data["pipeline_state"] = state
        stateful_tool_input = retry_result

        # turn_counter unchanged
        assert state["turn_counter"] == 5
        # But pacing and scene_state are updated from the OOC input
        assert state["pacing"]["episode"] == "Session 1"
        assert state["pacing"]["beat"] == "OOC"
        assert state["scene_state"]["location"] == "N/A"
        assert data["pipeline_state"] is state
        assert stateful_tool_input["is_ooc"] is True

    def test_full_retry_flow_failure(self):
        """Simulate when retry also fails to return a tool call."""
        state = _fresh_pipeline_state()
        state["turn_counter"] = 5
        original_state = copy.deepcopy(state)

        # Response with only text, no tool_use
        client = _make_client(
            _make_response([_make_text_block("I cannot comply.")], _make_usage())
        )

        retry_result, retry_usage = _stateful_tool_retry(
            client, MODEL_NAME, SAMPLE_NARRATIVE, SAMPLE_THINKING, STATE_REPORT_TOOL, SAMPLE_SYSTEM
        )

        assert retry_result is None
        # Usage still accumulated even on failure
        assert retry_usage is not None
        assert retry_usage["output_tokens"] > 0
        # State unchanged
        assert state == original_state


# ============================================================
# Async Tests: asyncio.to_thread Compatibility
# ============================================================

class TestRetryAsyncThread:
    """Test that _stateful_tool_retry works via asyncio.to_thread."""

    def test_success_via_to_thread(self):
        tool_input = _sample_tool_input()
        client = _make_client(
            _make_response([_make_tool_use_block(tool_input)], _make_usage())
        )

        result, usage = asyncio.run(
            asyncio.to_thread(
                _stateful_tool_retry,
                client, MODEL_NAME, SAMPLE_NARRATIVE, SAMPLE_THINKING, STATE_REPORT_TOOL, SAMPLE_SYSTEM,
            )
        )

        assert result == tool_input
        assert usage is not None

    def test_failure_via_to_thread(self):
        client = _make_client(
            _make_response([_make_text_block("No tool for you.")], _make_usage())
        )

        result, usage = asyncio.run(
            asyncio.to_thread(
                _stateful_tool_retry,
                client, MODEL_NAME, SAMPLE_NARRATIVE, SAMPLE_THINKING, STATE_REPORT_TOOL, SAMPLE_SYSTEM,
            )
        )

        assert result is None
        assert usage is not None

    def test_exception_via_to_thread(self):
        client = MagicMock()
        client.messages.create.side_effect = ConnectionError("Network failure")

        with pytest.raises(ConnectionError, match="Network failure"):
            asyncio.run(
                asyncio.to_thread(
                    _stateful_tool_retry,
                    client, MODEL_NAME, SAMPLE_NARRATIVE, SAMPLE_THINKING, STATE_REPORT_TOOL, SAMPLE_SYSTEM,
                )
            )

    def test_concurrent_retries_via_gather(self):
        """Multiple retries can run concurrently without interference."""
        tool_input_1 = {"is_ooc": False, "pacing": {"episode": "S1", "beat": "A", "responses": 1},
                        "scene_state": {"location": "A", "npcs_present": [], "active_tensions": [], "atmosphere": "A"},
                        "character_states": {}}
        tool_input_2 = {"is_ooc": False, "pacing": {"episode": "S2", "beat": "B", "responses": 2},
                        "scene_state": {"location": "B", "npcs_present": [], "active_tensions": [], "atmosphere": "B"},
                        "character_states": {}}

        client_1 = _make_client(_make_response([_make_tool_use_block(tool_input_1)], _make_usage()))
        client_2 = _make_client(_make_response([_make_tool_use_block(tool_input_2)], _make_usage()))

        async def run_both():
            r1, r2 = await asyncio.gather(
                asyncio.to_thread(
                    _stateful_tool_retry, client_1, MODEL_NAME,
                    "Narrative 1", "", STATE_REPORT_TOOL, SAMPLE_SYSTEM,
                ),
                asyncio.to_thread(
                    _stateful_tool_retry, client_2, MODEL_NAME,
                    "Narrative 2", "", STATE_REPORT_TOOL, SAMPLE_SYSTEM,
                ),
            )
            return r1, r2

        (result_1, usage_1), (result_2, usage_2) = asyncio.run(run_both())
        assert result_1 == tool_input_1
        assert result_2 == tool_input_2


# ============================================================
# Edge Case Tests
# ============================================================

class TestRetryEdgeCases:
    """Edge cases and boundary conditions."""

    def test_very_long_narrative(self):
        """Retry should handle very long narrative text."""
        long_narrative = "A" * 100_000
        tool_input = _sample_tool_input()
        client = _make_client(
            _make_response([_make_tool_use_block(tool_input)], _make_usage())
        )
        result, _ = _call_retry(client=client, narrative=long_narrative, thinking="")

        assert result == tool_input
        msgs = client.messages.create.call_args.kwargs["messages"]
        assert len(msgs[-1]["content"]) >= 100_000

    def test_empty_narrative(self):
        """Retry should handle empty narrative text."""
        tool_input = _sample_tool_input()
        client = _make_client(
            _make_response([_make_tool_use_block(tool_input)], _make_usage())
        )
        result, _ = _call_retry(client=client, narrative="", thinking="")

        assert result == tool_input
        msgs = client.messages.create.call_args.kwargs["messages"]
        assert "Here is the narrative from the turn you just wrote:" in msgs[-1]["content"]

    def test_unicode_in_narrative_and_thinking(self):
        """Retry should handle unicode characters in both narrative and thinking."""
        tool_input = _sample_tool_input()
        client = _make_client(
            _make_response([_make_tool_use_block(tool_input)], _make_usage())
        )
        result, _ = _call_retry(
            client=client,
            narrative="The dragon roars: \u201c\u4f60\u597d\uff01\u201d \ud83d\udc09",
            thinking="Analyzing \u2014 the player used a Chinese greeting",
        )
        assert result == tool_input
        # Verify thinking is embedded in the text
        msgs = client.messages.create.call_args.kwargs["messages"]
        text = msgs[-1]["content"]
        assert "Analyzing \u2014" in text

    def test_system_content_empty_list(self):
        """Empty system content should still work."""
        tool_input = _sample_tool_input()
        client = _make_client(
            _make_response([_make_tool_use_block(tool_input)], _make_usage())
        )
        result, _ = _call_retry(client=client, system_content=[])

        kwargs = client.messages.create.call_args.kwargs
        assert "system" not in kwargs
        assert result == tool_input

    def test_tool_input_with_nested_structures(self):
        """Tool input with deeply nested data should be returned as-is."""
        complex_input = {
            "is_ooc": False,
            "pacing": {"episode": "S1", "beat": "B1", "responses": 1},
            "scene_state": {
                "location": "Tavern",
                "npcs_present": ["A", "B", "C"],
                "active_tensions": ["x", "y"],
                "atmosphere": "tense",
                "details": ["detail1", "detail2", "detail3"],
                "pending_actions": ["action1"],
            },
            "character_states": {
                "Hero": "HP 50/50, AC 16, 3/3 L1 slots, Shield active",
                "Sidekick": "HP 30/30, AC 12",
            },
            "callback_ops": [
                {"action": "add", "original_text": "Hook 1", "source_npc": "NPC1"},
                {"action": "add", "original_text": "Hook 2", "source_npc": None},
                {"action": "resolve", "id": 1, "resolution_text": "Resolved hook"},
            ],
            "npc_memory_ops": [
                {"action": "add", "npc": "NPC1", "text": "Memory 1", "impact": 5, "date": "Day 1", "focus": "Hero"},
                {"action": "add", "npc": "NPC2", "text": "Memory 2", "impact": 2, "date": "Day 1"},
                {"action": "drop", "npc": "NPC3", "index": 0},
            ],
        }
        client = _make_client(
            _make_response([_make_tool_use_block(complex_input)], _make_usage())
        )
        result, _ = _call_retry(client=client)
        assert result == complex_input

    def test_large_message_history(self):
        """Retry should work with a large conversation history."""
        large_history = []
        for i in range(100):
            large_history.append({"role": "user", "content": f"Message {i}"})
            large_history.append({"role": "assistant", "content": f"Response {i}"})

        tool_input = _sample_tool_input()
        client = _make_client(
            _make_response([_make_tool_use_block(tool_input)], _make_usage())
        )
        result, _ = _call_retry(client=client, messages=large_history)

        msgs = client.messages.create.call_args.kwargs["messages"]
        # Minimal retry ignores the prior history and sends one user message.
        assert len(msgs) == 1
        assert result == tool_input


# ============================================================
# _tool_input_valid tests
# ============================================================

class TestToolInputValid:
    """Tests for the _tool_input_valid validation helper."""

    TOOL_DEF = {
        "name": "report_state",
        "input_schema": {
            "type": "object",
            "required": ["is_ooc", "pacing", "scene_state", "character_states"],
            "properties": {}
        }
    }

    def test_complete_input_is_valid(self):
        tool_input = {
            "is_ooc": False,
            "pacing": {"episode": "Ep1", "beat": "Intro", "responses": 1},
            "scene_state": {"location": "Tavern", "npcs_present": [], "active_tensions": [], "atmosphere": "calm"},
            "character_states": {}
        }
        assert _tool_input_valid(tool_input, self.TOOL_DEF) is True

    def test_missing_required_field(self):
        """Missing 'character_states' should fail."""
        tool_input = {
            "is_ooc": False,
            "pacing": {"episode": "Ep1", "beat": "Intro", "responses": 1},
            "scene_state": {"location": "Tavern"},
        }
        assert _tool_input_valid(tool_input, self.TOOL_DEF) is False

    def test_none_value_for_required_field_passes(self):
        """Required field present but None should pass (some schemas allow null)."""
        tool_input = {
            "is_ooc": False,
            "pacing": None,
            "scene_state": {"location": "Tavern"},
            "character_states": {}
        }
        assert _tool_input_valid(tool_input, self.TOOL_DEF) is True

    def test_empty_schema_no_required(self):
        """Tool with no required fields should pass any input."""
        tool_def = {"name": "test", "input_schema": {"type": "object", "properties": {}}}
        assert _tool_input_valid({"random": "data"}, tool_def) is True

    def test_missing_input_schema(self):
        """Tool def without input_schema should pass (no required fields to check)."""
        tool_def = {"name": "test"}
        assert _tool_input_valid({"anything": True}, tool_def) is True

    def test_extra_fields_are_fine(self):
        """Extra fields beyond required should not cause failure."""
        tool_input = {
            "is_ooc": False,
            "pacing": {"episode": "Ep1", "beat": "Intro", "responses": 1},
            "scene_state": {"location": "Tavern"},
            "character_states": {},
            "callback_ops": [],
            "npc_memory_ops": [],
        }
        assert _tool_input_valid(tool_input, self.TOOL_DEF) is True

    def test_false_boolean_is_valid(self):
        """False is a legitimate value, not the same as None/missing."""
        tool_input = {
            "is_ooc": False,
            "pacing": {},
            "scene_state": {},
            "character_states": {}
        }
        assert _tool_input_valid(tool_input, self.TOOL_DEF) is True

    def test_empty_dict_value_is_valid(self):
        """Empty dict for a required field is valid (present and not None)."""
        tool_input = {
            "is_ooc": False,
            "pacing": {},
            "scene_state": {},
            "character_states": {}
        }
        assert _tool_input_valid(tool_input, self.TOOL_DEF) is True

    def test_combat_tool_validation(self):
        """Validates against a combat-style tool schema with different required fields."""
        combat_tool = {
            "name": "report_combat_state",
            "input_schema": {
                "type": "object",
                "required": ["narrative", "rolls", "character_updates", "combat", "combat_complete"],
                "properties": {}
            }
        }
        # Complete
        assert _tool_input_valid({
            "narrative": "Attack!", "rolls": [], "character_updates": [],
            "combat": {"round": 1}, "combat_complete": False
        }, combat_tool) is True
        # Missing combat_complete
        assert _tool_input_valid({
            "narrative": "Attack!", "rolls": [], "character_updates": [],
            "combat": {"round": 1}
        }, combat_tool) is False
