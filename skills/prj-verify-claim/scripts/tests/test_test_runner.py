#!/usr/bin/env python3
"""Unit tests for test_runner.py.

Detection tests use temp directories with marker files. Execution tests
patch the single subprocess seam `_run`.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import test_runner  # noqa: E402


class TestDetectRunnerAutoDetect(unittest.TestCase):
    """Four fixture cases for auto-detect from repo files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_detects_bun_from_lockfile(self):
        (self.wt / "bun.lockb").write_text("", encoding="utf-8")
        # bun should win even when a package.json also exists.
        (self.wt / "package.json").write_text("{}", encoding="utf-8")
        result = test_runner.detect_runner(self.wt)
        self.assertEqual(result.runner, "bun")
        self.assertEqual(result.source, "auto")

    def test_detects_npm_from_package_json(self):
        (self.wt / "package.json").write_text("{}", encoding="utf-8")
        result = test_runner.detect_runner(self.wt)
        self.assertEqual(result.runner, "npm")
        self.assertEqual(result.source, "auto")

    def test_detects_pytest_from_pyproject(self):
        (self.wt / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        result = test_runner.detect_runner(self.wt)
        self.assertEqual(result.runner, "pytest")
        self.assertEqual(result.source, "auto")

    def test_detects_cargo_from_manifest(self):
        (self.wt / "Cargo.toml").write_text("[package]\nname = 'x'\n", encoding="utf-8")
        result = test_runner.detect_runner(self.wt)
        self.assertEqual(result.runner, "cargo")
        self.assertEqual(result.source, "auto")

    def test_no_markers_returns_none(self):
        result = test_runner.detect_runner(self.wt)
        self.assertIsNone(result.runner)
        self.assertEqual(result.source, "none")

    def test_missing_worktree_returns_none(self):
        result = test_runner.detect_runner(self.wt / "does-not-exist")
        self.assertIsNone(result.runner)
        self.assertEqual(result.source, "none")


class TestDetectRunnerOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_override_known_runner_key(self):
        # Even with markers present, override wins.
        (self.wt / "Cargo.toml").write_text("", encoding="utf-8")
        result = test_runner.detect_runner(self.wt, override="bun")
        self.assertEqual(result.runner, "bun")
        self.assertEqual(result.source, "override")

    def test_override_raw_command_is_custom(self):
        result = test_runner.detect_runner(self.wt, override="make test")
        self.assertEqual(result.runner, "custom")
        self.assertEqual(result.source, "override")

    def test_override_with_whitespace_is_trimmed(self):
        result = test_runner.detect_runner(self.wt, override="  pnpm  ")
        self.assertEqual(result.runner, "pnpm")


class TestBuildCommand(unittest.TestCase):
    def test_known_runner_uses_default_command(self):
        det = test_runner.DetectionResult(runner="bun", source="auto")
        cmd = test_runner.build_command(det)
        self.assertEqual(cmd, ["bun", "test"])

    def test_extra_args_appended(self):
        det = test_runner.DetectionResult(runner="pytest", source="auto")
        cmd = test_runner.build_command(det, extra=["tests/unit"])
        self.assertEqual(cmd, ["pytest", "tests/unit"])

    def test_custom_runner_uses_override_string(self):
        det = test_runner.DetectionResult(runner="custom", source="override")
        cmd = test_runner.build_command(det, override="make test-fast")
        self.assertEqual(cmd, ["make", "test-fast"])

    def test_custom_without_override_raises(self):
        det = test_runner.DetectionResult(runner="custom", source="override")
        with self.assertRaises(test_runner.RunnerError):
            test_runner.build_command(det)

    def test_no_runner_raises(self):
        det = test_runner.DetectionResult(runner=None, source="none")
        with self.assertRaises(test_runner.RunnerError):
            test_runner.build_command(det)


class TestRunInWorktree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name)
        (self.wt / "pyproject.toml").write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_returns_runresult_with_command_and_rc(self):
        det = test_runner.detect_runner(self.wt)
        self.assertEqual(det.runner, "pytest")

        def fake_run(cmd, cwd, timeout):
            return (0, "1 passed\n", "", False)

        with patch.object(test_runner, "_run", side_effect=fake_run):
            result = test_runner.run_in_worktree(self.wt, det)
        self.assertEqual(result.runner, "pytest")
        self.assertEqual(result.returncode, 0)
        self.assertIn("1 passed", result.stdout)
        self.assertFalse(result.timed_out)

    def test_run_propagates_nonzero_rc(self):
        det = test_runner.detect_runner(self.wt)

        def fake_run(cmd, cwd, timeout):
            return (1, "", "fail\n", False)

        with patch.object(test_runner, "_run", side_effect=fake_run):
            result = test_runner.run_in_worktree(self.wt, det)
        self.assertEqual(result.returncode, 1)
        self.assertIn("fail", result.stderr)

    def test_run_timeout_sets_flag(self):
        det = test_runner.detect_runner(self.wt)

        def fake_run(cmd, cwd, timeout):
            return (124, "partial\n", "", True)

        with patch.object(test_runner, "_run", side_effect=fake_run):
            result = test_runner.run_in_worktree(self.wt, det)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, 124)

    def test_missing_worktree_raises(self):
        det = test_runner.DetectionResult(runner="bun", source="auto")
        with self.assertRaises(test_runner.RunnerError):
            test_runner.run_in_worktree(Path("/nonexistent/path/zzz"), det)


if __name__ == "__main__":
    unittest.main()
