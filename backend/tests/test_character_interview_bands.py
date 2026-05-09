"""Tests for the Phase 4 flakiness-bands extraction in finalize_profile and
the commit-flakiness-bands endpoint validation logic.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from character_interview import _extract_flakiness_bands  # noqa: E402


PROFILE_BASE = "# Test Character\n\nA fictional person.\n\n## Voice\n- Whatever\n"


def _wrap_with_proposal(profile: str, json_blob: str) -> str:
    return profile + "\n\n<<<FLAKINESS_BANDS_PROPOSAL>>>\n" + json_blob + "\n<<<END_PROPOSAL>>>\n"


VALID_PROPOSAL_JSON = '''{
  "work":      {"as_planned": 0.99, "modified": 0.0,  "cancelled": 0.01},
  "shae":      {"as_planned": 0.95, "modified": 0.04, "cancelled": 0.01},
  "family":    {"as_planned": 0.98, "modified": 0.02, "cancelled": 0.0},
  "social":    {"as_planned": 0.80, "modified": 0.15, "cancelled": 0.05},
  "self_care": {"as_planned": 0.50, "modified": 0.20, "cancelled": 0.30},
  "admin":     {"as_planned": 0.85, "modified": 0.13, "cancelled": 0.02}
}'''


def test_extract_valid_proposal():
    raw = _wrap_with_proposal(PROFILE_BASE, VALID_PROPOSAL_JSON)
    cleaned, proposal = _extract_flakiness_bands(raw)
    assert proposal is not None
    assert proposal["work"]["as_planned"] == 0.99
    assert proposal["self_care"]["cancelled"] == 0.30
    # Markers stripped from cleaned output
    assert "<<<FLAKINESS_BANDS_PROPOSAL>>>" not in cleaned
    assert "<<<END_PROPOSAL>>>" not in cleaned
    # Profile content preserved
    assert "# Test Character" in cleaned
    assert "## Voice" in cleaned


def test_extract_no_marker_returns_none():
    cleaned, proposal = _extract_flakiness_bands(PROFILE_BASE)
    assert proposal is None
    assert cleaned == PROFILE_BASE


def test_extract_malformed_json_returns_none():
    raw = _wrap_with_proposal(PROFILE_BASE, "{this is not json")
    cleaned, proposal = _extract_flakiness_bands(raw)
    assert proposal is None
    # Markers still get stripped even on parse failure
    assert "<<<FLAKINESS_BANDS_PROPOSAL>>>" not in cleaned


def test_extract_missing_category_returns_none():
    bad_json = '{"work": {"as_planned": 1.0, "modified": 0.0, "cancelled": 0.0}}'  # only 1 of 6 keys
    raw = _wrap_with_proposal(PROFILE_BASE, bad_json)
    _, proposal = _extract_flakiness_bands(raw)
    assert proposal is None


def test_extract_bucket_out_of_range_returns_none():
    bad_json = VALID_PROPOSAL_JSON.replace("0.99", "1.5")  # work.as_planned > 1.0
    raw = _wrap_with_proposal(PROFILE_BASE, bad_json)
    _, proposal = _extract_flakiness_bands(raw)
    assert proposal is None


def test_extract_row_does_not_sum_to_one_returns_none():
    bad_json = '''{
      "work":      {"as_planned": 0.50, "modified": 0.20, "cancelled": 0.10},
      "shae":      {"as_planned": 0.95, "modified": 0.04, "cancelled": 0.01},
      "family":    {"as_planned": 0.98, "modified": 0.02, "cancelled": 0.0},
      "social":    {"as_planned": 0.80, "modified": 0.15, "cancelled": 0.05},
      "self_care": {"as_planned": 0.50, "modified": 0.20, "cancelled": 0.30},
      "admin":     {"as_planned": 0.85, "modified": 0.13, "cancelled": 0.02}
    }'''  # work sums to 0.80
    raw = _wrap_with_proposal(PROFILE_BASE, bad_json)
    _, proposal = _extract_flakiness_bands(raw)
    assert proposal is None


def test_extract_tolerance_for_floating_point():
    """Rows summing to 1.0 ± 0.01 are accepted (floating-point tolerance)."""
    bad_json = '''{
      "work":      {"as_planned": 0.99, "modified": 0.005,"cancelled": 0.005},
      "shae":      {"as_planned": 0.95, "modified": 0.04, "cancelled": 0.01},
      "family":    {"as_planned": 0.98, "modified": 0.02, "cancelled": 0.0},
      "social":    {"as_planned": 0.80, "modified": 0.15, "cancelled": 0.05},
      "self_care": {"as_planned": 0.50, "modified": 0.20, "cancelled": 0.30},
      "admin":     {"as_planned": 0.85, "modified": 0.13, "cancelled": 0.02}
    }'''
    raw = _wrap_with_proposal(PROFILE_BASE, bad_json)
    _, proposal = _extract_flakiness_bands(raw)
    assert proposal is not None


def test_extract_strips_trailing_whitespace_from_profile():
    raw = _wrap_with_proposal(PROFILE_BASE, VALID_PROPOSAL_JSON) + "\n\n\n"
    cleaned, _ = _extract_flakiness_bands(raw)
    assert cleaned == cleaned.rstrip() + ""  # no trailing whitespace


# ── commit endpoint validation logic ─────────────────────────────────
# The endpoint itself requires FastAPI; instead we directly exercise the
# validation rules to keep tests fast.

def _validate_bands(bands: dict) -> tuple[bool, str]:
    """Mirror the endpoint's validation. Returns (ok, error_msg)."""
    if not isinstance(bands, dict):
        return False, "must be object"
    REQ_KEYS = ("work", "shae", "family", "social", "self_care", "admin")
    REQ_BUCKETS = ("as_planned", "modified", "cancelled")
    for key in REQ_KEYS:
        bucket = bands.get(key)
        if not isinstance(bucket, dict):
            return False, f"missing/invalid: {key}"
        total = 0.0
        for b in REQ_BUCKETS:
            v = bucket.get(b)
            if not isinstance(v, (int, float)):
                return False, f"{key}.{b} not numeric"
            v = float(v)
            if not (0.0 <= v <= 1.0):
                return False, f"{key}.{b} out of range"
            total += v
        if abs(total - 1.0) > 0.01:
            return False, f"{key} sums to {total}"
    return True, ""


def test_validation_accepts_canonical_zara_bands():
    import json as _json
    bands = _json.loads(VALID_PROPOSAL_JSON)
    ok, err = _validate_bands(bands)
    assert ok, err


def test_validation_rejects_extra_garbage_keys_does_not_crash():
    """Extra keys beyond the 6 expected are silently ignored — only the 6
    are validated. This is intentional: the endpoint cleans to just the
    expected set when persisting."""
    import json as _json
    bands = _json.loads(VALID_PROPOSAL_JSON)
    bands["bogus"] = {"as_planned": "not a number", "modified": -5, "cancelled": 99}
    ok, err = _validate_bands(bands)
    assert ok, f"extra key shouldn't fail validation: {err}"
