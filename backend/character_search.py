"""Perplexity Sonar search tool — used by the Characters correspondence model.

Exposes a `search_web` tool the main model can call mid-stream. Tool input is just
a query string. The handler runs Sonar (base tier), returns the synthesized answer
and a compact list of sources, and the model continues narration with that as
context.

Why Sonar (not Tavily, not raw search): Sonar returns a synthesized answer + sources
in one call. The character paraphrases the answer naturally — no second-pass
summarization step. Cheaper per turn, lower latency.

API details:
- SDK: `perplexityai` on pip (imports as `from perplexity import Perplexity`)
- Model: "sonar" (base tier — cheapest, no reasoning, no deep research)
- Cost: returned directly by API in `usage.cost.total_cost`. We don't recompute.
- API key: pulled from api_keys.json["perplexity"], threaded in by caller.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

SONAR_MODEL = "sonar"
SONAR_TIMEOUT_S = 30
SONAR_MAX_TOKENS = 1024  # answer length cap — characters paraphrase, don't transcribe
SONAR_MAX_SOURCES = 5    # cap citations passed back to the main model


# ── Tool definition (Anthropic tool schema for Opus 3) ─────────────

SONAR_SEARCH_TOOL = {
    "name": "search_web",
    "description": (
        "Search the web for current, factual, or local information. Use this for things "
        "the character would naturally check on their phone or mention they looked up: "
        "showtimes, business hours, addresses, current events, sports scores, song lyrics, "
        "song/movie/book titles, recent news, weather, etc. "
        "Do NOT use for things you'd reasonably know from general knowledge — names of "
        "common things, basic facts, opinions, advice, conversation. "
        "ONE search per turn maximum. When you get the answer, rephrase the relevant bit "
        "in the character's voice — take only what answers the moment, drop everything else."
    ),
    "input_schema": {
        "type": "object",
        "required": ["query", "reason"],
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "What to search for — phrase it as you'd type into a search box. "
                    "Be specific (include city, date, year if relevant). 1-2 short phrases."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One short phrase explaining why the character is searching this — "
                    "shown to the user as a UI banner so they see what the character "
                    "looked up and why. Examples: 'checking Roxy hours', 'looking up "
                    "Bills score', 'recipe for the cake she mentioned'. Keep it under "
                    "60 characters."
                ),
            },
        },
    },
}


# ── Runtime call ───────────────────────────────────────────────────

def run_sonar_search(api_key: str, query: str) -> dict:
    """Run a single Sonar search. Returns:
      {
        "ok": bool,
        "answer": str,                  # synthesized answer (or empty on error)
        "sources": list[{"title": str, "url": str, "snippet": str|None, "date": str|None}],
        "usage": dict,                  # raw token counts for telemetry
        "cost": float,                  # dollars from usage.cost.total_cost (Perplexity-computed)
        "error": str|None,
      }

    Soft-fails on any exception — caller should pass the answer (or an error message)
    back to the main model as a tool_result so the turn doesn't deadlock.
    """
    if not api_key:
        return {
            "ok": False,
            "answer": "",
            "sources": [],
            "usage": {},
            "cost": 0.0,
            "error": "Perplexity API key is not configured. Add a 'perplexity' key in API settings.",
        }
    if not query or not isinstance(query, str) or not query.strip():
        return {
            "ok": False,
            "answer": "",
            "sources": [],
            "usage": {},
            "cost": 0.0,
            "error": "Empty search query.",
        }

    try:
        from perplexity import Perplexity
    except ImportError as e:
        logger.error(f"perplexity SDK not installed: {e}")
        return {
            "ok": False,
            "answer": "",
            "sources": [],
            "usage": {},
            "cost": 0.0,
            "error": "Perplexity SDK is not installed on the server (pip install perplexityai).",
        }

    client = Perplexity(api_key=api_key)

    try:
        response = client.chat.completions.create(
            model=SONAR_MODEL,
            messages=[{"role": "user", "content": query.strip()}],
            max_tokens=SONAR_MAX_TOKENS,
            timeout=SONAR_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning(f"sonar search failed: {type(e).__name__}: {e}")
        return {
            "ok": False,
            "answer": "",
            "sources": [],
            "usage": {},
            "cost": 0.0,
            "error": f"Search failed: {type(e).__name__}",
        }
    finally:
        try:
            client.close()
        except Exception:
            pass

    # Extract answer text from choices[0].message.content
    answer = ""
    try:
        choices = getattr(response, "choices", None) or []
        if choices:
            msg = getattr(choices[0], "message", None)
            content = getattr(msg, "content", None) if msg else None
            if isinstance(content, str):
                answer = content
    except Exception as e:
        logger.warning(f"sonar response parse: choices/message: {e}")

    # Sources: prefer search_results (rich), fall back to citations (URLs only)
    sources: list[dict] = []
    try:
        results = getattr(response, "search_results", None) or []
        for r in results[:SONAR_MAX_SOURCES]:
            sources.append({
                "title": getattr(r, "title", "") or "",
                "url": getattr(r, "url", "") or "",
                "snippet": getattr(r, "snippet", None),
                "date": getattr(r, "date", None) or getattr(r, "last_updated", None),
            })
        if not sources:
            cites = getattr(response, "citations", None) or []
            for url in cites[:SONAR_MAX_SOURCES]:
                if isinstance(url, str) and url:
                    sources.append({"title": "", "url": url, "snippet": None, "date": None})
    except Exception as e:
        logger.warning(f"sonar response parse: sources: {e}")

    # Usage and cost — Perplexity returns dollar cost directly in usage.cost.total_cost
    usage_dict: dict = {}
    cost_value = 0.0
    try:
        usage = getattr(response, "usage", None)
        if usage is not None:
            usage_dict = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(usage, "total_tokens", 0) or 0,
                "num_search_queries": getattr(usage, "num_search_queries", 0) or 0,
            }
            cost_obj = getattr(usage, "cost", None)
            if cost_obj is not None:
                # Prefer the SDK-reported total; never recompute (Perplexity's pricing changes).
                cost_value = float(getattr(cost_obj, "total_cost", 0.0) or 0.0)
    except Exception as e:
        logger.warning(f"sonar response parse: usage/cost: {e}")

    if not answer.strip():
        return {
            "ok": False,
            "answer": "",
            "sources": sources,
            "usage": usage_dict,
            "cost": cost_value,
            "error": "Search returned no answer.",
        }

    return {
        "ok": True,
        "answer": answer.strip(),
        "sources": sources,
        "usage": usage_dict,
        "cost": cost_value,
        "error": None,
    }


def format_tool_result_text(result: dict) -> str:
    """Render the search result as the tool_result content the main model will read."""
    if not isinstance(result, dict):
        return "Search failed: malformed result."
    if not result.get("ok"):
        return f"Search failed: {result.get('error') or 'unknown error'}"

    parts = [result.get("answer", "").strip()]
    sources = result.get("sources") or []
    if sources:
        # Sources serve three purposes: (1) cross-reference if the answer reads
        # thin, (2) source-name attribution if the user asks "where'd you see
        # that?", (3) link-sharing on TEXT channel (real friends text articles).
        # On voice/video/in-person channels, URLs are still off-limits — only
        # title/site name can be spoken.
        src_lines = []
        for i, s in enumerate(sources, 1):
            title = s.get("title") or ""
            url = s.get("url") or ""
            date = s.get("date") or ""
            line = f"  [{i}] {title}".rstrip()
            if date:
                line += f" ({date})"
            if url:
                line += f" — {url}"
            src_lines.append(line)
        if src_lines:
            parts.append(
                "\nSources — channel-conditional usage:"
                "\n  - TEXT channel: sharing an actual URL is fine and friend-natural"
                " (\"saw this, [url]\" / \"thought of you [url]\"). Pick at most ONE link;"
                " don't dump the source list."
                "\n  - PHONE / VIDEO / IN-PERSON channel: NEVER recite URLs. Use only"
                " the title/site name if asked (\"Reuters\", \"AP\", \"ESPN\")."
            )
            parts.extend(src_lines)

    parts.append(
        "\n(Rephrase in voice — take only what answers the moment. Don't write"
        " academic-style citations. On TEXT channel you may attach ONE link if"
        " the article itself is what you're sharing; on voice/in-person never"
        " recite URLs — name the source casually by title only if asked.)"
    )
    return "\n".join(parts)
