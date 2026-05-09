"""Unit tests for character_planner — grace period, idempotence, carry-forward,
major-event roll honoring, and the planner's contract that backend rolls
the magnitude bucket while the model only narrates the specific event.
"""
import os
import random
import sys
import tempfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from character_planner import (  # noqa: E402
    PLANNER_MODEL,
    _carry_forward_events,
    _next_monday,
    build_planner_tool,
    compute_planner_cost,
    run_weekly_planner,
    this_week_monday,
)
from character_schedule import (  # noqa: E402
    init_schedule,
    load_schedule,
    read_life_stream_all,
    save_schedule,
)
from character_project_state import save_project_state  # noqa: E402
from game_systems.characters import LIFE_EVENT_GRACE_DAYS, roll_major_event  # noqa: E402


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "character_profile.di"), "w", encoding="utf-8") as f:
            f.write("# Test character\nA fictional person who works at a cafe.\n")
        save_project_state(d, {
            "wellbeing": {"state": "Even", "wb_mod": 0},
            "scheduler_state": {"first_seen_date": "2026-04-01"},  # past grace
            "callbacks": {"open": []},
        })
        yield d


def _mock_planner_response(events: list):
    block = SimpleNamespace(type="tool_use", name="report_week", input={"events": events})
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(
            input_tokens=2000, output_tokens=800,
            cache_read_input_tokens=1500, cache_creation_input_tokens=500,
        ),
    )


def _mock_client(response):
    c = MagicMock()
    c.messages.create.return_value = response
    return c


def _basic_week(week_start_iso: str = "2026-05-11") -> list[dict]:
    """A canonical 6-event week for tests."""
    return [
        {"kind": "work", "title": "Open cafe — morning rush",
         "when_local": f"{week_start_iso}T06:00:00-04:00", "duration_min": 480},
        {"kind": "work", "title": "Open cafe",
         "when_local": "2026-05-12T06:00:00-04:00", "duration_min": 480},
        {"kind": "social", "title": "Friday Shae night",
         "with": ["Shae"], "when_local": "2026-05-15T19:00:00-04:00",
         "duration_min": 180, "anticipation": "looking_forward"},
        {"kind": "self_care", "title": "Yoga (the inconsistent one)",
         "when_local": "2026-05-14T17:30:00-04:00", "duration_min": 75},
        {"kind": "family", "title": "Sunday call with mom",
         "with": ["Mei-Ling"], "when_local": "2026-05-17T11:00:00-04:00",
         "duration_min": 45},
        {"kind": "admin", "title": "Inventory + books",
         "when_local": "2026-05-12T15:00:00-04:00", "duration_min": 90},
    ]


# ── _next_monday ─────────────────────────────────────────────────────

def test_next_monday_from_sunday_evening():
    sun = datetime(2026, 5, 10, 20, 0).astimezone()
    assert _next_monday(sun) == date(2026, 5, 11)


def test_next_monday_from_monday_returns_following_monday():
    mon = datetime(2026, 5, 11, 9, 0).astimezone()
    assert _next_monday(mon) == date(2026, 5, 18)


def test_next_monday_from_wednesday():
    wed = datetime(2026, 5, 13, 12, 0).astimezone()
    assert _next_monday(wed) == date(2026, 5, 18)


def test_next_monday_from_saturday():
    sat = datetime(2026, 5, 9, 14, 0).astimezone()
    assert _next_monday(sat) == date(2026, 5, 11)


# ── this_week_monday ─────────────────────────────────────────────────

def test_this_week_monday_from_wednesday():
    wed = datetime(2026, 5, 13, 12, 0).astimezone()
    assert this_week_monday(wed) == date(2026, 5, 11)


def test_this_week_monday_from_monday_returns_today():
    mon = datetime(2026, 5, 11, 9, 0).astimezone()
    assert this_week_monday(mon) == date(2026, 5, 11)


def test_this_week_monday_from_sunday():
    sun = datetime(2026, 5, 17, 20, 0).astimezone()
    assert this_week_monday(sun) == date(2026, 5, 11)


def test_run_planner_with_explicit_week_of(project_dir):
    """Caller can override the target Monday — e.g., finalize_interview seeds
    the current week instead of next week."""
    client = _mock_client(_mock_planner_response(_basic_week()))
    out = run_weekly_planner(
        client, project_dir,
        now_dt=datetime(2026, 5, 13, 14, 0).astimezone(),  # Wednesday
        rng=random.Random(0),
        week_of=date(2026, 5, 11),  # Monday of THIS week
    )
    assert out["week_of"] == "2026-05-11"
    sched = load_schedule(project_dir)
    assert sched["week_of"] == "2026-05-11"


# ── _carry_forward_events ────────────────────────────────────────────

def test_carry_forward_keeps_far_future_planned():
    week_end = datetime(2026, 5, 18).astimezone()  # next-Monday end of new week
    schedule = {"events": [
        {"id": "sch-1", "status": "planned",
         "when_local": "2026-06-01T19:00:00-04:00", "title": "concert"},  # 3 weeks out
        {"id": "sch-2", "status": "planned",
         "when_local": "2026-05-12T08:00:00-04:00", "title": "this week"},  # in next-week window, drop
        {"id": "sch-3", "status": "planned",
         "when_local": "2026-05-05T08:00:00-04:00", "title": "past"},  # past, drop
    ]}
    salvaged = _carry_forward_events(schedule, week_end)
    ids = [e["id"] for e in salvaged]
    assert ids == ["sch-1"]


def test_carry_forward_drops_resolved_events():
    week_end = datetime(2026, 5, 18).astimezone()
    schedule = {"events": [
        {"id": "sch-1", "status": "as_planned",
         "when_local": "2026-06-01T19:00:00-04:00", "title": "happened"},
    ]}
    assert _carry_forward_events(schedule, week_end) == []


def test_carry_forward_handles_missing_schedule():
    assert _carry_forward_events({}, datetime(2026, 5, 18).astimezone()) == []
    assert _carry_forward_events(None, datetime(2026, 5, 18).astimezone()) == []


# ── compute_planner_cost ─────────────────────────────────────────────

def test_compute_planner_cost_zero_for_empty():
    assert compute_planner_cost({}) == 0.0


def test_compute_planner_cost_arithmetic():
    usage = {
        "input_tokens": 5000, "cache_read_tokens": 4000,
        "cache_creation_tokens": 500, "output_tokens": 1000,
    }
    cost = compute_planner_cost(usage)
    # uncached_input = 5000 - 4000 - 500 = 500
    # 500*3 + 4000*0.30 + 500*6 + 1000*15 = 1500 + 1200 + 3000 + 15000 = 20700 / 1M
    assert abs(cost - 0.0207) < 1e-6


# ── build_planner_tool ───────────────────────────────────────────────

def test_planner_tool_schema():
    tool = build_planner_tool()
    assert tool["name"] == "report_week"
    item = tool["input_schema"]["properties"]["events"]["items"]
    assert "is_major_event" in item["properties"]
    assert set(item["required"]) == {"kind", "title", "when_local", "duration_min"}


# ── run_weekly_planner: bad inputs ───────────────────────────────────

def test_run_planner_bad_project_dir():
    out = run_weekly_planner(MagicMock(), "/nonexistent/path/123")
    assert out["error"] == "bad project_dir"


# ── run_weekly_planner: first_seen_date stamping ─────────────────────

def test_first_seen_date_stamped_on_first_run_and_week_generated(project_dir):
    """On a fresh character with no first_seen_date, planner stamps today AND
    generates the week's schedule (no major event due to grace, but normal
    plans still get made). Without this, fresh characters had no schedule for
    1+ weeks while waiting for a second cron firing.
    """
    save_project_state(project_dir, {
        "scheduler_state": {},  # no first_seen_date
        "callbacks": {"open": []},
        "wellbeing": {"state": "Even", "wb_mod": 0},
    })
    client = _mock_client(_mock_planner_response(_basic_week()))
    out = run_weekly_planner(
        client, project_dir,
        now_dt=datetime(2026, 5, 10, 20, 0).astimezone(),
        rng=random.Random(0),
    )
    # First-run still generates the week (does NOT bail after stamping)
    assert out["skipped"] is False
    assert out["events_planned"] == 6
    # Major event roll suppressed by grace period
    assert out["major_event"] is False
    # first_seen_date is now stamped
    from character_project_state import load_project_state
    st = load_project_state(project_dir)
    assert st["scheduler_state"]["first_seen_date"] == "2026-05-10"
    # Schedule was actually written
    sched = load_schedule(project_dir)
    assert sched is not None
    assert len(sched["events"]) == 6


def test_grace_period_returns_no_event(project_dir):
    """Within LIFE_EVENT_GRACE_DAYS of first_seen_date, planner runs but
    roll_major_event returns None — no major event for first 2 weeks."""
    rng = random.Random(0)
    today_iso = "2026-04-05"  # only 4 days after first_seen_date 2026-04-01
    seed = roll_major_event(today_iso, "2026-04-01", rng=rng)
    assert seed is None


def test_post_grace_period_can_roll_major(project_dir):
    """After grace period (14 days), roll_major_event can produce events."""
    # Use a high-bias seed: 99 → major roll
    class _ForcedRng:
        def __init__(self):
            self.calls = 0
            self.rolls = [99, 0]  # 99 → major (98<99<=100), 0 → first item in bucket
        def randint(self, a, b):
            v = self.rolls[self.calls % len(self.rolls)]
            self.calls += 1
            return max(a, min(b, v))
        def choice(self, seq):
            return seq[0]
        def random(self):
            return 0.5

    today_iso = "2026-04-30"  # 29 days after 2026-04-01 (past grace)
    seed = roll_major_event(today_iso, "2026-04-01", rng=_ForcedRng())
    assert seed is not None
    assert seed["magnitude"] == "major"


# ── run_weekly_planner: idempotence ──────────────────────────────────

def test_idempotence_skips_when_week_already_planned(project_dir):
    """Running the planner twice for the same target Monday is a no-op on
    the second call."""
    # Pre-stamp last_planner_run_week_of for next Monday
    save_project_state(project_dir, {
        "scheduler_state": {
            "first_seen_date": "2026-04-01",
            "last_planner_run_week_of": "2026-05-11",
        },
        "callbacks": {"open": []},
        "wellbeing": {"state": "Even", "wb_mod": 0},
    })
    client = MagicMock()
    out = run_weekly_planner(
        client, project_dir,
        now_dt=datetime(2026, 5, 10, 20, 0).astimezone(),  # Sunday → next Mon = 5/11
    )
    assert out["skipped"] is True
    assert out["reason"] == "week already planned"
    assert client.messages.create.call_count == 0


# ── run_weekly_planner: full flow with mocked Sonnet ─────────────────

def test_planner_writes_schedule_with_correct_week(project_dir):
    """Full flow: planner writes a schedule.json with week_of=next Monday and
    the model's events, stamps revision_history per event, and bumps
    scheduler_state.last_planner_run_week_of."""
    client = _mock_client(_mock_planner_response(_basic_week()))
    out = run_weekly_planner(
        client, project_dir,
        now_dt=datetime(2026, 5, 10, 20, 0).astimezone(),
        rng=random.Random(0),  # very unlikely to hit major (default seed)
    )
    assert out["skipped"] is False
    assert out["week_of"] == "2026-05-11"
    sched = load_schedule(project_dir)
    assert sched["week_of"] == "2026-05-11"
    assert len(sched["events"]) == 6  # all from _basic_week
    for ev in sched["events"]:
        assert ev["status"] == "planned"
        assert ev["resolution"] is None
        assert isinstance(ev["revision_history"], list)
        assert ev["revision_history"][0]["by"] == "planner"
        assert ev["id"].startswith("sch-")

    # State updated
    from character_project_state import load_project_state
    st = load_project_state(project_dir)
    assert st["scheduler_state"]["last_planner_run_week_of"] == "2026-05-11"


def test_new_event_ids_dont_collide_with_previous_week(project_dir):
    """Regression: when zero events get salvaged from the previous week's
    schedule, the planner must NOT restart event ids at sch-1 (would collide
    with life_stream entries from the previous week that reference sch-1
    etc). It should continue from existing_schedule.next_id at minimum.
    """
    # Previous week had events sch-1..sch-12, no chat-added far-future events
    save_schedule(project_dir, {
        "week_of": "2026-05-04",
        "generated_at": "2026-05-03T20:00:00-04:00",
        "next_id": 13,  # next free id from previous week
        "events": [
            # All in the past or in the new week's window — none survive carry-forward
            {"id": f"sch-{i}", "status": "as_planned",
             "kind": "work", "title": f"shift {i}",
             "when_local": f"2026-05-0{i % 7 + 1}T08:00:00-04:00",
             "duration_min": 480, "revision_history": []}
            for i in range(1, 13)
        ],
    })
    client = _mock_client(_mock_planner_response(_basic_week()))
    run_weekly_planner(
        client, project_dir,
        now_dt=datetime(2026, 5, 10, 20, 0).astimezone(),
        rng=random.Random(0),
    )
    sched = load_schedule(project_dir)
    new_ids = [int(e["id"].split("-")[1]) for e in sched["events"]]
    # Every new event id must be >= previous schedule's next_id (13)
    assert all(eid >= 13 for eid in new_ids), \
        f"new ids {new_ids} would collide with previous-week ids 1..12"


def test_planner_carries_forward_far_future_events(project_dir):
    """Events more than a week out from the new week_of carry forward into
    the new schedule."""
    # Existing schedule has a future event 3 weeks out
    save_schedule(project_dir, {
        "week_of": "2026-05-04",
        "generated_at": "2026-05-03T20:00:00-04:00",
        "next_id": 100,
        "events": [
            {"id": "sch-99", "status": "planned",
             "kind": "social", "title": "concert in June",
             "when_local": "2026-06-15T19:00:00-04:00", "duration_min": 180,
             "with": ["Diana"], "revision_history": []},
        ],
    })
    client = _mock_client(_mock_planner_response(_basic_week()))
    run_weekly_planner(
        client, project_dir,
        now_dt=datetime(2026, 5, 10, 20, 0).astimezone(),
        rng=random.Random(0),
    )
    sched = load_schedule(project_dir)
    ids = [e["id"] for e in sched["events"]]
    assert "sch-99" in ids
    # New events get fresh ids continuing from max existing
    new_ids = [e["id"] for e in sched["events"] if e["id"] != "sch-99"]
    assert all(e.startswith("sch-") for e in new_ids)
    assert all(int(e.split("-")[1]) > 99 for e in new_ids)


# ── run_weekly_planner: major event handling ─────────────────────────

def test_major_event_writes_life_stream_entry(project_dir):
    """When backend rolls a major event AND model emits is_major_event=true,
    a major_event entry lands in life_stream."""
    # Force the RNG to roll major
    class _ForcedRng:
        def __init__(self):
            self.calls = 0
            self.rolls = [99, 0]
        def randint(self, a, b):
            v = self.rolls[self.calls % len(self.rolls)]
            self.calls += 1
            return max(a, min(b, v))
        def choice(self, seq):
            return seq[0]
        def random(self):
            return 0.5

    events = _basic_week()
    events[2]["is_major_event"] = True  # Friday Shae night gets the major-event tag

    client = _mock_client(_mock_planner_response(events))
    out = run_weekly_planner(
        client, project_dir,
        now_dt=datetime(2026, 5, 10, 20, 0).astimezone(),
        rng=_ForcedRng(),
    )
    assert out["major_event"] is True
    # Life stream got a major_event entry
    stream = read_life_stream_all(project_dir)
    major = [e for e in stream if e.get("kind") == "major_event"]
    assert len(major) == 1
    assert major[0]["ref"].startswith("sch-")
    # Schedule has the event marked magnitude=major
    sched = load_schedule(project_dir)
    major_evs = [e for e in sched["events"] if e.get("magnitude") == "major"]
    assert len(major_evs) == 1


def test_no_major_event_no_life_stream_entry(project_dir):
    """No major-event roll → no major_event life_stream entry."""
    client = _mock_client(_mock_planner_response(_basic_week()))
    # rng.randint(1, 100) where seeded 0 should return ~50, well under "major" (99-100)
    out = run_weekly_planner(
        client, project_dir,
        now_dt=datetime(2026, 5, 10, 20, 0).astimezone(),
        rng=random.Random(0),
    )
    assert out["major_event"] is False
    stream = read_life_stream_all(project_dir)
    major = [e for e in stream if e.get("kind") == "major_event"]
    assert len(major) == 0


def test_planner_no_events_from_model(project_dir):
    """If the model returns an empty events list, planner bails without
    overwriting the schedule."""
    save_schedule(project_dir, init_schedule("2026-05-04", "2026-05-03T20:00:00-04:00"))
    client = _mock_client(_mock_planner_response([]))
    out = run_weekly_planner(
        client, project_dir,
        now_dt=datetime(2026, 5, 10, 20, 0).astimezone(),
        rng=random.Random(0),
    )
    assert out["skipped"] is True
    assert out["reason"] == "no events from model"
    # Schedule unchanged
    sched = load_schedule(project_dir)
    assert sched["week_of"] == "2026-05-04"
