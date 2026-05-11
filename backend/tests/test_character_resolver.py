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


def test_run_resolver_no_upcoming_events_with_unplanned_zero(project_dir):
    """No upcoming events in the next-cron window AND Poisson rolls 0
    unplanned → quick bail with no API call. The resolver still ran
    (skipped=False) but nothing was notable to pre-roll."""
    # Event far in the future, outside next_cron window from Sat 1pm
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-15T20:00:00-04:00", kind="social", duration_min=120),
    ])

    class _Rng:
        def __init__(self):
            self.i = 0
            self.vals = [0.99, 0.10]  # not skip, then poisson immediately exits at k=0
        def random(self):
            v = self.vals[min(self.i, len(self.vals) - 1)]
            self.i += 1
            return v

    client = MagicMock()
    out = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
        rng=_Rng(),
    )
    assert out["skipped"] is False
    assert out["preroll_events"] == 0
    assert out["preroll_unplanned"] == 0
    assert out["reason"] == "empty preroll"
    # No API call when nothing to narrate
    assert client.messages.create.call_count == 0


def test_run_resolver_no_upcoming_events_but_unplanned_fires(project_dir):
    """Regression: when no upcoming events but Poisson rolls > 0 unplanned
    moments, Sonnet narrates them. Their at_local is in the upcoming window
    → stored as pending_unplanned. life_stream is empty until next cron stamps.
    """
    # Far-future event, won't be in the upcoming window
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-15T20:00:00-04:00", kind="social", duration_min=120),
    ])

    class _Rng:
        def __init__(self):
            self.i = 0
            self.vals = [0.99, 0.99, 0.50, 0.30, 0.50, 0.50]
        def random(self):
            v = self.vals[min(self.i, len(self.vals) - 1)]
            self.i += 1
            return v

    client = _mock_client(_mock_resolver_response(
        resolutions=[],
        unplanned=[
            {"summary": "Marcus came in at 8:30 and lingered through her second pour.",
             "tone": "even", "kind": "small_thing"},
            {"summary": "The espresso machine started hissing again.",
             "tone": "frayed", "kind": "incident"},
        ],
    ))
    out = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
        rng=_Rng(),
    )
    assert out["skipped"] is False
    assert out["preroll_events"] == 0
    assert out["preroll_unplanned"] == 2
    # API was called
    assert client.messages.create.call_count == 1
    # life_stream still empty — pending_unplanned waits for next cron to stamp
    assert read_life_stream_all(project_dir) == []
    # Schedule has pending_unplanned populated
    sched = load_schedule(project_dir)
    assert len(sched.get("pending_unplanned", [])) == 2


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
    # Skip-roll fires for the upcoming window's pre-roll. Existing
    # behavior: stamping STAGE 1 still happens for events whose pending
    # had been pre-rolled previously (n/a here — no prior pre-roll), and
    # fallback STAGE 2 still stamps elapsed-but-unrolled events thin.
    assert out["skipped"] is True
    assert out["fallback_stamped"] == 2  # both events fell to fallback
    assert client.messages.create.call_count == 0
    sched = load_schedule(project_dir)
    statuses = {ev["id"]: ev["status"] for ev in sched["events"]}
    assert statuses == {"sch-1": "as_planned", "sch-2": "as_planned"}
    # Thin life_stream entries from fallback stamping
    stream = read_life_stream_all(project_dir)
    assert len(stream) == 2
    assert all(e["kind"] == "resolved_planned" for e in stream)
    refs = sorted(e["ref"] for e in stream)
    assert refs == ["sch-1", "sch-2"]


def test_skip_roll_forced_to_run_when_chat_revision_in_upcoming(project_dir):
    """Hard-override: when a chat-driven revision is present on an upcoming
    event since last_resolver_run, skip-roll's chat_forced gate forces the
    pre-roll to run regardless of the dice."""
    # Event in the upcoming window from now=Sat 1pm; next_cron=Sat 6pm
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T15:00:00-04:00", kind="social", duration_min=60,
                       revision_history=[
                           {"by": "chat", "at": "2026-05-09T12:30:00-04:00",
                            "reason": "Kira bailed",
                            "diff": {"with": {"from": ["Kira"], "to": []}}},
                       ]),
    ])
    save_project_state(project_dir, {
        "wellbeing": {"state": "Even", "wb_mod": 0},
        "flakiness_bands": ZARA_BANDS,
        "scheduler_state": {"last_resolver_run_at": "2026-05-09T08:00:00-04:00"},
        "callbacks": {"open": []},
    })

    class _ZeroRng:
        def random(self): return 0.0  # would skip if not for hard-override
        def seed(self, *a, **k): pass

    client = _mock_client(_mock_resolver_response(
        resolutions=[{"event_id": "sch-1", "outcome": "as_planned",
                      "what_happened": "rescheduled vibes, fine", "how_it_went": "even", "mood_delta": 0}],
        unplanned=[],
    ))
    out = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
        rng=_ZeroRng(),
    )
    # Pre-roll ran (not skipped) because chat-revision forced it
    assert out["skipped"] is False
    assert client.messages.create.call_count == 1
    # Event has pending_resolution (pre-rolled, not yet stamped)
    sched = load_schedule(project_dir)
    ev = sched["events"][0]
    assert ev["status"] == "planned"  # not stamped yet — event_end > now
    assert isinstance(ev.get("pending_resolution"), dict)


# ── run_daily_resolver: outcome roll honored ─────────────────────────

def test_resolver_overrides_model_outcome_with_rolled_bucket(project_dir):
    """Even if the model emits a different outcome than the backend rolled,
    the rolled bucket wins. Trust the dice. Now the rolled outcome lands
    in pending_resolution at pre-roll time — pending.outcome must equal
    the backend roll, not what the model emitted."""
    # Upcoming event in next-cron window (now=1pm, next=6pm; event ends 5pm)
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T16:00:00-04:00", kind="self_care", duration_min=60),
    ])

    class _SeqRng:
        def __init__(self, vals):
            self.vals = list(vals)
            self.i = 0
        def random(self):
            v = self.vals[self.i]; self.i += 1
            return v
        def seed(self, *a, **k): pass

    # Sequence: skip-roll, outcome roll (lands in cancelled for self_care since
    # 0.10 < 0.30 cancelled), then poisson (0.10 < ~0.223 = exits at k=0)
    rng = _SeqRng([0.99, 0.10, 0.10])

    client = _mock_client(_mock_resolver_response(
        resolutions=[{"event_id": "sch-1", "outcome": "as_planned",  # model says as_planned
                      "what_happened": "model said it happened", "how_it_went": "even",
                      "mood_delta": 1}],
        unplanned=[],
    ))
    run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
        rng=rng,
    )
    sched = load_schedule(project_dir)
    ev = sched["events"][0]
    # Status remains planned (event hasn't ended yet at now=1pm)
    assert ev["status"] == "planned"
    # Pending resolution has the BACKEND's rolled outcome, not the model's
    assert ev["pending_resolution"]["outcome"] == "cancelled"


# ── run_daily_resolver: idempotence ──────────────────────────────────

def test_resolver_double_run_no_double_pre_roll(project_dir):
    """First pass pre-rolls an upcoming event into pending_resolution.
    Second pass with same now_dt should NOT re-roll that event (it has
    pending already). Idempotence guard for forward-rolling."""
    # Upcoming event in next-cron window
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T16:00:00-04:00", kind="work", duration_min=60),
    ])

    client = _mock_client(_mock_resolver_response(
        resolutions=[{"event_id": "sch-1", "outcome": "as_planned",
                      "what_happened": "afternoon shift", "how_it_went": "even", "mood_delta": 0}],
        unplanned=[],
    ))

    class _Rng:
        def __init__(self, vals):
            self.i = 0
            self.vals = list(vals)
        def random(self):
            v = self.vals[min(self.i, len(self.vals) - 1)]; self.i += 1
            return v

    # First pass: not skip, outcome=as_planned (≥0.05 cancelled+modified for work),
    # poisson exits at k=1 returning 0 (0.10 ≤ e^-1.5 ≈ 0.223)
    out1 = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
        rng=_Rng([0.99, 0.99, 0.10]),
    )
    assert out1["skipped"] is False
    assert out1["preroll_events"] == 1

    # Second pass: not skip, no upcoming events to roll, poisson 0 → empty-bail
    out2 = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 1).astimezone(),
        rng=_Rng([0.99, 0.10]),
    )
    assert out2["preroll_events"] == 0  # already pre-rolled, no re-roll
    assert out2.get("reason") == "empty preroll"
    # Only one API call total — the second pass had nothing to do
    assert client.messages.create.call_count == 1


# ── run_daily_resolver: mood delta accumulation ──────────────────────

def test_mood_deltas_apply_at_stamp_time_clamped(project_dir):
    """Mood deltas are stored on pending_resolution at pre-roll time but only
    applied to wb_mod when the event is STAMPED (next cron, after event_end
    has passed). Two stamped +2 deltas should clamp at +WB_MOD_CAP."""
    # Pre-set events with pending_resolution already in place. Event_ends
    # in the past so STAGE 1 stamps them in this run.
    _seed_schedule(project_dir, [
        {"id": "sch-1", "kind": "work", "title": "shift1",
         "when_local": "2026-05-09T08:00:00-04:00", "duration_min": 60,
         "with": [], "location": "", "anticipation": "neutral", "magnitude": "normal",
         "status": "planned", "resolution": None, "revision_history": [],
         "pending_resolution": {
             "outcome": "as_planned", "what_happened": "great", "how_it_went": "buoyant",
             "mood_delta": 2,
         }},
        {"id": "sch-2", "kind": "work", "title": "shift2",
         "when_local": "2026-05-09T09:00:00-04:00", "duration_min": 60,
         "with": [], "location": "", "anticipation": "neutral", "magnitude": "normal",
         "status": "planned", "resolution": None, "revision_history": [],
         "pending_resolution": {
             "outcome": "as_planned", "what_happened": "great", "how_it_went": "buoyant",
             "mood_delta": 2,
         }},
    ])

    class _NoSkipRng:
        def __init__(self):
            self.i = 0
            self.vals = [0.99] * 10
        def random(self):
            v = self.vals[self.i % len(self.vals)]; self.i += 1
            return v

    client = _mock_client(_mock_resolver_response(resolutions=[], unplanned=[]))
    run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
        rng=_NoSkipRng(),
    )
    from character_project_state import load_project_state
    state = load_project_state(project_dir)
    # +2 + +2 = +4, clamped to +2
    assert state["wellbeing"]["wb_mod"] == WB_MOD_CAP
    # Both events stamped
    sched = load_schedule(project_dir)
    statuses = {e["id"]: e["status"] for e in sched["events"]}
    assert statuses == {"sch-1": "as_planned", "sch-2": "as_planned"}


# ── run_daily_resolver: API failure soft-fails events ────────────────

def test_full_lifecycle_pre_roll_then_stamp_at_next_cron(project_dir):
    """End-to-end forward-rolling lifecycle:
    1. Cron at 1pm pre-rolls an event ending 5pm (in 1pm-6pm window).
       Stores pending_resolution. life_stream still empty.
    2. Cron at 6pm sees the event with pending_resolution + event_end <= now.
       STAMPS it: status flips, life_stream entry written, mood_delta applied.
    """
    save_project_state(project_dir, {
        "wellbeing": {"state": "Even", "wb_mod": 0},
        "flakiness_bands": ZARA_BANDS,
        "scheduler_state": {"last_resolver_run_at": "2026-05-09T08:00:00-04:00"},
        "callbacks": {"open": []},
    })
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T16:00:00-04:00", kind="work", duration_min=60),
    ])

    class _Rng:
        def __init__(self, vals):
            self.i = 0
            self.vals = list(vals)
        def random(self):
            v = self.vals[min(self.i, len(self.vals) - 1)]; self.i += 1
            return v

    # ── Step 1: 1pm pre-rolls sch-1 (window 1pm-6pm) ──
    client = _mock_client(_mock_resolver_response(
        resolutions=[{"event_id": "sch-1", "outcome": "as_planned",
                      "what_happened": "afternoon was steady", "how_it_went": "even", "mood_delta": 1}],
        unplanned=[],
    ))
    out_1pm = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
        rng=_Rng([0.99, 0.99, 0.10]),  # not skip, outcome ok, poisson 0
    )
    assert out_1pm["preroll_events"] == 1
    sched = load_schedule(project_dir)
    ev = sched["events"][0]
    assert ev["status"] == "planned"  # not yet stamped
    assert ev["pending_resolution"]["outcome"] == "as_planned"
    assert ev["pending_resolution"]["mood_delta"] == 1
    assert read_life_stream_all(project_dir) == []  # not yet in ledger

    # ── Step 2: 6pm cron stamps sch-1 (event_end=5pm < now=6pm) ──
    out_6pm = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 18, 0).astimezone(),
        rng=_Rng([0.99, 0.10]),  # not skip, no upcoming, poisson 0
    )
    assert out_6pm["stamped_events"] == 1
    sched = load_schedule(project_dir)
    ev = sched["events"][0]
    assert ev["status"] == "as_planned"
    assert "pending_resolution" not in ev
    assert ev["resolution"]["what_happened"] == "afternoon was steady"
    assert ev["resolution"]["mood_delta"] == 1
    # Life stream now has the entry, timestamped at event_start (4pm).
    # at_local on resolved_planned reflects when the event happened in the
    # character's day, not when the resolver stamped it. resolution.at on
    # the schedule event keeps the end-time stamp for bookkeeping.
    stream = read_life_stream_all(project_dir)
    assert len(stream) == 1
    assert stream[0]["ref"] == "sch-1"
    assert stream[0]["summary"] == "afternoon was steady"
    assert stream[0]["at_local"].startswith("2026-05-09T16:00")
    assert ev["resolution"]["at"].startswith("2026-05-09T17:00")
    # Wellbeing wb_mod bumped by +1 from the stamped mood_delta
    from character_project_state import load_project_state
    state = load_project_state(project_dir)
    assert state["wellbeing"]["wb_mod"] == 1


def test_chat_mutation_invalidates_pending_resolution(project_dir):
    """When an event has pending_resolution and chat cancels/modifies it,
    pending must clear so the next cron re-rolls based on current state."""
    save_project_state(project_dir, {
        "wellbeing": {"state": "Even", "wb_mod": 0},
        "flakiness_bands": ZARA_BANDS,
        "scheduler_state": {"last_resolver_run_at": "2026-05-09T08:00:00-04:00"},
        "callbacks": {"open": []},
    })
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T16:00:00-04:00", kind="social", duration_min=120),
    ])

    # Step 1: cron pre-rolls
    class _Rng:
        def __init__(self, vals):
            self.i = 0
            self.vals = list(vals)
        def random(self):
            v = self.vals[min(self.i, len(self.vals) - 1)]; self.i += 1
            return v

    client = _mock_client(_mock_resolver_response(
        resolutions=[{"event_id": "sch-1", "outcome": "as_planned",
                      "what_happened": "fun night", "how_it_went": "buoyant", "mood_delta": 1}],
        unplanned=[],
    ))
    run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 13, 0).astimezone(),
        rng=_Rng([0.99, 0.99, 0.10]),
    )
    sched = load_schedule(project_dir)
    assert sched["events"][0].get("pending_resolution") is not None

    # Step 2: chat mutates the event (cancel)
    from character_schedule import apply_schedule_ops
    apply_schedule_ops(project_dir, [
        {"op": "cancel", "event_id": "sch-1", "reason": "tired, bailing"}
    ], source="chat", now_iso="2026-05-09T15:00:00-04:00")

    # Pending should be cleared, status now cancelled
    sched = load_schedule(project_dir)
    ev = sched["events"][0]
    assert ev["status"] == "cancelled"
    assert ev.get("pending_resolution") is None


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
    # Thin life_stream entry should have been written so the event isn't
    # silently lost from the lived-experience ledger when narration fails.
    stream = read_life_stream_all(project_dir)
    assert len(stream) == 1
    assert stream[0]["kind"] == "resolved_planned"
    assert stream[0]["ref"] == "sch-1"


# ── preroll=False (on-message catch-up) ──────────────────────────────


def test_preroll_false_fallback_stamps_elapsed_without_api(project_dir):
    """Catch-up mode: an elapsed planned event with no pre-roll gets thin-
    stamped to life_stream via Stage 2 fallback. No Sonnet call. Stage 3
    pre-roll is skipped entirely."""
    # Elapsed event (06:00-14:00 shift) at "now" = 15:00. No pre-roll on it
    # because the prior cron at 13:00 was the bands-bail case (or whatever)
    # so it's still status=planned with no pending_resolution.
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T06:00:00-04:00", kind="work", duration_min=480),
        # An upcoming event later today — would be pre-rolled by normal cron
        # but catch-up must NOT touch it.
        _planned_event("sch-2", "2026-05-09T17:00:00-04:00", kind="social", duration_min=60),
    ])
    client = MagicMock()
    out = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 15, 0).astimezone(),
        preroll=False,
    )

    assert out["stage"] == "stamp_only"
    assert out["fallback_stamped"] == 1
    assert out["stamped_events"] == 0
    # No Sonnet call regardless of pre-roll window contents
    assert client.messages.create.call_count == 0

    sched = load_schedule(project_dir)
    assert sched["events"][0]["status"] == "as_planned"  # fallback-stamped
    assert sched["events"][1]["status"] == "planned"     # upcoming untouched
    assert sched["events"][1].get("pending_resolution") is None  # Stage 3 skipped

    stream = read_life_stream_all(project_dir)
    assert len(stream) == 1
    assert stream[0]["ref"] == "sch-1"
    assert stream[0]["kind"] == "resolved_planned"


def test_preroll_false_stamps_pending_preroll_at_event_end(project_dir):
    """Catch-up mode: a previously pre-rolled event whose ev_end has now
    passed gets Stage-1-stamped with the model's narration intact. No
    Sonnet call (we're using the cached pre-roll)."""
    ev = _planned_event("sch-1", "2026-05-09T06:00:00-04:00", kind="work", duration_min=480)
    ev["pending_resolution"] = {
        "outcome": "modified",
        "what_happened": "Marcus brought a friend; she stayed twenty minutes past close.",
        "how_it_went": "buoyant",
        "mood_delta": 1,
    }
    _seed_schedule(project_dir, [ev])

    client = MagicMock()
    out = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 15, 0).astimezone(),
        preroll=False,
    )

    assert out["stage"] == "stamp_only"
    assert out["stamped_events"] == 1
    assert out["fallback_stamped"] == 0
    assert client.messages.create.call_count == 0

    sched = load_schedule(project_dir)
    assert sched["events"][0]["status"] == "modified"
    assert sched["events"][0].get("pending_resolution") is None
    # life_stream picks up the narrated summary
    stream = read_life_stream_all(project_dir)
    assert len(stream) == 1
    assert "Marcus" in stream[0]["summary"]
    assert stream[0]["tone"] == "buoyant"


def test_preroll_false_does_not_call_sonnet_with_upcoming_events(project_dir):
    """Catch-up mode: even when there ARE events in the pre-roll window
    (next_cron horizon), Stage 3 is skipped and no Sonnet call happens.
    Schedule remains untouched for those events."""
    _seed_schedule(project_dir, [
        # Upcoming event well within the pre-roll horizon
        _planned_event("sch-1", "2026-05-09T17:00:00-04:00", kind="social", duration_min=60),
    ])
    client = MagicMock()
    out = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 15, 0).astimezone(),
        preroll=False,
    )

    assert out["stage"] == "stamp_only"
    assert client.messages.create.call_count == 0

    sched = load_schedule(project_dir)
    assert sched["events"][0]["status"] == "planned"
    assert sched["events"][0].get("pending_resolution") is None


# ── maybe_run_catchup_resolver wrapper ───────────────────────────────


def test_catchup_no_op_when_nothing_elapsed(project_dir, monkeypatch):
    """Wrapper skips the resolver entirely when no events have elapsed
    and no pending unplanned moments are due. Critical: no last_run stamp
    bump in this case — otherwise we'd shadow the cron's chat-revision
    skip-roll override."""
    from characters_runtime import maybe_run_catchup_resolver

    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T17:00:00-04:00", kind="social", duration_min=60),
    ])
    # Pin "now" to before the only event starts
    import character_resolver
    monkeypatch.setattr(
        "game_systems.characters.now_et",
        lambda: datetime(2026, 5, 9, 15, 0).astimezone(),
    )

    result = maybe_run_catchup_resolver(MagicMock(), project_dir)
    assert result is None
    # No last_run stamp written → state unchanged
    from character_project_state import load_project_state
    state = load_project_state(project_dir) or {}
    assert "last_resolver_run_at" not in (state.get("scheduler_state") or {})


def test_stamped_entry_at_local_is_event_start_not_end(project_dir):
    """Regression for the cafe-shift confusion: a Stage-2-fallback-stamped
    resolved_planned entry must use the event's START (when_local) as its
    at_local, not the end. A 06:00-14:00 shift should appear in life_stream
    at 06:00, not 14:00 — otherwise the title 'Open the cafe — Monday
    morning shift' combined with at_local=14:00 reads as 'she opened the
    cafe at 2pm'."""
    _seed_schedule(project_dir, [
        _planned_event("sch-cafe", "2026-05-11T06:00:00-04:00", kind="work", duration_min=480,
                       title="Open the cafe — Monday morning shift"),
    ])
    client = MagicMock()
    out = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 11, 15, 0).astimezone(),
        preroll=False,  # catch-up mode — Stage 2 fallback-stamps
    )
    assert out["fallback_stamped"] == 1

    stream = read_life_stream_all(project_dir)
    assert len(stream) == 1
    assert stream[0]["at_local"].startswith("2026-05-11T06:00"), \
        f"at_local should be event start (06:00), got {stream[0]['at_local']}"
    # resolution.at on the schedule event keeps end time for bookkeeping
    sched = load_schedule(project_dir)
    assert sched["events"][0]["resolution"]["at"].startswith("2026-05-11T14:00")


def test_sonnet_failure_does_not_lock_upcoming_events_thin(project_dir):
    """Regression: when Sonnet fails entirely (exception → empty usage),
    upcoming events in the pre-roll window must NOT be locked in with
    thin pending_resolution. They stay status=planned with no pending
    so the next cron retries the pre-roll cleanly.

    Also: last_resolver_run_at must NOT be stamped forward — otherwise
    the chat-revision force-no-skip window silently shrinks and the
    failure becomes invisible to the next cron.

    This is distinct from a successful Sonnet call that returns no
    resolutions for a specific event (model judgment) — that path
    legitimately writes thin pending for the un-narrated ones."""
    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T17:00:00-04:00", kind="social", duration_min=60),
    ])
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("api auth busted")

    class _NoSkipRng:
        def __init__(self):
            self.i = 0
            self.vals = [0.99]
        def random(self):
            v = self.vals[self.i % len(self.vals)]; self.i += 1
            return v

    out = run_daily_resolver(
        client, project_dir,
        now_dt=datetime(2026, 5, 9, 15, 0).astimezone(),
        rng=_NoSkipRng(),
    )

    assert out.get("error") == "sonnet_call_failed"
    assert out["preroll_events"] == 0

    # Event remains untouched — next cron will retry
    sched = load_schedule(project_dir)
    assert sched["events"][0]["status"] == "planned"
    assert sched["events"][0].get("pending_resolution") is None

    # last_run NOT stamped — the failure stays visible to the next cron
    from character_project_state import load_project_state
    state = load_project_state(project_dir) or {}
    assert "last_resolver_run_at" not in (state.get("scheduler_state") or {})


def test_catchup_fires_when_event_elapsed(project_dir, monkeypatch):
    """Wrapper triggers run_daily_resolver(preroll=False) when there's an
    elapsed planned event without resolution. This is the Zara cafe-shift
    case: 06:00-14:00 shift ended at 14:00, user messages at 15:00."""
    from characters_runtime import maybe_run_catchup_resolver

    _seed_schedule(project_dir, [
        _planned_event("sch-1", "2026-05-09T06:00:00-04:00", kind="work", duration_min=480),
    ])
    monkeypatch.setattr(
        "game_systems.characters.now_et",
        lambda: datetime(2026, 5, 9, 15, 0).astimezone(),
    )

    client = MagicMock()
    result = maybe_run_catchup_resolver(client, project_dir)
    assert result is not None
    assert result["stage"] == "stamp_only"
    assert result["fallback_stamped"] == 1
    assert client.messages.create.call_count == 0

    # The elapsed event landed in life_stream so off-screen / recall can find it
    stream = read_life_stream_all(project_dir)
    assert any(e.get("ref") == "sch-1" for e in stream)
