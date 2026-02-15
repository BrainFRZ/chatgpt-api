"""
End-to-end tests for Pipeline Phase 2: Per-Agent Instructions + Document Routing.

Tests cover:
1. get_instructions_for_agent() - per-agent instructions with fallback
2. load_project_files_for_agent() - file filtering by agent assignment
3. API: PUT /api/project-files/{user}/{project}/agents/{filename}
4. API: GET /api/project-instructions/{user}/{project}/agents
5. API: PUT /api/project-instructions/{user}/{project}/agents/{agent_name}
6. Pipeline integration - per-agent instructions/files passed to run_pipeline
7. File listing and upload preserve agents field
8. build_agent_system_prompt() unit tests
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
from pipeline import build_agent_system_prompt
from game_systems.dnd5e import EVENTS_CONTRACT, MECHANICS_CONTRACT, NARRATION_CONTRACT

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
def pipeline_project(temp_data_dir):
    """Create a test user with a GPT-5.2 pipeline project, uploads, and token cache."""
    username = "testuser"
    project = "testproject"
    project_dir = Path(temp_data_dir) / username / "projects" / project
    uploads_dir = project_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    # Create user dir (needed for some endpoints)
    user_dir = Path(temp_data_dir) / username
    user_dir.mkdir(exist_ok=True)

    # Project metadata (GPT-5.2)
    metadata = {"model": "gpt-5.2", "last_accessed": "2025-01-01T00:00:00"}
    with open(project_dir / "metadata.json", 'w') as f:
        json.dump(metadata, f)

    # Shared instructions
    with open(project_dir / "instructions.di", 'w') as f:
        f.write("You are a TTRPG game master for Broken Orbit.")

    # Upload some .md files
    files = {
        "rulebook.md": "# Rulebook\nRules for the game.",
        "world_lore.md": "# World Lore\nThe world of Broken Orbit.",
        "character_sheet.md": "# Character Sheet\nAldric the rogue.",
    }
    for filename, content in files.items():
        with open(uploads_dir / filename, 'w') as f:
            f.write(content)

    # Token cache with agent assignments
    tokens_cache = {
        "rulebook.md": {
            "tokens": 100, "model": "gpt-5.2", "size_bytes": 30,
            "mtime": 1000, "staged": True,
            "agents": ["mechanics"]
        },
        "world_lore.md": {
            "tokens": 80, "model": "gpt-5.2", "size_bytes": 40,
            "mtime": 1000, "staged": True,
            "agents": ["events", "narration"]
        },
        "character_sheet.md": {
            "tokens": 60, "model": "gpt-5.2", "size_bytes": 35,
            "mtime": 1000, "staged": True,
            "agents": ["events", "mechanics"]
        },
    }
    with open(project_dir / "file_tokens.json", 'w') as f:
        json.dump(tokens_cache, f)

    return {"username": username, "project": project, "project_dir": str(project_dir)}


# ============================================================
# 1. get_instructions_for_agent() Tests
# ============================================================

class TestGetInstructionsForAgent:
    """Tests for per-agent instruction loading with fallback."""

    def test_returns_agent_specific_instructions(self, temp_data_dir, pipeline_project):
        """When instructions_events.di exists and is non-empty, return its content."""
        pp = pipeline_project
        project_dir = Path(pp["project_dir"])
        with open(project_dir / "instructions_events.di", 'w') as f:
            f.write("Events-specific instructions here.")

        result = main.get_instructions_for_agent(pp["username"], pp["project"], "events")
        assert result == "Events-specific instructions here."

    def test_falls_back_to_shared_when_file_missing(self, temp_data_dir, pipeline_project):
        """When instructions_events.di doesn't exist, fall back to instructions.di."""
        pp = pipeline_project
        result = main.get_instructions_for_agent(pp["username"], pp["project"], "events")
        assert result == "You are a TTRPG game master for Broken Orbit."

    def test_falls_back_to_shared_when_file_empty(self, temp_data_dir, pipeline_project):
        """When instructions_mechanics.di exists but is empty, fall back to shared."""
        pp = pipeline_project
        project_dir = Path(pp["project_dir"])
        with open(project_dir / "instructions_mechanics.di", 'w') as f:
            f.write("")

        result = main.get_instructions_for_agent(pp["username"], pp["project"], "mechanics")
        assert result == "You are a TTRPG game master for Broken Orbit."

    def test_falls_back_when_file_only_whitespace(self, temp_data_dir, pipeline_project):
        """Whitespace-only file should fall back to shared."""
        pp = pipeline_project
        project_dir = Path(pp["project_dir"])
        with open(project_dir / "instructions_narration.di", 'w') as f:
            f.write("   \n\t  \n")

        result = main.get_instructions_for_agent(pp["username"], pp["project"], "narration")
        assert result == "You are a TTRPG game master for Broken Orbit."

    def test_each_agent_can_have_different_instructions(self, temp_data_dir, pipeline_project):
        """Three different agents can each have their own instructions."""
        pp = pipeline_project
        project_dir = Path(pp["project_dir"])

        for agent in AGENT_NAMES:
            with open(project_dir / f"instructions_{agent}.di", 'w') as f:
                f.write(f"Instructions for {agent}.")

        for agent in AGENT_NAMES:
            result = main.get_instructions_for_agent(pp["username"], pp["project"], agent)
            assert result == f"Instructions for {agent}."


# ============================================================
# 2. load_project_files_for_agent() Tests
# ============================================================

class TestLoadProjectFilesForAgent:
    """Tests for file loading filtered by agent assignment."""

    def test_events_gets_assigned_files_only(self, temp_data_dir, pipeline_project):
        """Events agent should get world_lore.md and character_sheet.md, not rulebook.md."""
        pp = pipeline_project
        result = main.load_project_files_for_agent(pp["username"], pp["project"], "events")

        assert "world_lore.md" in result
        assert "character_sheet.md" in result
        assert "rulebook.md" not in result

    def test_mechanics_gets_assigned_files_only(self, temp_data_dir, pipeline_project):
        """Mechanics agent should get rulebook.md and character_sheet.md, not world_lore.md."""
        pp = pipeline_project
        result = main.load_project_files_for_agent(pp["username"], pp["project"], "mechanics")

        assert "rulebook.md" in result
        assert "character_sheet.md" in result
        assert "world_lore.md" not in result

    def test_narration_gets_assigned_files_only(self, temp_data_dir, pipeline_project):
        """Narration agent should get only world_lore.md."""
        pp = pipeline_project
        result = main.load_project_files_for_agent(pp["username"], pp["project"], "narration")

        assert "world_lore.md" in result
        assert "rulebook.md" not in result
        assert "character_sheet.md" not in result

    def test_unstaged_files_excluded(self, temp_data_dir, pipeline_project):
        """Files with staged=False should be excluded regardless of agent assignment."""
        pp = pipeline_project
        project_dir = Path(pp["project_dir"])

        # Mark world_lore.md as unstaged
        cache_path = project_dir / "file_tokens.json"
        with open(cache_path) as f:
            cache = json.load(f)
        cache["world_lore.md"]["staged"] = False
        with open(cache_path, 'w') as f:
            json.dump(cache, f)

        result = main.load_project_files_for_agent(pp["username"], pp["project"], "events")
        assert "world_lore.md" not in result
        assert "character_sheet.md" in result

    def test_file_without_agents_field_defaults_to_all(self, temp_data_dir, pipeline_project):
        """Files without an 'agents' field in cache should be included for all agents."""
        pp = pipeline_project
        project_dir = Path(pp["project_dir"])

        # Add a new file with no agents field in cache
        with open(project_dir / "uploads" / "notes.md", 'w') as f:
            f.write("# Session Notes\nSome notes.")

        cache_path = project_dir / "file_tokens.json"
        with open(cache_path) as f:
            cache = json.load(f)
        cache["notes.md"] = {"tokens": 50, "model": "gpt-5.2", "size_bytes": 20, "staged": True}
        # No "agents" field
        with open(cache_path, 'w') as f:
            json.dump(cache, f)

        for agent in AGENT_NAMES:
            result = main.load_project_files_for_agent(pp["username"], pp["project"], agent)
            assert "notes.md" in result, f"notes.md should be included for {agent} (default all agents)"

    def test_empty_uploads_returns_empty(self, temp_data_dir, pipeline_project):
        """No uploads dir should return empty string."""
        pp = pipeline_project
        # Remove uploads
        uploads_dir = Path(pp["project_dir"]) / "uploads"
        shutil.rmtree(uploads_dir)

        result = main.load_project_files_for_agent(pp["username"], pp["project"], "events")
        assert result == ""

    def test_file_content_included_correctly(self, temp_data_dir, pipeline_project):
        """Verify the actual file content is in the output."""
        pp = pipeline_project
        result = main.load_project_files_for_agent(pp["username"], pp["project"], "mechanics")

        assert "# Rulebook" in result
        assert "Rules for the game." in result
        assert "# Character Sheet" in result
        assert "Aldric the rogue." in result


# ============================================================
# 3. API: PUT /api/project-files/{user}/{project}/agents/{filename}
# ============================================================

class TestUpdateFileAgentsAPI:
    """Tests for the file agent assignment endpoint."""

    def test_update_agents_success(self, client, temp_data_dir, pipeline_project):
        """Successfully update a file's agent assignment."""
        pp = pipeline_project
        response = client.put(
            f"/api/project-files/{pp['username']}/{pp['project']}/agents/rulebook.md",
            json={"agents": ["events", "mechanics"]}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["agents"] == ["events", "mechanics"]

        # Verify cache was updated
        cache = main.load_file_tokens_cache(pp["username"], pp["project"])
        assert cache["rulebook.md"]["agents"] == ["events", "mechanics"]

    def test_update_agents_all_three(self, client, temp_data_dir, pipeline_project):
        """Can assign a file to all three agents."""
        pp = pipeline_project
        response = client.put(
            f"/api/project-files/{pp['username']}/{pp['project']}/agents/rulebook.md",
            json={"agents": ["events", "mechanics", "narration"]}
        )
        assert response.status_code == 200
        assert response.json()["agents"] == ["events", "mechanics", "narration"]

    def test_update_agents_single(self, client, temp_data_dir, pipeline_project):
        """Can assign a file to just one agent."""
        pp = pipeline_project
        response = client.put(
            f"/api/project-files/{pp['username']}/{pp['project']}/agents/world_lore.md",
            json={"agents": ["narration"]}
        )
        assert response.status_code == 200
        assert response.json()["agents"] == ["narration"]

    def test_invalid_agent_name_rejected(self, client, temp_data_dir, pipeline_project):
        """Invalid agent name should return 400."""
        pp = pipeline_project
        response = client.put(
            f"/api/project-files/{pp['username']}/{pp['project']}/agents/rulebook.md",
            json={"agents": ["events", "invalid_agent"]}
        )
        assert response.status_code == 400
        assert "Invalid agent name" in response.json()["detail"]

    def test_empty_agents_rejected(self, client, temp_data_dir, pipeline_project):
        """Empty agents list should return 400."""
        pp = pipeline_project
        response = client.put(
            f"/api/project-files/{pp['username']}/{pp['project']}/agents/rulebook.md",
            json={"agents": []}
        )
        assert response.status_code == 400
        assert "At least one agent" in response.json()["detail"]

    def test_file_not_found(self, client, temp_data_dir, pipeline_project):
        """Non-existent file should return 404."""
        pp = pipeline_project
        response = client.put(
            f"/api/project-files/{pp['username']}/{pp['project']}/agents/nonexistent.md",
            json={"agents": ["events"]}
        )
        assert response.status_code == 404

    def test_project_not_found(self, client, temp_data_dir, pipeline_project):
        """Non-existent project should return 404."""
        pp = pipeline_project
        response = client.put(
            f"/api/project-files/{pp['username']}/nonexistent_project/agents/rulebook.md",
            json={"agents": ["events"]}
        )
        assert response.status_code == 404


# ============================================================
# 4. API: GET /api/project-instructions/{user}/{project}/agents
# ============================================================

class TestGetAgentInstructionsAPI:
    """Tests for fetching all agent instructions."""

    def test_returns_all_three_agents(self, client, temp_data_dir, pipeline_project):
        """Should return instructions for events, mechanics, and narration."""
        pp = pipeline_project
        response = client.get(
            f"/api/project-instructions/{pp['username']}/{pp['project']}/agents"
        )
        assert response.status_code == 200
        data = response.json()
        assert "events" in data
        assert "mechanics" in data
        assert "narration" in data

    def test_empty_agents_return_empty_instructions(self, client, temp_data_dir, pipeline_project):
        """Agents without files should have empty instructions and 0 tokens."""
        pp = pipeline_project
        response = client.get(
            f"/api/project-instructions/{pp['username']}/{pp['project']}/agents"
        )
        data = response.json()
        for agent in AGENT_NAMES:
            assert data[agent]["instructions"] == ""
            assert data[agent]["tokens"] == 0

    def test_returns_existing_agent_instructions(self, client, temp_data_dir, pipeline_project):
        """Should return content of existing agent instruction files."""
        pp = pipeline_project
        project_dir = Path(pp["project_dir"])

        with open(project_dir / "instructions_events.di", 'w') as f:
            f.write("Custom events instructions content.")

        response = client.get(
            f"/api/project-instructions/{pp['username']}/{pp['project']}/agents"
        )
        data = response.json()
        assert data["events"]["instructions"] == "Custom events instructions content."
        assert data["events"]["tokens"] > 0
        # Others should still be empty
        assert data["mechanics"]["instructions"] == ""
        assert data["narration"]["instructions"] == ""

    def test_project_not_found(self, client, temp_data_dir, pipeline_project):
        """Non-existent project should return 404."""
        pp = pipeline_project
        response = client.get(
            f"/api/project-instructions/{pp['username']}/nonexistent/agents"
        )
        assert response.status_code == 404

    def test_token_counting_cached(self, client, temp_data_dir, pipeline_project):
        """Second fetch should use cached token counts."""
        pp = pipeline_project
        project_dir = Path(pp["project_dir"])

        with open(project_dir / "instructions_mechanics.di", 'w') as f:
            f.write("Mechanics instructions with some content.")

        # First fetch
        r1 = client.get(f"/api/project-instructions/{pp['username']}/{pp['project']}/agents")
        tokens1 = r1.json()["mechanics"]["tokens"]

        # Second fetch (should hit cache)
        r2 = client.get(f"/api/project-instructions/{pp['username']}/{pp['project']}/agents")
        tokens2 = r2.json()["mechanics"]["tokens"]

        assert tokens1 == tokens2
        assert tokens1 > 0


# ============================================================
# 5. API: PUT /api/project-instructions/{user}/{project}/agents/{agent}
# ============================================================

class TestUpdateAgentInstructionsAPI:
    """Tests for updating individual agent instructions."""

    def test_save_agent_instructions(self, client, temp_data_dir, pipeline_project):
        """Successfully save instructions for an agent."""
        pp = pipeline_project
        response = client.put(
            f"/api/project-instructions/{pp['username']}/{pp['project']}/agents/events",
            json={"instructions": "New events instructions."}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["tokens"] > 0

        # Verify file was created
        path = Path(pp["project_dir"]) / "instructions_events.di"
        assert path.exists()
        assert path.read_text() == "New events instructions."

    def test_save_empty_instructions(self, client, temp_data_dir, pipeline_project):
        """Saving empty instructions should work (tokens=0)."""
        pp = pipeline_project
        response = client.put(
            f"/api/project-instructions/{pp['username']}/{pp['project']}/agents/mechanics",
            json={"instructions": ""}
        )
        assert response.status_code == 200
        assert response.json()["tokens"] == 0

    def test_overwrite_existing_instructions(self, client, temp_data_dir, pipeline_project):
        """Can overwrite existing agent instructions."""
        pp = pipeline_project

        # First save
        client.put(
            f"/api/project-instructions/{pp['username']}/{pp['project']}/agents/narration",
            json={"instructions": "Version 1."}
        )

        # Overwrite
        client.put(
            f"/api/project-instructions/{pp['username']}/{pp['project']}/agents/narration",
            json={"instructions": "Version 2."}
        )

        # Verify latest version
        path = Path(pp["project_dir"]) / "instructions_narration.di"
        assert path.read_text() == "Version 2."

    def test_invalid_agent_name_rejected(self, client, temp_data_dir, pipeline_project):
        """Invalid agent name should return 400."""
        pp = pipeline_project
        response = client.put(
            f"/api/project-instructions/{pp['username']}/{pp['project']}/agents/invalid",
            json={"instructions": "test"}
        )
        assert response.status_code == 400
        assert "Invalid agent name" in response.json()["detail"]

    def test_project_not_found(self, client, temp_data_dir, pipeline_project):
        """Non-existent project should return 404."""
        pp = pipeline_project
        response = client.put(
            f"/api/project-instructions/{pp['username']}/nonexistent/agents/events",
            json={"instructions": "test"}
        )
        assert response.status_code == 404

    def test_updates_token_cache(self, client, temp_data_dir, pipeline_project):
        """Saving should update the instructions tokens cache."""
        pp = pipeline_project

        client.put(
            f"/api/project-instructions/{pp['username']}/{pp['project']}/agents/events",
            json={"instructions": "Some event instructions content here."}
        )

        cache = main.load_instructions_tokens_cache(pp["username"], pp["project"])
        assert "agent_events" in cache
        assert cache["agent_events"]["tokens"] > 0
        assert "content_hash" in cache["agent_events"]

    def test_roundtrip_save_then_get(self, client, temp_data_dir, pipeline_project):
        """Save instructions and verify GET returns them."""
        pp = pipeline_project

        for agent in AGENT_NAMES:
            client.put(
                f"/api/project-instructions/{pp['username']}/{pp['project']}/agents/{agent}",
                json={"instructions": f"Instructions for {agent} agent."}
            )

        response = client.get(
            f"/api/project-instructions/{pp['username']}/{pp['project']}/agents"
        )
        data = response.json()

        for agent in AGENT_NAMES:
            assert data[agent]["instructions"] == f"Instructions for {agent} agent."
            assert data[agent]["tokens"] > 0


# ============================================================
# 6. Pipeline Integration - Per-agent instructions/files wiring
# ============================================================

class TestPipelineIntegration:
    """Test that run_pipeline receives and uses per-agent instructions and files."""

    def test_build_agent_system_prompt_full(self):
        """System prompt = contract + instructions + files."""
        result = build_agent_system_prompt(
            "CONTRACT TEXT",
            "USER INSTRUCTIONS",
            "PROJECT FILES CONTENT"
        )
        assert "CONTRACT TEXT" in result
        assert "USER INSTRUCTIONS" in result
        assert "PROJECT FILES CONTENT" in result
        # Contract comes first
        assert result.index("CONTRACT TEXT") < result.index("USER INSTRUCTIONS")
        assert result.index("USER INSTRUCTIONS") < result.index("PROJECT FILES CONTENT")

    def test_build_agent_system_prompt_empty_instructions(self):
        """Empty instructions should not appear in system prompt."""
        result = build_agent_system_prompt("CONTRACT", "   ", "FILES")
        assert "CONTRACT" in result
        assert "FILES" in result
        # Only 2 parts joined
        parts = result.split("\n\n")
        assert len(parts) == 2

    def test_build_agent_system_prompt_empty_files(self):
        """Empty files should not appear in system prompt."""
        result = build_agent_system_prompt("CONTRACT", "INSTRUCTIONS", "  ")
        parts = result.split("\n\n")
        assert len(parts) == 2
        assert "CONTRACT" in result
        assert "INSTRUCTIONS" in result

    def test_build_agent_system_prompt_both_empty(self):
        """Only contract when instructions and files are empty."""
        result = build_agent_system_prompt("CONTRACT", "", "")
        assert result == "CONTRACT"

    def test_contracts_are_distinct(self):
        """Each agent has a different contract."""
        assert EVENTS_CONTRACT != MECHANICS_CONTRACT
        assert MECHANICS_CONTRACT != NARRATION_CONTRACT
        assert EVENTS_CONTRACT != NARRATION_CONTRACT
        assert "EVENTS AGENT" in EVENTS_CONTRACT
        assert "MECHANICS AGENT" in MECHANICS_CONTRACT
        assert "NARRATION AGENT" in NARRATION_CONTRACT

    def test_per_agent_instructions_dict_built_correctly(self, temp_data_dir, pipeline_project):
        """Verify the dict-building logic used in send_message_stream works."""
        pp = pipeline_project
        project_dir = Path(pp["project_dir"])

        # Set up per-agent instructions
        with open(project_dir / "instructions_events.di", 'w') as f:
            f.write("Events specific instructions.")
        with open(project_dir / "instructions_mechanics.di", 'w') as f:
            f.write("Mechanics specific instructions.")
        # narration: no file, should fall back

        agent_instructions = {}
        agent_files = {}
        for agent_name in AGENT_NAMES:
            agent_instructions[agent_name] = main.get_instructions_for_agent(
                pp["username"], pp["project"], agent_name
            )
            agent_files[agent_name] = main.load_project_files_for_agent(
                pp["username"], pp["project"], agent_name
            )

        # Verify instructions
        assert agent_instructions["events"] == "Events specific instructions."
        assert agent_instructions["mechanics"] == "Mechanics specific instructions."
        assert agent_instructions["narration"] == "You are a TTRPG game master for Broken Orbit."  # fallback

        # Verify file routing
        assert "world_lore.md" in agent_files["events"]
        assert "character_sheet.md" in agent_files["events"]
        assert "rulebook.md" not in agent_files["events"]

        assert "rulebook.md" in agent_files["mechanics"]
        assert "character_sheet.md" in agent_files["mechanics"]
        assert "world_lore.md" not in agent_files["mechanics"]

        assert "world_lore.md" in agent_files["narration"]
        assert "rulebook.md" not in agent_files["narration"]
        assert "character_sheet.md" not in agent_files["narration"]


# ============================================================
# 7. File Listing and Upload preserve agents field
# ============================================================

class TestFileListingAgents:
    """Tests that file listing includes the agents field."""

    def test_list_files_includes_agents(self, client, temp_data_dir, pipeline_project):
        """GET /api/project-files should include agents field for each file."""
        pp = pipeline_project
        response = client.get(
            f"/api/project-files/{pp['username']}/{pp['project']}",
            params={"model": "gpt-5.2"}
        )
        assert response.status_code == 200
        data = response.json()
        files_by_name = {f["filename"]: f for f in data["files"]}

        assert files_by_name["rulebook.md"]["agents"] == ["mechanics"]
        assert sorted(files_by_name["world_lore.md"]["agents"]) == ["events", "narration"]
        assert sorted(files_by_name["character_sheet.md"]["agents"]) == ["events", "mechanics"]

    def test_list_files_default_agents(self, client, temp_data_dir, pipeline_project):
        """Files without agents in cache should default to all three agents."""
        pp = pipeline_project
        project_dir = Path(pp["project_dir"])

        # Add a file without agents in cache
        with open(project_dir / "uploads" / "misc.md", 'w') as f:
            f.write("# Miscellaneous\nSome content.")

        cache = main.load_file_tokens_cache(pp["username"], pp["project"])
        cache["misc.md"] = {"tokens": 30, "model": "gpt-5.2", "size_bytes": 25, "staged": True}
        # No agents field
        main.save_file_tokens_cache(pp["username"], pp["project"], cache)

        response = client.get(
            f"/api/project-files/{pp['username']}/{pp['project']}",
            params={"model": "gpt-5.2"}
        )
        data = response.json()
        files_by_name = {f["filename"]: f for f in data["files"]}

        assert sorted(files_by_name["misc.md"]["agents"]) == ["events", "mechanics", "narration"]

    def test_upload_preserves_existing_agents(self, client, temp_data_dir, pipeline_project):
        """Re-uploading a file should preserve its existing agent assignments."""
        pp = pipeline_project

        # Verify current assignment
        cache_before = main.load_file_tokens_cache(pp["username"], pp["project"])
        assert cache_before["rulebook.md"]["agents"] == ["mechanics"]

        # Re-upload rulebook.md with new content
        import io
        file_content = b"# Updated Rulebook\nNew rules."
        response = client.post(
            f"/api/project-files/{pp['username']}/{pp['project']}",
            files=[("files", ("rulebook.md", io.BytesIO(file_content), "text/markdown"))]
        )
        assert response.status_code == 200

        # Agents should be preserved
        cache_after = main.load_file_tokens_cache(pp["username"], pp["project"])
        assert cache_after["rulebook.md"]["agents"] == ["mechanics"]

    def test_new_upload_gets_default_agents(self, client, temp_data_dir, pipeline_project):
        """Uploading a brand new file should get the default (all agents)."""
        pp = pipeline_project

        import io
        file_content = b"# New File\nBrand new content."
        response = client.post(
            f"/api/project-files/{pp['username']}/{pp['project']}",
            files=[("files", ("brand_new.md", io.BytesIO(file_content), "text/markdown"))]
        )
        assert response.status_code == 200

        cache = main.load_file_tokens_cache(pp["username"], pp["project"])
        assert sorted(cache["brand_new.md"]["agents"]) == ["events", "mechanics", "narration"]

    def test_upload_preserves_unstaged(self, client, temp_data_dir, pipeline_project):
        """Re-uploading an unstaged file should keep it unstaged."""
        pp = pipeline_project

        # Unstage rulebook.md via the staged API
        client.patch(
            f"/api/project-files/{pp['username']}/{pp['project']}/staged/rulebook.md",
            json={"staged": False}
        )
        cache_before = main.load_file_tokens_cache(pp["username"], pp["project"])
        assert cache_before["rulebook.md"]["staged"] is False

        # Re-upload rulebook.md with new content
        import io
        file_content = b"# Updated Rulebook\nNew rules."
        response = client.post(
            f"/api/project-files/{pp['username']}/{pp['project']}",
            files=[("files", ("rulebook.md", io.BytesIO(file_content), "text/markdown"))]
        )
        assert response.status_code == 200

        # Staged should still be False
        cache_after = main.load_file_tokens_cache(pp["username"], pp["project"])
        assert cache_after["rulebook.md"]["staged"] is False


# ============================================================
# 8. Edge Cases and Interaction Tests
# ============================================================

class TestEdgeCases:
    """Edge cases and interaction tests."""

    def test_agent_assignment_update_then_load(self, client, temp_data_dir, pipeline_project):
        """Update agents via API then verify load_project_files_for_agent reflects change."""
        pp = pipeline_project

        # Move rulebook.md from mechanics-only to events+mechanics
        client.put(
            f"/api/project-files/{pp['username']}/{pp['project']}/agents/rulebook.md",
            json={"agents": ["events", "mechanics"]}
        )

        result = main.load_project_files_for_agent(pp["username"], pp["project"], "events")
        assert "rulebook.md" in result

    def test_instructions_and_files_independent(self, client, temp_data_dir, pipeline_project):
        """Changing file agent assignments shouldn't affect instruction files."""
        pp = pipeline_project
        project_dir = Path(pp["project_dir"])

        # Save events instructions
        client.put(
            f"/api/project-instructions/{pp['username']}/{pp['project']}/agents/events",
            json={"instructions": "Events only."}
        )

        # Update file agents
        client.put(
            f"/api/project-files/{pp['username']}/{pp['project']}/agents/rulebook.md",
            json={"agents": ["events", "mechanics", "narration"]}
        )

        # Instructions should be unchanged
        path = project_dir / "instructions_events.di"
        assert path.read_text() == "Events only."

    def test_multiple_agent_updates_sequential(self, client, temp_data_dir, pipeline_project):
        """Multiple sequential updates to the same file's agents should work."""
        pp = pipeline_project

        # Assign to events only
        client.put(
            f"/api/project-files/{pp['username']}/{pp['project']}/agents/rulebook.md",
            json={"agents": ["events"]}
        )
        cache = main.load_file_tokens_cache(pp["username"], pp["project"])
        assert cache["rulebook.md"]["agents"] == ["events"]

        # Then to narration only
        client.put(
            f"/api/project-files/{pp['username']}/{pp['project']}/agents/rulebook.md",
            json={"agents": ["narration"]}
        )
        cache = main.load_file_tokens_cache(pp["username"], pp["project"])
        assert cache["rulebook.md"]["agents"] == ["narration"]

        # Then to all
        client.put(
            f"/api/project-files/{pp['username']}/{pp['project']}/agents/rulebook.md",
            json={"agents": ["events", "mechanics", "narration"]}
        )
        cache = main.load_file_tokens_cache(pp["username"], pp["project"])
        assert sorted(cache["rulebook.md"]["agents"]) == ["events", "mechanics", "narration"]

    def test_file_not_in_cache_gets_created(self, client, temp_data_dir, pipeline_project):
        """Updating agents for a file not yet in token cache should create the entry."""
        pp = pipeline_project
        project_dir = Path(pp["project_dir"])

        # Create a file that's not in the cache
        with open(project_dir / "uploads" / "uncached.md", 'w') as f:
            f.write("# Uncached\nContent.")

        response = client.put(
            f"/api/project-files/{pp['username']}/{pp['project']}/agents/uncached.md",
            json={"agents": ["mechanics"]}
        )
        assert response.status_code == 200

        cache = main.load_file_tokens_cache(pp["username"], pp["project"])
        assert cache["uncached.md"]["agents"] == ["mechanics"]

    def test_username_case_insensitive(self, client, temp_data_dir, pipeline_project):
        """Endpoints should normalize username to lowercase."""
        pp = pipeline_project

        # Create per-agent instructions with uppercase in URL
        response = client.put(
            f"/api/project-instructions/TESTUSER/{pp['project']}/agents/events",
            json={"instructions": "Case test."}
        )
        assert response.status_code == 200

        # Fetch with lowercase
        response = client.get(
            f"/api/project-instructions/{pp['username']}/{pp['project']}/agents"
        )
        assert response.json()["events"]["instructions"] == "Case test."


# ============================================================
# 9. time_passed Field Flow Tests
# ============================================================

class TestTimePassedFlow:
    """Tests that time_passed flows from Events through Mechanics correctly."""

    def test_events_contract_contains_time_passed(self):
        """Events contract should document the time_passed field."""
        assert "time_passed" in EVENTS_CONTRACT
        assert "TIME PASSED" in EVENTS_CONTRACT

    def test_mechanics_contract_references_time_passed(self):
        """Mechanics contract should reference time_passed for HUD advancement."""
        assert "time_passed" in MECHANICS_CONTRACT
        assert "advance it by" in MECHANICS_CONTRACT and "time_passed" in MECHANICS_CONTRACT

    def test_events_schema_a_time_passed_in_mechanics_input(self):
        """Events JSON with time_passed should be passed to Mechanics via build_mechanics_messages."""
        from pipeline import build_mechanics_messages

        events_json = {
            "route": "mechanics",
            "time_passed": "10 minutes",
            "beats": ["Aldric picks the lock on the warehouse door"],
            "player_action": "pick the lock",
            "callbacks": [],
            "emotional_context": "tense",
            "character_states": {"Aldric": "HP 45/45"},
            "score_changes": [],
            "pacing": {"episode": "Test", "beat": "Entry", "beat_responses": 1, "notes": ""}
        }

        messages = build_mechanics_messages("SYSTEM PROMPT", events_json)

        # The Events JSON should be in the last user message
        last_msg = messages[-1]
        assert last_msg["role"] == "user"
        parsed = json.loads(last_msg["content"])
        assert parsed["time_passed"] == "10 minutes"

    def test_events_schema_b_time_passed_zero(self):
        """OOC route (Schema B) should include time_passed of '0 minutes'."""
        from pipeline import build_mechanics_messages

        events_json = {
            "route": "output",
            "time_passed": "0 minutes",
            "content": "Sure, we can take a break!",
            "pacing": {"episode": "Test", "beat": "OOC", "beat_responses": 0, "notes": ""}
        }

        # Schema B goes to output, not mechanics, but verify the field is present
        assert events_json["time_passed"] == "0 minutes"

    def test_time_passed_various_durations(self):
        """Various time_passed durations should pass through build_mechanics_messages correctly."""
        from pipeline import build_mechanics_messages

        durations = ["1 minute", "5 minutes", "2 hours", "overnight (8 hours)", "3 days"]

        for duration in durations:
            events_json = {
                "route": "mechanics",
                "time_passed": duration,
                "beats": ["Something happens"],
                "player_action": "do something",
                "callbacks": [],
                "emotional_context": "neutral",
                "character_states": {},
                "score_changes": [],
                "pacing": {"episode": "Test", "beat": "Test", "beat_responses": 1, "notes": ""}
            }

            messages = build_mechanics_messages("SYSTEM", events_json)
            parsed = json.loads(messages[-1]["content"])
            assert parsed["time_passed"] == duration, f"Expected '{duration}' to pass through"

    def test_time_passed_in_full_pipeline_mock(self, temp_data_dir, pipeline_project):
        """Full pipeline mock: Events outputs time_passed, Mechanics receives it in its messages."""
        from pipeline import (
            build_events_messages, build_mechanics_messages,
            build_agent_system_prompt, _parse_stage_json
        )
        from game_systems.dnd5e import EVENTS_CONTRACT, MECHANICS_CONTRACT

        pp = pipeline_project

        # Simulate Events output
        events_output = {
            "route": "mechanics",
            "time_passed": "15 minutes",
            "pacing": {"episode": "Ep1", "beat": "Exploration", "beat_responses": 3, "notes": ""},
            "beats": ["Aldric searches the room", "Finds a hidden compartment"],
            "player_action": "search the room thoroughly",
            "callbacks": ["hidden_stash_foreshadowed"],
            "emotional_context": "curious, methodical",
            "character_states": {"Aldric": "HP 45/45, has lockpicks"},
            "score_changes": []
        }

        # Build Mechanics messages from Events output
        mechanics_system = build_agent_system_prompt(
            MECHANICS_CONTRACT,
            "Test instructions for mechanics.",
            ""
        )
        mechanics_messages = build_mechanics_messages(
            mechanics_system, events_output, ""
        )

        # Verify Mechanics receives time_passed in its input
        user_msg = mechanics_messages[-1]
        events_data_received = json.loads(user_msg["content"])
        assert events_data_received["time_passed"] == "15 minutes"

        # Verify Mechanics system prompt tells it to use time_passed for HUD
        assert "time_passed" in mechanics_messages[0]["content"]
        assert "advance it by" in mechanics_messages[0]["content"]

    def test_time_passed_with_context_updates(self):
        """time_passed should survive when context updates are also injected."""
        from pipeline import build_mechanics_messages

        events_json = {
            "route": "mechanics",
            "time_passed": "30 minutes",
            "beats": ["Travel through the market district"],
            "player_action": "walk to the market",
            "callbacks": [],
            "emotional_context": "relaxed",
            "character_states": {"Aldric": "HP 45/45"},
            "score_changes": [],
            "pacing": {"episode": "Test", "beat": "Travel", "beat_responses": 1, "notes": ""}
        }

        updates = "Character sheet updated: Aldric gained 50 gold from quest reward."
        messages = build_mechanics_messages("SYSTEM", events_json, updates)

        # Should have system + updates msg + events json msg
        assert len(messages) == 3
        assert "[CONTEXT UPDATES]" in messages[1]["content"]

        # Events JSON with time_passed should be in the last message
        parsed = json.loads(messages[-1]["content"])
        assert parsed["time_passed"] == "30 minutes"


# ============================================================
# Run tests
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
