"""Tests for plot_beats parser + state helpers + injection rendering."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from game_systems.plot_beats import (
    parse_plot_doc,
    load_session_beats,
    init_beat_state,
    normalize_beat_state,
    advance_beat,
    set_beat,
    set_session,
    build_current_beat_injection,
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
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "Plot - Session 2.md"), "w", encoding="utf-8") as f:
                f.write(SAMPLE_DOC)
            beats = load_session_beats(tmp, 2)
            self.assertEqual(len(beats), 3)
            # Wrong session number → empty
            self.assertEqual(load_session_beats(tmp, 1), [])

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
                  "session_completed": []}
            new = advance_beat(bs, uploads_dir=tmp)
            self.assertEqual(new["current_beat"], 2)
            self.assertIn(1, new["completed_beats"])
            self.assertEqual(new["current_session"], 1)

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


if __name__ == "__main__":
    unittest.main()
