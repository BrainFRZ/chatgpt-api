"""
Tests for the Notes pad refactor (formerly "Updates"):
- CONTEXT UPDATES injection is fully removed from all paths
- build_events_messages no longer accepts updates_text
- build_mechanics_messages no longer accepts updates_text
- build_single_agent_injections no longer accepts updates_text
- run_pipeline no longer accepts updates_text
- Anthropic cache breakpoint moved from second-to-last to last assistant
- Notes data is still saved/loaded (endpoints unchanged)
"""

import json
import copy
import sys
import os
from unittest.mock import MagicMock, patch
from dataclasses import dataclass
from typing import Optional, Iterator

import pytest

# Ensure backend is on sys.path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import (
    build_events_messages,
    build_mechanics_messages,
    build_single_agent_injections,
    run_pipeline,
    migrate_pipeline_state,
    _fresh_pipeline_state,
    apply_callback_ops,
    apply_npc_memory_ops,
    apply_scene_state,
    build_callback_injection,
    build_npc_memories_injection,
    build_scene_state_injection,
    build_character_states_injection,
    PipelineResult,
)
from providers.anthropic_provider import AnthropicProvider, AnthropicOpusProvider


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def fresh_state():
    return _fresh_pipeline_state()


@pytest.fixture
def populated_state():
    """A pipeline state with data in every section."""
    state = _fresh_pipeline_state()
    state["pacing"] = {"episode": "Session 5", "beat": "The Market", "responses": 3, "notes": "calm before storm"}
    state["callback_ledger"] = {
        "next_id": 3,
        "open": [
            {"id": 1, "created_turn": 2, "original_text": "Merchant will return at dawn", "source_npc": "Merchant"},
            {"id": 2, "created_turn": 4, "original_text": "Strange noise in the cellar", "source_npc": None},
        ],
        "recently_resolved": []
    }
    state["npc_memories"] = {
        "Merchant": [
            {"text": "Sold a rare gem to the party", "quote": "A fair deal.", "date": "Day 3", "impact": 3, "tier": "moderate", "turn_created": 3},
        ]
    }
    state["scene_state"] = {
        "location": "Market Square",
        "npcs_present": ["Merchant"],
        "active_tensions": ["Thieves spotted nearby"],
        "scene_trigger": "Arrived at market",
        "atmosphere": "Bustling",
        "details": ["Stalls everywhere"],
        "pending_actions": ["Buy supplies"]
    }
    state["character_states"] = {
        "Aldric": {"state": "45/45 HP, AC 16", "last_updated": 5},
    }
    state["turn_counter"] = 6
    return state


# ============================================================
# Unit Tests: build_events_messages — no CONTEXT UPDATES
# ============================================================

class TestBuildEventsMessagesNoUpdates:
    """Verify build_events_messages never produces [CONTEXT UPDATES] blocks."""

    def test_no_updates_text_param(self):
        """build_events_messages should NOT accept updates_text parameter."""
        import inspect
        sig = inspect.signature(build_events_messages)
        param_names = list(sig.parameters.keys())
        assert "updates_text" not in param_names

    def test_no_context_updates_in_output(self, populated_state):
        """Even with a fully populated state, no CONTEXT UPDATES block appears."""
        messages = build_events_messages(
            system_prompt="You are Events.",
            history_messages=[],
            user_message={"role": "user", "content": "I search the market"},
            pipeline_state=populated_state,
        )
        user_content = messages[-1]["content"]
        assert "[CONTEXT UPDATES]" not in user_content
        assert "CONTEXT UPDATES" not in user_content

    def test_other_injections_still_present(self, populated_state):
        """All other state injections should still work."""
        messages = build_events_messages(
            system_prompt="You are Events.",
            history_messages=[],
            user_message={"role": "user", "content": "action"},
            pipeline_state=populated_state,
        )
        user_content = messages[-1]["content"]
        assert "[PIPELINE STATE]" in user_content
        assert "[CALLBACK LEDGER]" in user_content
        assert "[NPC MEMORIES: Merchant]" in user_content
        assert "[SCENE STATE]" in user_content
        assert "[CHARACTER STATES]" in user_content
        assert "action" in user_content

    def test_injection_order_without_updates(self, populated_state):
        """Injection order should be: PIPELINE STATE < CALLBACK < NPC MEMORIES < SCENE < CHARACTERS < user message."""
        messages = build_events_messages(
            system_prompt="sys",
            history_messages=[],
            user_message={"role": "user", "content": "my action"},
            pipeline_state=populated_state,
        )
        user_content = messages[-1]["content"]
        idx_pipeline = user_content.index("[PIPELINE STATE]")
        idx_callback = user_content.index("[CALLBACK LEDGER]")
        idx_memories = user_content.index("[NPC MEMORIES:")
        idx_scene = user_content.index("[SCENE STATE]")
        idx_char = user_content.index("[CHARACTER STATES]")
        idx_action = user_content.index("my action")
        assert idx_pipeline < idx_callback < idx_memories < idx_scene < idx_char < idx_action

    def test_empty_state_still_clean(self, fresh_state):
        """With empty state, output includes scaffold injections plus user message."""
        messages = build_events_messages(
            system_prompt="sys",
            history_messages=[],
            user_message={"role": "user", "content": "hello"},
            pipeline_state=fresh_state,
        )
        content = messages[-1]["content"]
        # Scaffold injections for empty scene/character states are always present
        assert content.endswith("hello")
        assert "[SCENE STATE]" in content
        assert "[CHARACTER STATES]" in content

    def test_rejects_updates_text_kwarg(self, fresh_state):
        """Passing updates_text as kwarg should raise TypeError."""
        with pytest.raises(TypeError):
            build_events_messages(
                system_prompt="sys",
                history_messages=[],
                user_message={"role": "user", "content": "hello"},
                pipeline_state=fresh_state,
                updates_text="some update",
            )

    def test_game_system_injection_still_works(self, fresh_state):
        """Game-specific injection (e.g. INVESTIGATOR STATE) still works without updates."""
        game_system = {
            "build_game_injection": lambda gs: "[INVESTIGATOR STATE]\nSAN 55/65\n[/INVESTIGATOR STATE]"
        }
        messages = build_events_messages(
            system_prompt="sys",
            history_messages=[],
            user_message={"role": "user", "content": "action"},
            pipeline_state=fresh_state,
            game_system=game_system,
        )
        user_content = messages[-1]["content"]
        assert "[INVESTIGATOR STATE]" in user_content
        assert "CONTEXT UPDATES" not in user_content


# ============================================================
# Unit Tests: build_mechanics_messages — no CONTEXT UPDATES
# ============================================================

class TestBuildMechanicsMessagesNoUpdates:
    """Verify build_mechanics_messages never produces [CONTEXT UPDATES] blocks."""

    def test_no_updates_text_param(self):
        """build_mechanics_messages should NOT accept updates_text parameter."""
        import inspect
        sig = inspect.signature(build_mechanics_messages)
        param_names = list(sig.parameters.keys())
        assert "updates_text" not in param_names

    def test_only_two_messages(self):
        """Without updates, result should be exactly [system, events_json]."""
        events_json = {"route": "mechanics", "beats": ["Attack"], "character_states": {}}
        messages = build_mechanics_messages("SYSTEM PROMPT", events_json)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "SYSTEM PROMPT"
        assert messages[1]["role"] == "user"
        # Verify the user message is the Events JSON
        parsed = json.loads(messages[1]["content"])
        assert parsed["route"] == "mechanics"

    def test_no_context_updates_in_output(self):
        """No CONTEXT UPDATES should appear anywhere in the messages."""
        events_json = {"route": "mechanics", "beats": ["Explore"]}
        messages = build_mechanics_messages("sys", events_json)
        for msg in messages:
            assert "CONTEXT UPDATES" not in msg["content"]

    def test_rejects_updates_text_kwarg(self):
        """Passing updates_text as kwarg should raise TypeError."""
        with pytest.raises(TypeError):
            build_mechanics_messages("sys", {"route": "mechanics"}, updates_text="update")

    def test_time_passed_preserved(self):
        """time_passed field should still be preserved in Events JSON without updates."""
        events_json = {
            "route": "mechanics",
            "time_passed": "2 hours",
            "beats": ["Travel"],
            "character_states": {},
        }
        messages = build_mechanics_messages("sys", events_json)
        parsed = json.loads(messages[-1]["content"])
        assert parsed["time_passed"] == "2 hours"


# ============================================================
# Unit Tests: build_single_agent_injections — no CONTEXT UPDATES
# ============================================================

class TestBuildSingleAgentInjectionsNoUpdates:
    """Verify build_single_agent_injections never produces [CONTEXT UPDATES] blocks."""

    def test_no_updates_text_param(self):
        """build_single_agent_injections should NOT accept updates_text parameter."""
        import inspect
        sig = inspect.signature(build_single_agent_injections)
        param_names = list(sig.parameters.keys())
        assert "updates_text" not in param_names

    def test_no_context_updates_with_populated_state(self, populated_state):
        """Populated state should produce injections but NO CONTEXT UPDATES."""
        result = build_single_agent_injections(populated_state)
        assert "[PIPELINE STATE]" in result
        assert "[CALLBACK LEDGER]" in result
        assert "[NPC MEMORIES: Merchant]" in result
        assert "[SCENE STATE]" in result
        assert "[CHARACTER STATES]" in result
        assert "CONTEXT UPDATES" not in result

    def test_empty_state_returns_scaffold(self, fresh_state):
        """Fresh state with no data should return scaffold injections for scene/character bootstrap."""
        result = build_single_agent_injections(fresh_state)
        assert "[SCENE STATE]" in result
        assert "[CHARACTER STATES]" in result

    def test_rejects_updates_text_kwarg(self, fresh_state):
        """Passing updates_text as kwarg should raise TypeError."""
        with pytest.raises(TypeError):
            build_single_agent_injections(fresh_state, updates_text="some update")

    def test_game_system_injection_works(self, fresh_state):
        """Game-specific injection still works without updates."""
        game_system = {
            "build_game_injection": lambda gs: "[INVESTIGATOR STATE]\nSAN 55\n[/INVESTIGATOR STATE]"
        }
        result = build_single_agent_injections(fresh_state, game_system=game_system)
        assert "[INVESTIGATOR STATE]" in result
        assert "CONTEXT UPDATES" not in result

    def test_hud_state_injection_works(self, populated_state):
        """HUD state injection (if present) still works without updates."""
        populated_state["hud_state"] = {"time": "Day 3, Morning", "location": "Market"}
        result = build_single_agent_injections(populated_state)
        assert "[HUD" in result  # HUD injection present
        assert "CONTEXT UPDATES" not in result


# ============================================================
# Unit Tests: run_pipeline — no updates_text parameter
# ============================================================

class TestRunPipelineNoUpdates:
    """Verify run_pipeline no longer accepts updates_text."""

    def test_no_updates_text_param(self):
        """run_pipeline should NOT accept updates_text parameter."""
        import inspect
        sig = inspect.signature(run_pipeline)
        param_names = list(sig.parameters.keys())
        assert "updates_text" not in param_names

    def test_rejects_updates_text_kwarg(self):
        """Passing updates_text as kwarg should raise TypeError."""
        with pytest.raises(TypeError):
            # This should fail at call time, not at execution
            # We need to provide enough args to get past initial validation
            run_pipeline(
                provider=MagicMock(),
                client=MagicMock(),
                username="test",
                project="proj",
                chat_name="chat",
                branch_path=[{"role": "system", "content": "sys"}],
                agent_instructions={"events": "", "mechanics": "", "narration": ""},
                agent_files={"events": "", "mechanics": "", "narration": ""},
                pipeline_state=None,
                updates_text="should fail",
            )


# ============================================================
# Unit Tests: Anthropic cache breakpoint — last assistant
# ============================================================

class TestAnthropicCacheBreakpointLastAssistant:
    """Verify cache breakpoint is now on LAST assistant message."""

    @pytest.fixture
    def provider(self):
        return AnthropicProvider()

    def _get_cache_breakpoint_indices(self, params):
        """Extract indices of messages with cache_control in converted messages."""
        indices = []
        for i, msg in enumerate(params["messages"]):
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("cache_control"):
                        indices.append(i)
                        break
        return indices

    def test_three_assistants_breakpoint_on_last(self, provider):
        """With 3 assistant messages, breakpoint should be on the last one."""
        messages = [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "2"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "3"},
            {"role": "assistant", "content": "A3"},  # <-- breakpoint here (index 5)
            {"role": "user", "content": "4"},
        ]
        params = provider.build_request(
            messages=messages,
            username="user",
            project=None,
            chat_name="chat",
            is_free_chat=False,
        )
        bp_indices = self._get_cache_breakpoint_indices(params)
        # Last assistant is at index 5 in params["messages"]
        assert 5 in bp_indices

    def test_two_assistants_breakpoint_on_last(self, provider):
        """With 2 assistant messages, breakpoint should be on the second (last)."""
        messages = [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "2"},
            {"role": "assistant", "content": "A2"},  # <-- breakpoint here (index 3)
            {"role": "user", "content": "3"},
        ]
        params = provider.build_request(
            messages=messages,
            username="user",
            project=None,
            chat_name="chat",
            is_free_chat=False,
        )
        bp_indices = self._get_cache_breakpoint_indices(params)
        assert 3 in bp_indices

    def test_one_assistant_now_gets_breakpoint(self, provider):
        """With only 1 assistant message, it should NOW get a cache breakpoint."""
        messages = [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "A1"},  # <-- breakpoint here (index 1)
            {"role": "user", "content": "2"},
        ]
        params = provider.build_request(
            messages=messages,
            username="user",
            project=None,
            chat_name="chat",
            is_free_chat=False,
        )
        bp_indices = self._get_cache_breakpoint_indices(params)
        assert len(bp_indices) == 1
        assert 1 in bp_indices

    def test_zero_assistants_no_breakpoint(self, provider):
        """With 0 assistant messages, no cache breakpoint should be set."""
        messages = [
            {"role": "user", "content": "Hello"},
        ]
        params = provider.build_request(
            messages=messages,
            username="user",
            project=None,
            chat_name="chat",
            is_free_chat=False,
        )
        bp_indices = self._get_cache_breakpoint_indices(params)
        assert len(bp_indices) == 0

    def test_cache_off_no_breakpoints(self, provider):
        """With use_cache=False, no breakpoints should be set."""
        messages = [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "2"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "3"},
        ]
        params = provider.build_request(
            messages=messages,
            username="user",
            project=None,
            chat_name="chat",
            is_free_chat=False,
            use_cache=False,
        )
        bp_indices = self._get_cache_breakpoint_indices(params)
        assert len(bp_indices) == 0

    def test_opus_provider_same_behavior(self):
        """OpusProvider should also place breakpoint on last assistant."""
        provider = AnthropicOpusProvider()
        messages = [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "2"},
            {"role": "assistant", "content": "A2"},  # last assistant (index 3)
            {"role": "user", "content": "3"},
        ]
        params = provider.build_request(
            messages=messages,
            username="user",
            project=None,
            chat_name="chat",
            is_free_chat=False,
        )
        bp_indices = self._get_cache_breakpoint_indices(params)
        assert 3 in bp_indices


# ============================================================
# Integration: No CONTEXT UPDATES anywhere in pipeline output
# ============================================================

class TestNoContextUpdatesInPipelineIntegration:
    """
    End-to-end: run a mocked pipeline and verify no CONTEXT UPDATES
    appear in any stage's messages.
    """

    def _make_mock_provider(self):
        """Create a mock provider that returns valid pipeline stage responses."""
        provider = MagicMock()
        provider.model_id = "gpt-5.2"

        events_response = {
            "route": "narration",
            "pacing": {"episode": "Ep1", "beat": "B1", "beat_responses": 1, "notes": ""},
            "beats": ["The hero enters"],
            "player_action": "enter the room",
            "callbacks": [],
            "emotional_context": "neutral",
            "character_states": {},
            "score_changes": [],
        }

        @dataclass
        class StageResult:
            content: str
            reasoning: Optional[str]
            parsed_json: dict
            input_tokens: int = 100
            cache_read_tokens: int = 0
            cache_creation_tokens: int = 0
            output_tokens: int = 50
            reasoning_tokens: int = 0

        provider.send_request_non_streaming = MagicMock(return_value={
            "content": json.dumps(events_response),
            "reasoning": None,
            "input_tokens": 100,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "output_tokens": 50,
            "reasoning_tokens": 0,
        })

        @dataclass
        class StreamEvent:
            event_type: str
            content: str = ""
            reasoning: str = ""
            usage: dict = None

        provider.send_request_stream = MagicMock(return_value=iter([
            StreamEvent(event_type="content_delta", content="The hero enters the dark room."),
            StreamEvent(event_type="done", usage={
                "content": "The hero enters the dark room.",
                "input_tokens": 100,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "output_tokens": 20,
                "reasoning_tokens": 0,
            })
        ]))

        provider.build_pipeline_request = MagicMock(return_value={"messages": []})
        provider.calculate_cost = MagicMock(return_value=0.001)

        return provider

    def test_pipeline_no_context_updates_in_events_messages(self):
        """Capture Events messages and verify no CONTEXT UPDATES."""
        provider = self._make_mock_provider()
        branch_path = [
            {"id": "sys", "role": "system", "content": "You are the narrator."},
            {"id": "u1", "role": "user", "content": "Hello", "parent_id": "sys"},
            {"id": "a1", "role": "assistant", "content": "Hi there!", "parent_id": "u1"},
            {"id": "u2", "role": "user", "content": "I enter the room", "parent_id": "a1"},
        ]

        captured_events_msgs = []
        original_build = build_events_messages

        def capturing_build(*args, **kwargs):
            result = original_build(*args, **kwargs)
            captured_events_msgs.extend(result)
            return result

        with patch("pipeline.build_events_messages", side_effect=capturing_build):
            events = list(run_pipeline(
                provider=provider,
                client=MagicMock(),
                username="test",
                project="testproj",
                chat_name="testchat",
                branch_path=branch_path,
                agent_instructions={"events": "", "mechanics": "", "narration": ""},
                agent_files={"events": "", "mechanics": "", "narration": ""},
                pipeline_state=None,
            ))

        # Verify no [CONTEXT UPDATES] injection block in user messages
        # (system prompts may mention the term in contract text — that's fine)
        user_msgs = [m for m in captured_events_msgs if m["role"] == "user"]
        for msg in user_msgs:
            assert "[CONTEXT UPDATES]" not in msg["content"], \
                f"Found [CONTEXT UPDATES] injection in Events user message: {msg['content'][:200]}"

    def test_pipeline_no_context_updates_in_mechanics_messages(self):
        """Capture Mechanics messages and verify no CONTEXT UPDATES."""
        provider = self._make_mock_provider()

        # Make Events return "mechanics" route so Mechanics stage runs
        mechanics_events_response = {
            "route": "mechanics",
            "pacing": {"episode": "Ep1", "beat": "B1", "beat_responses": 1, "notes": ""},
            "beats": ["Attack!"],
            "player_action": "I attack",
            "callbacks": [],
            "emotional_context": "intense",
            "character_states": {"Hero": "50/50 HP"},
            "score_changes": [],
        }
        mechanics_response = {
            "route": "narration",
            "mechanics_text": "Roll: 15+3=18 (hit)",
            "character_states": {"Hero": "50/50 HP"},
            "hud_state": {},
        }

        call_count = [0]
        def mock_non_streaming(cl, params):
            call_count[0] += 1
            if call_count[0] == 1:
                content = json.dumps(mechanics_events_response)
            else:
                content = json.dumps(mechanics_response)
            return {
                "content": content, "reasoning": None,
                "input_tokens": 100, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "output_tokens": 50, "reasoning_tokens": 0,
            }

        provider.send_request_non_streaming = mock_non_streaming

        branch_path = [
            {"id": "sys", "role": "system", "content": "You are the narrator."},
            {"id": "u1", "role": "user", "content": "I attack the guard", "parent_id": "sys"},
        ]

        captured_mechanics_msgs = []
        original_build = build_mechanics_messages

        def capturing_build(*args, **kwargs):
            result = original_build(*args, **kwargs)
            captured_mechanics_msgs.extend(result)
            return result

        with patch("pipeline.build_mechanics_messages", side_effect=capturing_build):
            events = list(run_pipeline(
                provider=provider,
                client=MagicMock(),
                username="test",
                project="testproj",
                chat_name="testchat",
                branch_path=branch_path,
                agent_instructions={"events": "", "mechanics": "", "narration": ""},
                agent_files={"events": "", "mechanics": "", "narration": ""},
                pipeline_state=None,
            ))

        # Verify no [CONTEXT UPDATES] injection block in user messages
        user_msgs = [m for m in captured_mechanics_msgs if m["role"] == "user"]
        for msg in user_msgs:
            assert "[CONTEXT UPDATES]" not in msg["content"], \
                f"Found [CONTEXT UPDATES] injection in Mechanics user message: {msg['content'][:200]}"

        # Also verify Mechanics only has 2 messages (system + events_json, no updates msg)
        assert len(captured_mechanics_msgs) == 2


# ============================================================
# Integration: Notes endpoints still work (data saved/loaded)
# ============================================================

class TestNotesEndpointsStillWork:
    """Verify save-updates and get-updates endpoints still function."""

    @pytest.fixture
    def setup_app(self, tmp_path, monkeypatch):
        """Set up test app with temp data directory."""
        import main
        monkeypatch.setattr(main, 'DATA_DIR', tmp_path)

        # Create test user directory and chat
        user_dir = tmp_path / "testuser"
        user_dir.mkdir()
        chat_data = {
            "messages": [
                {"id": "sys", "role": "system", "content": "Hello"},
            ],
            "stats": {"prompt_count": 0, "total_input_tokens": 0, "total_cache_read_tokens": 0,
                       "total_output_tokens": 0, "total_reasoning_tokens": 0, "total_cost": 0},
            "current_leaf_id": "sys",
            "updates": ""
        }
        (user_dir / "chat_test_chat.json").write_text(json.dumps(chat_data))

        from fastapi.testclient import TestClient
        return TestClient(main.app)

    def test_save_updates_endpoint(self, setup_app):
        """POST /api/save-updates should still save notes text."""
        client = setup_app
        resp = client.post("/api/save-updates", json={
            "username": "testuser",
            "chat_name": "test_chat",
            "updates": "My personal notes here"
        })
        assert resp.status_code == 200

    def test_get_updates_endpoint(self, setup_app):
        """GET /api/updates/{username}/{chat_name} should return saved notes."""
        client = setup_app
        # Save first
        client.post("/api/save-updates", json={
            "username": "testuser",
            "chat_name": "test_chat",
            "updates": "Notes content here"
        })
        # Load
        resp = client.get("/api/updates/testuser/test_chat")
        assert resp.status_code == 200
        data = resp.json()
        assert data["updates"] == "Notes content here"


# ============================================================
# Regression: main.py has no updates_text references
# ============================================================

class TestMainPyCleanup:
    """Verify main.py has been fully cleaned of updates injection code."""

    def test_no_updates_text_variable(self):
        """main.py should not contain any 'updates_text' references."""
        main_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
        with open(main_path, "r") as f:
            content = f.read()
        assert "updates_text" not in content

    def test_no_context_updates_literal(self):
        """main.py should not contain any 'CONTEXT UPDATES' string literals."""
        main_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
        with open(main_path, "r") as f:
            content = f.read()
        assert "CONTEXT UPDATES" not in content

    def test_no_add_updates_import(self):
        """main.py should not import add_updates_to_messages."""
        main_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
        with open(main_path, "r") as f:
            content = f.read()
        assert "add_updates_to_messages" not in content
        assert "anthropic_add_updates" not in content
        assert "openai_add_updates" not in content


# ============================================================
# Regression: pipeline.py has no updates_text references
# ============================================================

class TestPipelinePyCleanup:
    """Verify pipeline.py has been fully cleaned of updates injection code."""

    def test_no_updates_text_variable(self):
        """pipeline.py should not contain any 'updates_text' references."""
        pipeline_path = os.path.join(os.path.dirname(__file__), "..", "pipeline.py")
        with open(pipeline_path, "r") as f:
            content = f.read()
        assert "updates_text" not in content

    def test_no_context_updates_literal(self):
        """pipeline.py should not contain any 'CONTEXT UPDATES' string literals."""
        pipeline_path = os.path.join(os.path.dirname(__file__), "..", "pipeline.py")
        with open(pipeline_path, "r") as f:
            content = f.read()
        assert "CONTEXT UPDATES" not in content


# ============================================================
# Run tests
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
