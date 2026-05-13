#!/usr/bin/env python3
"""Unit tests for worktree.ensure_worktree.

Covers:
  - reuse existing worktree (no gh checkout invoked)
  - reuse + branch mismatch raises WorktreeError
  - provision fresh worktree via injected runner
  - provision failure cleans up
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import worktree as worktree_mod  # noqa: E402


def _fake_completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


class TestEnsureWorktreeReuse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "_bmad").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _prestage_worktree(self, pr_number: int) -> Path:
        wt = worktree_mod.worktree_path(self.root, pr_number)
        wt.mkdir(parents=True)
        # Drop a fake .git marker so _is_usable_worktree returns True.
        (wt / ".git").write_text("gitdir: /tmp/fake", encoding="utf-8")
        return wt

    def test_reuses_existing_worktree(self):
        wt = self._prestage_worktree(101)
        # Patch _current_branch to avoid invoking real git.
        with patch.object(worktree_mod, "_current_branch", return_value="feat/x"):
            result = worktree_mod.ensure_worktree(self.root, 101)
        self.assertTrue(result.reused)
        self.assertEqual(result.path, wt)
        self.assertEqual(result.branch, "feat/x")

    def test_reuse_branch_mismatch_raises(self):
        self._prestage_worktree(101)
        with patch.object(worktree_mod, "_current_branch", return_value="wrong"):
            with self.assertRaises(worktree_mod.WorktreeError):
                worktree_mod.ensure_worktree(
                    self.root, 101, expected_branch="feat/x"
                )

    def test_reuse_no_branch_required_returns_ok(self):
        self._prestage_worktree(101)
        with patch.object(worktree_mod, "_current_branch", return_value="any"):
            result = worktree_mod.ensure_worktree(self.root, 101)
        self.assertTrue(result.reused)


class TestEnsureWorktreeProvision(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "_bmad").mkdir()
        # Pretend the project root is a git repo.
        (self.root / ".git").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_provision_fresh_worktree_invokes_git_and_gh(self):
        calls: list[list[str]] = []

        def runner(cmd, cwd=None, **kwargs):
            calls.append(list(cmd))
            # Simulate git worktree add creating the directory.
            if cmd[:3] == ["git", "-C", str(self.root)] and cmd[3:5] == ["worktree", "add"]:
                Path(cmd[5]).mkdir(parents=True, exist_ok=True)
                (Path(cmd[5]) / ".git").write_text("gitdir: /tmp/x", encoding="utf-8")
                return _fake_completed(0)
            if cmd[:3] == ["gh", "pr", "checkout"]:
                return _fake_completed(0)
            return _fake_completed(0)

        with patch.object(worktree_mod, "_current_branch", return_value="pr/42"):
            result = worktree_mod.ensure_worktree(
                self.root, 42, _runner=runner
            )

        self.assertFalse(result.reused)
        self.assertEqual(result.branch, "pr/42")
        # First call should be git worktree add
        self.assertEqual(calls[0][:5], ["git", "-C", str(self.root), "worktree", "add"])
        # Second call should be gh pr checkout
        self.assertEqual(calls[1][:3], ["gh", "pr", "checkout"])

    def test_provision_failure_cleans_up(self):
        provisioned: list[Path] = []

        def runner(cmd, cwd=None, **kwargs):
            if cmd[:5] == ["git", "-C", str(self.root), "worktree", "add"]:
                target = Path(cmd[5])
                target.mkdir(parents=True, exist_ok=True)
                (target / ".git").write_text("gitdir: /tmp/x", encoding="utf-8")
                provisioned.append(target)
                return _fake_completed(0)
            if cmd[:3] == ["gh", "pr", "checkout"]:
                return _fake_completed(1, stderr="auth failed")
            if cmd[:5] == ["git", "-C", str(self.root), "worktree", "remove"]:
                target = Path(cmd[6])
                if target.exists():
                    # Simulate worktree remove
                    import shutil
                    shutil.rmtree(target, ignore_errors=True)
                return _fake_completed(0)
            return _fake_completed(0)

        with self.assertRaises(worktree_mod.WorktreeError) as ctx:
            worktree_mod.ensure_worktree(self.root, 42, _runner=runner)
        self.assertIn("gh pr checkout", str(ctx.exception))
        # Cleaned up
        self.assertFalse(provisioned[0].exists())

    def test_no_git_repo_raises(self):
        # Remove the .git marker we set up.
        import shutil
        shutil.rmtree(self.root / ".git")
        with self.assertRaises(worktree_mod.WorktreeError) as ctx:
            worktree_mod.ensure_worktree(self.root, 42)
        self.assertIn("not a git repo", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
