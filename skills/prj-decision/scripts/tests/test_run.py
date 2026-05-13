#!/usr/bin/env python3
"""End-to-end integration tests for prj-decision/run.py.

Each test builds a temporary project root with `_bmad/config.toml`, seeds a
per-PR cache with artifact fixtures, mocks the gh subprocess seam, and
exercises run.main(). State writes, decisions.log appends, and runlog entries
are asserted against the temp filesystem.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import run as run_module  # noqa: E402
import decision_io  # noqa: E402
import state_io  # noqa: E402


class _RunHarness(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "_bmad").mkdir()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_config(self, prj_repo: str | None = "octocat/hello"):
        toml = "[modules.prj]\n"
        if prj_repo is not None:
            toml += f'prj_repo = "{prj_repo}"\n'
        (self.root / "_bmad" / "config.toml").write_text(toml, encoding="utf-8")

    def _seed_pr(
        self,
        pr_number: int,
        phase: str = "Reviewed",
        last_maintainer_activity_at: str | None = None,
    ) -> Path:
        state_io.init_state(self.root, "octocat/hello")
        state = state_io.load_state(self.root)
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "pr_number": pr_number,
            "phase": phase,
            "phase_entered_at": now,
            "last_action_at": now,
            "contributor_login": "alice",
            "next_action": {"skill": "prj-decision", "mode": None},
        }
        if last_maintainer_activity_at:
            entry["last_maintainer_activity_at"] = last_maintainer_activity_at
        state["prs"][str(pr_number)] = entry
        state_io.save_state(self.root, state)
        cache = self.root / "_bmad-output" / "pr-workflow" / "prs" / str(pr_number)
        cache.mkdir(parents=True, exist_ok=True)
        return cache

    def _last_runlog(self) -> dict:
        log = state_io.runlog_path(self.root)
        self.assertTrue(log.exists(), "runlog should exist after run")
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def _invoke(self, argv_extra: list[str], gh_calls: list[list[str]] | None = None) -> int:
        argv = ["run.py", "--project-root", str(self.root), *argv_extra]
        gh_calls = gh_calls if gh_calls is not None else []

        def fake_run_gh(args: list[str], timeout: int = 30) -> str:
            gh_calls.append(args)
            return "ok\n"

        with patch.object(sys, "argv", argv), \
             patch.object(decision_io, "_run_gh", side_effect=fake_run_gh):
            return run_module.main()


class TestReadyToMergeFlow(_RunHarness):
    def test_clean_pr_lands_ready_to_merge(self):
        self._write_config()
        cache = self._seed_pr(10)
        (cache / "review.md").write_text(
            "---\nblockers: 0\nmajors: 0\nminors: 0\n---\n", encoding="utf-8",
        )
        (cache / "triage.md").write_text(
            "---\nclassification: actionable\n---\n", encoding="utf-8",
        )

        gh_calls: list[list[str]] = []
        rc = self._invoke(["--pr", "10"], gh_calls)
        self.assertEqual(rc, 0)

        entry = self._last_runlog()
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["decision"], "ready-to-merge")
        self.assertEqual(entry["transition"], "transitioned")

        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["10"]["phase"], "ReadyToMerge")

        # gh should have been called twice: label, then comment
        self.assertEqual(len(gh_calls), 2)
        self.assertIn("--add-label", gh_calls[0])
        self.assertIn("prj/decision:ready-to-merge", gh_calls[0])
        self.assertEqual(gh_calls[1][0:3], ["pr", "comment", "10"])

        # decisions.log was appended
        log_path = cache / "decisions.log"
        self.assertTrue(log_path.exists())
        log_entry = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(log_entry["decision"], "ready-to-merge")
        self.assertEqual(log_entry["kind"], "decision")


class TestRequestChangesFlow(_RunHarness):
    def test_blocker_findings_request_changes(self):
        self._write_config()
        cache = self._seed_pr(20)
        (cache / "review.md").write_text(
            "---\nblockers: 2\nmajors: 0\nminors: 0\n---\n", encoding="utf-8",
        )

        gh_calls: list[list[str]] = []
        rc = self._invoke(["--pr", "20"], gh_calls)
        self.assertEqual(rc, 0)
        entry = self._last_runlog()
        self.assertEqual(entry["decision"], "request-changes")
        self.assertEqual(entry["transition"], "cleared-next-action")

        # Phase stays at Reviewed
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["20"]["phase"], "Reviewed")
        # next_action cleared
        self.assertIsNone(state["prs"]["20"]["next_action"])

        # Label is the request-changes one
        self.assertIn("prj/decision:request-changes", gh_calls[0])


class TestCloseAsNotNowFlow(_RunHarness):
    def test_triple_gate_close_labels_but_no_state_kill(self):
        self._write_config()
        old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        cache = self._seed_pr(30, last_maintainer_activity_at=old)
        (cache / "triage.md").write_text(
            "---\nclassification: definite-no\n---\n", encoding="utf-8",
        )
        # Provide some review evidence to satisfy gate (c)
        (cache / "review.md").write_text(
            "---\nblockers: 0\n---\n", encoding="utf-8",
        )

        gh_calls: list[list[str]] = []
        rc = self._invoke(["--pr", "30"], gh_calls)
        self.assertEqual(rc, 0)

        entry = self._last_runlog()
        self.assertEqual(entry["decision"], "close-as-not-now")
        self.assertEqual(entry["transition"], "transitioned")

        state = state_io.load_state(self.root)
        # NOTE: state moves to Rejected (terminal in our queue) but no gh close call.
        self.assertEqual(state["prs"]["30"]["phase"], "Rejected")
        self.assertIn("prj/decision:close-as-not-now", gh_calls[0])
        # Confirm no `pr close` ever invoked
        for call in gh_calls:
            self.assertNotIn("close", call, f"unexpected gh close call: {call}")


class TestEarlyExits(_RunHarness):
    def test_missing_pr_arg_returns_bad_args(self):
        self._write_config()
        rc = self._invoke([])
        self.assertEqual(rc, 2)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "bad-args")

    def test_missing_repo_returns_misconfigured(self):
        self._write_config(prj_repo=None)
        rc = self._invoke(["--pr", "1"])
        self.assertEqual(rc, 2)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "misconfigured")

    def test_unknown_pr_returns_4(self):
        self._write_config()
        state_io.init_state(self.root, "octocat/hello")
        rc = self._invoke(["--pr", "777"])
        self.assertEqual(rc, 4)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "unknown-pr")

    def test_no_cache_returns_4(self):
        self._write_config()
        state_io.init_state(self.root, "octocat/hello")
        state = state_io.load_state(self.root)
        # Add PR but skip cache creation
        state["prs"]["50"] = {
            "pr_number": 50,
            "phase": "Reviewed",
            "phase_entered_at": "2026-05-10T00:00:00+00:00",
            "last_action_at": "2026-05-10T00:00:00+00:00",
            "contributor_login": "bob",
            "next_action": None,
        }
        state_io.save_state(self.root, state)
        rc = self._invoke(["--pr", "50"])
        self.assertEqual(rc, 4)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "no-cache")


class TestDryRun(_RunHarness):
    def test_dry_run_makes_no_writes(self):
        self._write_config()
        cache = self._seed_pr(60)
        (cache / "review.md").write_text(
            "---\nblockers: 0\n---\n", encoding="utf-8",
        )

        gh_calls: list[list[str]] = []
        rc = self._invoke(["--pr", "60", "--dry-run"], gh_calls)
        self.assertEqual(rc, 0)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "dry-run")
        self.assertEqual(entry["decision"], "ready-to-merge")

        # No gh calls
        self.assertEqual(gh_calls, [])
        # decisions.log NOT created
        log_path = cache / "decisions.log"
        self.assertFalse(log_path.exists(), "dry-run must not write decisions.log")
        # State phase unchanged
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["60"]["phase"], "Reviewed")


class TestGhFailureHandling(_RunHarness):
    def test_label_failure_returns_3(self):
        self._write_config()
        cache = self._seed_pr(70)
        (cache / "review.md").write_text(
            "---\nblockers: 0\n---\n", encoding="utf-8",
        )

        def fake_run_gh(args: list[str], timeout: int = 30) -> str:
            raise decision_io.GhError("403 forbidden")

        argv = ["run.py", "--project-root", str(self.root), "--pr", "70"]
        with patch.object(sys, "argv", argv), \
             patch.object(decision_io, "_run_gh", side_effect=fake_run_gh):
            rc = run_module.main()

        self.assertEqual(rc, 3)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "gh-error")
        self.assertIn("403 forbidden", entry["error"])


class TestRunLogShape(_RunHarness):
    def test_runlog_required_fields(self):
        self._write_config()
        cache = self._seed_pr(80)
        (cache / "review.md").write_text("---\nblockers: 0\n---\n", encoding="utf-8")
        self._invoke(["--pr", "80"])
        entry = self._last_runlog()
        for key in ("ts", "action", "skill", "status", "pr_number", "run_id", "duration_ms"):
            self.assertIn(key, entry, f"runlog missing key: {key}")
        self.assertEqual(entry["action"], "decision-pass")
        self.assertEqual(entry["skill"], "prj-decision")


if __name__ == "__main__":
    unittest.main()
