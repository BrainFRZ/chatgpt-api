"""Tests for the split-storage memory architecture (index + body files)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from character_storage import (  # noqa: E402
    CharacterStore,
    KIND_MEMORIES,
    MEMORIES_BODY_DIR,
    MEMORY_BODY_MAX_CHARS,
    MEMORY_HOOK_MAX_CHARS,
    _build_memory_md,
    _derive_hook,
    _extract_body_from_md,
    _memory_body_filename,
    _slugify,
    apply_memory_ops_to_store,
)


class TestHelpers(unittest.TestCase):
    def test_slugify_basic(self):
        self.assertEqual(_slugify("Friday Chili Night"), "friday_chili_night")

    def test_slugify_unicode_punctuation(self):
        self.assertEqual(_slugify("in-joke / coffee bean promise"), "in_joke_coffee_bean_promise")

    def test_slugify_empty(self):
        self.assertEqual(_slugify(""), "untitled")
        self.assertEqual(_slugify(None), "untitled")  # type: ignore

    def test_slugify_max_len(self):
        long = "a" * 200
        self.assertLessEqual(len(_slugify(long, max_len=60)), 60)

    def test_memory_body_filename_uses_focus(self):
        entry = {"id": 11, "focus": "in-joke / coffee bean promise"}
        self.assertEqual(_memory_body_filename(entry), "in_joke_coffee_bean_promise_0011.md")

    def test_memory_body_filename_falls_back_to_text(self):
        entry = {"id": 3, "text": "Met in middle school band in Cherry Hill 2002"}
        # no focus; falls back to slug of text[:40]
        name = _memory_body_filename(entry)
        self.assertTrue(name.endswith("_0003.md"))
        self.assertNotEqual(name, "_0003.md")  # has SOME slug

    def test_derive_hook_first_sentence(self):
        text = "The cafe almost went under. Months of stress."
        self.assertEqual(_derive_hook(text), "The cafe almost went under.")

    def test_derive_hook_no_terminator(self):
        text = "no terminator here just a long phrase"
        self.assertEqual(_derive_hook(text), text)

    def test_derive_hook_truncates_long_first_sentence(self):
        text = "x" * 500 + ". more"
        hook = _derive_hook(text, max_len=100)
        self.assertLessEqual(len(hook), 100)
        self.assertTrue(hook.endswith("…"))

    def test_extract_body_from_md_strips_frontmatter(self):
        content = "---\nid: 1\nhook: A hook\n---\n\nThe actual body text."
        self.assertEqual(_extract_body_from_md(content), "The actual body text.")

    def test_extract_body_from_md_no_frontmatter(self):
        content = "Just a plain body."
        self.assertEqual(_extract_body_from_md(content), "Just a plain body.")

    def test_build_memory_md_roundtrip(self):
        entry = {
            "id": 7, "date": "2025-01-01", "focus": "test", "impact": 3,
            "tier": "moderate", "hook": "A hook", "branch_id": None,
        }
        md = _build_memory_md(entry, "Body prose here.")
        self.assertIn("---", md)
        self.assertIn("id: 7", md)
        self.assertIn("hook: A hook", md)
        self.assertEqual(_extract_body_from_md(md), "Body prose here.")


class TestSplitStorageWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memstore_test_")
        self.store = CharacterStore(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_writes_index_line_and_body_file(self):
        ops = [{
            "action": "add",
            "text": "Long memory body text. Multiple sentences here. Could be paragraphs.",
            "impact": 3,
            "focus": "test memory",
            "hook": "Short hook for index scan",
        }]
        n = apply_memory_ops_to_store(
            self.store, ops, current_turn=1, today_iso="2026-05-11",
            branch_msg_id="msg-1",
        )
        self.assertEqual(n, 1)

        # Index line exists with hook + body_file, no text
        raw = self.store.read_all(KIND_MEMORIES)
        self.assertEqual(len(raw), 1)
        e = raw[0]
        self.assertNotIn("text", e)
        self.assertEqual(e.get("hook"), "Short hook for index scan")
        self.assertTrue(e.get("body_file", "").endswith(".md"))
        self.assertTrue(e["body_file"].startswith("memories/"))

        # Body file exists with the prose
        body_path = os.path.join(self.tmp, e["body_file"])
        self.assertTrue(os.path.isfile(body_path))
        with open(body_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Long memory body text", content)
        self.assertIn("---", content)  # frontmatter present

    def test_add_derives_hook_when_omitted(self):
        ops = [{"action": "add", "text": "The cafe almost went under. Months of stress.", "impact": 2}]
        apply_memory_ops_to_store(
            self.store, ops, current_turn=1, today_iso="2026-05-11",
            branch_msg_id="msg-1",
        )
        e = self.store.read_all(KIND_MEMORIES)[0]
        self.assertEqual(e["hook"], "The cafe almost went under.")

    def test_resolve_bodies_default_loads_text(self):
        ops = [{"action": "add", "text": "Body content for resolve test.", "impact": 3, "focus": "resolve"}]
        apply_memory_ops_to_store(
            self.store, ops, current_turn=1, today_iso="2026-05-11", branch_msg_id="msg-1",
        )
        # read_filtered with default resolve_bodies=True populates text
        loaded = self.store.read_filtered(KIND_MEMORIES)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].get("text"), "Body content for resolve test.")

    def test_resolve_bodies_false_returns_index_only(self):
        ops = [{"action": "add", "text": "Body content cheap-scan test.", "impact": 3, "focus": "scan"}]
        apply_memory_ops_to_store(
            self.store, ops, current_turn=1, today_iso="2026-05-11", branch_msg_id="msg-1",
        )
        # resolve_bodies=False — text should NOT be loaded
        index_only = self.store.read_filtered(KIND_MEMORIES, resolve_bodies=False)
        self.assertEqual(len(index_only), 1)
        self.assertNotIn("text", index_only[0])
        self.assertIn("hook", index_only[0])
        self.assertIn("body_file", index_only[0])

    def test_fetch_by_ids_loads_bodies_by_default(self):
        ops = [{"action": "add", "text": "Memory #1 body.", "impact": 3, "focus": "one"},
               {"action": "add", "text": "Memory #2 body.", "impact": 3, "focus": "two"}]
        apply_memory_ops_to_store(
            self.store, ops, current_turn=1, today_iso="2026-05-11", branch_msg_id="msg-1",
        )
        fetched = self.store.fetch_by_ids(KIND_MEMORIES, [1, 2])
        texts = sorted(e.get("text") for e in fetched)
        self.assertEqual(texts, ["Memory #1 body.", "Memory #2 body."])

    def test_drop_deletes_body_file(self):
        ops = [{"action": "add", "text": "Soon to be dropped.", "impact": 1, "focus": "drop test"}]
        apply_memory_ops_to_store(
            self.store, ops, current_turn=1, today_iso="2026-05-11", branch_msg_id="msg-1",
        )
        e = self.store.read_all(KIND_MEMORIES)[0]
        body_path = os.path.join(self.tmp, e["body_file"])
        self.assertTrue(os.path.isfile(body_path))

        apply_memory_ops_to_store(
            self.store, [{"action": "drop", "id": e["id"]}],
            current_turn=2, today_iso="2026-05-11", branch_msg_id="msg-2",
        )
        self.assertEqual(self.store.read_all(KIND_MEMORIES), [])
        self.assertFalse(os.path.isfile(body_path), "body file should be deleted with the memory")

    def test_body_max_chars_enforced(self):
        huge = "x" * (MEMORY_BODY_MAX_CHARS + 500)
        apply_memory_ops_to_store(
            self.store, [{"action": "add", "text": huge, "impact": 2, "focus": "huge"}],
            current_turn=1, today_iso="2026-05-11", branch_msg_id="msg-1",
        )
        e = self.store.read_filtered(KIND_MEMORIES)[0]
        self.assertLessEqual(len(e["text"]), MEMORY_BODY_MAX_CHARS)


class TestBackwardCompat(unittest.TestCase):
    """Legacy entries (text in the line, no body_file) keep working."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memstore_compat_")
        self.store = CharacterStore(self.tmp)
        # Manually write a legacy-format entry — what existed before this refactor
        path = self.store._path(KIND_MEMORIES)
        legacy = {
            "id": 1, "branch_id": None, "text": "Legacy memory still here.",
            "quote": None, "date": "2025-01-01", "impact": 3, "tier": "moderate",
            "turn_created": 0, "focus": "legacy", "last_referenced_date": "2025-01-01",
        }
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(legacy) + "\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_legacy_entry_reads_text(self):
        loaded = self.store.read_filtered(KIND_MEMORIES)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["text"], "Legacy memory still here.")

    def test_legacy_entry_no_body_file_but_text_present(self):
        loaded = self.store.read_filtered(KIND_MEMORIES, resolve_bodies=False)
        # Legacy entries still have text in the line; resolve_bodies=False
        # doesn't strip it (we only skip the body-file load, which isn't
        # applicable for legacy entries anyway).
        self.assertEqual(loaded[0]["text"], "Legacy memory still here.")

    def test_mixed_legacy_and_new(self):
        # Add a new-format entry alongside the existing legacy one
        apply_memory_ops_to_store(
            self.store,
            [{"action": "add", "text": "New format body.", "impact": 4, "focus": "new"}],
            current_turn=1, today_iso="2026-05-11", branch_msg_id="msg-1",
        )
        loaded = sorted(self.store.read_filtered(KIND_MEMORIES), key=lambda e: e["id"])
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["text"], "Legacy memory still here.")
        self.assertEqual(loaded[1]["text"], "New format body.")


class TestMigrationScript(unittest.TestCase):
    """Migration is idempotent + leaves new entries alone."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memstore_migrate_")
        # The migration script needs a character_profile.di to discover the project
        with open(os.path.join(self.tmp, "character_profile.di"), "w", encoding="utf-8") as f:
            f.write("# Test\n")
        self.store = CharacterStore(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_legacy(self):
        path = self.store._path(KIND_MEMORIES)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": 1, "text": "First legacy memory. With more detail.", "impact": 3,
                "focus": "first", "date": "2025-01-01",
            }) + "\n")
            f.write(json.dumps({
                "id": 2, "text": "Second legacy memory. Different focus.", "impact": 2,
                "focus": "second", "date": "2025-02-01",
            }) + "\n")

    def test_migrate_legacy_to_split(self):
        self._seed_legacy()
        from scripts.migrate_memories_to_files import migrate_one
        rep = migrate_one(self.tmp, dry_run=False)
        self.assertEqual(rep["migrated"], 2)
        self.assertEqual(rep["errors"], 0)

        # Body files exist
        bodies = sorted(os.listdir(os.path.join(self.tmp, MEMORIES_BODY_DIR)))
        self.assertEqual(len(bodies), 2)

        # Index has body_file, no text
        entries = sorted(self.store.read_all(KIND_MEMORIES), key=lambda e: e["id"])
        for e in entries:
            self.assertNotIn("text", e)
            self.assertIn("body_file", e)
            self.assertIn("hook", e)

        # Bodies load back as text
        loaded = sorted(self.store.read_filtered(KIND_MEMORIES), key=lambda e: e["id"])
        self.assertEqual(loaded[0]["text"], "First legacy memory. With more detail.")
        self.assertEqual(loaded[1]["text"], "Second legacy memory. Different focus.")

    def test_migrate_idempotent(self):
        self._seed_legacy()
        from scripts.migrate_memories_to_files import migrate_one
        rep1 = migrate_one(self.tmp, dry_run=False)
        rep2 = migrate_one(self.tmp, dry_run=False)
        self.assertEqual(rep1["migrated"], 2)
        self.assertEqual(rep2["migrated"], 0)
        self.assertEqual(rep2["skipped_already"], 2)


if __name__ == "__main__":
    unittest.main()
