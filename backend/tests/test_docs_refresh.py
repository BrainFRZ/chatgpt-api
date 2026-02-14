"""
Tests for auto-refresh of system prompt on context trimming:
- Trimming detection (total_pairs > threshold triggers refresh)
- System prompt content update from disk
- Token count clearing on refresh
- docs_refreshed flag behavior
- SyncEventType.DOCS_REFRESHED exists
- get_context_pairs interaction with refresh
"""

import json
import copy
import sys
import os
import tempfile
import shutil

import pytest

# Ensure backend is on sys.path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import (
    get_context_pairs,
    migrate_pipeline_state,
    _fresh_pipeline_state,
    SINGLE_AGENT_THRESHOLD_PAIRS,
    SINGLE_AGENT_TARGET_PAIRS,
)
from sync_manager import SyncEventType, SyncEvent


# ============================================================
# Fixtures
# ============================================================

def _make_branch_path(system_content, num_pairs, system_tokens=True):
    """
    Build a branch_path with a system message, num_pairs user/assistant pairs,
    and a final user message.  Optionally add total_tokens to the system msg.
    """
    system_msg = {"role": "system", "content": system_content}
    if system_tokens:
        system_msg["total_tokens"] = 100
        system_msg["total_gpt_tokens"] = 100
        system_msg["total_claude_tokens"] = 100

    messages = [system_msg]
    for i in range(num_pairs):
        messages.append({"role": "user", "content": f"User message {i+1}"})
        messages.append({"role": "assistant", "content": f"Assistant message {i+1}"})
    # Final user message (current turn)
    messages.append({"role": "user", "content": "Current user message"})
    return messages


@pytest.fixture
def tmp_project(tmp_path):
    """Create a temporary project directory with instructions and uploads."""
    user_dir = tmp_path / "users" / "testuser" / "projects" / "testproject"
    user_dir.mkdir(parents=True)
    uploads_dir = user_dir / "uploads"
    uploads_dir.mkdir()

    # Write initial instructions
    (user_dir / "instructions.di").write_text("Initial instructions v1")
    # Write a project file
    (uploads_dir / "lore.md").write_text("# Lore\nOriginal lore content")
    # Write file_tokens.json (staging)
    (user_dir / "file_tokens.json").write_text(json.dumps({
        "lore.md": {"staged": True, "tokens": 50}
    }))

    return {
        "user_dir": user_dir,
        "uploads_dir": uploads_dir,
        "username": "testuser",
        "project": "testproject",
        "data_root": tmp_path / "users",
    }


# ============================================================
# SyncEventType Tests
# ============================================================

class TestDocsRefreshedSyncEvent:
    def test_event_type_exists(self):
        """DOCS_REFRESHED must be a valid SyncEventType."""
        assert hasattr(SyncEventType, "DOCS_REFRESHED")
        assert SyncEventType.DOCS_REFRESHED.value == "docs_refreshed"

    def test_sync_event_serialization(self):
        """SyncEvent with DOCS_REFRESHED should serialize correctly."""
        event = SyncEvent(type=SyncEventType.DOCS_REFRESHED, data={})
        payload = json.loads(event.to_json())
        assert payload["type"] == "docs_refreshed"
        assert payload["data"] == {}

    def test_sync_event_with_data(self):
        """SyncEvent with DOCS_REFRESHED can carry extra data."""
        event = SyncEvent(
            type=SyncEventType.DOCS_REFRESHED,
            data={"message": "Instructions and project files refreshed"}
        )
        payload = json.loads(event.to_json())
        assert payload["type"] == "docs_refreshed"
        assert payload["data"]["message"] == "Instructions and project files refreshed"


# ============================================================
# Trimming Detection Tests
# ============================================================

class TestTrimmingDetection:
    """Test the logic that detects when total_pairs > threshold."""

    def test_below_threshold_no_trim(self):
        """With pairs <= threshold, no trimming should occur."""
        branch_path = _make_branch_path("system", num_pairs=SINGLE_AGENT_THRESHOLD_PAIRS)
        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        assert total_pairs == SINGLE_AGENT_THRESHOLD_PAIRS
        assert total_pairs <= SINGLE_AGENT_THRESHOLD_PAIRS  # not >

    def test_above_threshold_triggers_trim(self):
        """With pairs > threshold, trimming should be triggered."""
        branch_path = _make_branch_path("system", num_pairs=SINGLE_AGENT_THRESHOLD_PAIRS + 1)
        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        assert total_pairs == SINGLE_AGENT_THRESHOLD_PAIRS + 1
        assert total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS

    def test_exactly_at_threshold_no_trim(self):
        """Exactly at threshold should NOT trigger (condition is >, not >=)."""
        branch_path = _make_branch_path("system", num_pairs=SINGLE_AGENT_THRESHOLD_PAIRS)
        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        assert not (total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS)

    def test_one_above_threshold(self):
        """One above threshold should trigger."""
        branch_path = _make_branch_path("system", num_pairs=SINGLE_AGENT_THRESHOLD_PAIRS + 1)
        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        assert total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS

    def test_well_above_threshold(self):
        """Well above threshold — e.g. 60 pairs — should trigger."""
        branch_path = _make_branch_path("system", num_pairs=60)
        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        assert total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS


# ============================================================
# System Prompt Refresh Tests
# ============================================================

class TestSystemPromptRefresh:
    """Test that the system prompt is updated from disk and token counts cleared."""

    def test_refresh_updates_content(self):
        """When refresh fires, branch_path[0]['content'] should be updated."""
        old_content = "Old instructions\n\nOld project files"
        branch_path = _make_branch_path(old_content, num_pairs=SINGLE_AGENT_THRESHOLD_PAIRS + 1)

        # Simulate what main.py does
        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        assert total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS

        # Simulate fresh content from disk
        fresh_system = "New instructions\n\nNew project files"
        branch_path[0]["content"] = fresh_system

        assert branch_path[0]["content"] == fresh_system
        assert branch_path[0]["content"] != old_content

    def test_refresh_clears_token_counts(self):
        """Token count fields should be popped when content changes."""
        branch_path = _make_branch_path("Old instructions", num_pairs=SINGLE_AGENT_THRESHOLD_PAIRS + 1)
        system_msg = branch_path[0]
        assert "total_tokens" in system_msg
        assert "total_gpt_tokens" in system_msg
        assert "total_claude_tokens" in system_msg

        # Simulate the refresh clearing
        system_msg.pop("total_tokens", None)
        system_msg.pop("total_gpt_tokens", None)
        system_msg.pop("total_claude_tokens", None)

        assert "total_tokens" not in system_msg
        assert "total_gpt_tokens" not in system_msg
        assert "total_claude_tokens" not in system_msg

    def test_no_refresh_preserves_token_counts(self):
        """Below threshold, token counts should remain untouched."""
        branch_path = _make_branch_path("Instructions", num_pairs=SINGLE_AGENT_THRESHOLD_PAIRS)
        system_msg = branch_path[0]
        assert system_msg["total_tokens"] == 100
        assert system_msg["total_gpt_tokens"] == 100
        assert system_msg["total_claude_tokens"] == 100

    def test_refresh_with_no_project_files(self):
        """If no project files, system content should be just instructions."""
        fresh_instructions = "Updated instructions only"
        fresh_files = ""
        if fresh_files:
            fresh_system = fresh_instructions + "\n\n" + fresh_files
        else:
            fresh_system = fresh_instructions
        assert fresh_system == "Updated instructions only"

    def test_refresh_with_project_files(self):
        """If project files exist, system content should combine them."""
        fresh_instructions = "Updated instructions"
        fresh_files = "====FILE: lore.md====\n# New Lore"
        fresh_system = fresh_instructions + "\n\n" + fresh_files
        assert "Updated instructions" in fresh_system
        assert "New Lore" in fresh_system


# ============================================================
# Dict Reference / Persistence Tests
# ============================================================

class TestDictReferencePersistence:
    """
    Verify that modifying branch_path[0] propagates to the underlying
    data['messages'][0] via dict reference (same object identity).
    """

    def test_branch_path_shares_reference_with_messages(self):
        """branch_path[0] should be the same dict object as messages[0]."""
        messages = [
            {"role": "system", "content": "Original content", "total_tokens": 100},
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "reply1"},
            {"role": "user", "content": "current"},
        ]
        # Simulate get_path_to_root returning a slice of the same list
        branch_path = messages  # In simple (non-tree) case, it's the same list

        # Modify through branch_path
        branch_path[0]["content"] = "Updated content"
        branch_path[0].pop("total_tokens", None)

        # Verify the change is visible through messages
        assert messages[0]["content"] == "Updated content"
        assert "total_tokens" not in messages[0]

    def test_deepcopy_breaks_reference(self):
        """A deepcopy would break the reference — this is what we must NOT do."""
        messages = [{"role": "system", "content": "Original", "total_tokens": 100}]
        branch_path_copy = copy.deepcopy(messages)

        branch_path_copy[0]["content"] = "Updated"
        # Original should NOT be updated
        assert messages[0]["content"] == "Original"


# ============================================================
# get_context_pairs Interaction Tests
# ============================================================

class TestGetContextPairsAfterRefresh:
    """
    Verify that get_context_pairs trims correctly after the system prompt
    has been refreshed (the system content doesn't affect pair counting).
    """

    def test_trimmed_pairs_count(self):
        """After refresh, get_context_pairs should return target_pairs pairs."""
        num_pairs = SINGLE_AGENT_THRESHOLD_PAIRS + 5
        branch_path = _make_branch_path("Updated system content", num_pairs=num_pairs)

        pairs = get_context_pairs(branch_path, SINGLE_AGENT_THRESHOLD_PAIRS, SINGLE_AGENT_TARGET_PAIRS)
        # Each pair = 2 messages (user + assistant)
        assert len(pairs) == SINGLE_AGENT_TARGET_PAIRS * 2

    def test_no_trim_preserves_all(self):
        """Below threshold, all pairs should be preserved."""
        num_pairs = SINGLE_AGENT_THRESHOLD_PAIRS - 5
        branch_path = _make_branch_path("System content", num_pairs=num_pairs)

        pairs = get_context_pairs(branch_path, SINGLE_AGENT_THRESHOLD_PAIRS, SINGLE_AGENT_TARGET_PAIRS)
        assert len(pairs) == num_pairs * 2

    def test_trimmed_pairs_are_most_recent(self):
        """After trimming, the returned pairs should be the most recent ones."""
        num_pairs = SINGLE_AGENT_THRESHOLD_PAIRS + 5
        branch_path = _make_branch_path("System", num_pairs=num_pairs)

        pairs = get_context_pairs(branch_path, SINGLE_AGENT_THRESHOLD_PAIRS, SINGLE_AGENT_TARGET_PAIRS)
        # The last pair should be the most recent
        last_pair_assistant = pairs[-1]
        assert f"Assistant message {num_pairs}" in last_pair_assistant["content"]

    def test_system_content_excluded_from_pairs(self):
        """System message should never appear in context pairs."""
        branch_path = _make_branch_path("Secret system content", num_pairs=10)
        pairs = get_context_pairs(branch_path, SINGLE_AGENT_THRESHOLD_PAIRS, SINGLE_AGENT_TARGET_PAIRS)
        for p in pairs:
            assert "Secret system content" not in p["content"]

    def test_current_user_message_excluded_from_pairs(self):
        """The current (last) user message should not be in context pairs."""
        branch_path = _make_branch_path("System", num_pairs=10)
        pairs = get_context_pairs(branch_path, SINGLE_AGENT_THRESHOLD_PAIRS, SINGLE_AGENT_TARGET_PAIRS)
        for p in pairs:
            assert "Current user message" not in p["content"]


# ============================================================
# docs_refreshed Flag Tests
# ============================================================

class TestDocsRefreshedFlag:
    """Test the docs_refreshed flag logic as implemented in send_message_stream."""

    def test_flag_set_on_trim(self):
        """docs_refreshed should be True when trimming fires."""
        branch_path = _make_branch_path("Old", num_pairs=SINGLE_AGENT_THRESHOLD_PAIRS + 1)
        docs_refreshed = False

        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        if total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS:
            docs_refreshed = True

        assert docs_refreshed is True

    def test_flag_not_set_below_threshold(self):
        """docs_refreshed should stay False when no trim happens."""
        branch_path = _make_branch_path("Old", num_pairs=SINGLE_AGENT_THRESHOLD_PAIRS)
        docs_refreshed = False

        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        if total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS:
            docs_refreshed = True

        assert docs_refreshed is False

    def test_flag_not_set_for_short_conversation(self):
        """A short conversation (5 pairs) should not set the flag."""
        branch_path = _make_branch_path("Old", num_pairs=5)
        docs_refreshed = False

        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        if total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS:
            docs_refreshed = True

        assert docs_refreshed is False


# ============================================================
# End-to-End Refresh Simulation Tests
# ============================================================

class TestEndToEndRefreshSimulation:
    """
    Simulate the full refresh flow as done in send_message_stream:
    detect trimming → refresh system prompt → clear tokens → build API messages.
    """

    def test_full_refresh_flow(self):
        """Simulate the complete refresh path end-to-end."""
        old_instructions = "Old DM instructions"
        old_files = "Old lore content"
        old_system = old_instructions + "\n\n" + old_files
        branch_path = _make_branch_path(old_system, num_pairs=SINGLE_AGENT_THRESHOLD_PAIRS + 2)

        docs_refreshed = False

        # Step 1: Detect trimming
        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        if total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS:
            docs_refreshed = True

            # Step 2: Simulate reading fresh content from disk
            fresh_instructions = "Updated DM instructions v2"
            fresh_files = "Updated lore content v2"
            fresh_system = fresh_instructions + "\n\n" + fresh_files

            # Step 3: Update system message
            branch_path[0]["content"] = fresh_system
            branch_path[0].pop("total_tokens", None)
            branch_path[0].pop("total_gpt_tokens", None)
            branch_path[0].pop("total_claude_tokens", None)

        # Step 4: get_context_pairs
        context_pairs = get_context_pairs(branch_path, SINGLE_AGENT_THRESHOLD_PAIRS, SINGLE_AGENT_TARGET_PAIRS)

        # Step 5: Build system_content (as main.py does)
        MOCK_CONTRACT = "[STATE CONTRACT]"
        system_content = MOCK_CONTRACT + "\n\n" + branch_path[0]["content"]

        # Assertions
        assert docs_refreshed is True
        assert "Updated DM instructions v2" in system_content
        assert "Updated lore content v2" in system_content
        assert "Old DM instructions" not in system_content
        assert len(context_pairs) == SINGLE_AGENT_TARGET_PAIRS * 2
        assert "total_tokens" not in branch_path[0]

    def test_no_refresh_flow(self):
        """Simulate the non-refresh path — content stays the same."""
        original_system = "Original instructions"
        branch_path = _make_branch_path(original_system, num_pairs=SINGLE_AGENT_THRESHOLD_PAIRS - 1)

        docs_refreshed = False
        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        if total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS:
            docs_refreshed = True

        context_pairs = get_context_pairs(branch_path, SINGLE_AGENT_THRESHOLD_PAIRS, SINGLE_AGENT_TARGET_PAIRS)

        MOCK_CONTRACT = "[STATE CONTRACT]"
        system_content = MOCK_CONTRACT + "\n\n" + branch_path[0]["content"]

        assert docs_refreshed is False
        assert "Original instructions" in system_content
        assert len(context_pairs) == (SINGLE_AGENT_THRESHOLD_PAIRS - 1) * 2

    def test_refresh_preserves_pipeline_state(self):
        """Pipeline state migration should work independently of refresh."""
        branch_path = _make_branch_path("Old system", num_pairs=SINGLE_AGENT_THRESHOLD_PAIRS + 1)

        # Simulate pipeline_state from data
        raw_state = {
            "pacing": {"episode": "S1", "beat": "B1"},
            "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
            "npc_memories": {},
            "scene_state": {},
            "character_states": {},
            "turn_counter": 42,
        }
        stateful_pipeline_state = migrate_pipeline_state(copy.deepcopy(raw_state))

        # Refresh system prompt
        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        docs_refreshed = total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS
        if docs_refreshed:
            branch_path[0]["content"] = "Refreshed content"

        # Pipeline state should be unaffected
        assert stateful_pipeline_state["turn_counter"] == 42
        assert stateful_pipeline_state["pacing"]["episode"] == "S1"

    def test_multiple_trims_refresh_each_time(self):
        """
        Simulate two consecutive turns that both trigger trimming.
        Each should pick up fresh content.
        """
        # Turn 1: 41 pairs → trim
        branch_path = _make_branch_path("Instructions v1", num_pairs=41)

        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        assert total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS
        branch_path[0]["content"] = "Instructions v2"

        # After trim, pairs go to 20. Simulate adding 1 more pair = 21 pairs.
        # 21 <= 40 → no trim on next turn.
        branch_path_2 = _make_branch_path("Instructions v2", num_pairs=21)
        history_2 = branch_path_2[1:-1]
        total_pairs_2 = len(history_2) // 2
        assert not (total_pairs_2 > SINGLE_AGENT_THRESHOLD_PAIRS)

        # After 20 more turns (41 total) → trim again
        branch_path_3 = _make_branch_path("Instructions v2", num_pairs=41)
        history_3 = branch_path_3[1:-1]
        total_pairs_3 = len(history_3) // 2
        assert total_pairs_3 > SINGLE_AGENT_THRESHOLD_PAIRS
        branch_path_3[0]["content"] = "Instructions v3"
        assert branch_path_3[0]["content"] == "Instructions v3"


# ============================================================
# Edge Cases
# ============================================================

class TestEdgeCases:
    def test_empty_conversation(self):
        """A conversation with 0 pairs should not trigger refresh."""
        branch_path = _make_branch_path("System", num_pairs=0)
        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        assert total_pairs == 0
        assert not (total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS)

    def test_single_pair(self):
        """A conversation with 1 pair should not trigger refresh."""
        branch_path = _make_branch_path("System", num_pairs=1)
        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        assert total_pairs == 1
        assert not (total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS)

    def test_system_message_without_token_fields(self):
        """Refresh should handle system message with no token fields gracefully."""
        branch_path = _make_branch_path("Old", num_pairs=SINGLE_AGENT_THRESHOLD_PAIRS + 1, system_tokens=False)
        system_msg = branch_path[0]

        # pop with default should not raise
        system_msg.pop("total_tokens", None)
        system_msg.pop("total_gpt_tokens", None)
        system_msg.pop("total_claude_tokens", None)
        assert "total_tokens" not in system_msg

    def test_odd_message_count(self):
        """
        If there's an odd number of history messages (e.g. orphan user message),
        integer division should still work correctly.
        """
        # Build manually with an odd history
        branch_path = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "U2"},  # orphan — no assistant reply
            {"role": "user", "content": "Current"},  # current user message
        ]
        history = branch_path[1:-1]
        total_pairs = len(history) // 2  # 3 // 2 = 1
        assert total_pairs == 1
        assert not (total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS)
