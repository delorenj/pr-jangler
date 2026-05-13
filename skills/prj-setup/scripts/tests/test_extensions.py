#!/usr/bin/env python3
"""Tests for the four PR-Jangler-specific setup extension scripts.

The three template scripts (cleanup-legacy.py, merge-config.py, merge-help-csv.py)
are owned upstream by bmad-module-builder; their --help smoke tests are below
to satisfy the per-script "tests exist" lint, but their full behavior is
verified in the upstream repo.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = Path(__file__).resolve().parent.parent.parent
SCRIPTS = SKILL / "scripts"


def _run_help(script_name: str) -> tuple[int, str]:
    result = subprocess.run(
        ["python3", str(SCRIPTS / script_name), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode, result.stdout


class TestExtensionHelpSurfaces(unittest.TestCase):
    """Every script in scripts/ must expose --help and produce a usage line."""

    def test_check_deps_help(self):
        code, out = _run_help("check-deps.py")
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_smtp_test_help(self):
        code, out = _run_help("smtp-test.py")
        self.assertEqual(code, 0)
        self.assertIn("--config-path", out)

    def test_create_labels_help(self):
        code, out = _run_help("create-labels.py")
        self.assertEqual(code, 0)
        self.assertIn("--repo", out)

    def test_init_workflow_help(self):
        code, out = _run_help("init-workflow.py")
        self.assertEqual(code, 0)
        self.assertIn("--project-root", out)

    def test_cleanup_legacy_help(self):
        code, out = _run_help("cleanup-legacy.py")
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_merge_config_help(self):
        code, out = _run_help("merge-config.py")
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_merge_help_csv_help(self):
        code, out = _run_help("merge-help-csv.py")
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)


class TestCheckDeps(unittest.TestCase):
    def test_returns_zero_and_json_regardless_of_environment(self):
        result = subprocess.run(
            ["python3", str(SCRIPTS / "check-deps.py")],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        # stdout is valid JSON with a 'checks' key
        parsed = json.loads(result.stdout)
        self.assertIn("checks", parsed)
        self.assertIsInstance(parsed["checks"], list)
        self.assertEqual(len(parsed["checks"]), 3)

    def test_check_names_are_consistent(self):
        result = subprocess.run(
            ["python3", str(SCRIPTS / "check-deps.py")],
            capture_output=True, text=True, timeout=10,
        )
        parsed = json.loads(result.stdout)
        names = {c["name"] for c in parsed["checks"]}
        self.assertEqual(names, {"gh", "op", "git"})


class TestInitWorkflow(unittest.TestCase):
    def test_creates_full_tree_and_state_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["python3", str(SCRIPTS / "init-workflow.py"),
                 "--project-root", tmp, "--repo", "owner/repo"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            parsed = json.loads(result.stdout)
            self.assertEqual(parsed["status"], "ok")
            base = Path(tmp) / "_bmad-output" / "pr-workflow"
            for sub in ("prs", "logs", "reports", "worktrees"):
                self.assertTrue((base / sub).is_dir(), f"missing {sub}/")
            state = json.loads((base / "state.json").read_text())
            self.assertEqual(state["repo"], "owner/repo")
            self.assertEqual(state["heartbeat_count"], 0)
            self.assertEqual(state["prs"], {})

    def test_second_run_is_idempotent_and_does_not_overwrite_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            # First run
            subprocess.run(
                ["python3", str(SCRIPTS / "init-workflow.py"),
                 "--project-root", tmp, "--repo", "owner/repo"],
                capture_output=True, text=True, timeout=10,
            )
            # Mutate state to verify second run doesn't clobber
            state_path = Path(tmp) / "_bmad-output" / "pr-workflow" / "state.json"
            state = json.loads(state_path.read_text())
            state["heartbeat_count"] = 42
            state_path.write_text(json.dumps(state))
            # Second run
            result = subprocess.run(
                ["python3", str(SCRIPTS / "init-workflow.py"),
                 "--project-root", tmp, "--repo", "owner/repo"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0)
            parsed = json.loads(result.stdout)
            self.assertEqual(parsed["state_json"]["status"], "skipped-existing")
            # state.json content preserved
            preserved = json.loads(state_path.read_text())
            self.assertEqual(preserved["heartbeat_count"], 42)


class TestCreateLabels(unittest.TestCase):
    def test_missing_gh_short_circuits(self):
        # Simulate gh missing by passing a PATH that excludes gh but keeps the
        # python interpreter discoverable via sys.executable absolute path.
        env_no_gh = {"PATH": "/nonexistent"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("test-label|FBCA04|test\n")
            label_file = f.name
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "create-labels.py"),
                 "--repo", "owner/repo", "--labels", label_file],
                capture_output=True, text=True, timeout=10, env=env_no_gh,
            )
            # gh not on PATH -> exit 1, status: gh-missing on stderr
            self.assertEqual(result.returncode, 1)
            err = json.loads(result.stderr)
            self.assertEqual(err["status"], "gh-missing")
        finally:
            Path(label_file).unlink()

    def test_empty_repo_short_circuits(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("test-label|FBCA04|test\n")
            label_file = f.name
        try:
            result = subprocess.run(
                ["python3", str(SCRIPTS / "create-labels.py"),
                 "--repo", "", "--labels", label_file],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            err = json.loads(result.stderr)
            self.assertEqual(err["status"], "config-error")
        finally:
            Path(label_file).unlink()

    def test_labels_file_missing(self):
        result = subprocess.run(
            ["python3", str(SCRIPTS / "create-labels.py"),
             "--repo", "owner/repo", "--labels", "/nonexistent/path.txt"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        err = json.loads(result.stderr)
        self.assertEqual(err["status"], "labels-file-missing")


class TestSmtpTest(unittest.TestCase):
    def test_missing_config_path_returns_config_error(self):
        result = subprocess.run(
            ["python3", str(SCRIPTS / "smtp-test.py"),
             "--config-path", "/nonexistent/config.toml"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        err = json.loads(result.stderr)
        self.assertEqual(err["status"], "config-error")

    def test_empty_config_returns_missing_keys(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("[modules.prj]\nprj_repo = \"owner/repo\"\n")
            config_path = f.name
        try:
            result = subprocess.run(
                ["python3", str(SCRIPTS / "smtp-test.py"),
                 "--config-path", config_path],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 2)
            err = json.loads(result.stderr)
            self.assertEqual(err["status"], "config-error")
            self.assertIn("missing", err)
            # All required SMTP keys should be flagged missing
            for key in ("prj_email_to", "prj_email_from", "prj_smtp_host", "prj_smtp_port"):
                self.assertIn(key, err["missing"])
        finally:
            Path(config_path).unlink()


if __name__ == "__main__":
    unittest.main()
