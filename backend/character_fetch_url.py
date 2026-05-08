"""URL-fetch tool for the Characters correspondence model.

When the user pastes an article link in chat ("hey did you see this, [url]"),
the character can call fetch_url to actually read the page and respond. This
is independent of search_web (Perplexity) — it doesn't run a search, it just
gets the page text so the character has something to react to.

Like character_search.run_sonar_search, this returns a structured dict with
either a successful page payload or an error. The main loop in main.py wires
it into the same tool-call loop search_web uses.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Tool config
FETCH_URL_TIMEOUT_S = 10
FETCH_URL_MAX_BYTES = 5 * 1024 * 1024     # 5MB raw response cap
FETCH_URL_MAX_TEXT_CHARS = 8000           # truncate extracted text past this
FETCH_URL_USER_AGENT = "Chorus-Characters/1.0 (article reader)"


FETCH_URL_TOOL = {
    "name": "fetch_url",
    "description": (
        "Fetch a webpage the user shared and return its text content so you can "
        "read and react to it. Use when the user posts a link and clearly wants "
        "you to engage with what's on the other side ('did you see this?', "
        "'what do you think of this?', 'check this out'). Do NOT use it on "
        "every URL — only when the article itself is the point of the message. "
        "If the user is just citing a fact or domain in passing, skip it. "
        "After fetching, REPHRASE in your voice — don't paraphrase mechanically, "
        "don't summarize the whole article. React to what stood out the way "
        "THIS character would. ONE fetch per turn maximum."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The exact URL the user shared (must be a complete http:// or https:// link).",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Brief one-phrase reason you're fetching, shown to the user "
                    "as a notification. Example: 'reading the article you sent', "
                    "'looking at that link'. Keep it casual and short."
                ),
            },
        },
        "required": ["url", "reason"],
    },
}


# HTML tag/text utilities --------------------------------------------------

# Drop these elements entirely (their text is noise/scaffolding, not content).
_DROP_TAGS_RE = re.compile(
    r"<(script|style|noscript|svg|nav|header|footer|aside|form|iframe|figure)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_NEWLINES_RE = re.compile(r"\n{3,}")
# Convert <br>/<p>/<div>/<li>/<h1-6> close tags to newline before stripping.
_BLOCK_BREAK_RE = re.compile(
    r"</(p|div|li|h[1-6]|tr|article|section|blockquote)\s*>|<br\s*/?>",
    re.IGNORECASE,
)


def _extract_text_from_html(html: str) -> tuple[str, str | None]:
    """Strip noise + tags, normalize whitespace. Returns (text, title-or-None)."""
    if not html:
        return "", None

    # Title
    title = None
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        t = re.sub(r"\s+", " ", m.group(1)).strip()
        if t:
            title = t[:200]

    # Drop high-noise blocks first
    cleaned = _DROP_TAGS_RE.sub(" ", html)
    # Convert block-closers to newlines so paragraphs don't smash together
    cleaned = _BLOCK_BREAK_RE.sub("\n", cleaned)
    # Strip remaining tags
    text = _TAG_RE.sub("", cleaned)
    # Decode a few common entities
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
    )
    # Normalize whitespace
    text = _WHITESPACE_RE.sub(" ", text)
    text = _NEWLINES_RE.sub("\n\n", text)
    text = text.strip()
    return text, title


# Tool runner --------------------------------------------------------------

def run_fetch_url(url: str) -> dict:
    """Fetch `url` and return a structured result.

    Shape:
      {
        "ok": bool,
        "url": str,           # final URL after redirects
        "title": str | None,
        "text": str,          # extracted main text, truncated
        "truncated": bool,
        "error": str | None,
      }
    """
    import requests

    if not isinstance(url, str) or not url.strip():
        return {"ok": False, "url": url or "", "title": None, "text": "",
                "truncated": False, "error": "Missing URL"}
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"ok": False, "url": url, "title": None, "text": "",
                "truncated": False, "error": "URL must start with http:// or https://"}

    try:
        resp = requests.get(
            url,
            timeout=FETCH_URL_TIMEOUT_S,
            stream=True,
            allow_redirects=True,
            headers={"User-Agent": FETCH_URL_USER_AGENT, "Accept": "text/html,*/*"},
        )
    except Exception as e:
        return {"ok": False, "url": url, "title": None, "text": "",
                "truncated": False, "error": f"fetch failed: {type(e).__name__}: {e}"}

    if resp.status_code != 200:
        return {"ok": False, "url": resp.url or url, "title": None, "text": "",
                "truncated": False, "error": f"HTTP {resp.status_code}"}

    ct = (resp.headers.get("content-type") or "").lower().split(";")[0].strip()
    if not (ct.startswith("text/html") or ct.startswith("application/xhtml") or ct == "text/plain" or ct == ""):
        return {"ok": False, "url": resp.url or url, "title": None, "text": "",
                "truncated": False, "error": f"unsupported content-type: {ct or 'unknown'}"}

    # Stream-read with byte cap
    body_chunks: list[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                break
            total += len(chunk)
            if total > FETCH_URL_MAX_BYTES:
                break
            body_chunks.append(chunk)
    except Exception as e:
        return {"ok": False, "url": resp.url or url, "title": None, "text": "",
                "truncated": False, "error": f"read failed: {e}"}

    encoding = resp.encoding or "utf-8"
    try:
        body = b"".join(body_chunks).decode(encoding, errors="replace")
    except LookupError:
        body = b"".join(body_chunks).decode("utf-8", errors="replace")

    # If text/plain, we already have the text — just clip it.
    if ct == "text/plain":
        text = body
        title = None
    else:
        text, title = _extract_text_from_html(body)

    truncated = False
    if len(text) > FETCH_URL_MAX_TEXT_CHARS:
        text = text[:FETCH_URL_MAX_TEXT_CHARS]
        truncated = True

    return {
        "ok": True,
        "url": resp.url or url,
        "title": title,
        "text": text,
        "truncated": truncated,
        "error": None,
    }


def format_tool_result_text(result: dict) -> str:
    """Render the fetch result as the tool_result content for the main model."""
    if not isinstance(result, dict):
        return "Fetch failed: malformed result."
    if not result.get("ok"):
        return f"Fetch failed: {result.get('error') or 'unknown error'}"

    parts = []
    if result.get("title"):
        parts.append(f"[Title] {result['title']}")
    parts.append(f"[URL] {result.get('url') or ''}")
    parts.append("")
    parts.append(result.get("text") or "(empty)")
    if result.get("truncated"):
        parts.append(f"\n…(article truncated at {FETCH_URL_MAX_TEXT_CHARS} chars; you have the gist)")
    parts.append(
        "\n(Rephrase in voice — react to what stood out, the way THIS character would. "
        "Don't summarize the whole article. Don't paraphrase mechanically. The user "
        "has already seen this; share your read on it, not a recap.)"
    )
    return "\n".join(parts)
