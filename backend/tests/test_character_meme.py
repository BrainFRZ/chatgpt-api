"""Unit tests for character_meme — fuzzy template matching, image-post filter,
result-formatting branches, the no-query fallback, and the vision-pick graceful
degradation.

All tests are stdlib-only or use unittest.mock; no real Anthropic / Imgflip /
pullpush calls happen.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import character_meme as cm  # noqa: E402


# ── _fuzzy_match_template ──────────────────────────────────────────────────

class TestFuzzyMatchTemplate:
    @pytest.fixture
    def templates(self):
        return [
            {"id": "1", "name": "Two Buttons", "box_count": 2, "url": ""},
            {"id": "2", "name": "Distracted Boyfriend", "box_count": 3, "url": ""},
            {"id": "3", "name": "Drake Hotline Bling", "box_count": 2, "url": ""},
            {"id": "4", "name": "This Is Fine", "box_count": 2, "url": ""},
            {"id": "5", "name": "Is This A Pigeon", "box_count": 3, "url": ""},
        ]

    def test_exact_match(self, templates):
        best, alts = cm._fuzzy_match_template("Two Buttons", templates)
        assert best is not None
        assert best["name"] == "Two Buttons"

    def test_lowercase_match(self, templates):
        best, _ = cm._fuzzy_match_template("two buttons", templates)
        assert best is not None
        assert best["name"] == "Two Buttons"

    def test_substring_match(self, templates):
        best, _ = cm._fuzzy_match_template("distracted", templates)
        assert best is not None
        assert best["name"] == "Distracted Boyfriend"

    def test_token_overlap_matches_when_strong(self, templates):
        best, _ = cm._fuzzy_match_template("drake bling", templates)
        assert best is not None
        assert best["name"] == "Drake Hotline Bling"

    def test_no_match_returns_none(self, templates):
        best, alts = cm._fuzzy_match_template("zorklepuddle", templates)
        assert best is None
        # alts may be empty (no token overlap) — that's the strict no_match case
        assert isinstance(alts, list)

    def test_empty_query(self, templates):
        best, alts = cm._fuzzy_match_template("", templates)
        assert best is None
        assert alts == []

    def test_empty_template_list(self):
        best, alts = cm._fuzzy_match_template("two buttons", [])
        assert best is None
        assert alts == []

    def test_punctuation_stripped(self, templates):
        # "is this a pigeon?" with a question mark should still match
        best, _ = cm._fuzzy_match_template("is this a pigeon?", templates)
        assert best is not None
        assert best["name"] == "Is This A Pigeon"


# ── _reddit_post_is_image ──────────────────────────────────────────────────

class TestRedditPostIsImage:
    def test_direct_jpg_passes(self):
        post = {"url": "https://i.redd.it/abc.jpg", "over_18": False, "is_video": False, "is_gallery": False}
        assert cm._reddit_post_is_image(post) is True

    def test_direct_png_passes(self):
        post = {"url": "https://i.redd.it/abc.png", "over_18": False, "is_video": False, "is_gallery": False}
        assert cm._reddit_post_is_image(post) is True

    def test_url_with_query_string_passes(self):
        post = {"url": "https://i.redd.it/abc.jpg?auto=webp&s=xyz", "over_18": False, "is_video": False, "is_gallery": False}
        assert cm._reddit_post_is_image(post) is True

    def test_video_excluded(self):
        post = {"url": "https://i.redd.it/abc.jpg", "over_18": False, "is_video": True, "is_gallery": False}
        assert cm._reddit_post_is_image(post) is False

    def test_nsfw_excluded(self):
        post = {"url": "https://i.redd.it/abc.jpg", "over_18": True, "is_video": False, "is_gallery": False}
        assert cm._reddit_post_is_image(post) is False

    def test_gallery_excluded_via_field(self):
        post = {"url": "https://i.redd.it/abc.jpg", "over_18": False, "is_video": False, "is_gallery": True}
        assert cm._reddit_post_is_image(post) is False

    def test_blocked_host_v_redd_it(self):
        post = {"url": "https://v.redd.it/abc.mp4", "over_18": False, "is_video": False, "is_gallery": False}
        assert cm._reddit_post_is_image(post) is False

    def test_blocked_host_youtube(self):
        post = {"url": "https://youtube.com/watch?v=foo", "over_18": False, "is_video": False, "is_gallery": False}
        assert cm._reddit_post_is_image(post) is False

    def test_url_with_gallery_in_filename_NOT_blocked(self):
        # P2 fix verification: "gallery." substring no longer blocks legitimate filenames
        post = {"url": "https://i.imgur.com/gallery.jpg", "over_18": False, "is_video": False, "is_gallery": False}
        assert cm._reddit_post_is_image(post) is True

    def test_non_image_url_excluded(self):
        post = {"url": "https://example.com/article", "over_18": False, "is_video": False, "is_gallery": False}
        assert cm._reddit_post_is_image(post) is False

    def test_empty_url_excluded(self):
        post = {"url": "", "over_18": False, "is_video": False, "is_gallery": False}
        assert cm._reddit_post_is_image(post) is False

    def test_non_dict_input(self):
        assert cm._reddit_post_is_image(None) is False
        assert cm._reddit_post_is_image("a string") is False


# ── format_make_meme_result ────────────────────────────────────────────────

class TestFormatMakeMemeResult:
    def test_ok_kind_emits_embed_instruction(self):
        result = {
            "ok": True, "kind": "ok",
            "url": "https://i.imgflip.com/abc.jpg",
            "template_name": "Two Buttons",
            "requested_template": "two buttons",
            "suggestions": [],
        }
        out = cm.format_make_meme_result(result, "top text", "bottom text")
        assert "https://i.imgflip.com/abc.jpg" in out
        assert "![" in out and "](" in out  # markdown image syntax
        assert "Two Buttons" in out

    def test_no_match_kind_tells_model_to_surface(self):
        result = {
            "ok": False, "kind": "no_match",
            "url": "", "template_name": "",
            "requested_template": "spider-man double point",
            "suggestions": ["Spider-Man Pointing", "Drake Hotline Bling"],
            "error": "No template matched 'spider-man double point'.",
        }
        out = cm.format_make_meme_result(result, "a", "b")
        # Should instruct the model to surface in voice
        assert "TEMPLATE MISS" in out
        assert "spider-man double point" in out
        assert "Briefly mention" in out
        # Should NOT instruct silent skip
        assert "Don't mention the failure" not in out

    def test_creds_missing_silent_skip(self):
        result = {
            "ok": False, "kind": "creds_missing",
            "url": "", "template_name": "",
            "requested_template": "two buttons", "suggestions": [],
            "error": "Imgflip credentials are not configured.",
        }
        out = cm.format_make_meme_result(result, "a", "b")
        # Should NOT tell the character to surface
        assert "TEMPLATE MISS" not in out
        # Should explicitly tell model to silent-skip
        assert "Don't mention" in out or "infra" in out.lower()

    def test_api_error_silent_skip(self):
        result = {
            "ok": False, "kind": "api_error",
            "url": "", "template_name": "Two Buttons",
            "requested_template": "two buttons", "suggestions": [],
            "error": "Imgflip rejected: Invalid username/password combination",
        }
        out = cm.format_make_meme_result(result, "a", "b")
        assert "TEMPLATE MISS" not in out
        assert "infra" in out.lower()

    def test_malformed_input_handled(self):
        out = cm.format_make_meme_result(None, "", "")
        assert "malformed" in out.lower() or "failed" in out.lower()


# ── format_find_meme_post_result ───────────────────────────────────────────

class TestFormatFindMemePostResult:
    def test_ok_emits_embed(self):
        result = {
            "ok": True,
            "url": "https://i.redd.it/foo.jpg",
            "title": "tired monday",
            "permalink": "https://www.reddit.com/r/memes/comments/abc/tired_monday/",
            "subreddit": "memes",
            "why": "matches the exhausted-monday vibe",
            "error": None,
        }
        out = cm.format_find_meme_post_result(result)
        assert "https://i.redd.it/foo.jpg" in out
        assert "![" in out and "](" in out
        assert "memes" in out

    def test_error_tells_model_to_continue(self):
        result = {"ok": False, "url": "", "title": "", "permalink": "", "subreddit": "",
                  "why": "", "error": "No image candidates returned from Reddit."}
        out = cm.format_find_meme_post_result(result)
        assert "without a meme" in out.lower() or "continue" in out.lower()
        assert "don't force" in out.lower()


# ── _fetch_reddit_candidates fallback ──────────────────────────────────────

class TestFetchRedditCandidatesFallback:
    """Verify the no-query fallback fires when a specific query produces no images."""

    def _make_post(self, url="https://i.redd.it/abc.jpg", title="cat", subreddit="memes", score=10):
        return {
            "url": url,
            "title": title,
            "subreddit": subreddit,
            "score": score,
            "permalink": f"/r/{subreddit}/comments/x/{title}/",
            "over_18": False,
            "is_video": False,
            "is_gallery": False,
        }

    def test_query_hits_directly_no_fallback(self, monkeypatch):
        post = self._make_post()
        calls = []
        def fake(sub, q, size):
            calls.append((sub, q))
            return [post], None
        monkeypatch.setattr(cm, "_query_pullpush_sub", fake)
        cands, err = cm._fetch_reddit_candidates("cat", "memes", limit=10)
        assert err is None
        assert len(cands) >= 1
        # Single sub means single call; query was passed through
        assert len(calls) == 1
        assert calls[0][1] == "cat"

    def test_query_returns_empty_falls_back_to_no_query(self, monkeypatch):
        post = self._make_post()
        calls = []
        def fake(sub, q, size):
            calls.append((sub, q))
            # First pass (with query) returns nothing; second pass (empty) returns post
            if q:
                return [], None
            return [post], None
        monkeypatch.setattr(cm, "_query_pullpush_sub", fake)
        cands, err = cm._fetch_reddit_candidates("very specific phrase", "memes", limit=10)
        assert err is None
        assert len(cands) >= 1
        # Should have made TWO passes — one with query, one without
        queries_used = [c[1] for c in calls]
        assert "very specific phrase" in queries_used
        assert "" in queries_used

    def test_empty_query_no_fallback(self, monkeypatch):
        # When the model passes empty query, we shouldn't double-call
        post = self._make_post()
        calls = []
        def fake(sub, q, size):
            calls.append((sub, q))
            return [post], None
        monkeypatch.setattr(cm, "_query_pullpush_sub", fake)
        cands, err = cm._fetch_reddit_candidates("", "memes", limit=10)
        assert err is None
        assert len(cands) == 1
        # Default sub is just the one we passed; one call total
        assert len(calls) == 1

    def test_default_sub_blend_iterates_all(self, monkeypatch):
        post = self._make_post()
        calls = []
        def fake(sub, q, size):
            calls.append((sub, q))
            return [post], None
        monkeypatch.setattr(cm, "_query_pullpush_sub", fake)
        cands, err = cm._fetch_reddit_candidates("cat", None, limit=10)
        assert err is None
        # Should have called once per default sub
        subs_called = [c[0] for c in calls]
        assert set(subs_called) == set(cm._DEFAULT_SUBREDDITS)

    def test_all_subs_error_returns_error(self, monkeypatch):
        def fake(sub, q, size):
            return [], "503"
        monkeypatch.setattr(cm, "_query_pullpush_sub", fake)
        cands, err = cm._fetch_reddit_candidates("cat", "memes", limit=10)
        assert cands == []
        assert err is not None
        assert "failed" in err.lower()


# ── _vision_pick_meme graceful fallback ────────────────────────────────────

class TestVisionPickFallback:
    def test_no_anthropic_key(self):
        idx, why = cm._vision_pick_meme(
            anthropic_key="",
            candidates=[{"url": "x", "title": "y", "subreddit": "z"}],
            intent="i", voice_hint="v",
        )
        assert idx is None

    def test_no_candidates(self):
        idx, why = cm._vision_pick_meme(
            anthropic_key="k", candidates=[], intent="i", voice_hint="v",
        )
        assert idx is None

    def test_anthropic_exception_falls_back_to_top(self, monkeypatch):
        """When Anthropic raises (e.g. URL fetch error), we pick the top candidate."""
        candidates = [
            {"url": "https://i.redd.it/a.jpg", "title": "a", "subreddit": "memes"},
            {"url": "https://i.redd.it/b.jpg", "title": "b", "subreddit": "memes"},
        ]
        # Mock the Anthropic client to raise on messages.create
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = RuntimeError("BadRequestError: Unable to download")
        with patch.object(cm, "anthropic", create=True) as fake_anthropic_module:
            fake_anthropic_module.Anthropic = MagicMock(return_value=fake_client)
            idx, why = cm._vision_pick_meme(
                anthropic_key="k", candidates=candidates, intent="i", voice_hint="v",
            )
        assert idx == 0  # top-scored fallback
        assert "vision unavailable" in why.lower() or "fallback" in why.lower() or "picked" in why.lower()


# ── run_make_meme — creds missing ──────────────────────────────────────────

class TestRunMakeMemeCreds:
    def test_missing_username(self):
        r = cm.run_make_meme(
            imgflip_username="", imgflip_password="x",
            template_query="two buttons", top_text="a", bottom_text="b",
        )
        assert r["ok"] is False
        assert r["kind"] == "creds_missing"

    def test_missing_password(self):
        r = cm.run_make_meme(
            imgflip_username="x", imgflip_password="",
            template_query="two buttons", top_text="a", bottom_text="b",
        )
        assert r["ok"] is False
        assert r["kind"] == "creds_missing"

    def test_no_match_when_template_unknown(self, monkeypatch):
        # Mock the template fetch to return a known small list
        monkeypatch.setattr(cm, "_get_imgflip_templates", lambda: [
            {"id": "1", "name": "Two Buttons", "box_count": 2, "url": ""},
        ])
        r = cm.run_make_meme(
            imgflip_username="x", imgflip_password="x",
            template_query="zorklepuddle qwertyflop",
            top_text="a", bottom_text="b",
        )
        assert r["ok"] is False
        assert r["kind"] == "no_match"
        assert r["requested_template"] == "zorklepuddle qwertyflop"


# ── tool definition shapes ─────────────────────────────────────────────────

class TestToolSchemas:
    def test_make_meme_required_fields(self):
        s = cm.MAKE_MEME_TOOL["input_schema"]
        assert s["type"] == "object"
        assert "template" in s["required"]
        assert "top_text" in s["required"]
        assert "bottom_text" in s["required"]
        assert "reason" in s["required"]

    def test_find_meme_post_required_fields(self):
        s = cm.FIND_MEME_POST_TOOL["input_schema"]
        assert s["type"] == "object"
        assert "query" in s["required"]
        assert "intent" in s["required"]
        assert "reason" in s["required"]
        # subreddit is optional
        assert "subreddit" not in s["required"]
