#!/usr/bin/env python3
"""Integration tests for run.py.

Each test sets up a temporary "project root" with a minimal `_bmad/config.toml`
and exercises the main entry. Uses `--dry-run` to avoid actual dispatch.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run as run_module  # noqa: E402
import state_io  # noqa: E402


class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "_bmad").mkdir()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_config(self, prj_repo: str | None = "owner/repo"):
        toml = "[modules.prj]\n"
        if prj_repo is not None:
            toml += f'prj_repo = "{prj_repo}"\n'
        (self.root / "_bmad" / "config.toml").write_text(toml, encoding="utf-8")

    def _last_runlog_entry(self) -> dict:
        log = state_io.runlog_path(self.root)
        self.assertTrue(log.exists(), "runlog file should exist after run")
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        self.assertGreater(len(lines), 0)
        return json.loads(lines[-1])

    def _invoke(self, extra_args: list[str] | None = None) -> int:
        argv = ["run.py", "--project-root", str(self.root), "--dry-run"]
        if extra_args:
            argv.extend(extra_args)
        with patch.object(sys, "argv", argv):
            return run_module.main()

    def test_missing_prj_repo_aborts_with_misconfigured(self):
        self._write_config(prj_repo=None)
        rc = self._invoke()
        self.assertEqual(rc, 2)
        entry = self._last_runlog_entry()
        self.assertEqual(entry["status"], "misconfigured")
        self.assertEqual(entry["action"], "abort")

    def test_first_run_initializes_state_and_dry_runs_discover(self):
        self._write_config(prj_repo="owner/repo")
        rc = self._invoke()
        self.assertEqual(rc, 0)
        # state.json now exists
        self.assertTrue(state_io.state_path(self.root).exists())
        state = state_io.load_state(self.root)
        self.assertEqual(state["repo"], "owner/repo")
        self.assertEqual(state["heartbeat_count"], 1)
        # Last runlog entry shows discover was selected as a dry-run
        entry = self._last_runlog_entry()
        self.assertEqual(entry["status"], "dry-run")
        self.assertEqual(entry["skill"], "prj-discover")
        self.assertIn("would dispatch prj-discover", entry.get("next_action_hint", ""))

    def test_heartbeat_increments_each_invocation(self):
        self._write_config(prj_repo="owner/repo")
        self._invoke()
        self._invoke()
        state = state_io.load_state(self.root)
        self.assertEqual(state["heartbeat_count"], 2)

    def test_stub_mode_when_skill_not_installed(self):
        # Without --dry-run, target skill prj-discover doesn't exist → stub: not built
        self._write_config(prj_repo="owner/repo")
        argv = ["run.py", "--project-root", str(self.root)]
        with patch.object(sys, "argv", argv):
            rc = run_module.main()
        self.assertEqual(rc, 0)
        entry = self._last_runlog_entry()
        self.assertEqual(entry["status"], "stub: not built")
        self.assertEqual(entry["skill"], "prj-discover")

    def test_runlog_contains_required_fields(self):
        self._write_config(prj_repo="owner/repo")
        self._invoke()
        entry = self._last_runlog_entry()
        for key in ("ts", "run_id", "action", "skill", "priority", "reason",
                    "heartbeat_count", "status", "duration_ms"):
            self.assertIn(key, entry, f"runlog missing key: {key}")


if __name__ == "__main__":
    unittest.main()
