"""Unit tests for character_schedule — schedule.json + life_stream.jsonl
storage, mutation ops, and the outcome-roll dice mechanic.
"""
import json
import os
import random
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from character_schedule import (  # noqa: E402
    apply_schedule_ops,
    append_life_stream,
    append_life_stream_many,
    effective_bands,
    init_schedule,
    load_schedule,
    read_life_stream_all,
    read_life_stream_tail,
    roll_event_outcome,
    save_schedule,
    stamp_resolution,
)


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _seed_schedule(project_dir, events):
    sched = init_schedule("2026-05-04", "2026-05-03T20:00:00-04:00")
    sched["events"] = events
    sched["next_id"] = max((int(e["id"].split("-")[-1]) for e in events if e.get("id", "").startswith("sch-")), default=0) + 1
    save_schedule(project_dir, sched)
    return sched


# ── load/save ────────────────────────────────────────────────────────

def test_load_returns_none_when_missing(project_dir):
    assert load_schedule(project_dir) is None


def test_load_returns_none_for_falsy_dir():
    assert load_schedule(None) is None
    assert load_schedule("") is None


def test_save_returns_false_for_invalid_args():
    assert save_schedule(None, {"events": []}) is False
    assert save_schedule("/nonexistent/path/123", "not a dict") is False


def test_save_then_load_roundtrip(project_dir):
    sched = init_schedule("2026-05-04", "2026-05-03T20:00:00-04:00")
    sched["events"] = [{"id": "sch-1", "title": "test", "status": "planned"}]
    assert save_schedule(project_dir, sched) is True
    loaded = load_schedule(project_dir)
    assert loaded is not None
    assert loaded["week_of"] == "2026-05-04"
    assert len(loaded["events"]) == 1
    assert loaded["events"][0]["id"] == "sch-1"


# ── apply_schedule_ops: cancel ───────────────────────────────────────

def test_cancel_op_marks_status_cancelled(project_dir):
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "Avatar", "kind": "social", "status": "planned",
         "when_local": "2026-05-09T20:00:00-04:00", "with": ["Kira"], "revision_history": []},
    ])
    apply_schedule_ops(project_dir, [
        {"op": "cancel", "event_id": "sch-1", "reason": "Kira bailed"}
    ], source="chat", now_iso="2026-05-08T19:00:00-04:00")
    sched = load_schedule(project_dir)
    ev = sched["events"][0]
    assert ev["status"] == "cancelled"
    assert len(ev["revision_history"]) == 1
    assert ev["revision_history"][0]["by"] == "chat"
    assert ev["revision_history"][0]["reason"] == "Kira bailed"


def test_cancel_op_writes_life_stream_entry(project_dir):
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "Avatar", "kind": "social", "status": "planned",
         "when_local": "2026-05-09T20:00:00-04:00", "revision_history": []},
    ])
    apply_schedule_ops(project_dir, [
        {"op": "cancel", "event_id": "sch-1", "reason": "Kira bailed"}
    ], source="chat", now_iso="2026-05-08T19:00:00-04:00")
    stream = read_life_stream_all(project_dir)
    assert len(stream) == 1
    assert stream[0]["kind"] == "schedule_change"
    assert stream[0]["ref"] == "sch-1"
    assert "Cancelled" in stream[0]["summary"]


def test_cancel_op_unknown_event_is_no_op(project_dir):
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "Avatar", "status": "planned", "revision_history": []},
    ])
    apply_schedule_ops(project_dir, [
        {"op": "cancel", "event_id": "sch-99", "reason": "?"}
    ], source="chat", now_iso="2026-05-08T19:00:00-04:00")
    sched = load_schedule(project_dir)
    assert sched["events"][0]["status"] == "planned"  # unchanged
    assert read_life_stream_all(project_dir) == []


# ── apply_schedule_ops: modify ───────────────────────────────────────

def test_modify_op_changes_fields_and_appends_revision(project_dir):
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "Avatar", "kind": "social", "status": "planned",
         "when_local": "2026-05-08T20:00:00-04:00", "with": ["Kira"], "revision_history": []},
    ])
    apply_schedule_ops(project_dir, [
        {"op": "modify", "event_id": "sch-1",
         "fields": {"when_local": "2026-05-09T20:00:00-04:00", "with": ["Kira", "Sam"]},
         "reason": "moved a day"}
    ], source="chat", now_iso="2026-05-06T19:00:00-04:00")
    ev = load_schedule(project_dir)["events"][0]
    assert ev["when_local"] == "2026-05-09T20:00:00-04:00"
    assert ev["with"] == ["Kira", "Sam"]
    assert ev["status"] == "planned"  # modify doesn't change status
    rev = ev["revision_history"][0]
    assert rev["reason"] == "moved a day"
    assert "when_local" in rev["diff"]
    assert rev["diff"]["when_local"]["from"] == "2026-05-08T20:00:00-04:00"
    assert rev["diff"]["when_local"]["to"] == "2026-05-09T20:00:00-04:00"


def test_modify_op_no_actual_change_skips_revision(project_dir):
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "Avatar", "status": "planned",
         "when_local": "2026-05-08T20:00:00-04:00", "revision_history": []},
    ])
    apply_schedule_ops(project_dir, [
        {"op": "modify", "event_id": "sch-1",
         "fields": {"when_local": "2026-05-08T20:00:00-04:00"},  # same value
         "reason": "no-op"}
    ], source="chat", now_iso="2026-05-06T19:00:00-04:00")
    ev = load_schedule(project_dir)["events"][0]
    assert len(ev["revision_history"]) == 0


def test_modify_op_cannot_change_status(project_dir):
    """Regression: a modify op must not be able to set status (e.g. cancelled)
    via the fields dict. The cancel/modify distinction must hold so the side
    agent can't bypass cancel by smuggling status into a modify."""
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "Avatar", "status": "planned", "revision_history": []},
    ])
    apply_schedule_ops(project_dir, [
        {"op": "modify", "event_id": "sch-1",
         "fields": {"status": "cancelled", "title": "Avatar 2"},
         "reason": "test"}
    ], source="chat", now_iso="2026-05-06T19:00:00-04:00")
    ev = load_schedule(project_dir)["events"][0]
    assert ev["status"] == "planned"  # status untouched
    assert ev["title"] == "Avatar 2"  # other field still applied
    # diff in revision_history should NOT include status
    rev = ev["revision_history"][0]
    assert "status" not in rev["diff"]
    assert "title" in rev["diff"]


def test_modify_op_cannot_change_magnitude(project_dir):
    """Regression: modify must not alter magnitude. Magnitude is set by the
    planner (Phase 3) when injecting a major life event, and the side agent
    shouldn't be able to "promote" a normal event to major."""
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "x", "status": "planned", "magnitude": "normal", "revision_history": []},
    ])
    apply_schedule_ops(project_dir, [
        {"op": "modify", "event_id": "sch-1",
         "fields": {"magnitude": "major"},
         "reason": "test"}
    ], source="chat", now_iso="2026-05-06T19:00:00-04:00")
    ev = load_schedule(project_dir)["events"][0]
    assert ev["magnitude"] == "normal"


def test_modify_op_cannot_change_resolution(project_dir):
    """Regression: modify must not write a resolution. Resolutions come from
    stamp_resolution (resolver path), not from chat-driven modifies."""
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "x", "status": "planned", "resolution": None, "revision_history": []},
    ])
    apply_schedule_ops(project_dir, [
        {"op": "modify", "event_id": "sch-1",
         "fields": {"resolution": {"outcome": "as_planned", "what_happened": "fake"}},
         "reason": "test"}
    ], source="chat", now_iso="2026-05-06T19:00:00-04:00")
    ev = load_schedule(project_dir)["events"][0]
    assert ev["resolution"] is None


def test_apply_ops_repairs_bad_revision_history_field(project_dir):
    """Defensive: a manually-edited schedule.json with revision_history=null or
    a non-list value should not crash apply_schedule_ops; it should be
    reinitialized to a list."""
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "x", "status": "planned", "revision_history": None},
    ])
    apply_schedule_ops(project_dir, [
        {"op": "cancel", "event_id": "sch-1", "reason": "test"}
    ], source="chat", now_iso="2026-05-06T19:00:00-04:00")
    ev = load_schedule(project_dir)["events"][0]
    assert ev["status"] == "cancelled"
    assert isinstance(ev["revision_history"], list)
    assert len(ev["revision_history"]) == 1


def test_modify_op_protects_immutable_fields(project_dir):
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "Avatar", "status": "planned", "revision_history": []},
    ])
    apply_schedule_ops(project_dir, [
        {"op": "modify", "event_id": "sch-1",
         "fields": {"id": "sch-99", "revision_history": ["nuked"], "title": "Avatar 2"},
         "reason": "test"}
    ], source="chat", now_iso="2026-05-06T19:00:00-04:00")
    ev = load_schedule(project_dir)["events"][0]
    assert ev["id"] == "sch-1"  # id immutable
    assert ev["title"] == "Avatar 2"  # title mutable
    assert isinstance(ev["revision_history"], list) and len(ev["revision_history"]) == 1


# ── apply_schedule_ops: add ──────────────────────────────────────────

def test_add_op_appends_event_with_next_id(project_dir):
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "Avatar", "status": "planned", "revision_history": []},
    ])
    apply_schedule_ops(project_dir, [
        {"op": "add",
         "fields": {"kind": "social", "title": "Brunch with Diana",
                    "when_local": "2026-05-10T11:00:00-04:00", "with": ["Diana"]},
         "reason": "just committed"}
    ], source="chat", now_iso="2026-05-09T17:00:00-04:00")
    sched = load_schedule(project_dir)
    assert len(sched["events"]) == 2
    new_ev = sched["events"][1]
    assert new_ev["id"] == "sch-2"
    assert new_ev["title"] == "Brunch with Diana"
    assert new_ev["status"] == "planned"
    assert new_ev["resolution"] is None
    assert sched["next_id"] == 3  # bumped


def test_add_op_missing_title_is_skipped(project_dir):
    _seed_schedule(project_dir, [])
    apply_schedule_ops(project_dir, [
        {"op": "add", "fields": {"kind": "social"}, "reason": "no title"}
    ], source="chat", now_iso="2026-05-09T17:00:00-04:00")
    sched = load_schedule(project_dir)
    assert sched["events"] == []


# ── apply_schedule_ops: source tracking ──────────────────────────────

def test_source_propagates_to_revision_history(project_dir):
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "Avatar", "status": "planned", "revision_history": []},
    ])
    apply_schedule_ops(project_dir, [
        {"op": "cancel", "event_id": "sch-1", "reason": "x"}
    ], source="resolver", now_iso="2026-05-08T19:00:00-04:00")
    rev = load_schedule(project_dir)["events"][0]["revision_history"][0]
    assert rev["by"] == "resolver"


# ── apply_schedule_ops: missing schedule.json ────────────────────────

def test_apply_ops_when_schedule_missing(project_dir):
    # No schedule.json at all
    result = apply_schedule_ops(project_dir, [
        {"op": "cancel", "event_id": "sch-1", "reason": "x"}
    ], source="chat", now_iso="2026-05-08T19:00:00-04:00")
    assert result == {}


# ── apply_schedule_ops: empty ops short-circuit ──────────────────────

def test_apply_ops_empty_ops_no_writes(project_dir):
    _seed_schedule(project_dir, [{"id": "sch-1", "title": "x", "status": "planned", "revision_history": []}])
    apply_schedule_ops(project_dir, [], source="chat")
    assert read_life_stream_all(project_dir) == []


# ── life_stream ──────────────────────────────────────────────────────

def test_append_life_stream_creates_file(project_dir):
    entry = {"id": "ls-1", "at_local": "2026-05-09T14:00:00-04:00",
             "kind": "unplanned", "summary": "stub", "tone": "even"}
    assert append_life_stream(project_dir, entry) is True
    assert os.path.isfile(os.path.join(project_dir, "life_stream.jsonl"))
    entries = read_life_stream_all(project_dir)
    assert len(entries) == 1
    assert entries[0]["id"] == "ls-1"


def test_append_life_stream_many(project_dir):
    entries = [
        {"id": f"ls-{i}", "at_local": f"2026-05-09T{i:02d}:00:00-04:00",
         "kind": "unplanned", "summary": f"e{i}", "tone": "even"}
        for i in range(5)
    ]
    n = append_life_stream_many(project_dir, entries)
    assert n == 5
    assert len(read_life_stream_all(project_dir)) == 5


def test_read_life_stream_tail_filters_by_since(project_dir):
    entries = [
        {"id": "ls-a", "at_local": "2026-05-08T10:00:00-04:00", "kind": "unplanned", "summary": "old", "tone": "even"},
        {"id": "ls-b", "at_local": "2026-05-09T10:00:00-04:00", "kind": "unplanned", "summary": "mid", "tone": "even"},
        {"id": "ls-c", "at_local": "2026-05-09T15:00:00-04:00", "kind": "unplanned", "summary": "new", "tone": "even"},
    ]
    append_life_stream_many(project_dir, entries)
    tail = read_life_stream_tail(project_dir, since_iso="2026-05-09T00:00:00-04:00")
    assert [e["id"] for e in tail] == ["ls-b", "ls-c"]


def test_read_life_stream_tail_limit(project_dir):
    entries = [
        {"id": f"ls-{i}", "at_local": f"2026-05-09T{i:02d}:00:00-04:00",
         "kind": "unplanned", "summary": f"e{i}", "tone": "even"}
        for i in range(5)
    ]
    append_life_stream_many(project_dir, entries)
    tail = read_life_stream_tail(project_dir, limit=3)
    assert len(tail) == 3
    # Most-recent slice (last 3 in chronological order)
    assert [e["id"] for e in tail] == ["ls-2", "ls-3", "ls-4"]


def test_read_life_stream_skips_bad_lines(project_dir):
    path = os.path.join(project_dir, "life_stream.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"id":"ls-1","at_local":"2026-05-09T10:00:00-04:00"}\n')
        f.write('not-json garbage\n')
        f.write('{"id":"ls-2","at_local":"2026-05-09T11:00:00-04:00"}\n')
    entries = read_life_stream_all(project_dir)
    assert len(entries) == 2
    assert [e["id"] for e in entries] == ["ls-1", "ls-2"]


# ── stamp_resolution ─────────────────────────────────────────────────

def test_stamp_resolution_marks_event_resolved(project_dir):
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "Avatar", "status": "planned", "revision_history": []},
    ])
    ok = stamp_resolution(
        project_dir, "sch-1",
        outcome="as_planned", what_happened="went fine", how_it_went="even", mood_delta=0,
        at_iso="2026-05-09T22:00:00-04:00",
    )
    assert ok is True
    ev = load_schedule(project_dir)["events"][0]
    assert ev["status"] == "as_planned"
    assert ev["resolution"]["outcome"] == "as_planned"
    assert ev["resolution"]["what_happened"] == "went fine"
    assert ev["resolution"]["mood_delta"] == 0


def test_stamp_resolution_unknown_event_returns_false(project_dir):
    _seed_schedule(project_dir, [{"id": "sch-1", "title": "x", "status": "planned", "revision_history": []}])
    assert stamp_resolution(project_dir, "sch-99", outcome="as_planned") is False


def test_stamp_resolution_invalid_outcome_returns_false(project_dir):
    _seed_schedule(project_dir, [{"id": "sch-1", "title": "x", "status": "planned", "revision_history": []}])
    assert stamp_resolution(project_dir, "sch-1", outcome="went_great") is False


# ── outcome roll ─────────────────────────────────────────────────────

ZARA_BANDS = {
    "work":      {"as_planned": 0.99, "modified": 0.00, "cancelled": 0.01},
    "shae":      {"as_planned": 0.95, "modified": 0.04, "cancelled": 0.01},
    "family":    {"as_planned": 0.98, "modified": 0.02, "cancelled": 0.00},
    "social":    {"as_planned": 0.80, "modified": 0.15, "cancelled": 0.05},
    "self_care": {"as_planned": 0.50, "modified": 0.20, "cancelled": 0.30},
    "admin":     {"as_planned": 0.85, "modified": 0.13, "cancelled": 0.02},
}


def test_roll_event_outcome_returns_valid_bucket():
    rng = random.Random(42)
    ev = {"kind": "social"}
    for _ in range(100):
        out = roll_event_outcome(ev, ZARA_BANDS, mood_state="Even", rng=rng)
        assert out in ("as_planned", "modified", "cancelled")


def test_roll_event_outcome_distribution_within_tolerance():
    rng = random.Random(0)
    ev = {"kind": "social"}
    n = 10_000
    counts = {"as_planned": 0, "modified": 0, "cancelled": 0}
    for _ in range(n):
        counts[roll_event_outcome(ev, ZARA_BANDS, mood_state="Even", rng=rng)] += 1
    # Expected: 80/15/5. Tolerate ±2σ ≈ ±2 * sqrt(p*(1-p)/n) ≈ ±0.8% for p=0.05.
    assert abs(counts["as_planned"] / n - 0.80) < 0.02
    assert abs(counts["modified"]   / n - 0.15) < 0.02
    assert abs(counts["cancelled"]  / n - 0.05) < 0.015


def test_roll_event_outcome_unknown_kind_defaults_to_as_planned(rng=None):
    ev = {"kind": "totally_made_up"}
    rng = random.Random(0)
    counts = {"as_planned": 0, "modified": 0, "cancelled": 0}
    for _ in range(100):
        counts[roll_event_outcome(ev, ZARA_BANDS, mood_state="Even", rng=rng)] += 1
    # Default fallback is 100% as_planned for unknown kinds
    assert counts["as_planned"] == 100


# ── effective_bands (mood modifier) ──────────────────────────────────

def test_effective_bands_even_mood_unchanged():
    bands = effective_bands(ZARA_BANDS, "social", mood_state="Even")
    assert pytest.approx(bands["as_planned"], abs=1e-6) == 0.80
    assert pytest.approx(bands["modified"],   abs=1e-6) == 0.15
    assert pytest.approx(bands["cancelled"],  abs=1e-6) == 0.05


def test_effective_bands_rough_mood_bumps_social_cancel():
    bands = effective_bands(ZARA_BANDS, "social", mood_state="Rough")
    # +0.20 to cancelled, absorbed from as_planned
    assert pytest.approx(bands["cancelled"], abs=1e-6) == 0.25
    assert pytest.approx(bands["modified"],  abs=1e-6) == 0.15
    assert pytest.approx(bands["as_planned"],abs=1e-6) == 0.60


def test_effective_bands_rough_mood_self_care_takes_biggest_hit():
    bands = effective_bands(ZARA_BANDS, "self_care", mood_state="Rough")
    # +0.30 to cancelled (0.30 + 0.30 = 0.60), absorbed from as_planned
    assert pytest.approx(bands["cancelled"], abs=1e-6) == 0.60
    assert pytest.approx(bands["modified"],  abs=1e-6) == 0.20
    assert pytest.approx(bands["as_planned"],abs=1e-6) == 0.20


def test_effective_bands_rough_mood_doesnt_touch_work():
    bands = effective_bands(ZARA_BANDS, "work", mood_state="Rough")
    # work has no mood modifier — base unchanged
    assert pytest.approx(bands["as_planned"], abs=1e-6) == 0.99
    assert pytest.approx(bands["cancelled"],  abs=1e-6) == 0.01


def test_effective_bands_unknown_kind_returns_default():
    bands = effective_bands(ZARA_BANDS, "totally_made_up", mood_state="Even")
    assert bands == {"as_planned": 1.0, "modified": 0.0, "cancelled": 0.0}


def test_effective_bands_clamps_to_unit_interval():
    # Synthetic band that already maxes out cancelled — modifier shouldn't blow past 1.0
    bands_in = {"social": {"as_planned": 0.0, "modified": 0.5, "cancelled": 0.5}}
    bands = effective_bands(bands_in, "social", mood_state="Rough")
    assert 0.0 <= bands["cancelled"] <= 1.0
    assert 0.0 <= bands["modified"] <= 1.0
    assert 0.0 <= bands["as_planned"] <= 1.0
    # Sum should still be approximately 1.0
    s = bands["cancelled"] + bands["modified"] + bands["as_planned"]
    assert pytest.approx(s, abs=1e-6) == 1.0


# ── lock contention ──────────────────────────────────────────────────

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl-based lock is no-op on Windows; serialization is for the Linux VPS",
)
def test_concurrent_apply_schedule_ops_serialize_cleanly(project_dir):
    """Two threads applying ops simultaneously should both land their changes
    without corrupting the file. Tests fcntl serialization.
    """
    _seed_schedule(project_dir, [
        {"id": "sch-1", "title": "A", "kind": "social", "status": "planned", "revision_history": []},
        {"id": "sch-2", "title": "B", "kind": "social", "status": "planned", "revision_history": []},
    ])

    def _thread_a():
        apply_schedule_ops(project_dir, [
            {"op": "modify", "event_id": "sch-1", "fields": {"title": "A-modified"}, "reason": "t1"}
        ], source="chat", now_iso="2026-05-08T19:00:00-04:00")

    def _thread_b():
        apply_schedule_ops(project_dir, [
            {"op": "modify", "event_id": "sch-2", "fields": {"title": "B-modified"}, "reason": "t2"}
        ], source="chat", now_iso="2026-05-08T19:00:00-04:00")

    threads = [threading.Thread(target=_thread_a), threading.Thread(target=_thread_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    sched = load_schedule(project_dir)
    titles = {e["id"]: e["title"] for e in sched["events"]}
    # Both writes should have landed (they touch different events, so neither overwrites the other)
    assert titles["sch-1"] == "A-modified"
    assert titles["sch-2"] == "B-modified"
    # Life stream should have 2 entries
    assert len(read_life_stream_all(project_dir)) == 2
