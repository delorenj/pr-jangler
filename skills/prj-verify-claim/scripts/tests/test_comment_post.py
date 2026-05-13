#!/usr/bin/env python3
"""Unit tests for comment_post.py.

Template-rendering tests are pure. Posting tests patch the subprocess seam.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import comment_post  # noqa: E402


class TestBuildOpening(unittest.TestCase):
    def test_default_role_is_contributor(self):
        text = comment_post.build_opening("alice")
        self.assertIn("@alice", text)
        self.assertIn("thanks", text.lower())

    def test_first_timer_role_warmer(self):
        text = comment_post.build_opening("newbie", role="first-timer")
        self.assertIn("welcome", text.lower())

    def test_maintainer_role_brief(self):
        text = comment_post.build_opening("ada", role="maintainer")
        self.assertIn("@ada", text)
        self.assertIn("thanks for flagging", text.lower())

    def test_strips_at_prefix(self):
        text = comment_post.build_opening("@bob")
        self.assertIn("@bob", text)
        self.assertNotIn("@@bob", text)

    def test_empty_commenter_uses_fallback(self):
        text = comment_post.build_opening("")
        self.assertIn("@there", text)


class TestRenderPushback(unittest.TestCase):
    def _render(self, **overrides):
        kwargs = dict(
            claim="Calling X with empty args breaks the world",
            commenter="alice",
            strategy="failing-test",
            worktree="_bmad-output/pr-workflow/worktrees/42",
            command="bun test",
            observation="All 14 tests passed; the asserted breakage was not visible.",
            next_step=None,
            role="contributor",
            bot_signature=None,
        )
        kwargs.update(overrides)
        return comment_post.render_pushback(**kwargs)

    def test_substitution_includes_all_required_values(self):
        body = self._render()
        for needle in (
            "@alice",
            "failing-test",
            "_bmad-output/pr-workflow/worktrees/42",
            "bun test",
            "Calling X with empty args breaks the world",
            "All 14 tests passed",
        ):
            self.assertIn(needle, body, f"missing {needle!r} in body")

    def test_strips_html_comment_block(self):
        body = self._render()
        # The template's documentation block should not bleed into the
        # rendered output.
        self.assertNotIn("<!--", body)
        self.assertNotIn("-->", body)
        self.assertNotIn("Rendered by scripts/comment_post.py", body)

    def test_includes_bot_signature_by_default(self):
        body = self._render()
        self.assertIn("PR Jangler bot", body)

    def test_custom_bot_signature_respected(self):
        body = self._render(bot_signature="_signed by the agent_")
        self.assertIn("_signed by the agent_", body)
        self.assertNotIn("PR Jangler bot", body)

    def test_custom_next_step_respected(self):
        body = self._render(next_step="Could you attach the failing test log?")
        self.assertIn("Could you attach the failing test log?", body)

    def test_truncates_long_claims(self):
        long_claim = "x" * 600
        body = self._render(claim=long_claim)
        self.assertIn("…", body)
        # The full 600-char string must not be present verbatim.
        self.assertNotIn(long_claim, body)

    def test_first_timer_opening_in_body(self):
        body = self._render(role="first-timer")
        self.assertIn("welcome", body.lower())


class TestPostPrComment(unittest.TestCase):
    def test_constructs_gh_pr_comment_args(self):
        captured = {}

        def fake(args, stdin_text, timeout=30):
            captured["args"] = args
            captured["stdin"] = stdin_text
            return (0, "https://github.com/x/y/issues/1#c123\n", "")

        with patch.object(comment_post, "_run_gh_with_stdin", side_effect=fake):
            result = comment_post.post_pr_comment(
                "octocat/hello", 42, "hi there",
            )

        self.assertEqual(result["status"], "posted")
        self.assertEqual(result["repo"], "octocat/hello")
        self.assertEqual(result["pr_number"], 42)
        # gh argv shape
        self.assertEqual(captured["args"][:3], ["pr", "comment", "42"])
        self.assertIn("--repo", captured["args"])
        repo_idx = captured["args"].index("--repo")
        self.assertEqual(captured["args"][repo_idx + 1], "octocat/hello")
        self.assertIn("--body-file", captured["args"])
        bf_idx = captured["args"].index("--body-file")
        self.assertEqual(captured["args"][bf_idx + 1], "-")
        self.assertEqual(captured["stdin"], "hi there")

    def test_empty_body_raises(self):
        with self.assertRaises(comment_post.CommentError):
            comment_post.post_pr_comment("o/r", 1, "   ")

    def test_empty_repo_raises(self):
        with self.assertRaises(comment_post.CommentError):
            comment_post.post_pr_comment("", 1, "x")

    def test_zero_pr_raises(self):
        with self.assertRaises(comment_post.CommentError):
            comment_post.post_pr_comment("o/r", 0, "x")

    def test_nonzero_rc_raises(self):
        def fake(args, stdin_text, timeout=30):
            return (1, "", "auth required")

        with patch.object(comment_post, "_run_gh_with_stdin", side_effect=fake):
            with self.assertRaises(comment_post.CommentError) as ctx:
                comment_post.post_pr_comment("o/r", 5, "hello")
        self.assertIn("auth required", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
