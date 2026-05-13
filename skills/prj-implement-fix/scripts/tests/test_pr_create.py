#!/usr/bin/env python3
"""Unit tests for pr_create.

The gh seam (`_run_gh`) is patched to inject scripted return values. Tests assert:
  - happy path: `gh pr create` succeeds -> outcome.path == 'tier-2-pr'
  - contributor veto stderr -> falls back to `gh pr comment`
  - non-fallback failure -> raises GhCreateError
  - ensure_label + apply_label call the right argv
  - dry-run short-circuits with no subprocess calls
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import pr_create  # noqa: E402


class _ScriptedGh:
    """Callable that replays a queued list of (rc, stdout, stderr) tuples by argv match."""

    def __init__(self, plan: list[tuple[str, tuple[int, str, str]]]):
        # plan items: (substring_match_in_argv, (rc, stdout, stderr))
        self.plan = list(plan)
        self.calls: list[dict] = []

    def __call__(self, args, timeout: int = 60, stdin_text=None):
        self.calls.append({"args": list(args), "stdin": stdin_text, "timeout": timeout})
        joined = " ".join(args)
        for marker, result in self.plan:
            if marker in joined:
                return result
        # Default: success
        return (0, "ok\n", "")


class TestCreateFixPr(unittest.TestCase):
    def setUp(self):
        self.body = Path("/tmp/pj_body_test.md")
        self.body.write_text("body", encoding="utf-8")

    def tearDown(self):
        if self.body.exists():
            self.body.unlink()

    def _run_with_gh(self, scripted: _ScriptedGh, **kwargs):
        with patch.object(pr_create, "_run_gh", side_effect=scripted):
            return pr_create.create_fix_pr(
                repo="octocat/hello",
                base_branch="contrib/feature-x",
                head_branch="prj/auto-fix/42-fix-foo",
                title="[prj] fix foo (re #42)",
                body_path=self.body,
                pr_number=42,
                fix_plan_diff="--- a\n+++ b\n",
                dry_run=False,
                **kwargs,
            )

    def test_dry_run_skips_subprocess(self):
        scripted = _ScriptedGh([])
        with patch.object(pr_create, "_run_gh", side_effect=scripted):
            outcome = pr_create.create_fix_pr(
                repo="octocat/hello",
                base_branch="contrib/feature-x",
                head_branch="prj/auto-fix/42-fix-foo",
                title="[prj] fix foo (re #42)",
                body_path=self.body,
                pr_number=42,
                fix_plan_diff="x",
                dry_run=True,
            )
        self.assertEqual(outcome.path, "tier-2-pr")
        self.assertEqual(scripted.calls, [])

    def test_happy_path_pr_create_succeeds(self):
        scripted = _ScriptedGh([
            ("pr create", (0, "https://github.com/octocat/hello/pull/99\n", "")),
            # ensure_label + apply_label go through too -- default success.
        ])
        outcome = self._run_with_gh(scripted)
        self.assertEqual(outcome.path, "tier-2-pr")
        self.assertEqual(outcome.url, "https://github.com/octocat/hello/pull/99")
        # Verify argv shape on the pr-create call.
        first = scripted.calls[0]["args"]
        self.assertEqual(first[0:2], ["pr", "create"])
        self.assertIn("--base", first)
        self.assertEqual(first[first.index("--base") + 1], "contrib/feature-x")
        self.assertIn("--head", first)
        self.assertEqual(first[first.index("--head") + 1], "prj/auto-fix/42-fix-foo")
        # Label calls happened (ensure-label uses `label create`, apply uses `pr edit`).
        argv_joined = [" ".join(c["args"]) for c in scripted.calls]
        self.assertTrue(any("label create" in s for s in argv_joined))
        self.assertTrue(any("pr edit 42" in s for s in argv_joined))

    def test_contributor_veto_falls_back_to_comment(self):
        scripted = _ScriptedGh([
            ("pr create", (1, "", "Error: branch is protected and PR cannot be opened")),
            ("pr comment", (0, "https://github.com/octocat/hello/pull/42#issuecomment-1\n", "")),
        ])
        outcome = self._run_with_gh(scripted)
        self.assertEqual(outcome.path, "tier-1-comment-fallback")
        self.assertEqual(outcome.fallback_reason, "branch is protected")
        self.assertIn("issuecomment", outcome.url)

        # Body posted to comment should embed a ```diff fence
        comment_call = next(
            c for c in scripted.calls if "pr comment" in " ".join(c["args"])
        )
        self.assertIn("```diff", comment_call["stdin"])
        self.assertIn("PR Jangler: fix proposed inline", comment_call["stdin"])

    def test_non_fallback_failure_raises(self):
        scripted = _ScriptedGh([
            ("pr create", (1, "", "Error: validation failed (totally unrelated)")),
        ])
        with self.assertRaises(pr_create.GhCreateError):
            self._run_with_gh(scripted)

    def test_fallback_failure_also_raises(self):
        scripted = _ScriptedGh([
            ("pr create", (1, "", "Error: head and base ref are the same")),
            ("pr comment", (1, "", "Error: ratelimited")),
        ])
        with self.assertRaises(pr_create.GhCreateError):
            self._run_with_gh(scripted)


class TestLabelOps(unittest.TestCase):
    def test_ensure_label_argv(self):
        scripted = _ScriptedGh([])
        with patch.object(pr_create, "_run_gh", side_effect=scripted):
            pr_create.ensure_label("octocat/hello")
        self.assertEqual(len(scripted.calls), 1)
        argv = scripted.calls[0]["args"]
        self.assertEqual(argv[0:3], ["label", "create", "prj/fix-proposed"])
        self.assertIn("--force", argv)

    def test_apply_label_argv_uses_pr_edit(self):
        scripted = _ScriptedGh([])
        with patch.object(pr_create, "_run_gh", side_effect=scripted):
            pr_create.apply_label("octocat/hello", 7)
        argv = scripted.calls[0]["args"]
        self.assertEqual(argv[0:3], ["pr", "edit", "7"])
        self.assertIn("--add-label", argv)
        self.assertEqual(argv[argv.index("--add-label") + 1], "prj/fix-proposed")

    def test_apply_label_failure_is_warning_only(self):
        # If the gh seam returns non-zero, apply_label must NOT raise.
        scripted = _ScriptedGh([("pr edit", (1, "", "boom"))])
        with patch.object(pr_create, "_run_gh", side_effect=scripted):
            pr_create.apply_label("octocat/hello", 7)  # must not raise


if __name__ == "__main__":
    unittest.main()
