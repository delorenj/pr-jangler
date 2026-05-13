#!/usr/bin/env python3
"""Unit tests for test_runner.detect_runner and run_tests.

Covers:
  - detect_runner picks bun for bun+package.json projects
  - detect_runner picks pytest for pyproject.toml projects
  - detect_runner picks cargo for Rust projects
  - detect_runner honors config override
  - run_tests captures exit code, stdout, stderr
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import test_runner  # noqa: E402


class TestDetectRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bun_picked_for_bun_project(self):
        (self.root / "package.json").write_text(
            json.dumps({"scripts": {"test": "bun test"}}), encoding="utf-8",
        )
        (self.root / "bun.lockb").write_text("", encoding="utf-8")
        self.assertEqual(test_runner.detect_runner(self.root), "bun run test")

    def test_npm_picked_for_node_project_without_bun(self):
        (self.root / "package.json").write_text(
            json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8",
        )
        self.assertEqual(test_runner.detect_runner(self.root), "npm test")

    def test_pytest_picked_for_python_project(self):
        (self.root / "pyproject.toml").write_text("[tool.pytest]\n", encoding="utf-8")
        self.assertEqual(test_runner.detect_runner(self.root), "pytest")

    def test_cargo_picked_for_rust_project(self):
        (self.root / "Cargo.toml").write_text("[package]\nname='x'", encoding="utf-8")
        self.assertEqual(test_runner.detect_runner(self.root), "cargo test")

    def test_fallback_is_pytest(self):
        self.assertEqual(test_runner.detect_runner(self.root), "pytest")

    def test_config_override_wins(self):
        (self.root / "pyproject.toml").write_text("", encoding="utf-8")
        self.assertEqual(
            test_runner.detect_runner(self.root, config_override="custom-runner --fast"),
            "custom-runner --fast",
        )


class TestRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_passing_command(self):
        # Inject a fake subprocess.run that returns success.
        def fake(cmd, cwd, capture_output, text, timeout, check):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="ok", stderr="",
            )

        result = test_runner.run_tests("true", self.root, _runner=fake)
        self.assertTrue(result.passed)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "ok")

    def test_failing_command(self):
        def fake(cmd, cwd, capture_output, text, timeout, check):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="boom",
            )

        result = test_runner.run_tests("false", self.root, _runner=fake)
        self.assertTrue(result.failed)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stderr, "boom")

    def test_run_result_to_dict_truncates_long_output(self):
        result = test_runner.RunResult(
            command="x", cwd="/tmp", exit_code=0,
            stdout="a" * 5000, stderr="b" * 5000, duration_ms=1,
        )
        d = result.to_dict()
        self.assertLessEqual(len(d["stdout_tail"]), 2000)
        self.assertLessEqual(len(d["stderr_tail"]), 2000)


if __name__ == "__main__":
    unittest.main()
