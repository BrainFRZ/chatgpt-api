"""Unit tests for character_resolver — skip-roll, hard-override, outcome
honoring, idempotence, mood-delta accumulation, and the deterministic
contract (model narrates, backend rolls).
"""
import json
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from character_resolver import (  # noqa: E402
    RESOLVER_MODEL,
    SKIP_RATE,
    UNPLANNED_LAMBDA,
    UNPLANNED_MAX,
    WB_MOD_CAP,
    _had_chat_revision_in_window,
    _poisson,
    _spread_unplanned_times,
    build_resolver_tool,
    compute_resolver_cost,
    run_daily_resolver,
)
from character_schedule import (  # noqa: E402
    init_schedule,
    load_schedule,
    read_life_stream_all,
    save_schedule,
)
from character_project_state import save_project_state  # noqa: E402


ZARA_BANDS = {
    "work":      {"as_planned": 0.99, "modified": 0.00, "cancelled": 0.01},
    "shae":      {"as_planned": 0.95, "modified": 0.04, "cancelled": 0.01},
    "family":    {"as_planned": 0.98, "modified": 0.02, "cancelled": 0.00},
    "social":    {"as_planned": 0.80, "modified": 0.15, "cancelled": 0.05},
    "self_care": {"as_planned": 0.50, "modified": 0.20, "cancelled": 0.30},
    "admin":     {"as_planned": 0.85, "modified": 0.13, "cancelled": 0.02},
}


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as d:
        # Provide a minimal profile.di so the resolver can read it
        with open(os.path.join(d, "character_profile.di"), "w", encoding="utf-8") as f:
            f.write("# Test character\nA fictional person.\n")
        # Bootstrap project state with flakiness_bands and Even mood
        save_project_state(d, {
            "wellbeing": {"state": "Even", "wb_mod": 0},
            "flakiness_bands": ZARA_BANDS,
            "scheduler_state": {},
            "callbacks": {"open": []},
        })
        yield d


def _seed_schedule(project_dir, events):
    sched = init_schedule("2026-05-04", "2026-05-03T20:00:00-04:00")
    sched["events"] = events
    sched["next_id"] = len(events) + 1
    save_schedule(project_dir, sched)


def _planned_event(eid: str, when_local: str, kind: str = "social", duration_min: int = 60, **extra):
    ev = {
        "id": eid,
        "kind": kind,
        "title": f"Event {eid}",
        "with": [],
        "when_local": when_local,
        "duration_min": duration_min,
        "location": "",
        "anticipation": "neutral",
        "magnitude": "normal",
        "resolver_hints": "",
        "status": "planned",
        "resolution": None,
        "revision_history": [],
    }
    ev.update(extra)
    return ev


def _mock_resolver_response(resolutions: list, unplanned: list = None):
    inp = {"resolutions": resolutions, "unplanned": unplanned or []}
    block = SimpleNamespace(type="tool_use", name="report_resolver_pass", input=inp)
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(
            input_tokens=500, output_tokens=200,
            cache_read_input_tokens=400, cache_creation_input_tokens=100,
        ),
    )


def _mock_client(response):
    c = MagicMock()
    c.messages.create.return_value = response
    return c


# ── _poisson ─────────────────────────────────────────────────────────

def test_poisson_returns_nonnegative_integer():
    rng = random.Random(0)
    for _ in range(50):
        n = _poisson(1.5, rng)
        assert isinstance(n, int)
        assert n >= 0


def test_poisson_distribution_in_tolerance():
    """Empirical mean of 10k samples at λ=1.5 should be ~1.5 ± 0.05."""
    rng = random.Random(0)
    samples = [_poisson(1.5, rng) for _ in range(10_000)]
    mean = sum(samples) / len(samples)
    assert abs(mean - 1.5) < 0.05


# ── _spread_unplanned_times ──────────────────────────────────────────

def test_spread_unplanned_times_zero():
    s = datetime(2026, 5, 9, 8, 0).astimezone()
    e = datetime(2026, 5, 9, 13, 0).astimezone()
    assert _spread_unplanned_times(s, e, 0) == []


def test_spread_unplanned_times_one_returns_midpoint():
    s = datetime(2026, 5, 9, 8, 0).astimezone()
    e = datetime(2026, 5, 9, 12, 0).astimezone()
    times = _spread_unplanned_times(s, e, 1)
    assert len(times) == 1
    assert times[0] == datetime(2026, 5, 9, 10, 0).astimezone()


def test_spread_unplanned_times_three_evenly():
    s = datetime(2026, 5, 9, 8, 0).astimezone()
    e = datetime(2026, 5, 9, 12, 0).astimezone()
    times = _spread_unplanned_times(s, e, 3)
    # 3 points divides 4hr into 4 equal segments → 1hr, 2hr, 3hr after start
    expected = [datetime(2026, 5, 9, 9, 0), datetime(2026, 5, 9, 10, 0), datetime(2026, 5, 9, 11, 0)]
    for t, exp in zip(times, expected):
        assert t == exp.astimezone()


# ── _had_chat_revision_in_window ─────────────────────────────────────

def test_chat_revision_detected_when_present():
    events = [{
        "id": "sch-1",
        "revision_history": [{"by": "chat", "at": "2026-05-09T10:00:00-04:00", "reason": "..."}],
    }]
    assert _had_chat_revision_in_window(events, "2026-05-09T08:00:00-04:00") is True


def test_chat_revision_not_detected_when_outside_window():
    events = [{
        "id": "sch-1",
        "revision_history": [{"by": "chat", "at": "2026-05-08T10:00:00-04:00", "reason": "..."}],
    }]
    # last_run_iso is AFTER the revision — out of window
    assert _had_chat_revision_in_window(events, "2026-05-09T08:00:00-04:00") is False


def test_no_prior_run_no_chat_revisions_returns_false():
    """No last_run + no events with chat revisions = no force; skip-roll allowed."""
    events = []
    assert _had_chat_revision_in_window(events, None) is False


def test_no_prior_run_with_chat_revision_forces_run():
    """No last_run + at least one event has a chat-driven revision = force."""
    events = [{"id": "sch-1", "revision_history": [
        {"by": "chat", "at": "2026-05-09T07:00:00-04:00", "reason": "Kira bailed"},
    ]}]
    assert _had_chat_revision_in_window(events, None) is True


def test_resolver_only_revision_does_not_force():
    events = [{
        "id": "sch-1",
        "revision_history": [{"by": "resolver", "at": "2026-05-09T10:00:00-04:00", "reason": "..."}],
    }]
    assert _had_chat_revision_in_window(events, "2026-05-09T08:00:00-04:00") is False


# ── compute_resolver_cost ────────────────────────────────────────────

def test_compute_resolver_cost_zero_for_empty():
    assert compute_resolver_cost({}) == 0.0
    assert compute_resolver_cost(None) == 0.0


def test_compute_resolver_cost_arithmetic():
    usage = {
        "input_tokens": 1500,         # total input incl cache
        "cache_read_tokens": 1000,
        "cache_creation_tokens": 200,
        "output_tokens": 300,
    }
    cost = compute_resolver_cost(usage)
    # uncached_input = 1500 - 1000 - 200 = 300
    # 300 * 3 + 1000 * 0.30 + 200 * 6 + 300 * 15 = 900 + 300 + 1200 + 4500 = 6900 / 1M
    assert abs(cost - 0.0069) < 1e-6


# ── tool schema ──────────────────────────────────────────────────────

def test_resolver_tool_schema_shape():
    tool = build_resolver_tool()
    assert tool["name"] == "report_resolver_pass"
    props = tool["input_schema"]["properties"]
    assert set(tool["input_schema"]["required"]) == {"resolutions", "unplanned"}
    res_item = props["resolutions"]["items"]
    assert "event_id" in res_item["required"]
    assert "outcome" in res_item["required"]
    assert "what_happened" in res_item["required"]


# ── run_daily_resolver: bad inputs ───────────────────────────────────

def test_run_resolver_bad_project_dir():
    out = run_daily_resolver(MagicMock(), "/nonexistent/path/123")
    assert out["error"] == "bad project_dir"
    assert out["skipped"] is True


def test_run_resolver_no_schedule_file(project_dir):
    # project_dir exists but has no schedule.json
    out = run_daily_resolver(MagicMock(), project_dir, now_dt=datetime(2026, 5, 9, 13, 0).astimezone())
    assert out["skipped"] is True
    assert out["reason"] == "no schedule.json"


def test_run_resolver_bails_when_no_flakiness_bands(project_dir):
    """Regression: a character with no flakiness_bands set (pre-bootstrap or
    pre-interview) should NOT trigger a Sonnet call. The resolver auto-stamps
    elapsed events as as_planned and bails."""
    # Strip flakiness_bands from the project state
    save_project_state(project_dir, {
        "wellbeing": {"state": "Even", "wb_mod": 0},
        "flakiness_bands": None,  # NOT set
        "scheduler_state": {},
        "callbacks": {"open": []},
    })
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T08:00:00-04:00", kind="work"),
    ])
    client = MagicMock()
    out = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
    )
    assert out["skipped"] is True
    assert out["reason"] == "no flakiness_bands"
    assert out["elapsed_count"] == 1
    # No API call
    assert client.messages.create.call_count == 0
    # Event auto-stamped as_planned
    sched = load_schedule(project_dir)
    assert sched["events"][0]["status"] == "as_planned"


def test_run_resolver_no_elapsed_events(project_dir):
    # Schedule has only future events
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-15T20:00:00-04:00", kind="social", duration_min=120),
    ])
    out = run_daily_resolver(MagicMock(), project_dir, now_dt=datetime(2026, 5, 9, 13, 0).astimezone())
    assert out["skipped"] is False
    assert out["elapsed_count"] == 0


# ── run_daily_resolver: skip-roll ────────────────────────────────────

def test_skip_roll_auto_stamps_without_api(project_dir):
    """When skip-roll fires (RNG below SKIP_RATE) and no chat-driven mutations,
    elapsed events get auto-stamped as_planned with empty resolution. No API call."""
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T08:00:00-04:00", kind="work", duration_min=60),
        _planned_event("sch-2", "2026-05-09T09:00:00-04:00", kind="social", duration_min=30),
    ])
    # RNG seed=0 → first random.random() ~= 0.84 — that's > SKIP_RATE(0.35), so NOT skip
    # Pick a seed where the first random < 0.35
    rng = random.Random()
    rng.seed(99)  # try seeds until one yields < 0.35

    # Force skip by using a mock RNG that always returns 0.0
    class _ZeroRng:
        def random(self): return 0.0
    client = MagicMock()
    out = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
        rng=_ZeroRng(),
    )
    assert out["skipped"] is True
    assert out["elapsed_count"] == 2
    # No API call made
    assert client.messages.create.call_count == 0
    # Both events stamped as_planned with empty resolution
    sched = load_schedule(project_dir)
    statuses = {ev["id"]: ev["status"] for ev in sched["events"]}
    assert statuses == {"sch-1": "as_planned", "sch-2": "as_planned"}
    # No life_stream entries
    assert read_life_stream_all(project_dir) == []


def test_skip_roll_forced_to_run_when_chat_revision_present(project_dir):
    """Hard-override: even if skip-roll would fire, an elapsed event with a
    chat-driven revision since last_resolver_run forces the run."""
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T08:00:00-04:00", kind="social", duration_min=60,
                       revision_history=[
                           {"by": "chat", "at": "2026-05-09T07:30:00-04:00",
                            "reason": "Kira bailed",
                            "diff": {"with": {"from": ["Kira"], "to": []}}},
                       ]),
    ])
    # Set last_resolver_run_at BEFORE the chat revision
    save_project_state(project_dir, {
        "wellbeing": {"state": "Even", "wb_mod": 0},
        "flakiness_bands": ZARA_BANDS,
        "scheduler_state": {"last_resolver_run_at": "2026-05-09T06:00:00-04:00"},
        "callbacks": {"open": []},
    })

    class _ZeroRng:
        def random(self): return 0.0  # would skip if not for hard-override
        def seed(self, *a, **k): pass

    client = _mock_client(_mock_resolver_response(
        resolutions=[{"event_id": "sch-1", "outcome": "as_planned",
                      "what_happened": "happened anyway", "how_it_went": "even", "mood_delta": 0}],
        unplanned=[],
    ))
    out = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
        rng=_ZeroRng(),
    )
    assert out["skipped"] is False
    assert client.messages.create.call_count == 1


# ── run_daily_resolver: outcome roll honored ─────────────────────────

def test_resolver_overrides_model_outcome_with_rolled_bucket(project_dir):
    """Even if the model emits a different outcome than the backend rolled,
    the rolled bucket wins. Trust the dice, not the narration."""
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T08:00:00-04:00", kind="self_care", duration_min=60),
    ])

    # Use a fixed RNG so we can predict the rolled outcome deterministically
    # Set up so that after the skip-roll random.random() (which we want > 0.35),
    # the next random.random() falls in the cancelled bucket for self_care (0.30).
    class _SeqRng:
        def __init__(self, vals):
            self.vals = list(vals)
            self.i = 0
        def random(self):
            v = self.vals[self.i]; self.i += 1
            return v
        def seed(self, *a, **k): pass

    # First random: 0.99 (not skip; > SKIP_RATE 0.35)
    # Second: 0.10 (outcome: < 0.30 cancelled bucket for self_care)
    # Third: 0.10 (poisson: 0.10 <= e^-1.5 ≈ 0.223 → returns 0 unplanned)
    rng = _SeqRng([0.99, 0.10, 0.10])

    # Model says as_planned but we rolled cancelled → backend should override to cancelled
    client = _mock_client(_mock_resolver_response(
        resolutions=[{"event_id": "sch-1", "outcome": "as_planned",
                      "what_happened": "model said it happened", "how_it_went": "even",
                      "mood_delta": 1}],
        unplanned=[],
    ))
    run_daily_resolver(client, project_dir,
                      now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
                      rng=rng)
    sched = load_schedule(project_dir)
    ev = sched["events"][0]
    assert ev["status"] == "cancelled"  # backend's rolled outcome, not model's
    assert ev["resolution"]["outcome"] == "cancelled"


# ── run_daily_resolver: idempotence ──────────────────────────────────

def test_resolver_double_run_no_double_resolve(project_dir):
    """Running the resolver twice in quick succession should not re-resolve
    events that already got resolved on the first pass (their status is no
    longer 'planned' so they fall out of the elapsed filter)."""
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T08:00:00-04:00", kind="work", duration_min=60),
    ])

    client = _mock_client(_mock_resolver_response(
        resolutions=[{"event_id": "sch-1", "outcome": "as_planned",
                      "what_happened": "morning rush", "how_it_went": "even", "mood_delta": 0}],
        unplanned=[],
    ))

    # First pass — RNG forces run (no skip)
    class _NoSkipRng:
        def __init__(self):
            self.i = 0
            self.vals = [0.99, 0.99, 0.99, 0.99, 0.99, 0.99, 0.99]  # never skip, never weird outcomes
        def random(self):
            v = self.vals[self.i % len(self.vals)]; self.i += 1
            return v

    out1 = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
        rng=_NoSkipRng(),
    )
    assert out1["skipped"] is False
    assert out1["resolutions"] == 1

    # Second pass — same window. Should find no elapsed events to re-resolve
    out2 = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 1).astimezone(),
        rng=_NoSkipRng(),
    )
    assert out2["elapsed_count"] == 0


# ── run_daily_resolver: mood delta accumulation ──────────────────────

def test_mood_deltas_accumulate_and_clamp(project_dir):
    """Multiple resolutions with mood_deltas should sum into wb_mod, clamped at ±2."""
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T08:00:00-04:00", kind="work"),
        _planned_event("sch-2", "2026-05-09T09:00:00-04:00", kind="work"),
    ])

    class _NoSkipRng:
        def __init__(self):
            self.i = 0
            self.vals = [0.99, 0.99, 0.99, 0.99, 0.99, 0.99]
        def random(self):
            v = self.vals[self.i % len(self.vals)]; self.i += 1
            return v

    client = _mock_client(_mock_resolver_response(
        resolutions=[
            {"event_id": "sch-1", "outcome": "as_planned", "what_happened": "x", "how_it_went": "buoyant", "mood_delta": 2},
            {"event_id": "sch-2", "outcome": "as_planned", "what_happened": "y", "how_it_went": "buoyant", "mood_delta": 2},
        ],
        unplanned=[],
    ))
    out = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
        rng=_NoSkipRng(),
    )
    # Two +2 deltas would sum to +4 but clamped to +2
    assert out["wb_mod_after"] == WB_MOD_CAP


# ── run_daily_resolver: API failure soft-fails events ────────────────

def test_api_failure_does_not_strand_elapsed_events(project_dir):
    """If the model call fails, we still need to make progress on elapsed events
    (otherwise they'd be re-tried forever). Backend should auto-stamp them with
    the rolled bucket and zero narration."""
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T08:00:00-04:00", kind="work"),
    ])
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("boom")

    class _NoSkipRng:
        def __init__(self):
            self.i = 0
            self.vals = [0.99, 0.99, 0.99, 0.99]
        def random(self):
            v = self.vals[self.i % len(self.vals)]; self.i += 1
            return v

    run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
        rng=_NoSkipRng(),
    )
    # Event status should have moved off "planned" — even with a failed API,
    # backend auto-stamps with the rolled bucket.
    sched = load_schedule(project_dir)
    assert sched["events"][0]["status"] != "planned"
