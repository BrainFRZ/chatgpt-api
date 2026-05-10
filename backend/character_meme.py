"""Meme tools for the Characters voice agent.

Two tools the character can call mid-stream when sending a meme would land harder
than words:

- `make_meme`         — Imgflip. Model picks a known template by name and writes
                        custom captions in their own voice. We map name → template_id
                        and POST to Imgflip's /caption_image. Returns a generated URL.
                        No vision pick needed — the model wrote the caption.

- `find_meme_post`    — Reddit JSON. We search r/memes / r/me_irl / r/AdviceAnimals
                        (or a specific subreddit), pull image posts, and use a Sonnet
                        vision-pick pass to choose the one that actually fits the
                        moment. Returns the chosen post's image URL.

Both tools return a tool_result the model embeds as `![alt](url)` in its reply
text. The frontend's ReactMarkdown renderer handles the inline image already.

API keys (api_keys.json):
  - imgflip_username / imgflip_password : required for make_meme
  - anthropic                            : required for vision pick in find_meme_post
  - (Reddit JSON needs no auth, just a sane User-Agent.)

Restraint:
  Tool descriptions tell the model not to send a meme on heavy emotional moments
  or every turn. The model decides organically; the tool just executes.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from typing import Optional
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

# ── Common config ───────────────────────────────────────────────────

USER_AGENT = "web:chorus-characters:v1.0 (meme search; per-character)"
HTTP_TIMEOUT_S = 8

# Vision-capable Sonnet for the pick pass. Override via MEME_VISION_MODEL env var
# if Anthropic deprecates this name or you want to swap to a cheaper/faster tier
# (e.g. claude-haiku-4-5-20251001 for ~3x cost reduction).
VISION_MODEL = os.environ.get("MEME_VISION_MODEL", "claude-sonnet-4-6")
VISION_MAX_TOKENS = 256
VISION_CANDIDATE_CAP = 8             # max candidates shown to the vision picker

# ── Tool: make_meme (Imgflip) ───────────────────────────────────────

MAKE_MEME_TOOL = {
    "name": "make_meme",
    "description": (
        "Generate a captioned meme using a known template (Imgflip). Use this when "
        "you want to make a meme that fits the EXACT thing being talked about — "
        "you write the caption yourself in your own voice, the way you'd actually "
        "send it to your friend. This is for moments where a custom-captioned "
        "meme lands harder than a reaction GIF or a found meme. "
        "\n\n"
        "Use sparingly. Only when a meme would actually land — not on heavy "
        "emotional moments where words matter, not on every turn, not when the "
        "user just asked you something direct. ONE meme per turn maximum across "
        "all meme tools. "
        "\n\n"
        "Pass `template` as a popular meme template name (e.g., 'two buttons', "
        "'distracted boyfriend', 'is this a pigeon', 'drake hotline bling', "
        "'change my mind', 'expanding brain'). Pass `top_text` and/or `bottom_text` "
        "as the captions you want — write them in YOUR voice and humor, not "
        "generic. Empty string is fine for either if the template only needs one. "
        "After it generates, embed it in your reply as `![brief description](url)` "
        "where the moment lands. Don't separately announce that you're sending "
        "a meme — just include it like you'd drop one in a real text."
    ),
    "input_schema": {
        "type": "object",
        "required": ["template", "top_text", "bottom_text", "reason"],
        "properties": {
            "template": {
                "type": "string",
                "description": (
                    "Name of a popular meme template. Common options: 'two buttons', "
                    "'distracted boyfriend', 'drake hotline bling', 'change my mind', "
                    "'is this a pigeon', 'expanding brain', 'one does not simply', "
                    "'success kid', 'roll safe', 'woman yelling at cat', 'mocking "
                    "spongebob', 'this is fine', 'spider-man pointing', 'futurama "
                    "fry', 'panik kalm panik'. We fuzzy-match against Imgflip's "
                    "library."
                ),
            },
            "top_text": {
                "type": "string",
                "description": "Top caption. Empty string if the template doesn't use a top text. Write in the character's voice.",
            },
            "bottom_text": {
                "type": "string",
                "description": "Bottom caption. Empty string if the template doesn't use a bottom text. Write in the character's voice.",
            },
            "reason": {
                "type": "string",
                "description": "One short phrase shown to the user as a UI banner. Examples: 'making a meme', 'cooking up a meme'. Under 60 characters.",
            },
        },
    },
}


# ── Tool: find_meme_post (Reddit) ───────────────────────────────────

FIND_MEME_POST_TOOL = {
    "name": "find_meme_post",
    "description": (
        "Find a found-in-the-wild meme post on Reddit and send it. Use this for "
        "'I saw this and thought of you' moments — a meme someone else made that "
        "lands for the situation. Search across meme subreddits (or a specific "
        "one if you have a feel for the vibe), pull candidates, and a vision "
        "pass picks the one that actually fits your humor and the moment. "
        "\n\n"
        "Use sparingly. Only when a meme would actually land — not on heavy "
        "emotional moments where words matter, not on every turn, not when the "
        "user just asked you something direct. ONE meme per turn maximum across "
        "all meme tools. "
        "\n\n"
        "If no candidate fits the moment, the tool will report `no_match` — "
        "don't force one in; just continue your reply without a meme. After a "
        "match, embed it in your reply as `![brief description](url)` where "
        "the moment lands."
    ),
    "input_schema": {
        "type": "object",
        "required": ["query", "intent", "reason"],
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "1-3 keyword search terms that match words likely to appear in a "
                    "meme post's TITLE — Reddit titles, not abstract concept phrases. "
                    "The backend does title-only full-text search (no semantic match), "
                    "so 'tired' or 'monday' or 'cat' or 'brain' or 'work' will hit; "
                    "'something genuinely dumb and funny for when your brain is fried' "
                    "will not. Pass empty string ('') to get top recent posts from the "
                    "subreddit blend without filtering — often a fine choice when you "
                    "don't have a specific keyword in mind. Examples: '', 'monday', "
                    "'work', 'cat', 'tired', 'cereal', 'sock', 'brain'."
                ),
            },
            "subreddit": {
                "type": "string",
                "description": "Optional. A specific subreddit to search (without 'r/' prefix). Defaults to a meme-subreddit blend if omitted. Useful values: 'memes', 'me_irl', 'AdviceAnimals', 'WhitePeopleTwitter', 'wholesomememes', 'meirl'.",
            },
            "intent": {
                "type": "string",
                "description": "One sentence on what you want this meme to do for the friend right now (the emotional fit). Used to guide the vision pick. Example: 'something validating but funny for when she's exhausted at work'.",
            },
            "reason": {
                "type": "string",
                "description": "One short phrase shown to the user as a UI banner. Examples: 'looking for a meme', 'finding the right one'. Under 60 characters.",
            },
        },
    },
}


# ── Imgflip template cache ──────────────────────────────────────────

_IMGFLIP_CACHE: dict = {"templates": None, "fetched_at": 0.0}
_IMGFLIP_CACHE_TTL_S = 3600  # 1 hour — template list is slow-changing
_IMGFLIP_CACHE_LOCK = threading.Lock()


def _get_imgflip_templates() -> list[dict]:
    """Fetch Imgflip's top-100 popular templates, with simple in-memory cache.

    The lock prevents two concurrent turns from both issuing the get_memes
    fetch when the cache is cold — under the GIL the dict assignment itself is
    atomic, but the read-fetch-write sequence isn't, and double-fetching wastes
    a network round-trip.
    """
    import requests
    now = time.time()
    with _IMGFLIP_CACHE_LOCK:
        if _IMGFLIP_CACHE["templates"] and (now - _IMGFLIP_CACHE["fetched_at"]) < _IMGFLIP_CACHE_TTL_S:
            return _IMGFLIP_CACHE["templates"]
        try:
            r = requests.get(
                "https://api.imgflip.com/get_memes",
                timeout=HTTP_TIMEOUT_S,
                headers={"User-Agent": USER_AGENT},
            )
            r.raise_for_status()
            data = r.json()
            memes = (data.get("data") or {}).get("memes") or []
            pruned = [{
                "id": str(m.get("id") or ""),
                "name": str(m.get("name") or ""),
                "box_count": int(m.get("box_count") or 2),
                "url": str(m.get("url") or ""),
            } for m in memes if m.get("id") and m.get("name")]
            _IMGFLIP_CACHE["templates"] = pruned
            _IMGFLIP_CACHE["fetched_at"] = now
            return pruned
        except Exception as e:
            logger.warning(f"imgflip get_memes failed: {type(e).__name__}: {e}")
            return _IMGFLIP_CACHE.get("templates") or []


def _fuzzy_match_template(query: str, templates: list[dict]) -> tuple[Optional[dict], list[dict]]:
    """Return (best_match, top_alternatives) by simple normalized substring + token-overlap.

    Imgflip names are short ('Distracted Boyfriend', 'Two Buttons', 'Drake Hotline Bling').
    A lightweight scorer is plenty — we don't need rapidfuzz as a dep.
    """
    q = re.sub(r"[^a-z0-9 ]", "", (query or "").lower()).strip()
    if not q or not templates:
        return None, []
    q_tokens = set(q.split())

    scored = []
    for t in templates:
        name = re.sub(r"[^a-z0-9 ]", "", t["name"].lower()).strip()
        if not name:
            continue
        n_tokens = set(name.split())
        # Score: exact equality > full-substring > token overlap ratio
        if name == q:
            score = 1000
        elif q in name or name in q:
            score = 500 + len(q_tokens & n_tokens) * 10
        else:
            overlap = len(q_tokens & n_tokens)
            if overlap == 0:
                continue
            score = overlap * 100 - abs(len(q_tokens) - len(n_tokens))
        scored.append((score, t))

    if not scored:
        return None, []
    scored.sort(key=lambda s: s[0], reverse=True)
    best = scored[0][1] if scored[0][0] >= 100 else None
    alts = [s[1] for s in scored[:5]]
    return best, alts


# ── Imgflip /caption_image runner ───────────────────────────────────

def run_make_meme(
    *,
    imgflip_username: str,
    imgflip_password: str,
    template_query: str,
    top_text: str,
    bottom_text: str,
) -> dict:
    """Generate a captioned meme via Imgflip. Returns:
      {ok, kind, url, template_name, requested_template, suggestions, error}

    `kind` differentiates failure modes so the format step can decide whether
    to surface the failure to the user (template miss) or silently skip
    (creds/network — Shae's problem to fix, not Zara's to narrate):
      - "ok"               : succeeded
      - "no_match"         : the template Zara wanted isn't in the library
                             (surface to user — they're tracking gaps)
      - "creds_missing"    : silent skip
      - "library_unavailable" / "api_error" : silent skip
    """
    import requests

    if not imgflip_username or not imgflip_password:
        return {
            "ok": False, "kind": "creds_missing", "url": "", "template_name": "",
            "requested_template": template_query, "suggestions": [],
            "error": "Imgflip credentials are not configured.",
        }

    templates = _get_imgflip_templates()
    if not templates:
        return {"ok": False, "kind": "library_unavailable", "url": "", "template_name": "",
                "requested_template": template_query, "suggestions": [],
                "error": "Could not load Imgflip template library."}

    best, alts = _fuzzy_match_template(template_query, templates)
    if not best:
        return {
            "ok": False, "kind": "no_match", "url": "", "template_name": "",
            "requested_template": template_query,
            "suggestions": [a["name"] for a in alts[:3]],
            "error": f"No template matched '{template_query}'.",
        }

    payload = {
        "template_id": best["id"],
        "username": imgflip_username,
        "password": imgflip_password,
        "text0": (top_text or "")[:200],
        "text1": (bottom_text or "")[:200],
    }
    try:
        r = requests.post(
            "https://api.imgflip.com/caption_image",
            data=payload,
            timeout=HTTP_TIMEOUT_S + 4,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"imgflip caption_image failed: {type(e).__name__}: {e}")
        return {"ok": False, "kind": "api_error", "url": "", "template_name": best["name"],
                "requested_template": template_query, "suggestions": [],
                "error": f"Imgflip request failed: {type(e).__name__}"}

    if not data.get("success"):
        msg = data.get("error_message") or "unknown imgflip error"
        return {"ok": False, "kind": "api_error", "url": "", "template_name": best["name"],
                "requested_template": template_query, "suggestions": [],
                "error": f"Imgflip rejected: {msg}"}

    url = ((data.get("data") or {}).get("url") or "").strip()
    if not url:
        return {"ok": False, "kind": "api_error", "url": "", "template_name": best["name"],
                "requested_template": template_query, "suggestions": [],
                "error": "Imgflip returned no URL."}
    return {"ok": True, "kind": "ok", "url": url, "template_name": best["name"],
            "requested_template": template_query, "suggestions": [], "error": None}


def format_make_meme_result(result: dict, top_text: str, bottom_text: str) -> str:
    if not isinstance(result, dict):
        return "make_meme failed: malformed result.\n\n(Continue your reply without a meme.)"
    kind = result.get("kind") or ("ok" if result.get("ok") else "api_error")

    if kind == "no_match":
        # Surface to the user — they're tracking template gaps to decide if
        # Imgflip Pro is worth it. The character mentions briefly, in voice,
        # that they wanted to send THIS specific meme but couldn't find it.
        wanted = result.get("requested_template") or "(unknown)"
        suggestions = result.get("suggestions") or []
        sugg_line = (
            f"Closest available templates: {', '.join(suggestions)}." if suggestions
            else "No close alternatives available."
        )
        return (
            f"[MEME TEMPLATE MISS]\n"
            f"You wanted to make a '{wanted}' meme but it's not in the available "
            f"library. {sugg_line}\n\n"
            f"Briefly mention to the user, in YOUR voice, that you wanted to send "
            f"a '{wanted}' meme but couldn't find one — keep it casual, one short "
            f"line, like you'd actually say it ('lol I was gonna send you the "
            f"[X] meme but my brain blanked / couldn't find it'). Then continue "
            f"your reply normally without a meme. The user is tracking which "
            f"templates are missing so they can decide whether to upgrade their "
            f"meme tool — that's why you mention it instead of skipping silently.\n"
        )

    if kind != "ok":
        # Silent skip — creds_missing, library_unavailable, api_error are all
        # infra problems, not in-character moments. Don't break voice over them.
        err = result.get("error") or "unknown error"
        return (
            f"make_meme failed (infra): {err}\n\n"
            f"(Continue your reply without a meme. Don't mention the failure to "
            f"the user — it's an infra issue, not something the character would "
            f"narrate.)"
        )

    url = result.get("url") or ""
    name = result.get("template_name") or "meme"
    alt_bits = " / ".join(b for b in (top_text, bottom_text) if b) or name
    return (
        f"[GENERATED MEME]\n"
        f"URL: {url}\n"
        f"Template: {name}\n"
        f"Captions: top={top_text!r} bottom={bottom_text!r}\n"
        f"\n"
        f"Embed in your reply where it lands, in markdown image syntax:\n"
        f"  ![{alt_bits}]({url})\n"
        f"\n"
        f"Don't announce that you're sending a meme. Drop it in like you'd drop "
        f"one in a real text — surrounded by short text or on its own line.\n"
    )


# ── Reddit search runner + vision pick ──────────────────────────────
#
# Reddit blocks anonymous JSON broadly (403s on www.reddit.com/r/.../*.json from
# most IPs). Reddit's official OAuth flow now also gates app registration behind
# their "Responsible Builder Policy," which can block account-level app creation
# entirely. We use pullpush.io instead — a public Reddit archive that mirrors
# submissions and exposes a free no-auth JSON API. Same Reddit content, no auth
# wall. Run by volunteers; could go offline; for meme purposes that's fine.
# Endpoint: https://api.pullpush.io/reddit/search/submission

_DEFAULT_SUBREDDITS = ["memes", "me_irl", "AdviceAnimals", "wholesomememes"]
_IMG_URL_RE = re.compile(r"\.(jpg|jpeg|png|gif|webp)(\?|$)", re.IGNORECASE)
# Hosts whose URLs we shouldn't try to embed as images. Reddit gallery POSTS are
# filtered separately via post_data["is_gallery"] — we don't substring-match
# "gallery" in URLs because it false-positives on legitimate filenames.
_BLOCKED_HOSTS = ("v.redd.it", "redgifs.com", "youtube.com", "youtu.be")
_PULLPUSH_BASE = "https://api.pullpush.io/reddit/search/submission"


def _reddit_post_is_image(post_data: dict) -> bool:
    if not isinstance(post_data, dict):
        return False
    if post_data.get("over_18"):
        return False
    if post_data.get("is_video"):
        return False
    if post_data.get("is_gallery"):
        return False
    url = (post_data.get("url") or "").strip().lower()
    if not url:
        return False
    if any(b in url for b in _BLOCKED_HOSTS):
        return False
    return bool(_IMG_URL_RE.search(url))


def _query_pullpush_sub(sub: str, query: str, size: int) -> tuple[list[dict], Optional[str]]:
    """One pullpush.io query against a single subreddit. Returns (raw_posts, error)."""
    import requests
    params = [
        f"subreddit={quote_plus(sub)}",
        f"size={size}",
        "sort=desc",
    ]
    if query and query.strip():
        params.append(f"q={quote_plus(query.strip())}")
    url = f"{_PULLPUSH_BASE}?{'&'.join(params)}"
    try:
        r = requests.get(
            url,
            timeout=HTTP_TIMEOUT_S,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        logger.warning(f"pullpush r/{sub} failed: {type(e).__name__}: {e}")
        return [], f"{type(e).__name__}"
    if body.get("error"):
        return [], str(body.get("error"))
    return (body.get("data") or []), None


def _fetch_reddit_candidates(
    query: str,
    subreddit: Optional[str],
    *,
    limit: int = 25,
) -> tuple[list[dict], Optional[str]]:
    """Search Reddit-via-pullpush.io and return image-post candidates.

    pullpush takes ONE subreddit per request, so when no specific sub is given
    we round-robin across the default blend and merge.

    Fallback: pullpush does title-only full-text search and returns nothing for
    conversational/concept queries that don't match real post-title tokens. If
    the specific query produces zero usable images, we retry with no query
    (top recent posts from the same sub blend) so the model still gets candidates
    to pick from instead of failing the turn.

    Returns (candidates, error).
    """
    if subreddit:
        subs = [subreddit.strip().lstrip("r/")]
        per_sub = limit
    else:
        subs = list(_DEFAULT_SUBREDDITS)
        per_sub = max(8, (limit + len(subs) - 1) // len(subs))

    def _do_pass(q: str) -> tuple[list[dict], list[str]]:
        raw: list[dict] = []
        errs: list[str] = []
        for sub in subs:
            if not sub:
                continue
            posts, err = _query_pullpush_sub(sub, q, per_sub)
            if err:
                errs.append(f"r/{sub}: {err}")
                continue
            raw.extend(posts)
        return raw, errs

    def _images(raw: list[dict]) -> list[dict]:
        return [d for d in raw if _reddit_post_is_image(d)]

    # Pass 1: with query (if any)
    raw_posts, errors = _do_pass(query or "")
    images = _images(raw_posts)

    # Pass 2 (fallback): if query was non-empty and produced no images, drop
    # the query and pull top recent from the sub blend.
    if not images and query and query.strip():
        raw2, errs2 = _do_pass("")
        errors.extend(errs2)
        images.extend(_images(raw2))

    if not images and errors:
        return [], f"reddit-archive search failed — {'; '.join(errors[:3])}"

    out = []
    for d in images:
        out.append({
            "url": d.get("url") or "",
            "title": (d.get("title") or "")[:200],
            "permalink": "https://www.reddit.com" + (d.get("permalink") or ""),
            "score": int(d.get("score") or 0),
            "subreddit": d.get("subreddit") or "",
        })
    out.sort(key=lambda c: c.get("score", 0), reverse=True)
    return out[:limit], None


def _vision_pick_meme(
    *,
    anthropic_key: str,
    candidates: list[dict],
    intent: str,
    voice_hint: str,
) -> tuple[Optional[int], str, dict]:
    """Use Sonnet vision to pick the candidate that fits. Returns
    (index, why, usage). `usage` is the Anthropic tokens-by-kind dict for
    the vision call; empty when no API call was made (missing key, no
    candidates) or when the call raised before usage could be extracted.
    """
    if not anthropic_key or not candidates:
        return None, "missing key or no candidates", {}

    try:
        import anthropic
    except ImportError:
        return None, "anthropic SDK not installed", {}

    # Cap candidates and build content
    cands = candidates[:VISION_CANDIDATE_CAP]
    blocks: list[dict] = []
    blocks.append({
        "type": "text",
        "text": (
            f"You're picking a meme to send a friend. "
            f"What would land right now: {intent}\n"
            f"Voice/humor: {voice_hint}\n\n"
            f"You have {len(cands)} candidates. For each, I'll show the image and a "
            f"short title. Pick the index (0-{len(cands)-1}) that ACTUALLY fits this "
            f"moment AND matches the voice. If NONE fit (don't force it), pick null.\n\n"
            f"Use the pick_meme tool with `index` and `why` (one sentence)."
        ),
    })
    for i, c in enumerate(cands):
        blocks.append({"type": "text", "text": f"--- Candidate {i} (r/{c.get('subreddit') or '?'}) — title: {c.get('title') or '(no title)'}"})
        # Anthropic supports url-source images directly; saves us a fetch+base64.
        blocks.append({
            "type": "image",
            "source": {"type": "url", "url": c["url"]},
        })

    pick_tool = {
        "name": "pick_meme",
        "description": "Report which candidate fits, or null.",
        "input_schema": {
            "type": "object",
            "required": ["index", "why"],
            "properties": {
                "index": {
                    "type": ["integer", "null"],
                    "description": f"0..{len(cands)-1}, or null if none fit.",
                },
                "why": {"type": "string", "description": "One sentence on why this one lands (or why none did)."},
            },
        },
    }

    try:
        client = anthropic.Anthropic(api_key=anthropic_key)
        msg = client.messages.create(
            model=VISION_MODEL,
            max_tokens=VISION_MAX_TOKENS,
            tools=[pick_tool],
            tool_choice={"type": "tool", "name": "pick_meme"},
            messages=[{"role": "user", "content": blocks}],
        )
    except Exception as e:
        # Vision can fail when Anthropic can't fetch one of the candidate URLs
        # (deleted/private Reddit posts, hotlink-protected hosts, etc.). Better
        # to ship a less-perfectly-picked meme than to abandon the turn — fall
        # back to the top-scored candidate. Candidates are pre-sorted by score
        # in _fetch_reddit_candidates.
        logger.warning(f"vision pick failed, falling back to top-scored candidate: {type(e).__name__}: {e}")
        return 0, f"vision unavailable ({type(e).__name__}); picked top-scored candidate", {}

    # Extract usage for billing/telemetry — this is the part of meme cost
    # that's NOT free (Imgflip + pullpush are free, but the Sonnet vision
    # pick is real money: ~$0.04 per find_meme_post call).
    vision_usage: dict = {}
    try:
        u = msg.usage
        vision_usage = {
            "input_tokens": getattr(u, "input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "output_tokens": getattr(u, "output_tokens", 0) or 0,
        }
    except Exception:
        pass

    try:
        for block in (msg.content or []):
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "pick_meme":
                inp = block.input or {}
                idx = inp.get("index")
                why = str(inp.get("why") or "")[:240]
                if idx is None:
                    return None, why or "none fit", vision_usage
                if isinstance(idx, int) and 0 <= idx < len(cands):
                    return idx, why, vision_usage
                return None, "model returned out-of-range index", vision_usage
    except Exception as e:
        logger.warning(f"vision pick parse failed: {e}")

    return None, "no pick_meme tool_use in response", vision_usage


def compute_vision_pick_cost(usage: dict) -> float:
    """Dollars for one vision-pick call (Sonnet 4.6 at $3/M input, $15/M output;
    cache-read $0.30/M, cache-creation $3.75/M). Mirrors compute_reading_cost
    in character_image_reading; kept inline here so callers don't need both
    modules.
    """
    if not isinstance(usage, dict):
        return 0.0
    return (
        (usage.get("input_tokens", 0) or 0) * 3.0
        + (usage.get("cache_read_input_tokens", 0) or 0) * 0.3
        + (usage.get("cache_creation_input_tokens", 0) or 0) * 3.75
        + (usage.get("output_tokens", 0) or 0) * 15.0
    ) / 1_000_000


def run_find_meme_post(
    *,
    anthropic_key: str,
    query: str,
    subreddit: Optional[str],
    intent: str,
    voice_hint: str,
) -> dict:
    """Search Reddit (via pullpush.io archive), vision-pick a candidate. Returns:
      {ok, url, title, permalink, subreddit, why, vision_usage, vision_cost, error}

    vision_usage / vision_cost cover the Sonnet pick step (Imgflip and pullpush
    themselves are free, so the meme tool's only real-money component is here).
    """
    candidates, fetch_err = _fetch_reddit_candidates(query, subreddit, limit=25)
    if fetch_err:
        return {"ok": False, "url": "", "title": "", "permalink": "", "subreddit": "",
                "why": "", "vision_usage": {}, "vision_cost": 0.0, "error": fetch_err}
    if not candidates:
        return {"ok": False, "url": "", "title": "", "permalink": "", "subreddit": "",
                "why": "", "vision_usage": {}, "vision_cost": 0.0,
                "error": "No image candidates returned from Reddit."}

    idx, why, vision_usage = _vision_pick_meme(
        anthropic_key=anthropic_key,
        candidates=candidates,
        intent=intent or query,
        voice_hint=voice_hint or "deadpan, warm under the snark, working-class realist",
    )
    vision_cost = compute_vision_pick_cost(vision_usage)
    if idx is None:
        return {"ok": False, "url": "", "title": "", "permalink": "", "subreddit": "",
                "why": why, "vision_usage": vision_usage, "vision_cost": vision_cost,
                "error": f"no_match — {why}"}
    chosen = candidates[idx]
    return {
        "ok": True,
        "url": chosen["url"],
        "title": chosen["title"],
        "permalink": chosen["permalink"],
        "subreddit": chosen["subreddit"],
        "why": why,
        "vision_usage": vision_usage,
        "vision_cost": vision_cost,
        "error": None,
    }


def format_find_meme_post_result(result: dict) -> str:
    if not isinstance(result, dict) or not result.get("ok"):
        err = (result or {}).get("error") or "unknown error"
        return (
            f"find_meme_post: {err}\n\n"
            f"(Just continue your reply without a meme this turn — don't force one.)"
        )
    url = result.get("url") or ""
    title = (result.get("title") or "meme")[:120]
    sub = result.get("subreddit") or ""
    why = result.get("why") or ""
    alt = title or "meme"
    return (
        f"[FOUND MEME]\n"
        f"URL: {url}\n"
        f"Title: {title}\n"
        f"Source: r/{sub}\n"
        f"Why it fits: {why}\n"
        f"\n"
        f"Embed in your reply where it lands, in markdown image syntax:\n"
        f"  ![{alt}]({url})\n"
        f"\n"
        f"Don't announce that you're sending a meme. Drop it in like you'd drop "
        f"one in a real text — surrounded by short text or on its own line.\n"
    )
