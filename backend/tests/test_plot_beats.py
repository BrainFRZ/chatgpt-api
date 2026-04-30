"""Tests for plot_beats parser + state helpers + injection rendering."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from game_systems.plot_beats import (
    parse_plot_doc,
    load_session_beats,
    load_session_title,
    derive_canonical_pacing,
    init_beat_state,
    normalize_beat_state,
    advance_beat,
    set_beat,
    set_session,
    build_current_beat_injection,
    _PLOT_FILENAME_RE,
    _parse_session_spec,
)


SAMPLE_DOC = """## Session 1: "The Pull"

Some prelude.

### Beat 1: The Job

Body of beat 1.

More text.

### Beat 2: Legwork

Body of beat 2 — recon and intel.

### Beat 3: The Approach

Final beat body.
"""


class TestParser(unittest.TestCase):
    def test_parses_three_beats(self):
        beats = parse_plot_doc(SAMPLE_DOC)
        self.assertEqual(len(beats), 3)
        self.assertEqual(beats[0]["number"], 1)
        self.assertEqual(beats[0]["title"], "The Job")
        self.assertIn("Body of beat 1", beats[0]["body"])
        self.assertEqual(beats[2]["number"], 3)
        self.assertEqual(beats[2]["title"], "The Approach")

    def test_empty_doc(self):
        self.assertEqual(parse_plot_doc(""), [])
        self.assertEqual(parse_plot_doc(None), [])

    def test_no_beat_headers(self):
        self.assertEqual(parse_plot_doc("# Title\n\nNo beats here."), [])


class TestLoadSessionBeats(unittest.TestCase):
    def test_finds_session_file_case_insensitive(self):
        # Filename in lowercase to verify case-insensitive matching.
        # SAMPLE_DOC's `## Session 1:` header matches the filename's "1".
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "plot - session 1.md"), "w", encoding="utf-8") as f:
                f.write(SAMPLE_DOC)
            beats = load_session_beats(tmp, 1)
            self.assertEqual(len(beats), 3)
            # Wrong session number → empty
            self.assertEqual(load_session_beats(tmp, 2), [])

    def test_missing_dir(self):
        self.assertEqual(load_session_beats("/nonexistent/path", 1), [])


class TestStateHelpers(unittest.TestCase):
    def test_init_default(self):
        bs = init_beat_state()
        self.assertEqual(bs["current_session"], 1)
        self.assertEqual(bs["current_beat"], 1)
        self.assertEqual(bs["completed_beats"], [])

    def test_normalize_garbage(self):
        bs = normalize_beat_state({"current_session": "garbage",
                                    "current_beat": None})
        self.assertEqual(bs["current_session"], 1)
        self.assertEqual(bs["current_beat"], 1)

    def test_normalize_preserves_valid(self):
        bs = normalize_beat_state({"current_session": 2, "current_beat": 5,
                                    "completed_beats": [1, 2, 3, 4]})
        self.assertEqual(bs["current_session"], 2)
        self.assertEqual(bs["current_beat"], 5)
        self.assertEqual(bs["completed_beats"], [1, 2, 3, 4])

    def test_advance_beat_within_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "Plot - Session 1.md"), "w", encoding="utf-8") as f:
                f.write(SAMPLE_DOC)
            bs = {"current_session": 1, "current_beat": 1, "completed_beats": [],
                  "session_completed": [], "beat_responses": 7}
            new = advance_beat(bs, uploads_dir=tmp)
            self.assertEqual(new["current_beat"], 2)
            self.assertIn(1, new["completed_beats"])
            self.assertEqual(new["current_session"], 1)
            # Per-beat counter must reset on advance.
            self.assertEqual(new["beat_responses"], 0)

    def test_set_beat_resets_response_counter(self):
        bs = {"current_session": 1, "current_beat": 2, "completed_beats": [1],
              "session_completed": [], "beat_responses": 5}
        new = set_beat(bs, 4)
        self.assertEqual(new["current_beat"], 4)
        self.assertEqual(new["beat_responses"], 0)

    def test_set_session_resets_response_counter(self):
        bs = {"current_session": 1, "current_beat": 3, "completed_beats": [1, 2],
              "session_completed": [], "beat_responses": 9}
        new = set_session(bs, 2)
        self.assertEqual(new["current_session"], 2)
        self.assertEqual(new["beat_responses"], 0)

    def test_advance_at_last_beat_rolls_over_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "Plot - Session 1.md"), "w", encoding="utf-8") as f:
                f.write(SAMPLE_DOC)  # Has Beats 1-3
            bs = {"current_session": 1, "current_beat": 3,
                  "completed_beats": [1, 2], "session_completed": []}
            new = advance_beat(bs, uploads_dir=tmp)
            self.assertEqual(new["current_session"], 2)
            self.assertEqual(new["current_beat"], 1)
            self.assertEqual(new["completed_beats"], [])
            self.assertIn(1, new["session_completed"])

    def test_set_beat_marks_earlier_complete(self):
        bs = init_beat_state()
        new = set_beat(bs, 5)
        self.assertEqual(new["current_beat"], 5)
        self.assertEqual(new["completed_beats"], [1, 2, 3, 4])

    def test_set_session_resets_beat(self):
        bs = {"current_session": 1, "current_beat": 5, "completed_beats": [1, 2, 3, 4],
              "session_completed": []}
        new = set_session(bs, 2)
        self.assertEqual(new["current_session"], 2)
        self.assertEqual(new["current_beat"], 1)
        self.assertEqual(new["completed_beats"], [])
        self.assertIn(1, new["session_completed"])


class TestInjection(unittest.TestCase):
    def _make_uploads(self, tmp):
        with open(os.path.join(tmp, "Plot - Session 1.md"), "w", encoding="utf-8") as f:
            f.write(SAMPLE_DOC)
        return tmp

    def test_renders_current_beat(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_uploads(tmp)
            block = build_current_beat_injection(
                {"current_session": 1, "current_beat": 2, "completed_beats": [1],
                 "session_completed": []},
                tmp,
            )
            self.assertIn("Beat 2", block)
            self.assertIn("Legwork", block)
            self.assertIn("Body of beat 2", block)
            self.assertIn("PACING RULE", block)
            self.assertIn("Completed this session: 1", block)
            self.assertIn("Next: Beat 3", block)

    def test_no_block_when_no_plot_doc(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = build_current_beat_injection(init_beat_state(), tmp)
            self.assertEqual(block, "")

    def test_handles_out_of_range_beat(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_uploads(tmp)
            block = build_current_beat_injection(
                {"current_session": 1, "current_beat": 99,
                 "completed_beats": [], "session_completed": []},
                tmp,
            )
            self.assertIn("no matching", block.lower())

    def test_last_beat_shows_end_of_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_uploads(tmp)
            block = build_current_beat_injection(
                {"current_session": 1, "current_beat": 3,
                 "completed_beats": [1, 2], "session_completed": []},
                tmp,
            )
            self.assertIn("end of this session", block.lower())


class TestFilenameMatching(unittest.TestCase):
    """Filename forms accepted by _PLOT_FILENAME_RE + _parse_session_spec."""

    def _spec_for(self, fn):
        m = _PLOT_FILENAME_RE.match(fn)
        return m.group("spec") if m else None

    def test_single_session_dash(self):
        self.assertEqual(self._spec_for("Plot - Session 1.md"), "1")
        self.assertEqual(_parse_session_spec("1"), {1})

    def test_episode_keyword(self):
        self.assertEqual(self._spec_for("Plot - Episode 5.md"), "5")
        self.assertEqual(self._spec_for("Plot - Episodes 1-3.md"), "1-3")
        self.assertEqual(_parse_session_spec("1-3"), {1, 2, 3})

    def test_and_separator(self):
        self.assertEqual(self._spec_for("Plot and Sessions 6-7.md"), "6-7")
        self.assertEqual(_parse_session_spec("6-7"), {6, 7})

    def test_project_name_prefix(self):
        spec = self._spec_for("Broken Orbit - Plot and Sessions 6-7.md")
        self.assertEqual(spec, "6-7")

    def test_s_prefixed_range(self):
        self.assertEqual(self._spec_for("Plot and Sessions S1-S2.md"), "S1-S2")
        self.assertEqual(_parse_session_spec("S1-S2"), {1, 2})

    def test_non_plot_files_ignored(self):
        self.assertIsNone(self._spec_for("random.md"))
        self.assertIsNone(self._spec_for("NPCs.md"))
        self.assertIsNone(self._spec_for("Plot - Session.md"))

    def test_garbage_spec_rejected(self):
        # Filename matches but the spec is unparseable → empty session set.
        self.assertEqual(_parse_session_spec("notes"), set())


MULTI_SESSION_DOC = """## Engine

Some preamble.

## Session 6: "The Walls Have Eyes" [FULL DETAIL]

### Beats

Stuff about session 6.

### Beat 1: Cold Open

Body of S6 beat 1.

### Beat 2: Investigation

Body of S6 beat 2.

## Session 7: "First Sparks" [FULL DETAIL]

### Beats

Stuff about session 7.

### Beat 1: The Spark

Body of S7 beat 1.

## Session Triggers

Common rules — should not match as session 0.
"""


class TestMultiSessionFile(unittest.TestCase):
    """Files like `Broken Orbit - Plot and Sessions 6-7.md` cover multiple
    sessions; the H2 `## Session N:` blocks delimit them."""

    def _make(self, tmp):
        path = os.path.join(tmp, "Broken Orbit - Plot and Sessions 6-7.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(MULTI_SESSION_DOC)
        return tmp

    def test_load_title_picks_correct_session_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp)
            self.assertEqual(load_session_title(tmp, 6), "The Walls Have Eyes")
            self.assertEqual(load_session_title(tmp, 7), "First Sparks")
            # Session 8 isn't covered by this file.
            self.assertIsNone(load_session_title(tmp, 8))

    def test_load_beats_only_for_requested_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp)
            s6 = load_session_beats(tmp, 6)
            s7 = load_session_beats(tmp, 7)
            self.assertEqual(len(s6), 2)
            self.assertEqual(s6[0]["title"], "Cold Open")
            self.assertEqual(s6[1]["title"], "Investigation")
            self.assertEqual(len(s7), 1)
            self.assertEqual(s7[0]["title"], "The Spark")

    def test_session_triggers_h2_is_not_treated_as_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp)
            # `## Session Triggers` is not a numbered session — should not
            # leak in as session 0 or anything weird.
            self.assertIsNone(load_session_title(tmp, 0))

    def test_derive_canonical_pacing_for_range_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp)
            p = derive_canonical_pacing(
                {"current_session": 6, "current_beat": 2}, tmp
            )
            self.assertEqual(p["episode"], "Session 6: The Walls Have Eyes")
            self.assertEqual(p["beat"], "Beat 2: Investigation")


SEVERING_DOC = """# The Severing — Episodes 1-3 (Condensed)

**Format Notes:** ...

---

# Episode 1 — Convergence

**Level:** 1 | **Location:** Millhaven

## Episode Goals
- Introduce both PCs.
- Establish blight threat.

## Beat 1 — Arrival [Social]

Body of episode 1, beat 1.

## Beat 2 — Investigation [Social/Exploration]

Body of episode 1, beat 2.

## Episode End

Resolution prose.

# Episode 2 — Journey East

## Beat 1 — Journey Begins [Exploration/Social]

Body of episode 2, beat 1.

## Beat 2 — The Halfway Inn [Social]

Body of episode 2, beat 2.

# Episode 3 — Saltmere

## Beat 1 — Saltmere [Social]

Body of episode 3, beat 1.
"""


class TestBumpBeatResponseCounter(unittest.TestCase):
    """Backend-tracked per-beat turn counter (mirrored into pacing.responses)."""

    def test_increments_existing_counter(self):
        from pipeline import _bump_beat_response_counter
        ps = {"beat_state": {"current_session": 1, "current_beat": 2,
                              "beat_responses": 4, "completed_beats": [1],
                              "session_completed": []}}
        _bump_beat_response_counter(ps)
        self.assertEqual(ps["beat_state"]["beat_responses"], 5)

    def test_initializes_when_missing(self):
        from pipeline import _bump_beat_response_counter
        ps = {}
        _bump_beat_response_counter(ps)
        self.assertEqual(ps["beat_state"]["beat_responses"], 1)

    def test_apply_canonical_pacing_mirrors_into_pacing_responses(self):
        from pipeline import _apply_canonical_pacing
        ps = {
            "beat_state": {"current_session": 1, "current_beat": 2,
                           "beat_responses": 3, "completed_beats": [1],
                           "session_completed": []},
            "pacing": {"episode": "stale", "beat": "stale", "responses": 999},
        }
        # uploads_dir=None still mirrors the counter — episode/beat
        # overrides only happen when a plot doc exists, but the responses
        # mirror is independent.
        _apply_canonical_pacing(ps, None)
        self.assertEqual(ps["pacing"]["responses"], 3)


class TestSeveringFormat(unittest.TestCase):
    """The Severing uses `# Episode N — Title` (H1, em-dash) for sessions
    and `## Beat N — Title [Tag]` (H2, em-dash, bracketed tag) for beats.
    Different from Dead Air's `## Session N: ...` / `### Beat N: ...`."""

    def _make(self, tmp):
        path = os.path.join(tmp, "Plot - Episodes 1-3.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SEVERING_DOC)
        return tmp

    def test_load_episode_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp)
            self.assertEqual(load_session_title(tmp, 1), "Convergence")
            self.assertEqual(load_session_title(tmp, 2), "Journey East")
            self.assertEqual(load_session_title(tmp, 3), "Saltmere")

    def test_load_h2_beats_with_em_dash_and_brackets(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp)
            beats = load_session_beats(tmp, 1)
            self.assertEqual(len(beats), 2)
            self.assertEqual(beats[0]["title"], "Arrival")        # bracket stripped
            self.assertEqual(beats[1]["title"], "Investigation")  # bracket stripped

    def test_derive_uses_episode_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp)
            p = derive_canonical_pacing(
                {"current_session": 2, "current_beat": 2}, tmp
            )
            # Echoes the doc's wording — "Episode" not "Session".
            self.assertEqual(p["episode"], "Episode 2: Journey East")
            self.assertEqual(p["beat"], "Beat 2: The Halfway Inn")

    def test_episode_goals_h2_not_treated_as_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp)
            # `## Episode Goals` (no digit) should not match as session 0.
            self.assertIsNone(load_session_title(tmp, 0))


if __name__ == "__main__":
    unittest.main()
