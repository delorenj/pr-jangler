#!/usr/bin/env python3
"""Unit tests for github_post.py.

Crucial invariants:
- prj_post_review_comment=False must NOT invoke gh.
- enabled=True must invoke `gh pr review --comment --body-file ...`.
- --approve and --request-changes are NEVER passed.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github_post  # noqa: E402


class TestPostReviewComment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.body = Path(self.tmp.name) / "review.md"
        self.body.write_text("# Review\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_flag_skips_gh_entirely(self):
        called = {"count": 0}

        def fake(args, timeout=60):
            called["count"] += 1
            return ""

        with patch.object(github_post, "_run_gh", side_effect=fake):
            result = github_post.post_review_comment(
                repo="octocat/hello",
                pr_number=1,
                body_file=self.body,
                enabled=False,
            )
        self.assertEqual(called["count"], 0)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["pr_number"], 1)
        self.assertIn("prj_post_review_comment=False", result["reason"])

    def test_enabled_invokes_gh_with_comment_and_body_file(self):
        captured = {}

        def fake(args, timeout=60):
            captured["args"] = list(args)
            return "Review posted\n"

        with patch.object(github_post, "_run_gh", side_effect=fake):
            result = github_post.post_review_comment(
                repo="octocat/hello",
                pr_number=42,
                body_file=self.body,
                enabled=True,
            )

        self.assertEqual(result["status"], "posted")
        self.assertEqual(captured["args"][:2], ["pr", "review"])
        self.assertIn("--comment", captured["args"])
        self.assertIn("--body-file", captured["args"])
        idx = captured["args"].index("--body-file")
        self.assertEqual(captured["args"][idx + 1], str(self.body))
        # PR number positional argument
        self.assertIn("42", captured["args"])
        # Repo flag
        repo_idx = captured["args"].index("--repo")
        self.assertEqual(captured["args"][repo_idx + 1], "octocat/hello")

    def test_enabled_never_passes_approve_or_request_changes(self):
        captured = {}

        def fake(args, timeout=60):
            captured["args"] = list(args)
            return ""

        with patch.object(github_post, "_run_gh", side_effect=fake):
            github_post.post_review_comment(
                repo="octocat/hello",
                pr_number=42,
                body_file=self.body,
                enabled=True,
            )
        forbidden = {"--approve", "--request-changes", "-a", "-r"}
        self.assertEqual(
            forbidden.intersection(captured["args"]),
            set(),
            "github_post must never pass approve/request-changes flags",
        )

    def test_defensive_run_gh_blocks_forbidden_flags(self):
        # If someone in the future tries to add --approve via a code path we
        # missed, the seam itself refuses.
        with self.assertRaises(github_post.GhPostError) as ctx:
            github_post._run_gh(["pr", "review", "1", "--approve"])
        self.assertIn("forbidden", str(ctx.exception).lower())

    def test_enabled_missing_body_file_raises(self):
        missing = Path(self.tmp.name) / "no-such-file.md"
        with self.assertRaises(github_post.GhPostError) as ctx:
            github_post.post_review_comment(
                repo="octocat/hello",
                pr_number=1,
                body_file=missing,
                enabled=True,
            )
        self.assertIn("does not exist", str(ctx.exception))

    def test_enabled_missing_repo_raises(self):
        with self.assertRaises(github_post.GhPostError):
            github_post.post_review_comment(
                repo="",
                pr_number=1,
                body_file=self.body,
                enabled=True,
            )

    def test_disabled_with_missing_repo_still_skips(self):
        # The flag short-circuits before any repo / body validation.
        result = github_post.post_review_comment(
            repo="",
            pr_number=1,
            body_file=Path(self.tmp.name) / "missing.md",
            enabled=False,
        )
        self.assertEqual(result["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
