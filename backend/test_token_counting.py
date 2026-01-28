#!/usr/bin/env python3
"""
Unit tests for token counting and context window management.
"""

import sys
import unittest

# Add the backend directory to path for imports
sys.path.insert(0, '/home/chatgpt/backend')

from main import (
    count_tokens,
    calculate_context_window,
    create_empty_stats,
)


class TestCountTokens(unittest.TestCase):
    """Test the token counting function."""

    def test_count_tokens_empty_string(self):
        """Empty string should have 0 tokens."""
        self.assertEqual(count_tokens(""), 0)

    def test_count_tokens_simple_text(self):
        """Simple text should have reasonable token count."""
        tokens = count_tokens("Hello, world!")
        self.assertGreater(tokens, 0)
        self.assertLess(tokens, 10)  # Should be around 3-4 tokens

    def test_count_tokens_longer_text(self):
        """Longer text should have more tokens."""
        short_text = "Hello"
        long_text = "Hello " * 100
        short_tokens = count_tokens(short_text)
        long_tokens = count_tokens(long_text)
        self.assertGreater(long_tokens, short_tokens)


class TestCalculateContextWindow(unittest.TestCase):
    """Test context window calculation with rolling window logic."""

    def make_message(self, role: str, content: str, total_tokens: int = None) -> dict:
        """Helper to create a message dict."""
        msg = {"role": role, "content": content}
        if total_tokens is not None:
            msg["total_tokens"] = total_tokens
        return msg

    def test_empty_messages_except_system_and_user(self):
        """With just system + user message, should return 1."""
        messages = [
            self.make_message("system", "You are helpful."),
            self.make_message("user", "Hello"),
        ]
        result = calculate_context_window(messages, threshold=1000, target=800)
        self.assertEqual(result, 1)

    def test_under_threshold_include_all(self):
        """Under threshold, all messages should be included (return 1)."""
        messages = [
            self.make_message("system", "System", total_tokens=10),
            self.make_message("user", "User1", total_tokens=10),
            self.make_message("assistant", "Asst1", total_tokens=10),
            self.make_message("user", "User2", total_tokens=10),
            self.make_message("assistant", "Asst2", total_tokens=10),
            self.make_message("user", "User3", total_tokens=10),  # New user message
        ]
        # Total = 60 tokens, well under 1000 threshold
        result = calculate_context_window(messages, threshold=1000, target=800)
        self.assertEqual(result, 1)

    def test_over_threshold_triggers_trimming(self):
        """Over threshold, should trim to target."""
        messages = [
            self.make_message("system", "System", total_tokens=100),
            self.make_message("user", "Old1", total_tokens=100),
            self.make_message("assistant", "Old1", total_tokens=100),
            self.make_message("user", "Old2", total_tokens=100),
            self.make_message("assistant", "Old2", total_tokens=100),
            self.make_message("user", "Recent", total_tokens=100),
            self.make_message("assistant", "Recent", total_tokens=100),
            self.make_message("user", "New", total_tokens=100),  # New user message
        ]
        # Total = 800 tokens, over 600 threshold
        # Base (system + new user) = 200, target = 400
        # Should include recent msgs until we hit 400
        result = calculate_context_window(messages, threshold=600, target=400)
        # Should start at index > 1 (cutting old messages)
        self.assertGreater(result, 1)

    def test_respects_user_boundary(self):
        """Should cut on user message boundaries, not in middle of exchange."""
        messages = [
            self.make_message("system", "System", total_tokens=100),
            self.make_message("user", "U1", total_tokens=50),
            self.make_message("assistant", "A1", total_tokens=50),
            self.make_message("user", "U2", total_tokens=50),
            self.make_message("assistant", "A2", total_tokens=50),
            self.make_message("user", "New", total_tokens=100),
        ]
        # Total = 400, threshold = 350, target = 250
        # Should cut such that we start on a user message
        result = calculate_context_window(messages, threshold=350, target=250)
        if result < len(messages):
            self.assertEqual(messages[result]["role"], "user")

    def test_handles_attached_files_in_fallback_counting(self):
        """When total_tokens is missing, should count attached files."""
        messages = [
            self.make_message("system", "System", total_tokens=10),
            {
                "role": "user",
                "content": "Check this file",
                "attached_files": [
                    {"filename": "test.py", "content": "print('hello world')"}
                ]
                # No total_tokens - should count content + files
            },
            self.make_message("assistant", "OK", total_tokens=5),
            self.make_message("user", "New msg", total_tokens=10),
        ]
        # Should not raise and should count file content
        result = calculate_context_window(messages, threshold=1000, target=800)
        self.assertEqual(result, 1)

    def test_handles_attached_files_in_new_user_message(self):
        """Should count attached files in the new user message."""
        messages = [
            self.make_message("system", "System", total_tokens=10),
            self.make_message("user", "Old msg", total_tokens=10),
            self.make_message("assistant", "Reply", total_tokens=10),
            {
                "role": "user",
                "content": "New message with file",
                "attached_files": [
                    {"filename": "large.txt", "content": "x" * 10000}
                ]
                # No total_tokens - should count content + large file
            },
        ]
        # Should complete without error
        result = calculate_context_window(messages, threshold=100000, target=80000)
        self.assertIsInstance(result, int)

    def test_zero_total_tokens_is_valid(self):
        """total_tokens=0 is a valid cached value, should not recalculate."""
        messages = [
            self.make_message("system", "System", total_tokens=10),
            self.make_message("user", "Empty response?", total_tokens=10),
            self.make_message("assistant", "", total_tokens=0),  # Empty response
            self.make_message("user", "New", total_tokens=10),
        ]
        # Should use the cached 0, not recalculate
        result = calculate_context_window(messages, threshold=1000, target=800)
        self.assertEqual(result, 1)


class TestCreateEmptyStats(unittest.TestCase):
    """Test the create_empty_stats function structure."""

    def test_has_all_required_fields(self):
        """Should have all required stats fields."""
        stats = create_empty_stats()
        required_fields = [
            "total_input_tokens",
            "total_cached_tokens",
            "total_output_tokens",
            "total_reasoning_tokens",
            "total_cost",
            "total_prompts",
            "first_prompt_date",
            "last_accessed",
        ]
        for field in required_fields:
            self.assertIn(field, stats, f"Missing required field: {field}")

    def test_token_fields_are_zero(self):
        """All token counts should start at 0."""
        stats = create_empty_stats()
        self.assertEqual(stats["total_input_tokens"], 0)
        self.assertEqual(stats["total_cached_tokens"], 0)
        self.assertEqual(stats["total_output_tokens"], 0)
        self.assertEqual(stats["total_reasoning_tokens"], 0)
        self.assertEqual(stats["total_prompts"], 0)

    def test_cost_is_zero(self):
        """Cost should start at 0.0."""
        stats = create_empty_stats()
        self.assertEqual(stats["total_cost"], 0.0)

    def test_dates_are_set(self):
        """Date fields should be set to valid ISO format strings."""
        stats = create_empty_stats()
        self.assertIsNotNone(stats["first_prompt_date"])
        self.assertIsNotNone(stats["last_accessed"])
        # Should be valid ISO date format
        self.assertRegex(stats["first_prompt_date"], r"\d{4}-\d{2}-\d{2}")


class TestTokenStringParsing(unittest.TestCase):
    """Test parsing of token strings like 'I:100 C:50 O:200 R:25 T:375'."""

    def parse_token_string(self, tokens_str: str) -> dict:
        """Parse a token string the same way get_user_stats does."""
        msg_input = msg_cached = msg_output = msg_reasoning = 0
        try:
            for part in tokens_str.split():
                if part.startswith("I:"):
                    msg_input = int(part[2:])
                elif part.startswith("C:"):
                    msg_cached = int(part[2:])
                elif part.startswith("O:"):
                    msg_output = int(part[2:])
                elif part.startswith("R:"):
                    msg_reasoning = int(part[2:])
        except (ValueError, AttributeError):
            msg_input = msg_cached = msg_output = msg_reasoning = 0
        return {
            "input": msg_input,
            "cached": msg_cached,
            "output": msg_output,
            "reasoning": msg_reasoning,
        }

    def test_parse_valid_token_string(self):
        """Parse a valid token string."""
        result = self.parse_token_string("I:1000 C:500 O:200 R:50 T:1750")
        self.assertEqual(result["input"], 1000)
        self.assertEqual(result["cached"], 500)
        self.assertEqual(result["output"], 200)
        self.assertEqual(result["reasoning"], 50)

    def test_parse_empty_string(self):
        """Empty string should result in zeros."""
        result = self.parse_token_string("")
        self.assertEqual(result["input"], 0)
        self.assertEqual(result["cached"], 0)
        self.assertEqual(result["output"], 0)
        self.assertEqual(result["reasoning"], 0)

    def test_parse_partial_string(self):
        """String with only some fields should parse those."""
        result = self.parse_token_string("I:100 O:50")
        self.assertEqual(result["input"], 100)
        self.assertEqual(result["cached"], 0)
        self.assertEqual(result["output"], 50)
        self.assertEqual(result["reasoning"], 0)

    def test_parse_malformed_string(self):
        """Malformed string should default to zeros."""
        result = self.parse_token_string("I:notanumber C:50")
        self.assertEqual(result["input"], 0)
        self.assertEqual(result["cached"], 0)
        self.assertEqual(result["output"], 0)
        self.assertEqual(result["reasoning"], 0)

    def test_parse_unknown_fields_ignored(self):
        """Unknown fields should be ignored."""
        result = self.parse_token_string("I:100 X:999 C:50 Z:888 O:25")
        self.assertEqual(result["input"], 100)
        self.assertEqual(result["cached"], 50)
        self.assertEqual(result["output"], 25)
        self.assertEqual(result["reasoning"], 0)


class TestProviderTokenCounting(unittest.TestCase):
    """Test provider-specific token counting methods."""

    def test_gpt_token_counting_uses_tiktoken(self):
        """OpenAI provider should use tiktoken for counting."""
        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()

        tokens = provider.count_tokens("Hello, world!")
        self.assertGreater(tokens, 0)
        self.assertIsInstance(tokens, int)

    def test_claude_estimation_method(self):
        """Claude provider should have character-based estimation."""
        from providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider()

        # count_tokens uses ~3.8 chars per token
        text = "a" * 380  # Should be ~100 tokens
        tokens = provider.count_tokens(text)
        self.assertGreater(tokens, 80)
        self.assertLess(tokens, 120)

    def test_claude_buffered_estimation(self):
        """Claude buffered estimation should be higher than regular."""
        from providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider()

        text = "a" * 1000
        regular = provider.count_tokens(text)
        buffered = provider.count_tokens_buffered(text)

        # Buffered should be ~15% higher
        self.assertGreater(buffered, regular)


if __name__ == "__main__":
    unittest.main(verbosity=2)
