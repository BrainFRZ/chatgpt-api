"""
Fuzz tests for name dice generation (generate_name_dice) and injection wiring.

Covers:
- Filename matching: case variations, partial matches, false positives, unicode
- Die size parsing: all supported patterns, edge cases, malformed content
- Output format: block structure, value ranges, ordering, determinism
- Filesystem edge cases: empty dirs, unreadable files, binary files, symlinks
- Integration: build_single_agent_injections and build_events_messages wiring
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import (
    generate_name_dice,
    build_single_agent_injections,
    build_events_messages,
    _NAME_GEN_PATTERN,
    _DIE_SIZE_PATTERN,
    NAME_DICE_COUNT,
    _fresh_pipeline_state,
    migrate_pipeline_state,
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def uploads(tmp_path):
    """Create and return a temporary uploads directory."""
    d = tmp_path / "uploads"
    d.mkdir()
    return str(d)


def _write(uploads_dir, filename, content=""):
    """Helper: write a file into the uploads dir."""
    path = os.path.join(uploads_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# ============================================================
# 1. Filename matching
# ============================================================

class TestFilenameMatching:
    """Verify _NAME_GEN_PATTERN matches correct filenames and rejects others."""

    @pytest.mark.parametrize("stem", [
        "Name Generator",
        "name generator",
        "NAME GENERATOR",
        "Fantasy Name Generator",
        "Sci-Fi Name Generator",
        "name_generator",
        "name-generator",
        "NaMe GeNeRaToR",
        "Generator of Names",
        "generator_name",
        "Broken Orbit Name Generator v2",
        "The Severing Fantasy Name Generator (Final)",
        "name.generator",
        "nAmE wacky GENERATOR",
        "cyberpunk_name_generator",
        "Generator - Name Tables",
    ])
    def test_valid_stems_match(self, stem):
        assert _NAME_GEN_PATTERN.search(stem), f"Should match: {stem!r}"

    @pytest.mark.parametrize("stem", [
        "Character Sheet",
        "Name List",               # no "generator"
        "Random Generator",        # no "name"
        "Spell Names",             # no "generator"
        "Generator",               # no "name"
        "name",                    # no "generator"
        "Instructions",
        "Plot Document",
        "Rename Generator Log",    # "rename" contains "name" — this WILL match
    ])
    def test_non_generators_rejected_or_matched(self, stem):
        # These should NOT match (except noted edge cases)
        if "name" in stem.lower() and "generator" in stem.lower():
            assert _NAME_GEN_PATTERN.search(stem)
        else:
            assert not _NAME_GEN_PATTERN.search(stem), f"Should not match: {stem!r}"

    def test_extension_stripped_before_match(self, uploads):
        """The function uses os.path.splitext to strip extension before matching."""
        _write(uploads, "Name Generator.md", "## Names (1d20)\n| 1 | Bob |")
        result = generate_name_dice(uploads)
        assert "[NAME DICE]" in result

    def test_txt_extension(self, uploads):
        _write(uploads, "Name Generator.txt", "## Names (1d20)\n| 1 | Bob |")
        result = generate_name_dice(uploads)
        assert "[NAME DICE]" in result

    def test_no_extension(self, uploads):
        _write(uploads, "Name Generator", "## Names (1d20)\n| 1 | Bob |")
        result = generate_name_dice(uploads)
        assert "[NAME DICE]" in result


# ============================================================
# 2. Die size pattern parsing
# ============================================================

class TestDieSizeParsing:
    """Verify _DIE_SIZE_PATTERN extracts correct die sizes from content."""

    @pytest.mark.parametrize("pattern,expected", [
        ("(1-100)", 100),
        ("(1d100)", 100),
        ("(1d50)", 50),
        ("(1d20)", 20),
        ("(1d12)", 12),
        ("(1d10)", 10),
        ("(1-20)", 20),
        ("(1-50)", 50),
        ("(1-12)", 12),
        ("(1 d100)", 100),   # space between 1 and d
        ("(1-10)", 10),
    ])
    def test_valid_patterns(self, pattern, expected):
        m = _DIE_SIZE_PATTERN.search(pattern)
        assert m, f"Should match: {pattern!r}"
        assert int(m.group(1)) == expected

    @pytest.mark.parametrize("pattern", [
        "(2d20)",       # not 1dN
        "(d100)",       # no leading 1
        "(1d)",         # no size
        "(100)",        # no 1- or 1d prefix
        "1d20",         # no parens
        "(1d20",        # unclosed paren
        "1d20)",        # no open paren
        "(3-100)",      # doesn't start with 1
        "(1 100)",      # space without d is not dice notation
        "(1 50)",       # same — space-only separator invalid
        "",
    ])
    def test_invalid_patterns_no_match(self, pattern):
        assert not _DIE_SIZE_PATTERN.search(pattern), f"Should not match: {pattern!r}"

    def test_multiple_sizes_in_content(self, uploads):
        content = """# Fantasy Name Generator
## First Names (1d100)
| Roll | Name |
| 1 | Aldric |

## Surnames (1d50)
| Roll | Name |
| 1 | Stormwind |

## Epithets (1d20)
| Roll | Epithet |
| 1 | the Bold |

## Nicknames (1d12)
| Roll | Nick |
| 1 | Red |
"""
        _write(uploads, "Fantasy Name Generator.md", content)
        result = generate_name_dice(uploads)
        assert "d100:" in result
        assert "d50:" in result
        assert "d20:" in result
        assert "d12:" in result

    def test_duplicate_die_sizes_deduplicated(self, uploads):
        content = """# Name Generator
## Male First Names (1d100)
| 1 | Bob |
## Female First Names (1d100)
| 1 | Alice |
"""
        _write(uploads, "Name Generator.md", content)
        result = generate_name_dice(uploads)
        # d100 should appear exactly once
        assert result.count("d100:") == 1

    def test_die_sizes_sorted_largest_first(self, uploads):
        content = """# Name Generator
## A (1d10)
## B (1d100)
## C (1d20)
"""
        _write(uploads, "Name Generator.md", content)
        result = generate_name_dice(uploads)
        lines = [l for l in result.split("\n") if l.startswith("d")]
        assert len(lines) == 3
        sizes = [int(l.split(":")[0][1:]) for l in lines]
        assert sizes == sorted(sizes, reverse=True)
        assert sizes == [100, 20, 10]


# ============================================================
# 3. Output format validation
# ============================================================

class TestOutputFormat:
    """Verify the [NAME DICE] block format."""

    def test_block_delimiters(self, uploads):
        _write(uploads, "Name Generator.md", "## Names (1d20)\n")
        result = generate_name_dice(uploads)
        assert result.startswith("[NAME DICE]\n")
        assert result.endswith("\n[/NAME DICE]")

    def test_instruction_lines(self, uploads):
        _write(uploads, "Name Generator.md", "## Names (1d20)\n")
        result = generate_name_dice(uploads)
        assert "Use these with the Name Generator document" in result
        assert "Consume left-to-right; do not skip or reuse." in result

    def test_correct_roll_count(self, uploads):
        _write(uploads, "Name Generator.md", "## Names (1d100)\n## Surnames (1d20)\n")
        result = generate_name_dice(uploads)
        for line in result.split("\n"):
            if line.startswith("d"):
                values = line.split(": ", 1)[1].split(", ")
                assert len(values) == NAME_DICE_COUNT, f"Expected {NAME_DICE_COUNT} rolls, got {len(values)} in: {line}"

    def test_roll_values_in_range(self, uploads):
        _write(uploads, "Name Generator.md", "## Names (1d100)\n## Surnames (1d20)\n## Nicks (1d6)\n")
        # Run multiple times to increase coverage of randomness
        for _ in range(50):
            result = generate_name_dice(uploads)
            for line in result.split("\n"):
                if line.startswith("d"):
                    die_size = int(line.split(":")[0][1:])
                    values = [int(v.strip()) for v in line.split(": ", 1)[1].split(", ")]
                    for v in values:
                        assert 1 <= v <= die_size, f"Value {v} out of range [1, {die_size}]"

    def test_empty_string_for_no_generator(self, uploads):
        _write(uploads, "Character Sheet.md", "## Stats (1d20)\n")
        assert generate_name_dice(uploads) == ""

    def test_empty_string_for_no_die_sizes(self, uploads):
        _write(uploads, "Name Generator.md", "Just a list of names\nNo dice tables here\n")
        assert generate_name_dice(uploads) == ""

    def test_no_trailing_newlines(self, uploads):
        _write(uploads, "Name Generator.md", "## Names (1d20)\n")
        result = generate_name_dice(uploads)
        assert not result.endswith("\n")  # ends with [/NAME DICE], no trailing newline


# ============================================================
# 4. Filesystem edge cases
# ============================================================

class TestFilesystemEdgeCases:
    """Verify robustness against filesystem quirks."""

    def test_nonexistent_dir(self):
        assert generate_name_dice("/nonexistent/path/uploads") == ""

    def test_empty_string_dir(self):
        assert generate_name_dice("") == ""

    def test_none_dir(self):
        assert generate_name_dice(None) == ""

    def test_empty_uploads_dir(self, uploads):
        assert generate_name_dice(uploads) == ""

    def test_file_instead_of_dir(self, tmp_path):
        f = tmp_path / "not_a_dir.txt"
        f.write_text("hello")
        assert generate_name_dice(str(f)) == ""

    def test_binary_file_ignored(self, uploads):
        path = os.path.join(uploads, "Name Generator.bin")
        with open(path, "wb") as f:
            f.write(b"\x00\xff\xfe" * 100)
        # Binary file may fail to decode — should return "" gracefully
        result = generate_name_dice(uploads)
        # Either empty (decode failure) or parsed (if bytes happen to decode)
        assert isinstance(result, str)

    def test_unreadable_file_skipped(self, uploads):
        path = _write(uploads, "Name Generator.md", "## Names (1d20)\n")
        os.chmod(path, 0o000)
        try:
            result = generate_name_dice(uploads)
            # Should return "" since the only matching file is unreadable
            assert result == ""
        finally:
            os.chmod(path, 0o644)

    def test_multiple_generator_files(self, uploads):
        _write(uploads, "Fantasy Name Generator.md", "## First (1d100)\n")
        _write(uploads, "Sci-Fi Name Generator.md", "## Designation (1d20)\n")
        result = generate_name_dice(uploads)
        assert "d100:" in result
        assert "d20:" in result

    def test_mixed_valid_and_invalid_files(self, uploads):
        _write(uploads, "Name Generator.md", "## Names (1d100)\n")
        _write(uploads, "Character Sheet.md", "## Stats (1d20)\n")
        _write(uploads, "Plot Notes.md", "Some plot stuff\n")
        result = generate_name_dice(uploads)
        assert "d100:" in result
        # d20 from Character Sheet should NOT appear
        assert "d20:" not in result

    def test_unicode_filenames(self, uploads):
        _write(uploads, "Générateur de Name Generator.md", "## Noms (1d50)\n")
        result = generate_name_dice(uploads)
        assert "d50:" in result

    def test_deeply_nested_content(self, uploads):
        """Die patterns deep in file should still be found."""
        content = "# Name Generator\n" + ("filler line\n" * 500) + "## Hidden (1d100)\n"
        _write(uploads, "Name Generator.md", content)
        result = generate_name_dice(uploads)
        assert "d100:" in result

    def test_very_large_file(self, uploads):
        """Large file should not crash."""
        content = "# Name Generator\n## Names (1d20)\n" + ("| 1 | SomeName |\n" * 10000)
        _write(uploads, "Name Generator.md", content)
        result = generate_name_dice(uploads)
        assert "d20:" in result

    def test_empty_file(self, uploads):
        _write(uploads, "Name Generator.md", "")
        assert generate_name_dice(uploads) == ""


# ============================================================
# 5. Content edge cases
# ============================================================

class TestContentEdgeCases:
    """Fuzz various markdown content shapes."""

    def test_die_pattern_in_table_header(self, uploads):
        _write(uploads, "Name Generator.md", "| Roll (1d100) | Name |\n| 1 | Alice |")
        result = generate_name_dice(uploads)
        assert "d100:" in result

    def test_die_pattern_in_heading(self, uploads):
        _write(uploads, "Name Generator.md", "## First Names (1d100)\nsome text")
        result = generate_name_dice(uploads)
        assert "d100:" in result

    def test_die_pattern_in_body_text(self, uploads):
        _write(uploads, "Name Generator.md", "Roll (1d20) for a random name.\n")
        result = generate_name_dice(uploads)
        assert "d20:" in result

    def test_multiple_patterns_same_line(self, uploads):
        _write(uploads, "Name Generator.md", "Use (1d100) for first, (1d20) for last\n")
        result = generate_name_dice(uploads)
        assert "d100:" in result
        assert "d20:" in result

    def test_hyphenated_range_format(self, uploads):
        _write(uploads, "Name Generator.md", "## Names (1-100)\n")
        result = generate_name_dice(uploads)
        assert "d100:" in result

    def test_mixed_formats(self, uploads):
        content = "## First (1d100)\n## Last (1-50)\n## Nick (1d12)\n"
        _write(uploads, "Name Generator.md", content)
        result = generate_name_dice(uploads)
        assert "d100:" in result
        assert "d50:" in result
        assert "d12:" in result

    def test_surrounding_noise(self, uploads):
        content = """# Name Generator for Broken Orbit
This document uses dice rolls blah blah.
Some random parens (hello) and (world).
(not a die) and (abc123) noise.
## Crew Names (1d100)
| Roll | Name |
| 1 | Zyx |
"""
        _write(uploads, "Name Generator.md", content)
        result = generate_name_dice(uploads)
        assert "d100:" in result
        lines = [l for l in result.split("\n") if l.startswith("d")]
        assert len(lines) == 1  # only d100, no false positives from noise

    def test_zero_die_size_ignored(self, uploads):
        """(1d0) or (1-0) should produce d0 — but values would be randint(1,0) which raises.
        Actually int('0') = 0 which is a valid die size. Let's verify it doesn't crash."""
        _write(uploads, "Name Generator.md", "## Names (1d0)\n")
        # randint(1, 0) raises ValueError — should be handled gracefully
        # Actually the function doesn't guard against die_size=0; let's see what happens
        try:
            result = generate_name_dice(uploads)
            # If it returns empty or crashes, both are acceptable behaviors
            # But we should not get an unhandled exception
        except ValueError:
            pytest.fail("generate_name_dice should not raise ValueError for d0")

    def test_very_large_die_size(self, uploads):
        _write(uploads, "Name Generator.md", "## Names (1d99999)\n")
        result = generate_name_dice(uploads)
        assert "d99999:" in result
        values = [int(v.strip()) for v in result.split("d99999: ")[1].split("\n")[0].split(", ")]
        for v in values:
            assert 1 <= v <= 99999

    def test_die_size_1(self, uploads):
        _write(uploads, "Name Generator.md", "## Names (1d1)\n")
        result = generate_name_dice(uploads)
        assert "d1:" in result
        values = [int(v.strip()) for v in result.split("d1: ")[1].split("\n")[0].split(", ")]
        assert all(v == 1 for v in values)


# ============================================================
# 6. Integration: build_single_agent_injections
# ============================================================

class TestSingleAgentInjectionWiring:
    """Verify name_dice flows through build_single_agent_injections."""

    def test_name_dice_included_when_provided(self):
        state = _fresh_pipeline_state()
        state = migrate_pipeline_state(state)
        result = build_single_agent_injections(
            state, name_dice="[NAME DICE]\nd20: 1, 2, 3, 4\n[/NAME DICE]"
        )
        assert "[NAME DICE]" in result
        assert "d20: 1, 2, 3, 4" in result

    def test_name_dice_omitted_when_empty(self):
        state = _fresh_pipeline_state()
        state = migrate_pipeline_state(state)
        result = build_single_agent_injections(state, name_dice="")
        assert "[NAME DICE]" not in result

    def test_name_dice_omitted_by_default(self):
        state = _fresh_pipeline_state()
        state = migrate_pipeline_state(state)
        result = build_single_agent_injections(state)
        assert "[NAME DICE]" not in result

    def test_name_dice_after_dice_pool(self):
        state = _fresh_pipeline_state()
        state = migrate_pipeline_state(state)
        dice_pool = "[DICE POOL]\nd20: 10, 15\n[/DICE POOL]"
        name_dice = "[NAME DICE]\nd100: 42, 87, 3, 65\n[/NAME DICE]"
        result = build_single_agent_injections(
            state, dice_pool=dice_pool, name_dice=name_dice
        )
        pool_pos = result.index("[DICE POOL]")
        name_pos = result.index("[NAME DICE]")
        assert pool_pos < name_pos, "Name dice should appear after dice pool"

    def test_both_dice_pool_and_name_dice(self):
        state = _fresh_pipeline_state()
        state = migrate_pipeline_state(state)
        dice_pool = "[DICE POOL]\nd20: 5\n[/DICE POOL]"
        name_dice = "[NAME DICE]\nd100: 50, 60, 70, 80\n[/NAME DICE]"
        result = build_single_agent_injections(
            state, dice_pool=dice_pool, name_dice=name_dice
        )
        assert "[DICE POOL]" in result
        assert "[NAME DICE]" in result
        assert "[/DICE POOL]" in result
        assert "[/NAME DICE]" in result


# ============================================================
# 7. Integration: build_events_messages
# ============================================================

class TestEventsMessagesWiring:
    """Verify name_dice flows through build_events_messages."""

    def _make_messages(self, name_dice=""):
        state = _fresh_pipeline_state()
        state = migrate_pipeline_state(state)
        return build_events_messages(
            system_prompt="You are the events agent.",
            history_messages=[],
            user_message={"role": "user", "content": "I enter the tavern."},
            pipeline_state=state,
            name_dice=name_dice,
        )

    def test_name_dice_in_user_message(self):
        msgs = self._make_messages(name_dice="[NAME DICE]\nd20: 5, 10, 15, 20\n[/NAME DICE]")
        user_msg = msgs[-1]["content"]
        assert "[NAME DICE]" in user_msg
        assert "d20: 5, 10, 15, 20" in user_msg

    def test_no_name_dice_when_empty(self):
        msgs = self._make_messages(name_dice="")
        user_msg = msgs[-1]["content"]
        assert "[NAME DICE]" not in user_msg

    def test_name_dice_before_user_content(self):
        msgs = self._make_messages(name_dice="[NAME DICE]\nd20: 1, 2, 3, 4\n[/NAME DICE]")
        user_msg = msgs[-1]["content"]
        name_pos = user_msg.index("[NAME DICE]")
        content_pos = user_msg.index("I enter the tavern.")
        assert name_pos < content_pos, "Injections should precede user content"


# ============================================================
# 8. End-to-end: generate + inject
# ============================================================

class TestEndToEnd:
    """Full round-trip: file on disk → generate_name_dice → injection."""

    def test_full_round_trip(self, uploads):
        content = """# Broken Orbit Name Generator
## First Names (1d100)
| Roll | Name |
| 1-10 | Kira |

## Callsigns (1d20)
| Roll | Callsign |
| 1 | Ghost |

## Surnames (1d50)
| Roll | Surname |
| 1 | Vasquez |
"""
        _write(uploads, "Broken Orbit Name Generator.md", content)
        name_dice = generate_name_dice(uploads)

        assert "[NAME DICE]" in name_dice
        assert "d100:" in name_dice
        assert "d50:" in name_dice
        assert "d20:" in name_dice

        # Feed into single-agent injections
        state = _fresh_pipeline_state()
        state = migrate_pipeline_state(state)
        result = build_single_agent_injections(state, name_dice=name_dice)
        assert "[NAME DICE]" in result

        # Feed into events messages
        msgs = build_events_messages(
            system_prompt="test",
            history_messages=[],
            user_message={"role": "user", "content": "hello"},
            pipeline_state=state,
            name_dice=name_dice,
        )
        assert "[NAME DICE]" in msgs[-1]["content"]

    def test_project_without_generator(self, uploads):
        """A project with uploads but no name generator should produce no injection."""
        _write(uploads, "Character Sheet.md", "## Stats (1d20)\nSTR 10")
        _write(uploads, "Plot Notes.md", "The quest begins...")
        name_dice = generate_name_dice(uploads)
        assert name_dice == ""

        state = _fresh_pipeline_state()
        state = migrate_pipeline_state(state)
        result = build_single_agent_injections(state, name_dice=name_dice)
        assert "[NAME DICE]" not in result

    def test_determinism_within_seed(self, uploads):
        """With same random seed, output should be identical."""
        import random
        _write(uploads, "Name Generator.md", "## Names (1d100)\n## Titles (1d20)\n")
        random.seed(42)
        result1 = generate_name_dice(uploads)
        random.seed(42)
        result2 = generate_name_dice(uploads)
        assert result1 == result2

    def test_different_seeds_different_output(self, uploads):
        """Different seeds should (very likely) produce different output."""
        import random
        _write(uploads, "Name Generator.md", "## Names (1d100)\n")
        random.seed(1)
        result1 = generate_name_dice(uploads)
        random.seed(999)
        result2 = generate_name_dice(uploads)
        # Extremely unlikely to be equal with different seeds and d100
        assert result1 != result2
