"""Plot-doc beat tracking for campaigns.

The user's plot docs are structured as:
  Plot - Session 1.md
  Plot - Session 2.md
  Plot - Session 3.md

Each contains `### Beat N: Title` sections with prose describing what the
beat is, what its mechanical/narrative goals are, and what should happen.

This module:
  1. Parses beat sections from each plot doc
  2. Provides a `[CURRENT BEAT]` injection block for the system prompt
  3. Supports advancement via slash command (`/beat <N>`, `/beat next`,
     `/session <N>`) and planner signal (`beat_complete: true`)

State shape (lives on pipeline_state.beat_state):
  {
    "current_session": 1,         # which Plot - Session N.md is active
    "current_beat": 2,            # 1-indexed beat number
    "completed_beats": [1],       # beats finished this session
    "session_completed": [],      # sessions whose final beat was played
  }

Sex / hack / combat / net_combat modes do NOT advance beats — when those
modes open, beat_state is snapshotted; when they close, it's restored.
The model never sees its own beat-advancement powers inside those modes.
"""

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)


_PLOT_FILENAME_RE = re.compile(r"^plot\s*-\s*session\s*(\d+)\.md$", re.IGNORECASE)
_BEAT_HEADER_RE = re.compile(r"^###\s+Beat\s+(\d+)\s*:\s*(.+)$", re.MULTILINE)


def parse_plot_doc(text: str) -> list[dict]:
    """Parse `### Beat N: Title` sections out of a plot-doc markdown string.

    Returns a list of {number, title, body} ordered by appearance.  The
    body is the markdown content from after the header to the next beat
    header (or end of doc).
    """
    if not text:
        return []
    matches = list(_BEAT_HEADER_RE.finditer(text))
    beats = []
    for i, m in enumerate(matches):
        number = int(m.group(1))
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        beats.append({"number": number, "title": title, "body": body})
    return beats


def load_session_beats(uploads_dir: str, session_num: int) -> list[dict]:
    """Load beats from the `Plot - Session N.md` file in uploads_dir.

    Returns [] if the file is missing or has no parseable beat headers.
    File matching is case-insensitive and tolerates surrounding whitespace.
    """
    if not uploads_dir or not os.path.isdir(uploads_dir):
        return []
    target_path = None
    try:
        for fn in os.listdir(uploads_dir):
            m = _PLOT_FILENAME_RE.match(fn.strip())
            if m and int(m.group(1)) == int(session_num):
                target_path = os.path.join(uploads_dir, fn)
                break
    except OSError:
        return []
    if not target_path or not os.path.isfile(target_path):
        return []
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        logger.warning("plot_beats: failed to read %s: %s", target_path, e)
        return []
    return parse_plot_doc(text)


def init_beat_state() -> dict:
    """Default beat_state for a fresh chat.  Session 1, Beat 1, nothing
    completed yet."""
    return {
        "current_session": 1,
        "current_beat": 1,
        "completed_beats": [],
        "session_completed": [],
    }


def normalize_beat_state(bs: Optional[dict]) -> dict:
    """Coerce a possibly-missing-or-malformed beat_state into a valid
    shape with sensible defaults.  Idempotent."""
    out = init_beat_state()
    if isinstance(bs, dict):
        try:
            out["current_session"] = max(1, int(bs.get("current_session", 1) or 1))
        except (TypeError, ValueError):
            pass
        try:
            out["current_beat"] = max(1, int(bs.get("current_beat", 1) or 1))
        except (TypeError, ValueError):
            pass
        cb = bs.get("completed_beats")
        if isinstance(cb, list):
            out["completed_beats"] = [
                int(x) for x in cb if isinstance(x, (int, str)) and str(x).isdigit()
            ]
        sc = bs.get("session_completed")
        if isinstance(sc, list):
            out["session_completed"] = [
                int(x) for x in sc if isinstance(x, (int, str)) and str(x).isdigit()
            ]
    return out


def advance_beat(bs: dict, uploads_dir: Optional[str] = None) -> dict:
    """Mark the current beat complete and advance to the next one.

    If the current beat is the last in the session AND uploads_dir is
    provided so we can detect the session's final beat, the session
    rolls over to the next one (current_beat resets to 1).
    """
    bs = normalize_beat_state(bs)
    cur = bs["current_beat"]
    if cur not in bs["completed_beats"]:
        bs["completed_beats"].append(cur)
        bs["completed_beats"].sort()
    # Determine if we're at the end of the session.
    last_beat_in_session = None
    if uploads_dir:
        beats = load_session_beats(uploads_dir, bs["current_session"])
        if beats:
            last_beat_in_session = max(b["number"] for b in beats)
    if last_beat_in_session is not None and cur >= last_beat_in_session:
        # Roll over.
        if bs["current_session"] not in bs["session_completed"]:
            bs["session_completed"].append(bs["current_session"])
        bs["current_session"] += 1
        bs["current_beat"] = 1
        bs["completed_beats"] = []
    else:
        bs["current_beat"] = cur + 1
    return bs


def set_beat(bs: dict, beat_num: int) -> dict:
    """Force-set current_beat to a specific number.  Marks all earlier
    beats in the session as completed (since you're past them)."""
    bs = normalize_beat_state(bs)
    n = max(1, int(beat_num))
    bs["current_beat"] = n
    bs["completed_beats"] = sorted(set(
        list(bs["completed_beats"]) + [i for i in range(1, n)]
    ))
    return bs


def set_session(bs: dict, session_num: int) -> dict:
    """Force-set the active session.  Resets current_beat to 1 and
    clears completed_beats for the new session."""
    bs = normalize_beat_state(bs)
    n = max(1, int(session_num))
    if bs["current_session"] != n:
        # Mark the prior session done if we're moving forward.
        if (n > bs["current_session"]
                and bs["current_session"] not in bs["session_completed"]):
            bs["session_completed"].append(bs["current_session"])
        bs["current_session"] = n
        bs["current_beat"] = 1
        bs["completed_beats"] = []
    return bs


# Body-truncation budget for the [CURRENT BEAT] injection.  The full beat
# body is sometimes 2000+ chars (Beat 6 of Session 2 is one of the longest).
# We send the full body — saving a few hundred chars by truncation isn't
# worth losing pacing-relevant detail.  Bumped from "preview" to "full" so
# the model has the entire beat's mechanical specs in front of it.
_BEAT_BODY_MAX_CHARS = 6000


def build_current_beat_injection(bs: dict, uploads_dir: str) -> str:
    """Render the `[CURRENT BEAT]` block for the system prompt.  Returns
    "" if no plot beats are parseable for the active session.
    """
    bs = normalize_beat_state(bs)
    if not uploads_dir:
        return ""
    beats = load_session_beats(uploads_dir, bs["current_session"])
    if not beats:
        return ""
    by_num = {b["number"]: b for b in beats}
    cur = by_num.get(bs["current_beat"])
    if not cur:
        # Beat number out of range for this session — surface the issue
        # but don't crash.
        return (
            f"[CURRENT BEAT]\n"
            f"Session {bs['current_session']}, Beat {bs['current_beat']} "
            f"(no matching `### Beat {bs['current_beat']}:` header in "
            f"`Plot - Session {bs['current_session']}.md`).\n"
            f"Available beats this session: "
            f"{', '.join(str(b['number']) for b in beats)}.\n"
        )
    body = cur["body"]
    if len(body) > _BEAT_BODY_MAX_CHARS:
        body = body[:_BEAT_BODY_MAX_CHARS].rstrip() + "\n\n[... beat body truncated ...]"
    completed = bs["completed_beats"]
    next_beat = by_num.get(bs["current_beat"] + 1)
    next_line = (
        f"Next: Beat {next_beat['number']} — {next_beat['title']}"
        if next_beat else
        "Next: end of this session's plot doc"
    )
    completed_line = (
        f"Completed this session: {', '.join(str(n) for n in completed)}"
        if completed else "Completed this session: none yet"
    )
    return (
        "[CURRENT BEAT]\n"
        f"Session {bs['current_session']}, Beat {cur['number']}: {cur['title']}\n"
        f"{completed_line}. {next_line}.\n"
        "\n"
        "PACING RULE: Stay on this beat until its goals are demonstrably met. "
        "Do NOT compress multiple beats into one exchange. Do NOT skip ahead — "
        "if the player tries to push past Beat content, redirect or hold the "
        "scene until the beat plays out. Beats only advance via the player's "
        "`/beat next` or `/beat <N>` slash command, OR by the planner emitting "
        "`beat_complete: true` when the beat's listed objectives have actually "
        "occurred in the fiction. Sex / hack / combat / net_combat modes do "
        "NOT advance beats — they're sealed chambers.\n"
        "\n"
        "--- BEAT BODY ---\n"
        f"{body}\n"
        "--- END BEAT BODY ---"
    )
