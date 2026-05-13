#!/usr/bin/env python3
"""Unit tests for worktree.py.

Tests patch the single subprocess seam `_run` so no real `gh` or `git`
invocations leave the process.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worktree  # noqa: E402


class TestWorktreePath(unittest.TestCase):
    def test_path_is_deterministic(self):
        root = Path("/tmp/proj")
        p = worktree.worktree_path(root, 101)
        self.assertEqual(
            p,
            Path("/tmp/proj/_bmad-output/pr-workflow/worktrees/101"),
        )

    def test_zero_pr_number_raises(self):
        with self.assertRaises(worktree.WorktreeError):
            worktree.worktree_path(Path("/tmp/proj"), 0)

    def test_negative_pr_number_raises(self):
        with self.assertRaises(worktree.WorktreeError):
            worktree.worktree_path(Path("/tmp/proj"), -1)


class TestProvisionWorktree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_requires_non_empty_repo(self):
        with self.assertRaises(worktree.WorktreeError):
            worktree.provision_worktree(self.root, 1, "")

    def test_creates_worktree_via_gh_pr_checkout(self):
        """When the target does not exist, gh pr checkout is run and the
        resulting checkout directory is renamed to the canonical PR-number
        path."""
        target = worktree.worktree_path(self.root, 42)
        parent = target.parent

        def fake_run(cmd, cwd=None, timeout=120):
            # Simulate `gh pr checkout` creating a head-branch-named directory
            # under the parent.
            self.assertEqual(cmd[:3], ["gh", "pr", "checkout"])
            self.assertEqual(cmd[3], "42")
            self.assertIn("--repo", cmd)
            self.assertIn("--force", cmd)
            parent.mkdir(parents=True, exist_ok=True)
            checkout_dir = parent / "feature-foo"
            checkout_dir.mkdir()
            (checkout_dir / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
            return (0, "", "")

        with patch.object(worktree, "_run", side_effect=fake_run):
            result = worktree.provision_worktree(self.root, 42, "octocat/hello")

        self.assertEqual(result.pr_number, 42)
        self.assertTrue(result.created)
        self.assertFalse(result.reused)
        self.assertEqual(result.path, target)
        self.assertTrue(target.exists())
        self.assertTrue((target / ".git").exists())

    def test_isolation_path_is_per_pr(self):
        """Two different PRs land in different worktree paths."""

        def fake_run(cmd, cwd=None, timeout=120):
            number = cmd[3]
            parent = Path(cwd)
            checkout = parent / f"feat-{number}"
            checkout.mkdir(parents=True, exist_ok=True)
            (checkout / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
            return (0, "", "")

        with patch.object(worktree, "_run", side_effect=fake_run):
            r1 = worktree.provision_worktree(self.root, 1, "o/r")
            r2 = worktree.provision_worktree(self.root, 2, "o/r")

        self.assertNotEqual(r1.path, r2.path)
        self.assertTrue(r1.path.exists())
        self.assertTrue(r2.path.exists())

    def test_reuse_existing_worktree(self):
        target = worktree.worktree_path(self.root, 7)
        target.mkdir(parents=True)
        (target / ".git").write_text("ok", encoding="utf-8")

        # Should not call _run when target exists and refresh is False.
        with patch.object(worktree, "_run", side_effect=AssertionError("should not run")):
            result = worktree.provision_worktree(self.root, 7, "o/r")

        self.assertTrue(result.reused)
        self.assertFalse(result.refreshed)
        self.assertFalse(result.created)

    def test_refresh_existing_worktree(self):
        target = worktree.worktree_path(self.root, 7)
        target.mkdir(parents=True)
        (target / ".git").write_text("ok", encoding="utf-8")

        called = []

        def fake_run(cmd, cwd=None, timeout=120):
            called.append(cmd)
            return (0, "Already up to date.\n", "")

        with patch.object(worktree, "_run", side_effect=fake_run):
            result = worktree.provision_worktree(
                self.root, 7, "o/r", refresh=True,
            )

        self.assertTrue(result.reused)
        self.assertTrue(result.refreshed)
        self.assertEqual(called[0][:2], ["git", "fetch"])

    def test_existing_directory_without_git_raises(self):
        target = worktree.worktree_path(self.root, 9)
        target.mkdir(parents=True)  # exists but no .git inside
        with self.assertRaises(worktree.WorktreeError):
            worktree.provision_worktree(self.root, 9, "o/r")

    def test_gh_nonzero_exit_raises(self):
        def fake_run(cmd, cwd=None, timeout=120):
            return (1, "", "could not authenticate")

        with patch.object(worktree, "_run", side_effect=fake_run):
            with self.assertRaises(worktree.WorktreeError) as ctx:
                worktree.provision_worktree(self.root, 5, "o/r")
        self.assertIn("could not authenticate", str(ctx.exception))


class TestCleanupWorktree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cleanup_absent_is_noop(self):
        result = worktree.cleanup_worktree(self.root, 99)
        self.assertEqual(result["status"], "absent")

    def test_cleanup_removes_valid_worktree(self):
        target = worktree.worktree_path(self.root, 11)
        target.mkdir(parents=True)
        (target / ".git").write_text("ok", encoding="utf-8")
        (target / "README.md").write_text("contents", encoding="utf-8")
        result = worktree.cleanup_worktree(self.root, 11)
        self.assertEqual(result["status"], "removed")
        self.assertFalse(target.exists())

    def test_cleanup_skips_non_worktree_without_force(self):
        target = worktree.worktree_path(self.root, 12)
        target.mkdir(parents=True)
        (target / "file.txt").write_text("not a worktree", encoding="utf-8")
        result = worktree.cleanup_worktree(self.root, 12)
        self.assertEqual(result["status"], "skipped-not-a-worktree")
        self.assertTrue(target.exists())

    def test_cleanup_force_removes_anything(self):
        target = worktree.worktree_path(self.root, 13)
        target.mkdir(parents=True)
        (target / "file.txt").write_text("data", encoding="utf-8")
        result = worktree.cleanup_worktree(self.root, 13, force=True)
        self.assertEqual(result["status"], "removed")
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
