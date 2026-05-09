"""Unit tests for character_project_state — load/save/overlay/persist
plus the file-locking and PROJECT_LEVEL_FIELDS contract.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from character_project_state import (  # noqa: E402
    PROJECT_LEVEL_FIELDS,
    PROJECT_STATE_FILENAME,
    load_project_state,
    save_project_state,
    overlay_project_state_into,
    persist_project_state_from,
    _project_state_path,
)


@pytest.fixture
def project_dir():
    """Temp project dir fresh for each test."""
    with tempfile.TemporaryDirectory() as d:
        yield d


def test_load_returns_none_when_file_missing(project_dir):
    assert load_project_state(project_dir) is None


def test_load_returns_none_when_project_dir_falsy():
    assert load_project_state(None) is None
    assert load_project_state("") is None


def test_save_returns_false_for_falsy_project_dir():
    assert save_project_state(None, {"callbacks": {}}) is False
    assert save_project_state("", {"callbacks": {}}) is False


def test_save_returns_false_for_non_dict_state(project_dir):
    assert save_project_state(project_dir, None) is False
    assert save_project_state(project_dir, "not a dict") is False
    assert save_project_state(project_dir, []) is False


def test_save_then_load_round_trip(project_dir):
    state = {
        "callbacks": {"next_id": 5, "open": [{"id": 1, "topic": "test"}], "resolved": [], "dismissed": []},
        "wellbeing": {"state": "Even", "rolled_date": "2026-05-09", "wb_mod": 1},
        "arc_state": "established daily rhythm",
        "flakiness_bands": {
            "work":   {"as_planned": 0.99, "modified": 0.0,  "cancelled": 0.01},
            "social": {"as_planned": 0.80, "modified": 0.15, "cancelled": 0.05},
        },
        "scheduler_state": {
            "last_resolver_run_at": "2026-05-09T08:00:00-04:00",
            "last_planner_run_week_of": "2026-05-04",
        },
        "last_ripeness_rolled_date": "2026-05-09",
        # Chat-level field that should NOT be saved
        "channel": "text",
        "wall_clock": {"last_user_message_at": "2026-05-09T12:00:00Z"},
    }
    assert save_project_state(project_dir, state) is True

    loaded = load_project_state(project_dir)
    assert loaded is not None
    # All project-level fields round-tripped
    for f in PROJECT_LEVEL_FIELDS:
        assert loaded[f] == state[f], f"Field {f} did not round-trip"
    # Chat-level fields NOT in the file
    assert "channel" not in loaded
    assert "wall_clock" not in loaded


def test_save_creates_file_at_expected_path(project_dir):
    save_project_state(project_dir, {"callbacks": {}})
    assert os.path.isfile(_project_state_path(project_dir))
    assert os.path.basename(_project_state_path(project_dir)) == PROJECT_STATE_FILENAME


def test_save_overwrites_atomically(project_dir):
    save_project_state(project_dir, {"arc_state": "first"})
    save_project_state(project_dir, {"arc_state": "second"})
    loaded = load_project_state(project_dir)
    assert loaded["arc_state"] == "second"


def test_save_only_persists_known_project_fields(project_dir):
    state = {
        "callbacks": {"next_id": 1, "open": [], "resolved": [], "dismissed": []},
        "channel": "text",
        "wall_clock": {},
        "off_screen_log": {"days": []},
        "ripe_callbacks": [1, 2, 3],
        "_render_payload": {"big": "transient"},
    }
    save_project_state(project_dir, state)
    loaded = load_project_state(project_dir)
    assert "callbacks" in loaded
    # Chat-level / transient fields filtered out
    assert "channel" not in loaded
    assert "wall_clock" not in loaded
    assert "off_screen_log" not in loaded
    assert "ripe_callbacks" not in loaded
    assert "_render_payload" not in loaded


def test_overlay_no_op_when_file_missing(project_dir):
    cs = {"callbacks": {"next_id": 1, "open": [], "resolved": [], "dismissed": []}, "channel": "text"}
    original = dict(cs)
    overlay_project_state_into(cs, project_dir)
    assert cs == original


def test_overlay_replaces_default_values_with_file(project_dir):
    save_project_state(project_dir, {
        "callbacks": {"next_id": 7, "open": [{"id": 5}], "resolved": [], "dismissed": []},
        "arc_state": "from file",
    })
    cs = {
        "callbacks": {"next_id": 1, "open": [], "resolved": [], "dismissed": []},  # default
        "arc_state": "",
        "wellbeing": {"state": "Even"},  # not in file
        "channel": "text",  # chat-level
    }
    overlay_project_state_into(cs, project_dir)
    assert cs["callbacks"]["next_id"] == 7
    assert cs["callbacks"]["open"] == [{"id": 5}]
    assert cs["arc_state"] == "from file"
    # Fields not in the file untouched
    assert cs["wellbeing"]["state"] == "Even"
    # Chat-level untouched
    assert cs["channel"] == "text"


def test_overlay_no_op_for_invalid_inputs(project_dir):
    overlay_project_state_into(None, project_dir)  # no crash
    overlay_project_state_into("not a dict", project_dir)  # no crash
    cs = {"callbacks": {}}
    overlay_project_state_into(cs, None)  # no crash, no read
    overlay_project_state_into(cs, "")
    assert cs == {"callbacks": {}}


def test_persist_helper_writes_only_project_fields(project_dir):
    cs = {
        "callbacks": {"next_id": 3, "open": [], "resolved": [], "dismissed": []},
        "wellbeing": {"state": "Even", "rolled_date": "2026-05-09", "wb_mod": 0},
        "arc_state": "test",
        "life_events": {"pending_seed": None},
        "last_ripeness_rolled_date": "2026-05-09",
        "channel": "phone",  # should NOT persist
    }
    persist_project_state_from(cs, project_dir)
    loaded = load_project_state(project_dir)
    assert loaded["arc_state"] == "test"
    assert loaded["last_ripeness_rolled_date"] == "2026-05-09"
    assert "channel" not in loaded


def test_persist_helper_no_op_for_invalid_state(project_dir):
    persist_project_state_from(None, project_dir)
    persist_project_state_from("not a dict", project_dir)
    # File should not be created
    assert not os.path.isfile(_project_state_path(project_dir))


def test_load_returns_none_for_corrupt_file(project_dir):
    """Garbage in the file should return None, not crash, so the next save
    can replace it with a valid payload."""
    path = _project_state_path(project_dir)
    with open(path, "w") as f:
        f.write("not valid json {{{")
    assert load_project_state(project_dir) is None


def test_load_returns_none_when_file_is_a_list(project_dir):
    """File that's valid JSON but wrong shape returns None."""
    path = _project_state_path(project_dir)
    with open(path, "w") as f:
        f.write("[1, 2, 3]")
    assert load_project_state(project_dir) is None


def test_save_then_overlay_roundtrip_preserves_nested_data(project_dir):
    """End-to-end: complex callback list + nested scheduler_state survive."""
    rich_state = {
        "callbacks": {
            "next_id": 12,
            "open": [
                {"id": 10, "topic": "job interview", "source": "user", "created_date": "2026-05-01"},
                {"id": 11, "topic": "her aunt visiting", "source": "user", "created_date": "2026-05-03"},
            ],
            "resolved": [{"id": 5, "topic": "old thing"}],
            "dismissed": [],
        },
        "scheduler_state": {
            "first_seen_date": "2026-04-01",
            "last_resolver_run_at": "2026-05-09T13:00:00-04:00",
            "last_planner_run_week_of": "2026-05-04",
        },
        "flakiness_bands": {
            "work":   {"as_planned": 0.99, "modified": 0.0,  "cancelled": 0.01},
        },
    }
    save_project_state(project_dir, rich_state)
    fresh_cs = {"callbacks": {}, "scheduler_state": {}}
    overlay_project_state_into(fresh_cs, project_dir)
    assert fresh_cs["callbacks"]["next_id"] == 12
    assert len(fresh_cs["callbacks"]["open"]) == 2
    assert fresh_cs["scheduler_state"]["first_seen_date"] == "2026-04-01"
    assert fresh_cs["scheduler_state"]["last_planner_run_week_of"] == "2026-05-04"
    assert fresh_cs["flakiness_bands"]["work"]["as_planned"] == 0.99


def test_PROJECT_LEVEL_FIELDS_includes_ripeness_date():
    """Regression guard: bug-review P1.2 added last_ripeness_rolled_date to
    project-level fields; ensure the constant doesn't silently drop it."""
    assert "last_ripeness_rolled_date" in PROJECT_LEVEL_FIELDS
    assert "callbacks" in PROJECT_LEVEL_FIELDS
    assert "wellbeing" in PROJECT_LEVEL_FIELDS
    assert "arc_state" in PROJECT_LEVEL_FIELDS
    assert "flakiness_bands" in PROJECT_LEVEL_FIELDS
    assert "scheduler_state" in PROJECT_LEVEL_FIELDS
    # Phase 3: life_events folded into the planner; major events are entries
    # in life_stream.jsonl with kind=major_event, no longer a state field.
    assert "life_events" not in PROJECT_LEVEL_FIELDS
    # Chat-level fields explicitly excluded
    assert "channel" not in PROJECT_LEVEL_FIELDS
    assert "wall_clock" not in PROJECT_LEVEL_FIELDS
    assert "off_screen_log" not in PROJECT_LEVEL_FIELDS
    assert "ripe_callbacks" not in PROJECT_LEVEL_FIELDS
