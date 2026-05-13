#!/usr/bin/env python3
"""Unit tests for regression_run.py.

The real test runner is never invoked: a callable `runner` hook is injected so
we can assert pass/fail behavior deterministically. We also cover the auto-
detect logic and the config-override path.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import regression_run  # noqa: E402


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRunRegression(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_passing_suite_reports_zero_regressions(self):
        def fake_runner(cmd, cwd, timeout):
            self.assertEqual(cmd, ["true"])
            return _FakeCompleted(returncode=0, stdout="all good\n")

        result = regression_run.run_regression(
            self.root, runner_command="true", runner=fake_runner,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.returncode_indicates_pass)
        self.assertEqual(result.regressions, 0)
        self.assertIn("all good", result.stdout_tail)

    def test_failing_suite_auto_reject_count(self):
        def fake_runner(cmd, cwd, timeout):
            return _FakeCompleted(returncode=1, stdout="1 failed", stderr="boom")

        result = regression_run.run_regression(
            self.root, runner_command="pytest -q", runner=fake_runner,
        )
        self.assertEqual(result.status, "ok")
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(result.returncode_indicates_pass)
        self.assertEqual(result.regressions, 1)
        self.assertIn("boom", result.stderr_tail)

    def test_runner_not_found_marks_runner_failed(self):
        def fake_runner(cmd, cwd, timeout):
            raise FileNotFoundError("no such tool")

        result = regression_run.run_regression(
            self.root, runner_command="missing-tool", runner=fake_runner,
        )
        self.assertEqual(result.status, "runner-failed")
        self.assertEqual(result.regressions, 1)
        self.assertIn("missing-tool", result.command)

    def test_runner_timeout_marks_runner_failed(self):
        def fake_runner(cmd, cwd, timeout):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout, output=b"partial", stderr=b"")

        result = regression_run.run_regression(
            self.root, runner_command="sleep 9999", runner=fake_runner,
        )
        self.assertEqual(result.status, "runner-failed")
        self.assertEqual(result.regressions, 1)
        self.assertIn("partial", result.stdout_tail)

    def test_detect_runner_prefers_bun(self):
        (self.root / "bun.lockb").write_text("", encoding="utf-8")
        (self.root / "package.json").write_text("{}", encoding="utf-8")
        self.assertEqual(regression_run.detect_runner(self.root), "bun test")

    def test_detect_runner_npm_when_package_json_only(self):
        (self.root / "package.json").write_text("{}", encoding="utf-8")
        self.assertEqual(regression_run.detect_runner(self.root), "npm test")

    def test_detect_runner_pytest_when_pyproject(self):
        (self.root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        self.assertEqual(regression_run.detect_runner(self.root), "pytest")

    def test_detect_runner_cargo_when_rust(self):
        (self.root / "Cargo.toml").write_text("[package]\nname=\"x\"", encoding="utf-8")
        self.assertEqual(regression_run.detect_runner(self.root), "cargo test")

    def test_detect_runner_default_pytest(self):
        # Empty repo — detection still returns a sane default.
        self.assertEqual(regression_run.detect_runner(self.root), "pytest")

    def test_load_runner_command_reads_config_override(self):
        (self.root / "_bmad").mkdir()
        (self.root / "_bmad" / "config.toml").write_text(
            "[modules.prj]\nprj_test_runner = \"custom test\"\n",
            encoding="utf-8",
        )
        self.assertEqual(regression_run.load_runner_command(self.root), "custom test")

    def test_load_runner_command_falls_back_to_detection(self):
        (self.root / "_bmad").mkdir()
        (self.root / "_bmad" / "config.toml").write_text("[modules.prj]\n", encoding="utf-8")
        (self.root / "Cargo.toml").write_text("[package]\nname=\"x\"", encoding="utf-8")
        self.assertEqual(regression_run.load_runner_command(self.root), "cargo test")


if __name__ == "__main__":
    unittest.main()
