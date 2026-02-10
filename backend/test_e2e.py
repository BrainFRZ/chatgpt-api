#!/usr/bin/env python3
"""
End-to-end tests for the chat API.
Mocks all provider API calls so no real tokens are used.
Tests: health, login, create-chat, send-message-stream (single + pipeline), edit/branch flow.
"""

import sys
sys.path.insert(0, '/home/chatgpt/backend')

import json
import os
import shutil
import unittest
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

from fastapi.testclient import TestClient

# We need to mock API keys before importing main
TEST_USER = "teste2euser"
TEST_USER_DIR = f"/home/chatgpt/data/users/{TEST_USER}"


def parse_sse_events(text: str) -> list[dict]:
    """Parse SSE text into a list of {type, data} dicts."""
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(": keepalive"):
            continue
        event_type = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data_lines.append(line[6:])
        if event_type and data_lines:
            raw = "\n".join(data_lines)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = raw
            events.append({"type": event_type, "data": data})
    return events


class BaseE2ETest(unittest.TestCase):
    """Base class that sets up test user and FastAPI test client."""

    @classmethod
    def setUpClass(cls):
        """Create test user directory and import app."""
        os.makedirs(TEST_USER_DIR, exist_ok=True)
        # Write a fake API key so endpoints don't reject us
        keys_path = os.path.join(TEST_USER_DIR, "api_keys.json")
        with open(keys_path, "w") as f:
            json.dump({"openai": "sk-test-fake-key-for-e2e"}, f)

        from main import app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        """Clean up test user data."""
        if os.path.exists(TEST_USER_DIR):
            shutil.rmtree(TEST_USER_DIR)


class TestHealthAndLogin(BaseE2ETest):
    """Test basic health and auth endpoints."""

    def test_health(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "healthy")

    def test_root(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")

    def test_login_new_user(self):
        resp = self.client.post("/api/login", json={"username": TEST_USER})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["username"], TEST_USER)
        self.assertTrue(data["has_api_key"])

    def test_login_empty_username(self):
        resp = self.client.post("/api/login", json={"username": ""})
        self.assertEqual(resp.status_code, 400)


class TestCreateChat(BaseE2ETest):
    """Test chat creation."""

    def test_create_chat(self):
        resp = self.client.post("/api/create-chat", json={
            "username": TEST_USER,
            "chat_name": "test-create",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")

        # Verify the chat was actually created and has a system message
        chat_resp = self.client.get(f"/api/chat/{TEST_USER}/test-create")
        self.assertEqual(chat_resp.status_code, 200)
        chat_data = chat_resp.json()
        self.assertIn("messages", chat_data)
        self.assertTrue(len(chat_data["messages"]) >= 1)
        self.assertEqual(chat_data["messages"][0]["role"], "system")

    def test_create_project_chat(self):
        # Create project directory first
        proj_dir = os.path.join(TEST_USER_DIR, "projects", "testproj")
        os.makedirs(proj_dir, exist_ok=True)

        resp = self.client.post("/api/create-chat", json={
            "username": TEST_USER,
            "chat_name": "test-proj-chat",
            "project": "testproj",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")

    def test_create_duplicate_chat(self):
        self.client.post("/api/create-chat", json={
            "username": TEST_USER,
            "chat_name": "test-dup",
        })
        resp = self.client.post("/api/create-chat", json={
            "username": TEST_USER,
            "chat_name": "test-dup",
        })
        self.assertEqual(resp.status_code, 400)


class TestGetChat(BaseE2ETest):
    """Test chat retrieval."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client.post("/api/create-chat", json={
            "username": TEST_USER,
            "chat_name": "test-get",
        })

    def test_get_chat(self):
        resp = self.client.get(f"/api/chat/{TEST_USER}/test-get")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("messages", data)
        self.assertIn("current_leaf_id", data)

    def test_get_nonexistent_chat(self):
        resp = self.client.get(f"/api/chat/{TEST_USER}/nonexistent-chat-xyz")
        self.assertEqual(resp.status_code, 404)


class TestSendMessageStreamNonPipeline(BaseE2ETest):
    """Test the streaming endpoint with a non-pipeline model (mocked)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client.post("/api/create-chat", json={
            "username": TEST_USER,
            "chat_name": "test-stream",
            "model": "gpt-4.1-mini",
        })

    @patch("main.ProviderRegistry")
    def test_stream_basic_message(self, mock_registry):
        """Test streaming a basic message with mocked provider."""
        from providers import StreamEvent

        mock_provider = MagicMock()
        mock_provider.name = "gpt-4.1-mini"
        mock_provider.model_id = "gpt-4.1-mini"
        mock_provider.context_limits = MagicMock(target=100000, threshold=120000)
        mock_provider.count_tokens = MagicMock(return_value=10)
        mock_provider.count_tokens_api = MagicMock(return_value=10)
        mock_provider.format_token_string = MagicMock(return_value="I:10 C:0 O:20 R:0 T:30")

        # Mock streaming response
        def fake_stream(client, params, ttfb_timeout=30.0):
            yield StreamEvent(event_type='content_delta', content='Hello ')
            yield StreamEvent(event_type='content_delta', content='World!')
            yield StreamEvent(event_type='done', usage={
                'content': 'Hello World!',
                'input_tokens': 10,
                'cache_read_tokens': 0,
                'cache_creation_tokens': 0,
                'output_tokens': 20,
                'reasoning_tokens': 0,
                'reasoning': None,
                'service_tier': 'default',
            })

        mock_provider.send_request_stream = MagicMock(side_effect=fake_stream)
        mock_provider.send_request_stream_with_fallback = MagicMock(side_effect=fake_stream)
        mock_provider.build_request = MagicMock(return_value={"model": "gpt-4.1-mini"})
        mock_provider.get_client = MagicMock(return_value=MagicMock())
        mock_provider.calculate_cost = MagicMock(return_value=0.001)

        mock_registry.get = MagicMock(return_value=mock_provider)
        mock_registry.get_required_api_key = MagicMock(return_value="openai")

        resp = self.client.post("/api/send-message-stream", json={
            "username": TEST_USER,
            "chat_name": "test-stream",
            "message": "Say hello",
            "model": "gpt-4.1-mini",
        })

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.headers.get("content-type", ""))

        events = parse_sse_events(resp.text)
        event_types = [e["type"] for e in events]

        self.assertIn("init", event_types)
        self.assertIn("content", event_types)
        self.assertIn("done", event_types)

        # Check init event has user_message_id
        init_event = next(e for e in events if e["type"] == "init")
        self.assertIn("user_message_id", init_event["data"])

        # Check content events
        content_events = [e for e in events if e["type"] == "content"]
        self.assertTrue(len(content_events) >= 1)

        # Check done event
        done_event = next(e for e in events if e["type"] == "done")
        self.assertIn("assistant_message", done_event["data"])
        self.assertIn("tokens", done_event["data"])
        self.assertIn("assistant_message_id", done_event["data"])
        self.assertIn("current_leaf_id", done_event["data"])


class TestSendMessageStreamPipeline(BaseE2ETest):
    """Test the streaming endpoint with pipeline (gpt-5.2 + project, mocked)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Create project dir
        proj_dir = os.path.join(TEST_USER_DIR, "projects", "pipetest")
        os.makedirs(proj_dir, exist_ok=True)

        cls.client.post("/api/create-chat", json={
            "username": TEST_USER,
            "chat_name": "test-pipeline",
            "project": "pipetest",
            "model": "gpt-5.2",
        })

    @patch("main.run_pipeline")
    @patch("main.generate_debug_transcript")
    @patch("main.ProviderRegistry")
    def test_pipeline_full_flow(self, mock_registry, mock_debug, mock_run_pipeline):
        """Test that gpt-5.2 + project activates pipeline and yields correct events."""
        mock_provider = MagicMock()
        mock_provider.name = "gpt-5.2"
        mock_provider.model_id = "gpt-5.2"
        mock_provider.context_limits = MagicMock(target=100000, threshold=120000)
        mock_provider.count_tokens = MagicMock(return_value=10)
        mock_provider.count_tokens_api = MagicMock(return_value=10)
        mock_provider.format_token_string = MagicMock(return_value="I:50 C:0 O:100 R:20 T:170")
        mock_provider.get_client = MagicMock(return_value=MagicMock())
        mock_provider.calculate_cost = MagicMock(return_value=0.005)

        mock_registry.get = MagicMock(return_value=mock_provider)
        mock_registry.get_required_api_key = MagicMock(return_value="openai")

        # Mock the pipeline generator
        from pipeline import PipelineResult

        def fake_pipeline(**kwargs):
            yield ("pipeline_stage", {"stage": "events", "status": "thinking"})
            yield ("pipeline_stage", {"stage": "events", "status": "complete"})
            yield ("pipeline_stage", {"stage": "mechanics", "status": "thinking"})
            yield ("pipeline_stage", {"stage": "mechanics", "status": "complete"})
            yield ("pipeline_stage", {"stage": "narration", "status": "thinking"})
            yield ("pipeline_stage", {"stage": "narration", "status": "streaming"})
            yield ("content", {"delta": "The dragon "})
            yield ("content", {"delta": "soared above."})
            yield ("pipeline_stage", {"stage": "narration", "status": "complete"})
            yield ("pipeline_done", PipelineResult(
                final_content="The dragon soared above.",
                events_json='{"route":"mechanics","content":""}',
                mechanics_json='{"route":"narration","content":""}',
                stages_run=["events", "mechanics", "narration"],
                aggregate_usage={
                    "input_tokens": 50, "cache_read_tokens": 0,
                    "cache_creation_tokens": 0, "output_tokens": 100,
                    "reasoning_tokens": 20
                },
                aggregate_cost=0.005,
                pipeline_state={"beats_remaining": 3},
                reasoning_summaries=["[Events] thought", "[Mechanics] planned"],
                service_tier_label="standard"
            ))

        mock_run_pipeline.side_effect = lambda **kw: fake_pipeline(**kw)

        resp = self.client.post("/api/send-message-stream", json={
            "username": TEST_USER,
            "chat_name": "test-pipeline",
            "message": "The hero draws their sword.",
            "project": "pipetest",
            "model": "gpt-5.2",
        })

        self.assertEqual(resp.status_code, 200)
        events = parse_sse_events(resp.text)
        event_types = [e["type"] for e in events]

        # Should have init
        self.assertIn("init", event_types)

        # Should have pipeline_stage events
        pipeline_events = [e for e in events if e["type"] == "pipeline_stage"]
        self.assertTrue(len(pipeline_events) >= 3, f"Expected >=3 pipeline_stage events, got {len(pipeline_events)}")

        # Check stage progression
        stages_seen = [e["data"]["stage"] for e in pipeline_events]
        self.assertIn("events", stages_seen)
        self.assertIn("mechanics", stages_seen)
        self.assertIn("narration", stages_seen)

        # Should have content events
        content_events = [e for e in events if e["type"] == "content"]
        self.assertTrue(len(content_events) >= 1)

        # Should have done event with pipeline_stages field
        done_event = next(e for e in events if e["type"] == "done")
        self.assertIn("pipeline_stages", done_event["data"])
        self.assertEqual(done_event["data"]["pipeline_stages"], ["events", "mechanics", "narration"])
        self.assertEqual(done_event["data"]["model"], "gpt-5.2")

        # Pipeline was called
        mock_run_pipeline.assert_called_once()

    @patch("main.run_pipeline")
    @patch("main.generate_debug_transcript")
    @patch("main.ProviderRegistry")
    def test_pipeline_short_circuit(self, mock_registry, mock_debug, mock_run_pipeline):
        """Test Events→output short circuit (e.g. OOC response)."""
        mock_provider = MagicMock()
        mock_provider.name = "gpt-5.2"
        mock_provider.model_id = "gpt-5.2"
        mock_provider.context_limits = MagicMock(target=100000, threshold=120000)
        mock_provider.count_tokens = MagicMock(return_value=10)
        mock_provider.count_tokens_api = MagicMock(return_value=10)
        mock_provider.format_token_string = MagicMock(return_value="I:20 C:0 O:30 R:5 T:55")
        mock_provider.get_client = MagicMock(return_value=MagicMock())
        mock_provider.calculate_cost = MagicMock(return_value=0.001)

        mock_registry.get = MagicMock(return_value=mock_provider)
        mock_registry.get_required_api_key = MagicMock(return_value="openai")

        from pipeline import PipelineResult

        def fake_pipeline_short(**kwargs):
            yield ("pipeline_stage", {"stage": "events", "status": "thinking"})
            yield ("pipeline_stage", {"stage": "events", "status": "complete"})
            yield ("content", {"delta": "Sure, I can help with that OOC."})
            yield ("pipeline_done", PipelineResult(
                final_content="Sure, I can help with that OOC.",
                events_json='{"route":"output","content":"Sure, I can help with that OOC."}',
                mechanics_json=None,
                stages_run=["events"],
                aggregate_usage={
                    "input_tokens": 20, "cache_read_tokens": 0,
                    "cache_creation_tokens": 0, "output_tokens": 30,
                    "reasoning_tokens": 5
                },
                aggregate_cost=0.001,
                pipeline_state=None,
                reasoning_summaries=[],
                service_tier_label="standard"
            ))

        mock_run_pipeline.side_effect = lambda **kw: fake_pipeline_short(**kw)

        resp = self.client.post("/api/send-message-stream", json={
            "username": TEST_USER,
            "chat_name": "test-pipeline",
            "message": "OOC: What's the word count?",
            "project": "pipetest",
            "model": "gpt-5.2",
        })

        self.assertEqual(resp.status_code, 200)
        events = parse_sse_events(resp.text)
        done_event = next(e for e in events if e["type"] == "done")
        # Short circuit: only events stage ran
        self.assertEqual(done_event["data"]["pipeline_stages"], ["events"])


class TestEditBranchFlow(BaseE2ETest):
    """Test that editing a message (parent_id) goes through the streaming endpoint."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client.post("/api/create-chat", json={
            "username": TEST_USER,
            "chat_name": "test-edit",
            "model": "gpt-4.1-mini",
        })

    @patch("main.ProviderRegistry")
    def _send_mocked_message(self, message, mock_registry, parent_id=None):
        """Helper: send a message with mocked provider, return done event data."""
        from providers import StreamEvent

        mock_provider = MagicMock()
        mock_provider.name = "gpt-4.1-mini"
        mock_provider.model_id = "gpt-4.1-mini"
        mock_provider.context_limits = MagicMock(target=100000, threshold=120000)
        mock_provider.count_tokens = MagicMock(return_value=10)
        mock_provider.count_tokens_api = MagicMock(return_value=10)
        mock_provider.format_token_string = MagicMock(return_value="I:10 C:0 O:20 R:0 T:30")
        mock_provider.get_client = MagicMock(return_value=MagicMock())
        mock_provider.calculate_cost = MagicMock(return_value=0.001)

        response_text = f"Response to: {message}"

        def fake_stream(client, params, ttfb_timeout=30.0):
            yield StreamEvent(event_type='content_delta', content=response_text)
            yield StreamEvent(event_type='done', usage={
                'content': response_text,
                'input_tokens': 10, 'cache_read_tokens': 0,
                'cache_creation_tokens': 0, 'output_tokens': 20,
                'reasoning_tokens': 0, 'reasoning': None,
                'service_tier': 'default',
            })

        mock_provider.send_request_stream = MagicMock(side_effect=fake_stream)
        mock_provider.send_request_stream_with_fallback = MagicMock(side_effect=fake_stream)
        mock_provider.build_request = MagicMock(return_value={"model": "gpt-4.1-mini"})

        mock_registry.get = MagicMock(return_value=mock_provider)
        mock_registry.get_required_api_key = MagicMock(return_value="openai")

        body = {
            "username": TEST_USER,
            "chat_name": "test-edit",
            "message": message,
            "model": "gpt-4.1-mini",
        }
        if parent_id:
            body["parent_id"] = parent_id

        resp = self.client.post("/api/send-message-stream", json=body)
        self.assertEqual(resp.status_code, 200)
        events = parse_sse_events(resp.text)
        done_event = next(e for e in events if e["type"] == "done")
        return done_event["data"]

    def test_edit_creates_branch(self):
        """Test that sending with parent_id creates a branch (sibling)."""
        # Send first message
        done1 = self._send_mocked_message("First message")
        user1_id = done1["user_message_id"]
        assistant1_id = done1["assistant_message_id"]

        # Send second message (normal continuation)
        done2 = self._send_mocked_message("Second message")
        user2_id = done2["user_message_id"]

        # Now "edit" the first message by sending with parent_id = system message's id
        # The parent of user1 is the system message. Get it from the chat.
        chat_resp = self.client.get(f"/api/chat/{TEST_USER}/test-edit")
        chat_data = chat_resp.json()
        all_msgs = chat_data["all_messages"]

        # Find user1's parent (system msg)
        user1_msg = next(m for m in all_msgs if m.get("id") == user1_id)
        system_id = user1_msg["parent_id"]

        # Send edit as sibling of user1 (same parent)
        done3 = self._send_mocked_message("Edited first message", parent_id=system_id)
        edited_user_id = done3["user_message_id"]
        edited_assistant_id = done3["assistant_message_id"]

        # Verify the branch was created
        self.assertNotEqual(edited_user_id, user1_id)
        self.assertNotEqual(edited_assistant_id, assistant1_id)
        self.assertEqual(done3["current_leaf_id"], edited_assistant_id)

        # Verify the chat now has the branch
        chat_resp2 = self.client.get(f"/api/chat/{TEST_USER}/test-edit")
        chat_data2 = chat_resp2.json()
        all_msgs2 = chat_data2["all_messages"]

        # The edited message should exist as a sibling
        edited_msg = next((m for m in all_msgs2 if m.get("id") == edited_user_id), None)
        self.assertIsNotNone(edited_msg)
        self.assertEqual(edited_msg["parent_id"], system_id)
        self.assertEqual(edited_msg["content"], "Edited first message")

    def test_parent_id_not_found(self):
        """Test that invalid parent_id returns 400."""
        resp = self.client.post("/api/send-message-stream", json={
            "username": TEST_USER,
            "chat_name": "test-edit",
            "message": "Should fail",
            "parent_id": "nonexistent-id-12345",
            "model": "gpt-4.1-mini",
        })
        self.assertEqual(resp.status_code, 400)


class TestPipelineEditFlow(BaseE2ETest):
    """Test that edits on a gpt-5.2 project chat go through the pipeline."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        proj_dir = os.path.join(TEST_USER_DIR, "projects", "editpipe")
        os.makedirs(proj_dir, exist_ok=True)

        cls.client.post("/api/create-chat", json={
            "username": TEST_USER,
            "chat_name": "test-edit-pipe",
            "project": "editpipe",
            "model": "gpt-5.2",
        })

    @patch("main.run_pipeline")
    @patch("main.generate_debug_transcript")
    @patch("main.ProviderRegistry")
    def test_edit_activates_pipeline(self, mock_registry, mock_debug, mock_run_pipeline):
        """Test that sending with parent_id on gpt-5.2 project still activates pipeline."""
        from pipeline import PipelineResult

        mock_provider = MagicMock()
        mock_provider.name = "gpt-5.2"
        mock_provider.model_id = "gpt-5.2"
        mock_provider.context_limits = MagicMock(target=100000, threshold=120000)
        mock_provider.count_tokens = MagicMock(return_value=10)
        mock_provider.count_tokens_api = MagicMock(return_value=10)
        mock_provider.format_token_string = MagicMock(return_value="I:50 C:0 O:100 R:20 T:170")
        mock_provider.get_client = MagicMock(return_value=MagicMock())
        mock_provider.calculate_cost = MagicMock(return_value=0.005)

        mock_registry.get = MagicMock(return_value=mock_provider)
        mock_registry.get_required_api_key = MagicMock(return_value="openai")

        def fake_pipeline(**kwargs):
            yield ("pipeline_stage", {"stage": "events", "status": "thinking"})
            yield ("pipeline_stage", {"stage": "events", "status": "complete"})
            yield ("pipeline_stage", {"stage": "mechanics", "status": "thinking"})
            yield ("pipeline_stage", {"stage": "mechanics", "status": "complete"})
            yield ("pipeline_stage", {"stage": "narration", "status": "thinking"})
            yield ("content", {"delta": "Narrated edit response."})
            yield ("pipeline_stage", {"stage": "narration", "status": "complete"})
            yield ("pipeline_done", PipelineResult(
                final_content="Narrated edit response.",
                events_json='{"route":"mechanics"}',
                mechanics_json='{"route":"narration"}',
                stages_run=["events", "mechanics", "narration"],
                aggregate_usage={
                    "input_tokens": 50, "cache_read_tokens": 0,
                    "cache_creation_tokens": 0, "output_tokens": 100,
                    "reasoning_tokens": 20
                },
                aggregate_cost=0.005,
                pipeline_state=None,
                reasoning_summaries=[],
                service_tier_label="standard"
            ))

        mock_run_pipeline.side_effect = lambda **kw: fake_pipeline(**kw)

        # First, send a normal message to get IDs
        resp1 = self.client.post("/api/send-message-stream", json={
            "username": TEST_USER,
            "chat_name": "test-edit-pipe",
            "message": "First pipeline message",
            "project": "editpipe",
            "model": "gpt-5.2",
        })
        events1 = parse_sse_events(resp1.text)
        done1 = next(e for e in events1 if e["type"] == "done")
        user1_id = done1["data"]["user_message_id"]

        # Reset the mock to track the edit call
        mock_run_pipeline.reset_mock()
        mock_run_pipeline.side_effect = lambda **kw: fake_pipeline(**kw)

        # Get system message ID (parent of user1)
        chat_resp = self.client.get(f"/api/chat/{TEST_USER}/test-edit-pipe?project=editpipe")
        all_msgs = chat_resp.json()["all_messages"]
        user1_msg = next(m for m in all_msgs if m.get("id") == user1_id)
        system_id = user1_msg["parent_id"]

        # Send edit (sibling of first message) — should still activate pipeline
        resp2 = self.client.post("/api/send-message-stream", json={
            "username": TEST_USER,
            "chat_name": "test-edit-pipe",
            "message": "Edited message going through pipeline",
            "project": "editpipe",
            "parent_id": system_id,
            "model": "gpt-5.2",
        })

        self.assertEqual(resp2.status_code, 200)
        events2 = parse_sse_events(resp2.text)
        event_types2 = [e["type"] for e in events2]

        # Pipeline must have been called for the edit
        mock_run_pipeline.assert_called_once()

        # Should have pipeline_stage events
        self.assertIn("pipeline_stage", event_types2)

        # Done event should have pipeline_stages
        done2 = next(e for e in events2 if e["type"] == "done")
        self.assertEqual(done2["data"]["pipeline_stages"], ["events", "mechanics", "narration"])


class TestSSEFormat(BaseE2ETest):
    """Test SSE event formatting and keepalive handling."""

    def test_parse_keepalive(self):
        """Test that keepalive comments are properly skipped by the SSE parser."""
        raw = "event: init\ndata: {\"user_message_id\": \"abc\"}\n\n: keepalive\n\nevent: done\ndata: {\"ok\": true}\n\n"
        events = parse_sse_events(raw)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["type"], "init")
        self.assertEqual(events[1]["type"], "done")


class TestNoApiKeyRejects(BaseE2ETest):
    """Test that requests with no API key are rejected."""

    def test_no_api_key_user(self):
        # Create user without API key
        nokey_dir = "/home/chatgpt/data/users/testnokey"
        os.makedirs(nokey_dir, exist_ok=True)
        try:
            self.client.post("/api/create-chat", json={
                "username": "testnokey",
                "chat_name": "nokey-chat",
            })
            resp = self.client.post("/api/send-message-stream", json={
                "username": "testnokey",
                "chat_name": "nokey-chat",
                "message": "test",
            })
            # Should fail with 400 or 403 for missing API key
            self.assertIn(resp.status_code, [400, 403, 500])
        finally:
            shutil.rmtree(nokey_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
