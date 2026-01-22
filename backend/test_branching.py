#!/usr/bin/env python3
"""
Test script for branching functionality.
Tests the tree utility functions without requiring a running server.
"""

import sys
import unittest

# Add the backend directory to path for imports
sys.path.insert(0, '/home/user/chatgpt-api/backend')

from main import (
    generate_message_id,
    build_message_index,
    get_path_to_root,
    get_children,
    get_siblings,
    get_deepest_leaf,
    is_migrated,
    migrate_chat_inline,
)


class TestBranchingFunctions(unittest.TestCase):
    """Test tree utility functions."""

    def setUp(self):
        """Create a sample tree structure for testing."""
        # Tree structure:
        #   system (root)
        #     └── user1
        #           └── asst1
        #                 ├── user2a (branch A)
        #                 │     └── asst2a
        #                 └── user2b (branch B)
        #                       └── asst2b
        #                             └── user3b
        #                                   └── asst3b

        self.system_id = "sys-001"
        self.user1_id = "user-001"
        self.asst1_id = "asst-001"
        self.user2a_id = "user-002a"
        self.asst2a_id = "asst-002a"
        self.user2b_id = "user-002b"
        self.asst2b_id = "asst-002b"
        self.user3b_id = "user-003b"
        self.asst3b_id = "asst-003b"

        self.messages = [
            {"id": self.system_id, "parent_id": None, "role": "system", "content": "You are helpful.", "timestamp": "2024-01-01T00:00:00"},
            {"id": self.user1_id, "parent_id": self.system_id, "role": "user", "content": "Hello", "timestamp": "2024-01-01T00:01:00"},
            {"id": self.asst1_id, "parent_id": self.user1_id, "role": "assistant", "content": "Hi!", "timestamp": "2024-01-01T00:02:00"},
            {"id": self.user2a_id, "parent_id": self.asst1_id, "role": "user", "content": "Branch A", "timestamp": "2024-01-01T00:03:00"},
            {"id": self.asst2a_id, "parent_id": self.user2a_id, "role": "assistant", "content": "Reply A", "timestamp": "2024-01-01T00:04:00"},
            {"id": self.user2b_id, "parent_id": self.asst1_id, "role": "user", "content": "Branch B", "timestamp": "2024-01-01T00:05:00"},
            {"id": self.asst2b_id, "parent_id": self.user2b_id, "role": "assistant", "content": "Reply B", "timestamp": "2024-01-01T00:06:00"},
            {"id": self.user3b_id, "parent_id": self.asst2b_id, "role": "user", "content": "Continue B", "timestamp": "2024-01-01T00:07:00"},
            {"id": self.asst3b_id, "parent_id": self.user3b_id, "role": "assistant", "content": "Reply B2", "timestamp": "2024-01-01T00:08:00"},
        ]

    def test_build_message_index(self):
        """Test building a message index."""
        index = build_message_index(self.messages)
        self.assertEqual(len(index), 9)
        self.assertEqual(index[self.user1_id]["content"], "Hello")
        self.assertEqual(index[self.asst2b_id]["content"], "Reply B")

    def test_get_path_to_root_branch_a(self):
        """Test getting path to leaf in branch A."""
        path = get_path_to_root(self.messages, self.asst2a_id)
        self.assertEqual(len(path), 5)
        self.assertEqual(path[0]["id"], self.system_id)
        self.assertEqual(path[1]["id"], self.user1_id)
        self.assertEqual(path[2]["id"], self.asst1_id)
        self.assertEqual(path[3]["id"], self.user2a_id)
        self.assertEqual(path[4]["id"], self.asst2a_id)

    def test_get_path_to_root_branch_b(self):
        """Test getting path to leaf in branch B."""
        path = get_path_to_root(self.messages, self.asst3b_id)
        self.assertEqual(len(path), 7)
        self.assertEqual(path[0]["id"], self.system_id)
        self.assertEqual(path[-1]["id"], self.asst3b_id)
        # Branch B path should not include branch A messages
        path_ids = [m["id"] for m in path]
        self.assertNotIn(self.user2a_id, path_ids)
        self.assertNotIn(self.asst2a_id, path_ids)

    def test_get_path_to_root_not_found(self):
        """Test getting path for non-existent message."""
        path = get_path_to_root(self.messages, "nonexistent")
        self.assertEqual(path, [])

    def test_get_path_to_root_empty_messages(self):
        """Test getting path from empty messages list."""
        path = get_path_to_root([], "any-id")
        self.assertEqual(path, [])

    def test_get_children(self):
        """Test getting children of a message."""
        # System message has one child (user1)
        children = get_children(self.messages, self.system_id)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["id"], self.user1_id)

        # asst1 has two children (user2a and user2b - the branch point)
        children = get_children(self.messages, self.asst1_id)
        self.assertEqual(len(children), 2)
        child_ids = {c["id"] for c in children}
        self.assertEqual(child_ids, {self.user2a_id, self.user2b_id})

    def test_get_siblings(self):
        """Test getting siblings of a message."""
        # user2a and user2b are siblings (both children of asst1)
        siblings = get_siblings(self.messages, self.user2a_id)
        self.assertEqual(len(siblings), 2)
        sibling_ids = {s["id"] for s in siblings}
        self.assertEqual(sibling_ids, {self.user2a_id, self.user2b_id})

        # Same result when querying from user2b
        siblings_b = get_siblings(self.messages, self.user2b_id)
        self.assertEqual(len(siblings_b), 2)

        # Messages with no siblings return list with just themselves
        siblings_user1 = get_siblings(self.messages, self.user1_id)
        self.assertEqual(len(siblings_user1), 1)
        self.assertEqual(siblings_user1[0]["id"], self.user1_id)

    def test_get_siblings_sorted_by_timestamp(self):
        """Test that siblings are sorted by timestamp."""
        siblings = get_siblings(self.messages, self.user2a_id)
        # user2a (00:03) should come before user2b (00:05)
        self.assertEqual(siblings[0]["id"], self.user2a_id)
        self.assertEqual(siblings[1]["id"], self.user2b_id)

    def test_get_deepest_leaf_branch_a(self):
        """Test finding deepest leaf starting from a message in branch A."""
        # Starting from user2a, deepest leaf is asst2a
        leaf = get_deepest_leaf(self.messages, self.user2a_id)
        self.assertEqual(leaf, self.asst2a_id)

    def test_get_deepest_leaf_branch_b(self):
        """Test finding deepest leaf starting from a message in branch B."""
        # Starting from user2b, deepest leaf is asst3b
        leaf = get_deepest_leaf(self.messages, self.user2b_id)
        self.assertEqual(leaf, self.asst3b_id)

    def test_get_deepest_leaf_from_branch_point(self):
        """Test finding deepest leaf from the branch point."""
        # Starting from asst1 (the branch point), should go to deepest of most recent branch
        # user2b is more recent than user2a, so should go down branch B
        leaf = get_deepest_leaf(self.messages, self.asst1_id)
        self.assertEqual(leaf, self.asst3b_id)

    def test_get_deepest_leaf_from_leaf(self):
        """Test that deepest leaf of a leaf is itself."""
        leaf = get_deepest_leaf(self.messages, self.asst2a_id)
        self.assertEqual(leaf, self.asst2a_id)

    def test_is_migrated_true(self):
        """Test is_migrated returns True for migrated chat."""
        data = {"messages": self.messages}
        self.assertTrue(is_migrated(data))

    def test_is_migrated_false(self):
        """Test is_migrated returns False for unmigrated chat."""
        data = {"messages": [{"role": "system", "content": "Hi"}]}
        self.assertFalse(is_migrated(data))

    def test_is_migrated_empty(self):
        """Test is_migrated returns False for empty chat."""
        data = {"messages": []}
        self.assertFalse(is_migrated(data))

    def test_migrate_chat_inline(self):
        """Test inline migration of a linear chat."""
        linear_messages = [
            {"role": "system", "content": "System prompt", "timestamp": "2024-01-01T00:00:00"},
            {"role": "user", "content": "Hello", "timestamp": "2024-01-01T00:01:00"},
            {"role": "assistant", "content": "Hi!", "timestamp": "2024-01-01T00:02:00"},
        ]
        data = {"messages": linear_messages}

        result = migrate_chat_inline(data)

        # All messages should have IDs
        for msg in result["messages"]:
            self.assertIn("id", msg)
            self.assertIn("parent_id", msg)

        # Chain should be correct
        self.assertIsNone(result["messages"][0]["parent_id"])  # System has no parent
        self.assertEqual(result["messages"][1]["parent_id"], result["messages"][0]["id"])
        self.assertEqual(result["messages"][2]["parent_id"], result["messages"][1]["id"])

        # current_leaf_id should be set to last message
        self.assertEqual(result["current_leaf_id"], result["messages"][-1]["id"])

    def test_migrate_chat_inline_idempotent(self):
        """Test that migration is idempotent."""
        data = {"messages": self.messages.copy()}
        original_ids = [m["id"] for m in data["messages"]]

        result = migrate_chat_inline(data)

        # IDs should be unchanged (already migrated)
        result_ids = [m["id"] for m in result["messages"]]
        self.assertEqual(original_ids, result_ids)


class TestEdgeCases(unittest.TestCase):
    """Test edge cases and error handling."""

    def test_empty_messages(self):
        """Test functions handle empty messages list."""
        self.assertEqual(build_message_index([]), {})
        self.assertEqual(get_path_to_root([], "any"), [])
        self.assertEqual(get_children([], "any"), [])
        self.assertEqual(get_siblings([], "any"), [])
        self.assertEqual(get_deepest_leaf([], "any"), "any")

    def test_messages_without_ids(self):
        """Test functions handle messages without IDs gracefully."""
        messages = [
            {"role": "system", "content": "Hi"},  # No id
            {"id": "user-1", "parent_id": None, "role": "user", "content": "Hello"},
        ]
        index = build_message_index(messages)
        self.assertEqual(len(index), 1)  # Only the one with id
        self.assertIn("user-1", index)

    def test_generate_message_id_unique(self):
        """Test that generated IDs are unique."""
        ids = {generate_message_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)  # All unique


if __name__ == "__main__":
    unittest.main(verbosity=2)
