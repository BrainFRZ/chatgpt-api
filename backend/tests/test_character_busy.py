"""Tests for character_busy — schedule-driven busy detection + SOS detection."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from character_busy import (  # noqa: E402
    BUSY_EVENT_KINDS,
    collapse_busy_placeholders_in_history,
    current_busy_event,
    describe_busy_event,
    is_sos_message,
    busy_status_for_notification,
    strip_sos_slash_command,
)


# Test schedule with a sleep block, a work shift, and a social event
def _make_schedule():
    # Anchored times — we'll feed known "now" relative to these
    base = datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc)  # 11pm UTC
    return {
        "events": [
            {
                "id": "sch-sleep",
                "kind": "sleep",
                "title": "sleep",
                "when_local": base.isoformat(),  # 11pm
                "duration_min": 480,             # 8 hours, ends 7am
                "status": "planned",
            },
            {
                "id": "sch-work",
                "kind": "work",
                "title": "open cafe",
                "when_local": (base + timedelta(hours=8)).isoformat(),  # 7am
                "duration_min": 480,             # 8 hour shift, ends 3pm
                "status": "planned",
            },
            {
                "id": "sch-yoga",
                "kind": "self_care",
                "title": "yoga",
                "when_local": (base + timedelta(hours=18)).isoformat(),  # 5pm
                "duration_min": 60,
                "status": "planned",
            },
            {
                "id": "sch-cancelled",
                "kind": "work",
                "title": "old shift",
                "when_local": (base + timedelta(hours=8)).isoformat(),
                "duration_min": 480,
                "status": "cancelled",   # should be ignored
            },
        ]
    }


# ── current_busy_event ────────────────────────────────────────────────────

class TestCurrentBusyEvent:
    def test_returns_sleep_event_when_in_window(self):
        sched = _make_schedule()
        # 1am — middle of sleep
        now = datetime(2026, 5, 10, 1, 0, tzinfo=timezone.utc)
        ev = current_busy_event(sched, now)
        assert ev is not None
        assert ev["kind"] == "sleep"

    def test_returns_work_event_when_in_window(self):
        sched = _make_schedule()
        # 10am — mid-shift
        now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
        ev = current_busy_event(sched, now)
        assert ev is not None
        assert ev["kind"] == "work"

    def test_returns_none_during_non_busy_event(self):
        # Yoga is self_care, not in BUSY_EVENT_KINDS by default
        sched = _make_schedule()
        # 5:30pm — mid-yoga
        now = datetime(2026, 5, 10, 17, 30, tzinfo=timezone.utc)
        ev = current_busy_event(sched, now)
        assert ev is None

    def test_returns_none_in_gaps(self):
        sched = _make_schedule()
        # 3:30pm — between work end (3pm) and yoga (5pm)
        now = datetime(2026, 5, 10, 15, 30, tzinfo=timezone.utc)
        assert current_busy_event(sched, now) is None

    def test_skips_cancelled_event(self):
        sched = _make_schedule()
        # During the cancelled shift's window — should not return it
        # The active "open cafe" shift IS in this same window though, so
        # we should still get a busy event (work). Verify the active one
        # is returned, not the cancelled one.
        now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
        ev = current_busy_event(sched, now)
        assert ev is not None
        assert ev["status"] != "cancelled"

    def test_explicit_busy_true_overrides_kind(self):
        sched = {
            "events": [{
                "id": "sch-1",
                "kind": "social",   # not in BUSY_EVENT_KINDS
                "busy": True,        # explicit override
                "title": "important",
                "when_local": datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc).isoformat(),
                "duration_min": 60,
                "status": "planned",
            }]
        }
        now = datetime(2026, 5, 10, 12, 30, tzinfo=timezone.utc)
        ev = current_busy_event(sched, now)
        assert ev is not None
        assert ev["title"] == "important"

    def test_explicit_busy_false_overrides_kind(self):
        sched = {
            "events": [{
                "id": "sch-1",
                "kind": "work",   # busy by default
                "busy": False,     # explicit override
                "when_local": datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc).isoformat(),
                "duration_min": 60,
                "status": "planned",
            }]
        }
        now = datetime(2026, 5, 10, 12, 30, tzinfo=timezone.utc)
        assert current_busy_event(sched, now) is None

    def test_none_schedule_returns_none(self):
        assert current_busy_event(None, datetime.now(timezone.utc)) is None
        assert current_busy_event({}, datetime.now(timezone.utc)) is None
        assert current_busy_event({"events": []}, datetime.now(timezone.utc)) is None

    def test_malformed_event_skipped(self):
        sched = {"events": [
            {"kind": "sleep"},  # missing when_local
            {"when_local": "not-a-date", "kind": "sleep", "duration_min": 60, "status": "planned"},
            {"when_local": datetime(2026, 5, 10, 1, 0, tzinfo=timezone.utc).isoformat(),
             "kind": "sleep", "duration_min": 0, "status": "planned"},  # zero duration
        ]}
        now = datetime(2026, 5, 10, 1, 30, tzinfo=timezone.utc)
        assert current_busy_event(sched, now) is None

    def test_event_at_exact_start_is_busy(self):
        sched = _make_schedule()
        now = datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc)  # exact start of sleep
        ev = current_busy_event(sched, now)
        assert ev is not None
        assert ev["kind"] == "sleep"

    def test_event_at_exact_end_is_NOT_busy(self):
        # End is exclusive: at exactly the boundary, the event has just ended
        sched = _make_schedule()
        now = datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc)  # exact end of sleep
        ev = current_busy_event(sched, now)
        # At 7am sleep just ended AND work just started — should return work
        assert ev is not None
        assert ev["kind"] == "work"


# ── is_sos_message ────────────────────────────────────────────────────────

class TestIsSosMessage:
    def test_uppercase_sos_word(self):
        assert is_sos_message("SOS need help now") is True

    def test_lowercase_sos_word(self):
        assert is_sos_message("sos shae i need you") is True

    def test_mixed_case(self):
        assert is_sos_message("Sos this is bad") is True

    def test_911_word(self):
        assert is_sos_message("911 my mom is in the hospital") is True

    def test_emergency_word(self):
        assert is_sos_message("emergency please answer") is True
        assert is_sos_message("EMERGENCY") is True

    def test_slash_command(self):
        assert is_sos_message("/sos help") is True
        assert is_sos_message("  /sos something happened") is True
        assert is_sos_message("/SOS uppercase") is True

    def test_normal_message_not_sos(self):
        assert is_sos_message("how was your day") is False
        assert is_sos_message("just got home") is False

    def test_word_boundary_avoids_false_positives(self):
        # "sosig" or "rules" shouldn't trigger SOS pattern
        assert is_sos_message("the cafe sells sosigs (sausages)") is False
        assert is_sos_message("the cosmos is wild today") is False

    def test_911_substring_in_year_should_not_trigger(self):
        # 911 with word boundaries — "9111" shouldn't trigger but "911" should
        assert is_sos_message("we lived at 9111 maple") is False

    def test_empty_or_non_string(self):
        assert is_sos_message("") is False
        assert is_sos_message("   ") is False
        assert is_sos_message(None) is False  # type: ignore
        assert is_sos_message(123) is False  # type: ignore


# ── describe_busy_event ───────────────────────────────────────────────────

class TestDescribeBusyEvent:
    def test_sleep(self):
        assert describe_busy_event({"kind": "sleep"}) == "asleep"

    def test_work_with_title(self):
        s = describe_busy_event({"kind": "work", "title": "open cafe"})
        assert "at work" in s and "open cafe" in s

    def test_work_without_title(self):
        assert describe_busy_event({"kind": "work"}) == "at work"

    def test_other_falls_back_to_title(self):
        assert describe_busy_event({"kind": "social", "title": "dinner with diana"}) == "dinner with diana"

    def test_empty_falls_back_to_busy(self):
        assert describe_busy_event({}) == "busy"
        assert describe_busy_event(None) == "busy"  # type: ignore


# ── busy_status_for_notification ──────────────────────────────────────────

class TestStripSosSlashCommand:
    def test_basic_strip(self):
        cleaned, was = strip_sos_slash_command("/sos help me")
        assert cleaned == "help me"
        assert was is True

    def test_uppercase_command(self):
        cleaned, was = strip_sos_slash_command("/SOS something is wrong")
        assert cleaned == "something is wrong"
        assert was is True

    def test_with_leading_whitespace(self):
        cleaned, was = strip_sos_slash_command("   /sos urgent")
        assert cleaned == "urgent"
        assert was is True

    def test_no_command_passes_through(self):
        cleaned, was = strip_sos_slash_command("hi how are you")
        assert cleaned == "hi how are you"
        assert was is False

    def test_command_only_no_args(self):
        cleaned, was = strip_sos_slash_command("/sos")
        assert cleaned == ""
        assert was is True

    def test_keyword_alone_doesnt_strip(self):
        # Plain "SOS shae help" — the keyword detector triggers SOS via
        # is_sos_message, but the strip helper only handles the literal /sos
        # slash-command prefix
        cleaned, was = strip_sos_slash_command("SOS shae help")
        assert cleaned == "SOS shae help"
        assert was is False

    def test_non_string(self):
        cleaned, was = strip_sos_slash_command(None)  # type: ignore
        assert cleaned == ""
        assert was is False


class TestCollapseBusyPlaceholders:
    def _ph(self, content="[no reply — asleep]", kind="sleep", desc="asleep", id_="ph-x"):
        return {
            "id": id_,
            "role": "assistant",
            "content": content,
            "busy_placeholder": True,
            "busy_event": {"kind": kind, "description": desc},
        }

    def _user(self, content, id_="u-x", timestamp=None):
        msg = {"id": id_, "role": "user", "content": content}
        if timestamp:
            msg["timestamp"] = timestamp
        return msg

    def _asst(self, content, id_="a-x"):
        return {"id": id_, "role": "assistant", "content": content}

    def test_no_placeholders_passes_through(self):
        msgs = [
            {"role": "system", "content": "sys"},
            self._user("hi", id_="u1"),
            self._asst("hello", id_="a1"),
        ]
        out = collapse_busy_placeholders_in_history(msgs)
        assert len(out) == 3
        assert out[1]["id"] == "u1"
        assert out[2]["id"] == "a1"

    def test_single_busy_gap_filtered_when_followed_by_real_assistant(self):
        # user → busy_placeholder → real_assistant should drop the placeholder
        msgs = [
            self._user("u1 you up", id_="u1"),
            self._ph(id_="ph1"),
        ]
        out = collapse_busy_placeholders_in_history(msgs)
        # After flush, just the user remains
        assert len(out) == 1
        assert out[0]["id"] == "u1"

    def test_multiple_overnight_messages_merge(self):
        msgs = [
            self._user("u1 you up", id_="u1"),
            self._ph(id_="ph1"),
            self._user("u2 you home from work", id_="u2"),
            self._ph(id_="ph2"),
            self._user("u3 still you up", id_="u3"),
            self._ph(id_="ph3"),
        ]
        out = collapse_busy_placeholders_in_history(msgs)
        # Should collapse to a single merged user message
        assert len(out) == 1
        merged = out[0]
        assert merged["role"] == "user"
        assert "[gap" in merged["content"]
        assert "u1 you up" in merged["content"]
        assert "u2 you home from work" in merged["content"]
        assert "u3 still you up" in merged["content"]
        # merged_from preserves origin IDs
        assert set(merged["merged_from"]) == {"u1", "u2", "u3"}

    def test_merged_user_inherits_latest_timestamp(self):
        msgs = [
            self._user("u1", id_="u1", timestamp="2026-05-09T22:00:00-04:00"),
            self._ph(id_="ph1"),
            self._user("u2", id_="u2", timestamp="2026-05-09T23:30:00-04:00"),
        ]
        out = collapse_busy_placeholders_in_history(msgs)
        assert len(out) == 1
        # Should base on the latest user (u2)
        assert out[0]["timestamp"] == "2026-05-09T23:30:00-04:00"

    def test_distinct_gap_descriptions_combined(self):
        # Work → sleep crossover, two different busy descriptions
        msgs = [
            self._user("during work", id_="u1"),
            self._ph(kind="work", desc="at work", id_="ph1"),
            self._user("after work, during sleep", id_="u2"),
            self._ph(kind="sleep", desc="asleep", id_="ph2"),
            self._user("morning", id_="u3"),
        ]
        out = collapse_busy_placeholders_in_history(msgs)
        assert len(out) == 1
        marker = out[0]["content"]
        assert "at work" in marker
        assert "asleep" in marker

    def test_assistant_response_breaks_merge(self):
        # user1 -> placeholder -> user2 -> real_assistant -> user3 -> placeholder -> user4
        msgs = [
            self._user("u1", id_="u1"),
            self._ph(id_="ph1"),
            self._user("u2", id_="u2"),
            self._asst("hey i replied", id_="a1"),
            self._user("u3", id_="u3"),
            self._ph(id_="ph2"),
            self._user("u4", id_="u4"),
        ]
        out = collapse_busy_placeholders_in_history(msgs)
        # Expect: merged(u1+u2) -> a1 -> merged(u3+u4)
        assert len(out) == 3
        assert out[0]["role"] == "user"
        assert "u1" in out[0]["content"]
        assert "u2" in out[0]["content"]
        assert out[1]["id"] == "a1"
        assert out[2]["role"] == "user"
        assert "u3" in out[2]["content"]
        assert "u4" in out[2]["content"]

    def test_input_not_mutated(self):
        msgs = [
            self._user("u1", id_="u1"),
            self._ph(id_="ph1"),
            self._user("u2", id_="u2"),
        ]
        original_u1_content = msgs[0]["content"]
        out = collapse_busy_placeholders_in_history(msgs)
        assert msgs[0]["content"] == original_u1_content
        # Output is a different list
        assert out is not msgs


class TestBusyStatusForNotification:
    def test_includes_end_time(self):
        ev = {
            "kind": "sleep",
            "title": "sleep",
            "when_local": datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc).isoformat(),
            "duration_min": 480,
        }
        notif = busy_status_for_notification(ev, datetime(2026, 5, 10, 1, 0, tzinfo=timezone.utc))
        assert notif["type"] == "character_busy"
        assert notif["kind"] == "sleep"
        assert notif["description"] == "asleep"
        assert notif["ends_at"] is not None
        # Should be 8am UTC (start + 8hr)
        end = datetime.fromisoformat(notif["ends_at"])
        assert end.hour == 8

    def test_handles_missing_duration(self):
        ev = {"kind": "sleep", "when_local": datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc).isoformat()}
        notif = busy_status_for_notification(ev, datetime(2026, 5, 10, 1, 0, tzinfo=timezone.utc))
        assert notif["ends_at"] is None
