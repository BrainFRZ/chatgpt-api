"""Unit tests for character_inner_state — soft-fail on API errors, output
shape preservation, empty-input guard, usage math, builder rendering, and
the regression guard that build_inner_state_injection is wired into the
Characters injection assembly.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from character_inner_state import (  # noqa: E402
    INNER_STATE_MODEL,
    OPUS45_INPUT_RATE,
    OPUS45_CACHE_READ_RATE,
    OPUS45_CACHE_WRITE_RATE,
    OPUS45_OUTPUT_RATE,
    _format_recent_dialogue,
    _summarize_state_for_inner,
    build_inner_state_tool,
    compute_inner_state_cost,
    run_inner_state,
)
from game_systems.characters import (  # noqa: E402
    build_characters_injections,
    build_inner_state_injection,
    build_prior_inner_states_injection,
)


def _mock_response_with_tool_input(tool_input: dict, *, name: str = "report_inner_state"):
    """Construct a mock anthropic SDK response with a single tool_use block
    carrying the given input dict. Sets a usage object with sensible defaults."""
    block = SimpleNamespace(type="tool_use", name=name, input=tool_input)
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=80,
            cache_creation_input_tokens=20,
        ),
    )


def _mock_client(response):
    """Mock anthropic client whose .messages.create returns `response`."""
    client = MagicMock()
    client.messages.create.return_value = response
    return client


# ── run_inner_state ──────────────────────────────────────────────────


def test_api_exception_returns_empty(tmp_path):
    """API exception → soft-fail to ({}, {}), no crash."""
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("boom")

    inner, usage = run_inner_state(
        client,
        str(tmp_path),
        characters_state={"wellbeing": {"state": "Even"}},
        user_input="hey",
    )
    assert inner == {}
    assert usage == {}


def test_all_four_fields_preserved(tmp_path):
    """Tool block returns all 4 fields → all preserved in output."""
    payload = {
        "feeling": "warm but cautious",
        "wanting": "to know if she still matters",
        "noticing": "his message landed at 2am — something's up",
        "holding_back": "the question about why he's been distant",
    }
    response = _mock_response_with_tool_input(payload)
    client = _mock_client(response)

    inner, usage = run_inner_state(
        client,
        str(tmp_path),
        characters_state={"wellbeing": {"state": "Even"}},
        user_input="hey, you up?",
    )
    assert inner == payload
    assert usage["output_tokens"] == 50


def test_partial_fields_only_present_keys_returned(tmp_path):
    """Tool block returns subset of fields → missing keys absent from output."""
    payload = {"feeling": "stung", "wanting": "an apology"}
    response = _mock_response_with_tool_input(payload)
    client = _mock_client(response)

    inner, _ = run_inner_state(
        client,
        str(tmp_path),
        characters_state={},
        user_input="hi",
    )
    assert set(inner.keys()) == {"feeling", "wanting"}
    assert "noticing" not in inner
    assert "holding_back" not in inner


def test_empty_user_input_no_api_call(tmp_path):
    """Empty user_input → returns ({}, {}) without invoking the client."""
    client = MagicMock()
    inner, usage = run_inner_state(
        client,
        str(tmp_path),
        characters_state={},
        user_input="",
    )
    assert inner == {}
    assert usage == {}
    client.messages.create.assert_not_called()


def test_whitespace_only_user_input_no_api_call(tmp_path):
    """Whitespace-only user_input is also treated as empty."""
    client = MagicMock()
    inner, usage = run_inner_state(
        client, str(tmp_path), characters_state={}, user_input="   \n\t  ",
    )
    assert inner == {}
    assert usage == {}
    client.messages.create.assert_not_called()


def test_empty_string_fields_filtered_out(tmp_path):
    """Whitespace-only or empty string fields from the tool are dropped."""
    payload = {
        "feeling": "  ",
        "wanting": "",
        "noticing": "his tone shifted",
        "holding_back": None,  # also non-string — should be dropped
    }
    response = _mock_response_with_tool_input(payload)
    client = _mock_client(response)

    inner, _ = run_inner_state(
        client, str(tmp_path), characters_state={}, user_input="hey",
    )
    assert inner == {"noticing": "his tone shifted"}


def test_usage_input_tokens_summed_correctly(tmp_path):
    """input_tokens in returned usage is raw + cache_read + cache_creation."""
    response = _mock_response_with_tool_input({"feeling": "ok"})
    client = _mock_client(response)

    _, usage = run_inner_state(
        client, str(tmp_path), characters_state={}, user_input="hi",
    )
    # 100 + 80 + 20 = 200
    assert usage["input_tokens"] == 200
    assert usage["cache_read_tokens"] == 80
    assert usage["cache_creation_tokens"] == 20
    assert usage["output_tokens"] == 50


# ── compute_inner_state_cost ─────────────────────────────────────────


def test_compute_cost_zero_for_empty_usage():
    assert compute_inner_state_cost({}) == 0.0
    assert compute_inner_state_cost(None) == 0.0


def test_compute_cost_arithmetic():
    """Cost = (uncached_input * rate + cache_read * cache_read_rate + cache_creation * cache_write_rate + output * output_rate) / 1M.

    Use simple round numbers so the assertion is readable.
    """
    usage = {
        "input_tokens": 1_000_000,         # raw total
        "cache_read_tokens": 500_000,
        "cache_creation_tokens": 200_000,
        "output_tokens": 100_000,
    }
    # uncached input = 1_000_000 - 500_000 - 200_000 = 300_000
    expected = (
        300_000 * OPUS45_INPUT_RATE
        + 500_000 * OPUS45_CACHE_READ_RATE
        + 200_000 * OPUS45_CACHE_WRITE_RATE
        + 100_000 * OPUS45_OUTPUT_RATE
    ) / 1_000_000.0
    assert compute_inner_state_cost(usage) == pytest.approx(expected)


# ── build_inner_state_injection ──────────────────────────────────────


def test_injection_empty_for_no_payload():
    """No _render_payload → empty string."""
    assert build_inner_state_injection({}) == ""


def test_injection_empty_for_empty_inner_state():
    """_render_payload.inner_state == {} → empty string."""
    state = {"_render_payload": {"inner_state": {}}}
    assert build_inner_state_injection(state) == ""


def test_injection_renders_only_populated_fields():
    """Only fields with non-empty strings appear in the rendered block."""
    state = {
        "_render_payload": {
            "inner_state": {
                "feeling": "warm but cautious",
                "wanting": "",
                "noticing": "he replied fast for once",
                "holding_back": "  ",
            }
        }
    }
    block = build_inner_state_injection(state)
    assert block.startswith("[INNER STATE")
    assert "weave into voice" in block
    assert "- feeling: warm but cautious" in block
    assert "- noticing: he replied fast for once" in block
    assert "wanting" not in block
    assert "holding back" not in block


def test_injection_field_order_stable():
    """Fields render in the canonical order: feeling, wanting, noticing, holding_back."""
    state = {
        "_render_payload": {
            "inner_state": {
                # supply in non-canonical order
                "holding_back": "the resentment",
                "noticing": "tone shift",
                "wanting": "to be heard",
                "feeling": "frayed",
            }
        }
    }
    block = build_inner_state_injection(state)
    # Verify each label appears in canonical order
    f_idx = block.index("- feeling:")
    w_idx = block.index("- wanting:")
    n_idx = block.index("- noticing:")
    h_idx = block.index("- holding back:")
    assert f_idx < w_idx < n_idx < h_idx


# ── Regression guard: builder is wired into the assembly ─────────────


def test_inner_state_builder_in_injection_assembly():
    """build_characters_injections must include build_inner_state_injection
    in its builder tuple — otherwise the inner-state pass output never
    reaches Opus's user message and the whole feature silently no-ops."""
    state = {
        "_render_payload": {
            "inner_state": {"feeling": "TEST_INNER_STATE_MARKER_xyz"},
            "memories_core": [],
            "memories_recalled": [],
            "profile_core": [],
            "profile_recalled": [],
            "growth_active": [],
            "growth_obsolete": [],
        },
        "wellbeing": {"state": "Even"},
        "callbacks": {"next_id": 1, "open": [], "resolved": [], "dismissed": []},
    }
    full_block = build_characters_injections(state)
    assert "TEST_INNER_STATE_MARKER_xyz" in full_block
    assert "[INNER STATE" in full_block


# ── Tool schema sanity ───────────────────────────────────────────────


def test_tool_schema_has_all_four_fields():
    """report_inner_state tool exposes exactly the four expected optional fields."""
    tool = build_inner_state_tool()
    assert tool["name"] == "report_inner_state"
    props = tool["input_schema"]["properties"]
    assert set(props.keys()) == {"feeling", "wanting", "noticing", "holding_back"}
    # All optional — schema must not have a 'required' key (or it must be empty)
    required = tool["input_schema"].get("required", [])
    assert required == [] or "required" not in tool["input_schema"]


def test_model_constant_is_sonnet_dashed():
    """Model constant must be Anthropic's dashed form, not the dot form (which
    is Chorus-internal). Anthropic API rejects 'claude-sonnet-4.6'."""
    assert INNER_STATE_MODEL == "claude-opus-4-5"


# ── Helper coverage ──────────────────────────────────────────────────


def test_summarize_state_dedupes_core_and_recalled_memories():
    """A memory present in both core and recalled lists should appear once."""
    state = {
        "_render_payload": {
            "memories_core": [
                {"id": 1, "impact": 5, "date": "2026-04-01", "text": "shared memory"},
                {"id": 2, "impact": 3, "date": "2026-04-02", "text": "core only"},
            ],
            "memories_recalled": [
                {"id": 1, "impact": 5, "date": "2026-04-01", "text": "shared memory"},
                {"id": 3, "impact": 4, "date": "2026-04-03", "text": "recalled only"},
            ],
        },
        "wellbeing": {"state": "Even"},
    }
    out = _summarize_state_for_inner(state)
    # All three distinct memories should appear
    assert "core only" in out
    assert "recalled only" in out
    # The shared memory should appear exactly ONCE despite being in both lists
    assert out.count("shared memory") == 1


def test_summarize_state_handles_empty_state():
    """No render payload, no wellbeing/arc/callbacks → returns wellbeing default
    without crashing."""
    out = _summarize_state_for_inner({})
    # _summarize_state_for_inner appends a [WELLBEING ...] line by default
    # (default state "Even"), so output is non-empty even with no other fields.
    assert "[WELLBEING" in out
    assert "Even" in out


def test_format_recent_dialogue_string_content():
    """Plain-string content blocks render as 'role: text'."""
    dialogue = [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "hi"},
    ]
    out = _format_recent_dialogue(dialogue)
    assert "user: hey" in out
    assert "assistant: hi" in out


def test_format_recent_dialogue_anthropic_content_blocks():
    """Anthropic content-block-list format (used for image messages) extracts text blocks."""
    dialogue = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what do you think of this?"},
                {"type": "image", "source": {"type": "base64", "data": "..."}},
            ],
        },
    ]
    out = _format_recent_dialogue(dialogue)
    assert "what do you think of this?" in out
    # Image block has no 'text' field — should not crash, should not bleed through
    assert "base64" not in out


def test_format_recent_dialogue_empty_returns_marker():
    """Empty dialogue list returns the no-prior-dialogue marker."""
    assert _format_recent_dialogue([]) == "(no prior dialogue)"
    assert _format_recent_dialogue(None) == "(no prior dialogue)"


# ── build_prior_inner_states_injection ──────────────────────────────


def test_prior_states_empty_for_no_payload():
    assert build_prior_inner_states_injection({}) == ""


def test_prior_states_empty_for_empty_list():
    state = {"_render_payload": {"prior_inner_states": []}}
    assert build_prior_inner_states_injection(state) == ""


def test_prior_states_renders_with_turn_labels():
    """Last-turn / N-turns-ago labels render correctly, oldest-first."""
    state = {
        "_render_payload": {
            "prior_inner_states": [
                {"turns_ago": 3, "payload": {"feeling": "stung that he forgot"}},
                {"turns_ago": 2, "payload": {"feeling": "warming", "noticing": "he's softening"}},
                {"turns_ago": 1, "payload": {"feeling": "relieved"}},
            ]
        }
    }
    block = build_prior_inner_states_injection(state)
    assert block.startswith("[PRIOR INNER STATES")
    # Turn labels
    assert "3 turns ago" in block
    assert "2 turns ago" in block
    assert "last turn" in block
    # Field content
    assert "stung that he forgot" in block
    assert "warming" in block
    assert "relieved" in block
    # Order preserved (oldest first → 3 ago appears before last turn)
    assert block.index("3 turns ago") < block.index("2 turns ago") < block.index("last turn")


def test_prior_states_skips_empty_payloads():
    """Entries with empty payload dicts are skipped, not rendered as blank lines."""
    state = {
        "_render_payload": {
            "prior_inner_states": [
                {"turns_ago": 3, "payload": {}},
                {"turns_ago": 2, "payload": {"feeling": "real"}},
                {"turns_ago": 1, "payload": {"feeling": "  "}},  # whitespace-only filtered
            ]
        }
    }
    block = build_prior_inner_states_injection(state)
    # Only the turn-2 entry has substantive content
    assert "2 turns ago" in block
    assert "real" in block
    # The empty-payload entries should not appear as bullet lines
    assert "3 turns ago" not in block
    assert "last turn" not in block


def test_prior_states_in_injection_assembly():
    """Regression guard: build_prior_inner_states_injection is in the assembly list."""
    state = {
        "_render_payload": {
            "inner_state": {},
            "memories_core": [], "memories_recalled": [],
            "profile_core": [], "profile_recalled": [],
            "growth_active": [], "growth_obsolete": [],
            "prior_inner_states": [
                {"turns_ago": 1, "payload": {"feeling": "TEST_PRIOR_MARKER_abc"}},
            ],
        },
        "wellbeing": {"state": "Even"},
        "callbacks": {"next_id": 1, "open": [], "resolved": [], "dismissed": []},
    }
    full_block = build_characters_injections(state)
    assert "TEST_PRIOR_MARKER_abc" in full_block
    assert "[PRIOR INNER STATES" in full_block
    # Assembly order: PRIOR INNER STATES appears BEFORE current INNER STATE
    # (we don't have a current inner state in this fixture, so just verify
    # the prior block exists)


def test_prior_states_renders_timestamp_when_available():
    """Absolute timestamp on each entry renders alongside the relative label."""
    state = {
        "_render_payload": {
            "prior_inner_states": [
                {
                    "turns_ago": 1,
                    "timestamp": "2026-05-09T15:42:00",
                    "payload": {"feeling": "warming"},
                },
            ]
        }
    }
    block = build_prior_inner_states_injection(state)
    assert "last turn" in block
    # Full weekday name + date + time, matching [NOW] format
    assert "Saturday" in block  # 2026-05-09 is a Saturday
    assert "2026-05-09" in block
    assert "3:42 PM" in block


def test_prior_states_works_without_timestamp():
    """Entries without timestamp still render (just the relative label)."""
    state = {
        "_render_payload": {
            "prior_inner_states": [
                {"turns_ago": 1, "payload": {"feeling": "calm"}},  # no timestamp
            ]
        }
    }
    block = build_prior_inner_states_injection(state)
    assert "last turn" in block
    assert "calm" in block
    # No timestamp parens should appear when timestamp is missing
    assert "(" not in block.split("\n")[1]  # the entry line


def test_prior_states_handles_invalid_timestamp_gracefully():
    """Unparseable timestamp does not crash; entry renders without ts."""
    state = {
        "_render_payload": {
            "prior_inner_states": [
                {
                    "turns_ago": 2,
                    "timestamp": "not a real timestamp",
                    "payload": {"feeling": "ok"},
                },
            ]
        }
    }
    block = build_prior_inner_states_injection(state)
    assert "2 turns ago" in block
    assert "ok" in block
    # No malformed timestamp leaks through
    assert "not a real timestamp" not in block


def test_prior_states_assembly_order_prior_before_current():
    """[PRIOR INNER STATES] must render BEFORE [INNER STATE] (current turn)."""
    state = {
        "_render_payload": {
            "inner_state": {"feeling": "CURRENT_TURN_MARKER"},
            "memories_core": [], "memories_recalled": [],
            "profile_core": [], "profile_recalled": [],
            "growth_active": [], "growth_obsolete": [],
            "prior_inner_states": [
                {"turns_ago": 1, "payload": {"feeling": "PRIOR_TURN_MARKER"}},
            ],
        },
        "wellbeing": {"state": "Even"},
        "callbacks": {"next_id": 1, "open": [], "resolved": [], "dismissed": []},
    }
    full_block = build_characters_injections(state)
    assert "PRIOR_TURN_MARKER" in full_block
    assert "CURRENT_TURN_MARKER" in full_block
    # Prior states (older) render before current inner state (now)
    assert full_block.index("PRIOR_TURN_MARKER") < full_block.index("CURRENT_TURN_MARKER")


def test_format_recent_dialogue_skips_invalid_entries():
    """Non-dict entries and entries with empty content are skipped."""
    dialogue = [
        {"role": "user", "content": "real message"},
        "not a dict",
        {"role": "assistant", "content": ""},
        None,
        {"role": "user", "content": "another real one"},
    ]
    out = _format_recent_dialogue(dialogue)
    assert "real message" in out
    assert "another real one" in out
    # Only the two real messages render
    assert out.count("user:") == 2
