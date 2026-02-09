"""
Tests for Pipeline Phase 3: Frontend Pipeline Progress UI (backend components).

Tests cover:
1. SyncEventType.PIPELINE_STAGE enum value
2. pipeline_stage SSE events emitted during streaming
3. pipeline_stage sync broadcasts to other clients
4. Full pipeline flow: Events → Mechanics → Narration with correct stage events
5. Short-circuit flow: Events → Output with correct stage events
6. Stage event data shape: {stage, status}
"""

import os
import sys
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, AsyncMock
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

import main
from sync_manager import SyncEventType, SyncEvent, SyncManager
from pipeline import (
    run_pipeline, PipelineResult, PipelineStageResult,
    STAGE_CONFIGS, build_agent_system_prompt,
    EVENTS_CONTRACT, MECHANICS_CONTRACT, NARRATION_CONTRACT
)
from providers.openai_provider import OpenAIProvider

AGENT_NAMES = ["events", "mechanics", "narration"]


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def temp_data_dir(monkeypatch):
    """Create a temporary data directory and patch main.DATA_DIR."""
    temp_dir = tempfile.mkdtemp()
    monkeypatch.setattr(main, 'DATA_DIR', Path(temp_dir))
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def client(temp_data_dir):
    """Create test client with isolated data directory."""
    return TestClient(main.app)


@pytest.fixture
def pipeline_chat(temp_data_dir):
    """Create a test user with API key, project, and chat for pipeline testing."""
    username = "testuser"
    project = "testproject"
    chat_name = "testchat"

    # Create user directory
    user_dir = Path(temp_data_dir) / username
    user_dir.mkdir(parents=True, exist_ok=True)

    # Create API key
    api_keys = {"openai": "sk-fake-test-key", "anthropic": ""}
    with open(user_dir / "api_keys.json", 'w') as f:
        json.dump(api_keys, f)

    # Create project directory
    project_dir = user_dir / "projects" / project
    project_dir.mkdir(parents=True, exist_ok=True)

    # Project metadata
    metadata = {"model": "gpt-5.2", "last_accessed": "2025-01-01T00:00:00"}
    with open(project_dir / "metadata.json", 'w') as f:
        json.dump(metadata, f)

    # Shared instructions
    with open(project_dir / "instructions.di", 'w') as f:
        f.write("You are a TTRPG game master.")

    # Create uploads dir with a file
    uploads_dir = project_dir / "uploads"
    uploads_dir.mkdir(exist_ok=True)
    with open(uploads_dir / "rules.md", 'w') as f:
        f.write("# Rules\nBasic game rules.")

    tokens_cache = {
        "rules.md": {
            "tokens": 50, "model": "gpt-5.2", "size_bytes": 25,
            "mtime": 1000, "staged": True,
            "agents": ["events", "mechanics", "narration"]
        }
    }
    with open(project_dir / "file_tokens.json", 'w') as f:
        json.dump(tokens_cache, f)

    # Create chat with system message and one exchange
    chat_data = {
        "messages": [
            {
                "id": "sys-001",
                "role": "system",
                "content": "You are a TTRPG game master.\n\n====FILE: rules.md====\n# Rules\nBasic game rules.\n====END FILE====",
                "total_tokens": 50
            },
            {
                "id": "user-001",
                "parent_id": "sys-001",
                "role": "user",
                "content": "I enter the tavern.",
                "timestamp": "2025-01-01T00:00:00",
                "total_tokens": 10
            },
            {
                "id": "asst-001",
                "parent_id": "user-001",
                "role": "assistant",
                "content": "The tavern is warm and inviting.",
                "timestamp": "2025-01-01T00:00:01",
                "total_tokens": 15
            }
        ],
        "model": "gpt-5.2",
        "current_leaf_id": "asst-001",
        "stats": {
            "total_messages": 2,
            "total_tokens": 75,
            "total_cost": 0.001,
            "last_accessed": "2025-01-01T00:00:01"
        }
    }

    with open(project_dir / f"chat_{chat_name}.json", 'w') as f:
        json.dump(chat_data, f)

    return {
        "username": username,
        "project": project,
        "chat_name": chat_name,
        "project_dir": str(project_dir),
    }


# ============================================================
# 1. SyncEventType.PIPELINE_STAGE Tests
# ============================================================

class TestPipelineStageEnum:
    """Tests for the PIPELINE_STAGE sync event type."""

    def test_pipeline_stage_exists(self):
        """PIPELINE_STAGE should be a valid SyncEventType."""
        assert hasattr(SyncEventType, 'PIPELINE_STAGE')
        assert SyncEventType.PIPELINE_STAGE.value == "pipeline_stage"

    def test_pipeline_stage_serializes_correctly(self):
        """SyncEvent with PIPELINE_STAGE should serialize to correct JSON."""
        event = SyncEvent(
            type=SyncEventType.PIPELINE_STAGE,
            data={"stage": "events", "status": "thinking"}
        )
        parsed = json.loads(event.to_json())
        assert parsed["type"] == "pipeline_stage"
        assert parsed["data"]["stage"] == "events"
        assert parsed["data"]["status"] == "thinking"

    def test_all_stage_statuses_serialize(self):
        """All pipeline stage statuses should serialize correctly."""
        for stage in ["events", "mechanics", "narration"]:
            for status in ["thinking", "complete", "streaming"]:
                event = SyncEvent(
                    type=SyncEventType.PIPELINE_STAGE,
                    data={"stage": stage, "status": status}
                )
                parsed = json.loads(event.to_json())
                assert parsed["data"]["stage"] == stage
                assert parsed["data"]["status"] == status


# ============================================================
# 2. Pipeline run_pipeline() stage event tests
# ============================================================

class TestPipelineStageEvents:
    """Tests for pipeline_stage events yielded by run_pipeline()."""

    def _make_mock_provider(self):
        """Create a mock OpenAI provider for pipeline testing."""
        provider = Mock(spec=OpenAIProvider)
        provider.build_pipeline_request.return_value = {"mock": True}
        provider.calculate_cost_with_tier.return_value = 0.001
        return provider

    def _make_events_response(self, route="mechanics"):
        """Create a mock Events stage response."""
        if route == "mechanics":
            content = json.dumps({
                "route": "mechanics",
                "pacing": {"episode": "Test", "beat": "Tavern", "beat_responses": 1, "notes": ""},
                "time_passed": "1 minute",
                "beats": ["Player enters tavern"],
                "player_action": "Enter the tavern",
                "callbacks": [],
                "emotional_context": "Calm exploration",
                "character_states": {"Player": "Full HP"},
                "score_changes": []
            })
        else:
            content = json.dumps({
                "route": "output",
                "pacing": {"episode": "Test", "beat": "OOC", "beat_responses": 0, "notes": ""},
                "time_passed": "0 minutes",
                "content": "Sure, let's take a break!"
            })
        return {
            'content': content,
            'reasoning': None,
            'input_tokens': 100,
            'cache_read_tokens': 0,
            'cache_creation_tokens': 0,
            'output_tokens': 50,
            'reasoning_tokens': 0
        }

    def _make_mechanics_response(self, route="narration"):
        """Create a mock Mechanics stage response."""
        if route == "narration":
            content = json.dumps({
                "route": "narration",
                "beats": [{"beat": "Enter tavern", "outcome": "Success", "rolls": [], "state_changes": []}],
                "dramatic_notes": "Warm atmosphere",
                "hud": "[Date: Day 1 | Time: 1800 | Loc: Tavern | Funds: 50gp]",
                "score_changes": []
            })
        else:
            content = json.dumps({
                "route": "output",
                "content": "Your AC is 15."
            })
        return {
            'content': content,
            'reasoning': None,
            'input_tokens': 80,
            'cache_read_tokens': 0,
            'cache_creation_tokens': 0,
            'output_tokens': 60,
            'reasoning_tokens': 0
        }

    def _make_narration_stream_events(self):
        """Create mock streaming events for Narration."""
        events = []
        for delta in ["The tavern ", "is warm ", "and inviting."]:
            evt = Mock()
            evt.event_type = 'content_delta'
            evt.content = delta
            events.append(evt)
        done_evt = Mock()
        done_evt.event_type = 'done'
        done_evt.usage = {
            'content': 'The tavern is warm and inviting.',
            'reasoning': None,
            'input_tokens': 60,
            'cache_read_tokens': 0,
            'cache_creation_tokens': 0,
            'output_tokens': 20,
            'reasoning_tokens': 0
        }
        events.append(done_evt)
        return events

    def test_full_pipeline_emits_all_stage_events(self):
        """Full pipeline (Events→Mechanics→Narration) should emit correct stage events."""
        provider = self._make_mock_provider()
        client = Mock()

        call_count = [0]
        def mock_non_streaming(c, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return self._make_events_response("mechanics")
            else:
                return self._make_mechanics_response("narration")

        provider.send_request_non_streaming.side_effect = mock_non_streaming
        provider.send_request_stream.return_value = self._make_narration_stream_events()

        events = list(run_pipeline(
            provider=provider,
            client=client,
            username="test",
            project="proj",
            chat_name="chat",
            branch_path=[
                {"role": "system", "content": "System prompt", "id": "sys"},
                {"role": "user", "content": "Hello", "id": "usr"}
            ],
            context_start_index=1,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=None
        ))

        # Extract just pipeline_stage events
        stage_events = [(t, d) for t, d in events if t == "pipeline_stage"]

        assert len(stage_events) == 7
        assert stage_events[0] == ("pipeline_stage", {"stage": "events", "status": "thinking"})
        assert stage_events[1] == ("pipeline_stage", {"stage": "events", "status": "complete"})
        assert stage_events[2] == ("pipeline_stage", {"stage": "mechanics", "status": "thinking"})
        assert stage_events[3] == ("pipeline_stage", {"stage": "mechanics", "status": "complete"})
        assert stage_events[4] == ("pipeline_stage", {"stage": "narration", "status": "thinking"})
        assert stage_events[5] == ("pipeline_stage", {"stage": "narration", "status": "streaming"})
        assert stage_events[6] == ("pipeline_stage", {"stage": "narration", "status": "complete"})

    def test_short_circuit_events_to_output(self):
        """Short-circuit (Events→Output) should only emit events stage events."""
        provider = self._make_mock_provider()
        client = Mock()

        provider.send_request_non_streaming.return_value = self._make_events_response("output")

        events = list(run_pipeline(
            provider=provider,
            client=client,
            username="test",
            project="proj",
            chat_name="chat",
            branch_path=[
                {"role": "system", "content": "System prompt", "id": "sys"},
                {"role": "user", "content": "Can we take a break?", "id": "usr"}
            ],
            context_start_index=1,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=None
        ))

        stage_events = [(t, d) for t, d in events if t == "pipeline_stage"]
        assert len(stage_events) == 2
        assert stage_events[0] == ("pipeline_stage", {"stage": "events", "status": "thinking"})
        assert stage_events[1] == ("pipeline_stage", {"stage": "events", "status": "complete"})

        # Should also have content and pipeline_done
        content_events = [d for t, d in events if t == "content"]
        assert len(content_events) == 1
        assert content_events[0]["delta"] == "Sure, let's take a break!"

    def test_short_circuit_mechanics_to_output(self):
        """Short-circuit (Mechanics→Output) should emit events + mechanics stage events."""
        provider = self._make_mock_provider()
        client = Mock()

        call_count = [0]
        def mock_non_streaming(c, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return self._make_events_response("mechanics")
            else:
                return self._make_mechanics_response("output")

        provider.send_request_non_streaming.side_effect = mock_non_streaming

        events = list(run_pipeline(
            provider=provider,
            client=client,
            username="test",
            project="proj",
            chat_name="chat",
            branch_path=[
                {"role": "system", "content": "System prompt", "id": "sys"},
                {"role": "user", "content": "What's my AC?", "id": "usr"}
            ],
            context_start_index=1,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=None
        ))

        stage_events = [(t, d) for t, d in events if t == "pipeline_stage"]
        assert len(stage_events) == 4
        assert stage_events[0] == ("pipeline_stage", {"stage": "events", "status": "thinking"})
        assert stage_events[1] == ("pipeline_stage", {"stage": "events", "status": "complete"})
        assert stage_events[2] == ("pipeline_stage", {"stage": "mechanics", "status": "thinking"})
        assert stage_events[3] == ("pipeline_stage", {"stage": "mechanics", "status": "complete"})

    def test_content_events_interleaved_with_stage_events(self):
        """Content deltas should come after narration streaming status."""
        provider = self._make_mock_provider()
        client = Mock()

        call_count = [0]
        def mock_non_streaming(c, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return self._make_events_response("mechanics")
            else:
                return self._make_mechanics_response("narration")

        provider.send_request_non_streaming.side_effect = mock_non_streaming
        provider.send_request_stream.return_value = self._make_narration_stream_events()

        events = list(run_pipeline(
            provider=provider,
            client=client,
            username="test",
            project="proj",
            chat_name="chat",
            branch_path=[
                {"role": "system", "content": "System prompt", "id": "sys"},
                {"role": "user", "content": "Hello", "id": "usr"}
            ],
            context_start_index=1,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=None
        ))

        # Find the indices of narration streaming and first content
        event_types = [t for t, _ in events]
        narration_streaming_idx = None
        first_content_idx = None
        for i, (t, d) in enumerate(events):
            if t == "pipeline_stage" and d.get("stage") == "narration" and d.get("status") == "streaming":
                narration_streaming_idx = i
            if t == "content" and first_content_idx is None:
                first_content_idx = i

        assert narration_streaming_idx is not None
        assert first_content_idx is not None
        # Streaming status must come before first content
        assert narration_streaming_idx < first_content_idx

    def test_pipeline_done_is_last_event(self):
        """pipeline_done should be the last event yielded."""
        provider = self._make_mock_provider()
        client = Mock()

        call_count = [0]
        def mock_non_streaming(c, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return self._make_events_response("mechanics")
            else:
                return self._make_mechanics_response("narration")

        provider.send_request_non_streaming.side_effect = mock_non_streaming
        provider.send_request_stream.return_value = self._make_narration_stream_events()

        events = list(run_pipeline(
            provider=provider,
            client=client,
            username="test",
            project="proj",
            chat_name="chat",
            branch_path=[
                {"role": "system", "content": "System prompt", "id": "sys"},
                {"role": "user", "content": "Hello", "id": "usr"}
            ],
            context_start_index=1,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=None
        ))

        last_event_type, last_event_data = events[-1]
        assert last_event_type == "pipeline_done"
        assert isinstance(last_event_data, PipelineResult)

    def test_stage_event_data_shape(self):
        """All pipeline_stage events should have exactly {stage, status} keys."""
        provider = self._make_mock_provider()
        client = Mock()

        provider.send_request_non_streaming.return_value = self._make_events_response("output")

        events = list(run_pipeline(
            provider=provider,
            client=client,
            username="test",
            project="proj",
            chat_name="chat",
            branch_path=[
                {"role": "system", "content": "System prompt", "id": "sys"},
                {"role": "user", "content": "Test", "id": "usr"}
            ],
            context_start_index=1,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=None
        ))

        for event_type, event_data in events:
            if event_type == "pipeline_stage":
                assert set(event_data.keys()) == {"stage", "status"}
                assert event_data["stage"] in AGENT_NAMES
                assert event_data["status"] in ["thinking", "complete", "streaming"]


# ============================================================
# 3. SSE endpoint pipeline_stage + sync broadcast tests
# ============================================================

class TestPipelineStageBroadcast:
    """Tests for pipeline_stage sync broadcasts from the streaming endpoint."""

    def test_pipeline_stage_broadcast_in_event_generator(self, temp_data_dir, client, pipeline_chat):
        """The streaming endpoint should broadcast pipeline_stage events via sync_manager."""
        pc = pipeline_chat

        broadcast_calls = []
        original_broadcast = SyncManager.broadcast_to_chat

        async def capture_broadcast(self, chat_key, event, exclude_connection_id=None):
            broadcast_calls.append({
                "chat_key": chat_key,
                "type": event.type.value,
                "data": event.data
            })
            return 0

        # Mock the pipeline to emit stage events, content, and done
        fake_events = [
            ("pipeline_stage", {"stage": "events", "status": "thinking"}),
            ("pipeline_stage", {"stage": "events", "status": "complete"}),
            ("content", {"delta": "Hello!"}),
            ("pipeline_done", PipelineResult(
                final_content="Hello!",
                events_json="{}",
                mechanics_json=None,
                stages_run=["events"],
                aggregate_usage={"input_tokens": 100, "cache_read_tokens": 0,
                                 "cache_creation_tokens": 0, "output_tokens": 50,
                                 "reasoning_tokens": 0},
                aggregate_cost=0.001,
                pipeline_state={"episode": "Test"},
                reasoning_summaries=[],
                service_tier_label="flex"
            ))
        ]

        with patch.object(SyncManager, 'broadcast_to_chat', capture_broadcast):
            with patch('main.run_pipeline', return_value=iter(fake_events)):
                response = client.post("/api/send-message-stream", json={
                    "username": pc["username"],
                    "chat_name": pc["chat_name"],
                    "message": "I look around.",
                    "project": pc["project"],
                    "model": "gpt-5.2"
                })

        # Parse SSE events from response
        sse_text = response.text
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {sse_text[:500]}"
        sse_events = []
        evt_type = None
        for line in sse_text.split('\n'):
            if line.startswith('event: '):
                evt_type = line[7:]
            elif line.startswith('data: ') and evt_type:
                try:
                    sse_events.append((evt_type, json.loads(line[6:])))
                except json.JSONDecodeError:
                    pass
                evt_type = None

        # Check that pipeline_stage SSE events were emitted
        pipeline_sse = [(t, d) for t, d in sse_events if t == "pipeline_stage"]
        assert len(pipeline_sse) == 2, f"Expected 2 pipeline_stage SSE events, got {len(pipeline_sse)}. All events: {[(t, d) for t, d in sse_events]}"
        assert pipeline_sse[0][1] == {"stage": "events", "status": "thinking"}
        assert pipeline_sse[1][1] == {"stage": "events", "status": "complete"}

        # Check that sync broadcasts were made for pipeline_stage
        pipeline_broadcasts = [c for c in broadcast_calls if c["type"] == "pipeline_stage"]
        assert len(pipeline_broadcasts) == 2
        assert pipeline_broadcasts[0]["data"] == {"stage": "events", "status": "thinking"}
        assert pipeline_broadcasts[1]["data"] == {"stage": "events", "status": "complete"}

    def test_non_pipeline_chat_no_stage_events(self, temp_data_dir, client, pipeline_chat):
        """A non-pipeline request (no project) should not emit pipeline_stage events."""
        pc = pipeline_chat

        # Create a non-project chat
        chat_data = {
            "messages": [
                {"id": "sys-001", "role": "system", "content": "Hello", "total_tokens": 5},
            ],
            "model": "gpt-5.2",
            "current_leaf_id": "sys-001",
            "stats": {"total_messages": 0, "total_tokens": 5, "total_cost": 0, "last_accessed": "2025-01-01T00:00:00"}
        }
        user_dir = Path(temp_data_dir) / pc["username"]
        with open(user_dir / "chat_freechat.json", 'w') as f:
            json.dump(chat_data, f)

        # Mock the streaming to return a simple response
        mock_stream_event_content = Mock()
        mock_stream_event_content.event_type = 'content_delta'
        mock_stream_event_content.content = 'Hi there!'

        mock_stream_event_done = Mock()
        mock_stream_event_done.event_type = 'done'
        mock_stream_event_done.usage = {
            'content': 'Hi there!',
            'reasoning': None,
            'input_tokens': 10,
            'cache_read_tokens': 0,
            'cache_creation_tokens': 0,
            'output_tokens': 5,
            'reasoning_tokens': 0,
            'service_tier': 'flex'
        }

        with patch.object(OpenAIProvider, 'send_request_stream_with_fallback',
                          return_value=[mock_stream_event_content, mock_stream_event_done]):
            with patch.object(OpenAIProvider, 'get_client', return_value=Mock()):
                response = client.post("/api/send-message-stream", json={
                    "username": pc["username"],
                    "chat_name": "freechat",
                    "message": "Hello",
                    "model": "gpt-5.2"
                })

        # Parse SSE events
        sse_events = []
        event_type = None
        for line in response.text.split('\n'):
            if line.startswith('event: '):
                event_type = line[7:]
            elif line.startswith('data: ') and event_type:
                sse_events.append(event_type)
                event_type = None

        # No pipeline_stage events for non-project chats
        assert "pipeline_stage" not in sse_events


# ============================================================
# 4. Full pipeline SSE event ordering tests
# ============================================================

class TestPipelineEventOrdering:
    """Tests for correct ordering of all events in a full pipeline run."""

    def test_full_flow_event_order(self):
        """Events should follow: stages → content → stages → done."""
        provider = Mock(spec=OpenAIProvider)
        provider.build_pipeline_request.return_value = {"mock": True}
        provider.calculate_cost_with_tier.return_value = 0.001

        events_json = {
            "route": "mechanics",
            "pacing": {"episode": "T", "beat": "B", "beat_responses": 1, "notes": ""},
            "time_passed": "1 minute",
            "beats": ["test"],
            "player_action": "test",
            "callbacks": [],
            "emotional_context": "test",
            "character_states": {},
            "score_changes": []
        }
        mechanics_json = {
            "route": "narration",
            "beats": [{"beat": "test", "outcome": "ok", "rolls": [], "state_changes": []}],
            "dramatic_notes": "",
            "hud": "[HUD]",
            "score_changes": []
        }

        call_count = [0]
        def mock_non_streaming(c, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return {'content': json.dumps(events_json), 'reasoning': None,
                        'input_tokens': 100, 'cache_read_tokens': 0,
                        'cache_creation_tokens': 0, 'output_tokens': 50, 'reasoning_tokens': 0}
            else:
                return {'content': json.dumps(mechanics_json), 'reasoning': None,
                        'input_tokens': 80, 'cache_read_tokens': 0,
                        'cache_creation_tokens': 0, 'output_tokens': 60, 'reasoning_tokens': 0}

        provider.send_request_non_streaming.side_effect = mock_non_streaming

        # Narration streams
        stream_events = []
        for text in ["Narrated ", "content."]:
            evt = Mock()
            evt.event_type = 'content_delta'
            evt.content = text
            stream_events.append(evt)
        done_evt = Mock()
        done_evt.event_type = 'done'
        done_evt.usage = {'content': 'Narrated content.', 'reasoning': None,
                          'input_tokens': 60, 'cache_read_tokens': 0,
                          'cache_creation_tokens': 0, 'output_tokens': 20, 'reasoning_tokens': 0}
        stream_events.append(done_evt)
        provider.send_request_stream.return_value = stream_events

        events = list(run_pipeline(
            provider=provider, client=Mock(),
            username="test", project="proj", chat_name="chat",
            branch_path=[
                {"role": "system", "content": "Sys", "id": "sys"},
                {"role": "user", "content": "Go", "id": "usr"}
            ],
            context_start_index=1,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=None
        ))

        types = [t for t, _ in events]

        # Expected order: stage events, then content mixed with stage, then done
        assert types[0] == "pipeline_stage"  # events/thinking
        assert types[1] == "pipeline_stage"  # events/complete
        assert types[2] == "pipeline_stage"  # mechanics/thinking
        assert types[3] == "pipeline_stage"  # mechanics/complete
        assert types[4] == "pipeline_stage"  # narration/thinking
        assert types[5] == "pipeline_stage"  # narration/streaming
        assert types[6] == "content"         # first content delta
        assert types[-2] == "pipeline_stage" # narration/complete
        assert types[-1] == "pipeline_done"  # done
