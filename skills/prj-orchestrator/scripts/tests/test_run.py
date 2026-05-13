#!/usr/bin/env python3
"""Integration tests for run.py.

Each test sets up a temporary "project root" with a minimal `_bmad/config.toml`
and exercises the main entry. Uses `--dry-run` to avoid actual dispatch.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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


class TestSelectOnly(unittest.TestCase):
    """`--select-only` is the daemon-facing pure selector.

    Contract:
      - No state mutation (no init, no heartbeat increment, no save).
      - No runlog entry appended.
      - No dispatch.
      - Emits one JSON object on stdout with status, repo, pr, phase, skill,
        mode, priority, reason, state_sha.
    """

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

    def _run_select_only(self) -> tuple[int, dict]:
        argv = ["run.py", "--project-root", str(self.root), "--select-only"]
        buf = io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(buf):
            rc = run_module.main()
        out = buf.getvalue().strip()
        self.assertTrue(out, "select-only must emit JSON on stdout")
        return rc, json.loads(out)

    def _required_keys(self) -> set[str]:
        return {"status", "repo", "pr", "phase", "skill", "mode",
                "priority", "reason", "state_sha"}

    def test_misconfigured_emits_json_and_does_not_write_runlog(self):
        self._write_config(prj_repo=None)
        rc, payload = self._run_select_only()
        self.assertEqual(rc, 2)
        self.assertEqual(payload["status"], "misconfigured")
        self.assertEqual(self._required_keys(), set(payload.keys()))
        self.assertEqual(payload["state_sha"], "no-state")
        # No runlog side effects in pure-selector mode
        self.assertFalse(state_io.runlog_path(self.root).exists())

    def test_fresh_project_returns_action_selected_without_writing_state(self):
        self._write_config(prj_repo="owner/repo")
        rc, payload = self._run_select_only()
        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "action-selected")
        self.assertEqual(payload["skill"], "prj-discover")
        self.assertEqual(payload["repo"], "owner/repo")
        self.assertIsNone(payload["pr"])
        self.assertIsNone(payload["phase"])
        self.assertGreater(payload["priority"], 0)
        # CRITICAL: state.json must NOT exist after a pure selector call.
        self.assertFalse(
            state_io.state_path(self.root).exists(),
            "select-only must not initialize state.json on disk",
        )
        # And no runlog either
        self.assertFalse(state_io.runlog_path(self.root).exists())
        self.assertEqual(payload["state_sha"], "no-state")

    def test_does_not_increment_heartbeat_on_repeated_calls(self):
        """Daemons may peek many times; cadence must not drift."""
        self._write_config(prj_repo="owner/repo")
        # First, materialize state via a normal dry-run heartbeat (heartbeat=1)
        argv = ["run.py", "--project-root", str(self.root), "--dry-run"]
        with patch.object(sys, "argv", argv):
            run_module.main()
        state_before = state_io.load_state(self.root)
        self.assertEqual(state_before["heartbeat_count"], 1)

        # Now call --select-only several times; heartbeat must stay at 1.
        for _ in range(3):
            self._run_select_only()
        state_after = state_io.load_state(self.root)
        self.assertEqual(state_after["heartbeat_count"], 1)

    def test_state_sha_is_stable_and_changes_with_state(self):
        """state_sha is the daemon's idempotency token."""
        self._write_config(prj_repo="owner/repo")
        # Materialize state with one heartbeat
        argv = ["run.py", "--project-root", str(self.root), "--dry-run"]
        with patch.object(sys, "argv", argv):
            run_module.main()

        _, first = self._run_select_only()
        _, second = self._run_select_only()
        self.assertEqual(first["state_sha"], second["state_sha"])
        self.assertNotEqual(first["state_sha"], "no-state")
        self.assertEqual(len(first["state_sha"]), 64)  # sha256 hex

        # Mutate state, sha must shift
        state = state_io.load_state(self.root)
        state["heartbeat_count"] = 99
        state_io.save_state(self.root, state)
        _, third = self._run_select_only()
        self.assertNotEqual(first["state_sha"], third["state_sha"])

    def test_idle_when_no_actionable_pr_and_no_system_due(self):
        """A state with no PRs and a non-cadence heartbeat between report hours
        should return status=idle. We engineer this by seeding state directly."""
        self._write_config(prj_repo="owner/repo")
        # Seed state so heartbeat is past the cadence boundary AND last_report
        # is set so the daily-report system action is suppressed (treat as just-sent).
        state_io.init_state(self.root, "owner/repo")
        state = state_io.load_state(self.root)
        # heartbeat_count=1 with default discover_every_n=3 means no cadence trigger.
        # Empty PRs would otherwise trigger discover (queue empty), so add a synthetic
        # terminal PR to bypass that and have nothing to do.
        from datetime import datetime, timezone
        state["heartbeat_count"] = 1
        state["last_report_sent"] = datetime.now(timezone.utc).isoformat()
        state["prs"] = {
            "1": {
                "pr_number": 1,
                "phase": "Archived",
                "phase_entered_at": "2026-05-13T00:00:00+00:00",
                "last_action_at": "2026-05-13T00:00:00+00:00",
                "contributor_login": "ghost",
            }
        }
        state_io.save_state(self.root, state)

        rc, payload = self._run_select_only()
        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "idle")
        self.assertIsNone(payload["skill"])
        self.assertIsNone(payload["pr"])

    def test_per_pr_action_includes_phase(self):
        """When a PR action is selected, the response includes the PR's current phase."""
        self._write_config(prj_repo="owner/repo")
        state_io.init_state(self.root, "owner/repo")
        state = state_io.load_state(self.root)
        state["heartbeat_count"] = 1
        from datetime import datetime, timezone
        state["last_report_sent"] = datetime.now(timezone.utc).isoformat()
        state["prs"] = {
            "42": {
                "pr_number": 42,
                "phase": "ReviewPending",
                "phase_entered_at": "2026-05-13T00:00:00+00:00",
                "last_action_at": "2026-05-13T00:00:00+00:00",
                "contributor_login": "alice",
                "next_action": {"skill": "prj-review", "mode": None},
            }
        }
        state_io.save_state(self.root, state)

        rc, payload = self._run_select_only()
        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "action-selected")
        self.assertEqual(payload["pr"], 42)
        self.assertEqual(payload["phase"], "ReviewPending")
        self.assertEqual(payload["skill"], "prj-review")


if __name__ == "__main__":
    unittest.main()
