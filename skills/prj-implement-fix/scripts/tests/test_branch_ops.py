#!/usr/bin/env python3
"""Unit tests for branch_ops.

The git seam (`_run_git`) is patched to capture argv without spawning git.
Tests assert:
  - slugify behaves on unicode / over-long inputs
  - branch name always carries the safe prefix
  - commit subject clamps to 72 chars
  - commit body REQUIRES Co-Authored-By trailer
  - extract_diff_from_fix_plan parses fenced diff/patch blocks
  - push_branch NEVER force-pushes; refuses unsafe branches
  - commit + push argv is exactly what we expect (deterministic)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import branch_ops  # noqa: E402


class TestPureHelpers(unittest.TestCase):
    def test_slugify_basic(self):
        self.assertEqual(branch_ops.slugify("Add Foo Bar"), "add-foo-bar")

    def test_slugify_unicode(self):
        self.assertEqual(branch_ops.slugify("naïve café"), "naive-cafe")

    def test_slugify_truncates_to_max(self):
        slug = branch_ops.slugify("a" * 200, max_len=30)
        self.assertEqual(len(slug), 30)
        self.assertEqual(slug, "a" * 30)

    def test_slugify_strips_trailing_hyphen_after_truncate(self):
        slug = branch_ops.slugify("xxxxx-" * 20, max_len=30)
        self.assertFalse(slug.endswith("-"))

    def test_slugify_empty_falls_back(self):
        self.assertEqual(branch_ops.slugify(""), "untitled")
        self.assertEqual(branch_ops.slugify("!!!"), "untitled")

    def test_build_branch_name_carries_safe_prefix(self):
        name = branch_ops.build_branch_name(42, "Fix the foo")
        self.assertTrue(name.startswith("prj/auto-fix/42-"))
        self.assertEqual(name, "prj/auto-fix/42-fix-the-foo")

    def test_build_branch_name_rejects_bad_pr_number(self):
        with self.assertRaises(branch_ops.GitOpError):
            branch_ops.build_branch_name(0, "x")
        with self.assertRaises(branch_ops.GitOpError):
            branch_ops.build_branch_name(-3, "x")

    def test_build_commit_subject_short(self):
        subj = branch_ops.build_commit_subject("fix the foo", 7)
        self.assertEqual(subj, "fix: fix the foo (re #7)")
        self.assertLessEqual(len(subj), 72)

    def test_build_commit_subject_clamps_long(self):
        long_summary = "x" * 200
        subj = branch_ops.build_commit_subject(long_summary, 7)
        self.assertLessEqual(len(subj), 72)
        self.assertTrue(subj.startswith("fix: "))
        self.assertTrue(subj.endswith(" (re #7)"))

    def test_build_commit_body_requires_claim_source(self):
        with self.assertRaises(branch_ops.GitOpError):
            branch_ops.build_commit_body("r", "k", "", 1)
        with self.assertRaises(branch_ops.GitOpError):
            branch_ops.build_commit_body("r", "k", "   ", 1)

    def test_build_commit_body_has_co_authored_by_and_refs(self):
        body = branch_ops.build_commit_body(
            "Test confirms the bug; minimal patch in src/foo.ts.",
            "Possible side-effect on cache layer.",
            "alice <alice@example.com>",
            42,
        )
        self.assertIn("Co-Authored-By: alice <alice@example.com>", body)
        self.assertIn("Refs: #42", body)
        # Lines wrapped to <= 72.
        for line in body.splitlines():
            self.assertLessEqual(len(line), 72, line)

    def test_extract_diff_from_fix_plan_finds_diff_fence(self):
        text = "## Plan\n\n```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n```\n"
        out = branch_ops.extract_diff_from_fix_plan(text)
        self.assertIn("--- a/x", out)
        self.assertIn("+2", out)

    def test_extract_diff_from_fix_plan_finds_patch_fence(self):
        text = "```patch\n--- a/y\n+++ b/y\n@@ -1 +1 @@\n-A\n+B\n```\n"
        out = branch_ops.extract_diff_from_fix_plan(text)
        self.assertIn("+B", out)

    def test_extract_diff_from_fix_plan_raises_when_absent(self):
        with self.assertRaises(branch_ops.GitOpError):
            branch_ops.extract_diff_from_fix_plan("no fence here")

    def test_extract_diff_from_fix_plan_raises_when_empty_block(self):
        with self.assertRaises(branch_ops.GitOpError):
            branch_ops.extract_diff_from_fix_plan("```diff\n\n```")


class TestGitSideEffects(unittest.TestCase):
    def setUp(self):
        self.calls: list[dict] = []

        def fake_run(args, cwd=None, env=None, stdin_text=None, timeout=120):
            self.calls.append({
                "args": list(args),
                "cwd": cwd,
                "env": env,
                "stdin": stdin_text,
            })
            # Default OK result; tests override for specific argv.
            return branch_ops.GitResult(0, "deadbeefcafe1234\n", "")

        self.patcher = patch.object(branch_ops, "_run_git", side_effect=fake_run)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_create_branch_refuses_unsafe_prefix(self):
        with self.assertRaises(branch_ops.GitOpError) as ctx:
            branch_ops.create_branch(Path("/tmp"), "wat", "HEAD")
        self.assertIn("refuse to create branch", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_create_branch_runs_checkout_b(self):
        branch_ops.create_branch(Path("/tmp"), "prj/auto-fix/1-foo", "HEAD")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            self.calls[0]["args"],
            ["checkout", "-b", "prj/auto-fix/1-foo", "HEAD"],
        )

    def test_apply_diff_calls_git_apply_index(self):
        branch_ops.apply_diff(Path("/tmp"), "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            self.calls[0]["args"],
            ["apply", "--index", "--whitespace=nowarn", "-"],
        )
        self.assertIn("+2", self.calls[0]["stdin"])

    def test_apply_diff_rejects_empty(self):
        with self.assertRaises(branch_ops.GitOpError):
            branch_ops.apply_diff(Path("/tmp"), "   \n")

    def test_commit_argv_has_no_amend_no_no_verify_no_gpgsign(self):
        body = "summary\n\nrationale.\n\nrisks.\n\nCo-Authored-By: x <x@y>\nRefs: #1\n"
        sha = branch_ops.commit_change(
            Path("/tmp"),
            subject="fix: x (re #1)",
            body=body,
            bot_user="prj-bot",
            bot_email="prj-bot@users.noreply.github.com",
            dry_run=False,
        )
        self.assertEqual(sha, "deadbeefcafe1234")
        # First call is the commit. Inspect argv.
        argv = self.calls[0]["args"]
        for forbidden in ("--amend", "--no-verify", "-S", "--gpg-sign"):
            self.assertNotIn(forbidden, argv, f"argv must not include {forbidden}")
        self.assertIn("commit", argv)
        # Bot identity is passed as -c overrides (not via git config writes).
        self.assertIn("-c", argv)
        self.assertIn("user.name=prj-bot", argv)
        self.assertIn("user.email=prj-bot@users.noreply.github.com", argv)
        # Stdin holds the commit message including Co-Authored-By.
        self.assertIn("Co-Authored-By:", self.calls[0]["stdin"])

    def test_commit_dry_run_makes_no_calls(self):
        body = "x\n\nCo-Authored-By: y <y@z>\nRefs: #1\n"
        sha = branch_ops.commit_change(
            Path("/tmp"), "fix: x (re #1)", body, "prj-bot", "p@x", dry_run=True
        )
        self.assertEqual(sha, "")
        self.assertEqual(self.calls, [])

    def test_commit_rejects_body_without_co_authored_by(self):
        with self.assertRaises(branch_ops.GitOpError):
            branch_ops.commit_change(
                Path("/tmp"),
                subject="fix: x (re #1)",
                body="no trailers here",
                bot_user="b",
                bot_email="b@x",
            )

    def test_push_branch_refuses_unsafe_branch(self):
        with self.assertRaises(branch_ops.GitOpError):
            branch_ops.push_branch(Path("/tmp"), "main")
        self.assertEqual(self.calls, [])

    def test_push_branch_argv_never_force(self):
        argv = branch_ops.push_branch(
            Path("/tmp"), "prj/auto-fix/9-foo", token=None, dry_run=False
        )
        self.assertNotIn("--force", argv)
        self.assertNotIn("--force-with-lease", argv)
        self.assertEqual(argv, ["push", "--set-upstream", "origin", "prj/auto-fix/9-foo"])
        # And it actually ran with that argv.
        self.assertEqual(self.calls[0]["args"], argv)

    def test_push_branch_passes_token_via_env(self):
        branch_ops.push_branch(
            Path("/tmp"), "prj/auto-fix/3-x", token="ghp_xxx", dry_run=False
        )
        self.assertEqual(self.calls[0]["env"], {"GITHUB_TOKEN": "ghp_xxx"})

    def test_push_branch_dry_run_returns_argv_no_call(self):
        argv = branch_ops.push_branch(
            Path("/tmp"), "prj/auto-fix/5-bar", dry_run=True
        )
        self.assertEqual(argv, ["push", "--set-upstream", "origin", "prj/auto-fix/5-bar"])
        self.assertEqual(self.calls, [])


class TestRunTests(unittest.TestCase):
    def test_run_tests_skipped_with_no_command(self):
        result = branch_ops.run_tests(Path("/tmp"), None)
        self.assertEqual(result["status"], "skipped")
        result2 = branch_ops.run_tests(Path("/tmp"), "")
        self.assertEqual(result2["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
