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
    "beat_responses": 0,          # in-character turns since this beat began
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


# Filename forms we accept (all case-insensitive, .md):
#   Plot - Session 1.md            (single session, dash separator)
#   Plot - Episode 5.md            (Session/Episode are interchangeable)
#   Plot - Episodes 1-3.md         (range)
#   Plot and Sessions S1-S2.md     ("and" separator, S-prefixed numbers)
#   Broken Orbit - Plot and Sessions 6-7.md   (project-name prefix)
# Captures the session-spec (e.g. "1", "1-3", "S1-S2", "6-7") in group "spec".
_PLOT_FILENAME_RE = re.compile(
    r"^(?:.*?\s+-\s+)?"           # optional "Project Name - " prefix
    r"plot"
    r"(?:\s*[-—]\s*|\s+and\s+)"   # " - " | " — " | " and "
    r"(?:sessions?|episodes?)"
    r"\s+(?P<spec>\S+)\.md$",
    re.IGNORECASE,
)

# Beat header inside a plot doc. Accepts:
#   ### Beat 1: The Job              (H3, colon — Dead Air style)
#   ## Beat 1 — Arrival [Social]      (H2, em-dash, bracketed tag — Severing style)
#   ## Beat 2 – Investigation         (en-dash also accepted)
# Trailing bracketed tags are tolerated and stripped from the captured title.
_BEAT_HEADER_RE = re.compile(
    r'^#{2,3}\s+Beat\s+(\d+)(?:\s*:\s*|\s+[—–]\s+)([^\n\[]+?)\s*(?:\[[^\]]*\])?\s*$',
    re.MULTILINE | re.IGNORECASE,
)

# Session/Episode-block header inside a plot doc. Accepts:
#   ## Session 1: "The Pull"                              (H2, colon, quoted)
#   ## Session 2: Ghost Frequency                          (H2, colon, unquoted)
#   ## Session 6: "The Walls Have Eyes" [FULL DETAIL]      (bracketed tag)
#   # Episode 1 — Convergence                              (H1, em-dash — Severing style)
#   ## Episode 5 – Title                                   (en-dash also accepted)
# Captures keyword ("Session" or "Episode") in group(1), number in
# group(2), title in group(3). Trailing bracketed tags are tolerated
# and stripped.
_SESSION_HEADER_RE = re.compile(
    r'^#{1,2}\s+(Session|Episode)\s+(\d+)'
    r'(?:\s*:\s*|\s+[—–]\s+)'
    r'"?([^"\n]+?)"?\s*(?:\[[^\]]*\])?\s*$',
    re.MULTILINE | re.IGNORECASE,
)


def _parse_session_spec(spec: str) -> set:
    """Parse the session-number portion of a plot-doc filename.

    Examples:
        "1"     -> {1}
        "6-7"   -> {6, 7}
        "1-3"   -> {1, 2, 3}
        "S1"    -> {1}
        "S1-S2" -> {1, 2}

    Returns an empty set on unparseable input.
    """
    if not spec:
        return set()
    parts = re.split(r"\s*-\s*", spec.strip().strip('"').strip("'"))
    nums = []
    for p in parts:
        m = re.match(r"^[Ss]?(\d+)$", p.strip())
        if not m:
            return set()
        nums.append(int(m.group(1)))
    if not nums:
        return set()
    if len(nums) == 1:
        return {nums[0]}
    if len(nums) == 2:
        a, b = sorted(nums)
        return set(range(a, b + 1))
    return set(nums)


def _iter_plot_doc_files(uploads_dir: str):
    """Yield (path, sessions_covered) for each plot-doc file in uploads_dir.

    `sessions_covered` is a set of session numbers parsed out of the
    filename — `{1}` for `Plot - Session 1.md`, `{6, 7}` for
    `Broken Orbit - Plot and Sessions 6-7.md`, etc.
    """
    if not uploads_dir or not os.path.isdir(uploads_dir):
        return
    try:
        names = os.listdir(uploads_dir)
    except OSError:
        return
    for fn in names:
        m = _PLOT_FILENAME_RE.match(fn.strip())
        if not m:
            continue
        sessions = _parse_session_spec(m.group("spec"))
        if not sessions:
            continue
        yield (os.path.join(uploads_dir, fn), sessions)


def _extract_session_block(text: str, session_num: int) -> Optional[tuple]:
    """Slice (keyword, title, body) for the requested session out of a plot
    doc. `keyword` is whichever wording the doc uses — `"Session"` or
    `"Episode"` — so callers can echo the doc's own terminology back into
    pacing strings.

    For files containing a single session this returns the whole
    post-header body; for multi-session files (Broken Orbit's 6-7,
    Severing's 1-3) it returns just the prose between the matching
    `## Session N:` / `# Episode N —` header and the next session-level
    header (or end of file).

    Returns None if the file has no header for that session number.
    """
    if not text:
        return None
    matches = list(_SESSION_HEADER_RE.finditer(text))
    if not matches:
        return None
    for i, m in enumerate(matches):
        try:
            n = int(m.group(2))
        except (TypeError, ValueError):
            continue
        if n != int(session_num):
            continue
        keyword = m.group(1).strip().capitalize()  # "Session" or "Episode"
        title = m.group(3).strip().strip('"').strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        return (keyword, title or None, body)
    return None


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


def parse_session_title(text: str, session_num: Optional[int] = None) -> Optional[str]:
    """Extract the session title from a `## Session N:` / `# Episode N —`
    header. If `session_num` is provided, returns the title for THAT
    session (multi-session files like `Plot and Sessions 6-7.md` have
    multiple session headers). If omitted, returns the first session
    title found in the doc.
    """
    if not text:
        return None
    if session_num is None:
        m = _SESSION_HEADER_RE.search(text)
        if not m:
            return None
        return (m.group(3).strip().strip('"').strip()) or None
    block = _extract_session_block(text, session_num)
    return block[1] if block else None


def load_session_title(uploads_dir: str, session_num: int) -> Optional[str]:
    """Return the title for `## Session <session_num>:` from whichever plot
    doc file in uploads_dir covers that session number — including
    multi-session files (e.g. `Plot and Sessions 6-7.md` covers 6 and 7).
    """
    for path, sessions in _iter_plot_doc_files(uploads_dir):
        if int(session_num) not in sessions:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            logger.warning("plot_beats: failed to read %s: %s", path, e)
            continue
        title = parse_session_title(text, session_num=session_num)
        if title:
            return title
    return None


def derive_canonical_pacing(bs: dict, uploads_dir: str) -> Optional[dict]:
    """Return `{episode, beat}` strings sourced deterministically from the
    plot doc for the active session/beat, or None if no plot doc exists.

    The `episode` field echoes the doc's own wording — `Session N: <Title>`
    for Dead Air / Broken Orbit (which use `## Session N:`), `Episode N:
    <Title>` for The Severing (which uses `# Episode N —`). The `beat`
    field is `Beat N: <Title>` from the matching beat header. When the
    model emits its own pacing, the pipeline overrides `episode` and
    `beat` with these — the model can't drift them anymore.
    """
    bs = normalize_beat_state(bs)
    if not uploads_dir or not os.path.isdir(uploads_dir):
        return None
    n = bs["current_session"]
    keyword = "Session"
    title = None
    body = ""
    for path, sessions in _iter_plot_doc_files(uploads_dir):
        if int(n) not in sessions:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            logger.warning("plot_beats: failed to read %s: %s", path, e)
            continue
        block = _extract_session_block(text, n)
        if block:
            keyword, title, body = block
            break
    if title is None:
        return None
    out: dict = {"episode": f"{keyword} {n}: {title}"}
    beats = parse_plot_doc(body) if body else []
    match = next((b for b in beats if b["number"] == bs["current_beat"]), None)
    if match:
        out["beat"] = f"Beat {match['number']}: {match['title']}"
    return out


def load_session_beats(uploads_dir: str, session_num: int) -> list[dict]:
    """Load beat sections (`### Beat N:` or `## Beat N — ...`) for the
    given session number from whichever plot-doc file in uploads_dir
    covers it. Filename matching is case-insensitive and tolerates a
    project-name prefix and session ranges (`Plot - Session 1.md`,
    `Plot - Episodes 1-3.md`, `Broken Orbit - Plot and Sessions 6-7.md`).

    If the file has session-level headers (`## Session N:` /
    `# Episode N —`), beats are sliced out of the matching session block
    so beats from sibling sessions don't leak. Files without any
    session-level headers fall back to scanning the whole text. Returns
    [] when no matching file or no parseable beats.
    """
    for path, sessions in _iter_plot_doc_files(uploads_dir):
        if int(session_num) not in sessions:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            logger.warning("plot_beats: failed to read %s: %s", path, e)
            continue
        block = _extract_session_block(text, int(session_num))
        if block:
            return parse_plot_doc(block[2])
        # No session-level headers in this file — fall back to whole-text
        # parse (preserves backward compat for files that just dump beats
        # without a `## Session N:` wrapper).
        if not _SESSION_HEADER_RE.search(text):
            return parse_plot_doc(text)
    return []


def init_beat_state() -> dict:
    """Default beat_state for a fresh chat.  Session 1, Beat 1, nothing
    completed yet."""
    return {
        "current_session": 1,
        "current_beat": 1,
        "completed_beats": [],
        "session_completed": [],
        # Backend-tracked count of in-character turns since the current
        # beat began. Resets to 0 on advance_beat / set_beat / set_session.
        # The pipeline mirrors this into pacing.responses every turn so
        # the sidebar shows a true per-beat counter (the model's previous
        # `pacing.responses` emit drifted because it was counting "total
        # responses since session start" inconsistently).
        "beat_responses": 0,
        # Session-cumulative counter for beat-progressing turns. Bumps
        # alongside beat_responses but only resets on session rollover
        # (not on advance_beat within a session). Lets the sidebar show
        # "Responses: X (Y total)" where X is beat-scoped and Y is
        # session-scoped.
        "session_responses": 0,
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
        try:
            out["beat_responses"] = max(0, int(bs.get("beat_responses", 0) or 0))
        except (TypeError, ValueError):
            pass
        try:
            out["session_responses"] = max(0, int(bs.get("session_responses", 0) or 0))
        except (TypeError, ValueError):
            pass
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
        bs["session_responses"] = 0  # New session → reset session-cumulative counter
    else:
        bs["current_beat"] = cur + 1
    bs["beat_responses"] = 0  # New beat → reset per-beat turn counter
    return bs


def set_beat(bs: dict, beat_num: int) -> dict:
    """Force-set current_beat to a specific number.  Marks all earlier
    beats in the session as completed (since you're past them)."""
    bs = normalize_beat_state(bs)
    n = max(1, int(beat_num))
    if bs["current_beat"] != n:
        bs["beat_responses"] = 0
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
        bs["beat_responses"] = 0
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
        "TEXTURE RULE: The beat body below contains both required mechanics "
        "AND layered offerings (optional NPC interactions, ambient details, "
        "dimensional character moments, alternative clue paths, time-pressure "
        "phases). Treat the layered offerings with the SAME narrative weight "
        "as the mechanical beats — they are the campaign's grain, not filler. "
        "When the body lists multiple ways an NPC can be approached, surface "
        "them as available choices to the player rather than collapsing to one "
        "default path. When the body lists a small character beat (a phone "
        "call taken aside, a vendor who only opens up to specific approaches, "
        "an older woman at a shrine), give that beat its own turn — do NOT "
        "compress it into an adverb or skip it silently. If the player passes "
        "on an offering, narrate it once visibly so they had the chance, then "
        "let it go. But never resolve a layered scene in a single mechanical "
        "exchange.\n"
        "\n"
        "--- BEAT BODY ---\n"
        f"{body}\n"
        "--- END BEAT BODY ---"
    )
