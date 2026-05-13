#!/usr/bin/env python3
"""Integration tests for prj-discover/run.py.

Each test builds a temporary project root with `_bmad/config.toml`, mocks the
GhClient via patch.object, and exercises the run.main() entry. State writes,
per-PR cache, and runlog entries are asserted against the temp filesystem.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

# Touch reconcile first so it patches sys.path with state_io's location.
import reconcile  # noqa: E402,F401
import run as run_module  # noqa: E402
import gh_client  # noqa: E402
import state_io  # noqa: E402


class _FakeClient:
    """Stand-in for GhClient with deterministic fixture data."""

    def __init__(
        self,
        remaining: int = 4500,
        limit: int = 5000,
        open_prs: list | None = None,
        details: dict | None = None,
        diff_stats: dict | None = None,
    ):
        self.remaining = remaining
        self.limit = limit
        self.open_prs = open_prs or []
        self.details = details or {}
        self.diff_stats_by_pr = diff_stats or {}

    def rate_limit(self):
        return gh_client.RateLimitStatus(
            remaining=self.remaining,
            limit=self.limit,
            deferred=self.remaining < 100,
        )

    def list_open(self):
        return self.open_prs

    def view(self, pr_number: int):
        return self.details.get(pr_number, {"comments": [], "reviews": [], "reviewThreads": []})

    def diff_stats(self, pr_number: int):
        return self.diff_stats_by_pr.get(pr_number, {"files_changed": 0})


class TestRunSweep(unittest.TestCase):
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

    def _last_runlog(self) -> dict:
        log = state_io.runlog_path(self.root)
        self.assertTrue(log.exists(), "runlog should exist after run")
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def _invoke(self, fake: _FakeClient, extra_args: list[str] | None = None) -> int:
        argv = ["run.py", "--project-root", str(self.root)]
        if extra_args:
            argv.extend(extra_args)
        with patch.object(sys, "argv", argv), \
             patch.object(run_module, "GhClient", return_value=fake):
            return run_module.main()

    def test_missing_prj_repo_returns_misconfigured(self):
        self._write_config(prj_repo=None)
        fake = _FakeClient()
        rc = self._invoke(fake)
        self.assertEqual(rc, 2)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "misconfigured")
        self.assertEqual(entry["skill"], "prj-discover")

    def test_rate_limit_low_defers_sweep(self):
        self._write_config()
        fake = _FakeClient(remaining=50)
        rc = self._invoke(fake)
        self.assertEqual(rc, 0)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "deferred-rate-limit")
        # State exists but no PRs entered
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"], {})

    def test_new_pr_added_state_persisted_and_meta_written(self):
        self._write_config()
        open_prs = [
            {
                "number": 101,
                "title": "Add foo",
                "author": {"login": "alice"},
                "createdAt": "2026-05-10T10:00:00Z",
                "headRefName": "feat/foo",
                "baseRefName": "main",
            }
        ]
        details = {
            101: {
                "comments": [{"id": "c1"}],
                "reviews": [],
                "reviewThreads": [],
            }
        }
        diff_stats = {101: {"files_changed": 5}}
        fake = _FakeClient(open_prs=open_prs, details=details, diff_stats=diff_stats)

        rc = self._invoke(fake)
        self.assertEqual(rc, 0)

        state = state_io.load_state(self.root)
        self.assertIn("101", state["prs"])
        self.assertEqual(state["prs"]["101"]["phase"], "Discovered")
        self.assertEqual(state["prs"]["101"]["contributor_login"], "alice")

        meta = self.root / "_bmad-output" / "pr-workflow" / "prs" / "101" / "meta.json"
        self.assertTrue(meta.exists())
        meta_data = json.loads(meta.read_text(encoding="utf-8"))
        self.assertEqual(meta_data["pr_number"], 101)
        self.assertEqual(meta_data["files_changed"], 5)
        self.assertEqual(meta_data["contributor_login"], "alice")

        entry = self._last_runlog()
        self.assertEqual(entry["status"], "ok")
        self.assertIn("report", entry)
        self.assertEqual(entry["report"]["new_prs"], [101])

    def test_dry_run_does_not_write_state_or_cache(self):
        self._write_config()
        open_prs = [
            {
                "number": 202,
                "title": "x",
                "author": {"login": "bob"},
                "createdAt": "2026-05-10T10:00:00Z",
                "headRefName": "feat/x",
                "baseRefName": "main",
            }
        ]
        details = {202: {"comments": [], "reviews": [], "reviewThreads": []}}
        fake = _FakeClient(open_prs=open_prs, details=details)

        rc = self._invoke(fake, extra_args=["--dry-run"])
        self.assertEqual(rc, 0)

        state = state_io.load_state(self.root)
        # Sweep should NOT have written PR 202 in dry-run mode
        self.assertNotIn("202", state["prs"])
        meta = self.root / "_bmad-output" / "pr-workflow" / "prs" / "202" / "meta.json"
        self.assertFalse(meta.exists(), "dry-run must not write per-PR cache")

        entry = self._last_runlog()
        self.assertEqual(entry["status"], "dry-run")
        self.assertEqual(entry["report"]["new_prs"], [202])

    def test_closed_pr_archived_on_subsequent_sweep(self):
        self._write_config()
        # Seed state with PR 303 already at ReviewPending
        state_io.init_state(self.root, "octocat/hello")
        state = state_io.load_state(self.root)
        state["prs"]["303"] = {
            "pr_number": 303,
            "phase": "ReviewPending",
            "phase_entered_at": "2026-05-01T00:00:00+00:00",
            "last_action_at": "2026-05-01T00:00:00+00:00",
            "contributor_login": "carol",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-review", "mode": None},
        }
        state_io.save_state(self.root, state)

        # Sweep with NO open PRs → 303 should be archived
        fake = _FakeClient(open_prs=[], details={})
        rc = self._invoke(fake)
        self.assertEqual(rc, 0)

        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["303"]["phase"], "Archived")

        entry = self._last_runlog()
        self.assertEqual(entry["report"]["archived_prs"], [303])

    def test_gh_error_logs_and_returns_3(self):
        self._write_config()

        class _Boom:
            def rate_limit(self):
                raise gh_client.GhClientError("network down")

        rc = 0
        argv = ["run.py", "--project-root", str(self.root)]
        with patch.object(sys, "argv", argv), \
             patch.object(run_module, "GhClient", return_value=_Boom()):
            rc = run_module.main()

        self.assertEqual(rc, 3)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "gh-error")
        self.assertIn("network down", entry["error"])

    def test_runlog_entry_has_required_fields(self):
        self._write_config()
        fake = _FakeClient(open_prs=[], details={})
        self._invoke(fake)
        entry = self._last_runlog()
        for key in ("ts", "action", "skill", "status", "duration_ms", "run_id"):
            self.assertIn(key, entry, f"runlog missing key: {key}")
        self.assertEqual(entry["action"], "discover-sweep")
        self.assertEqual(entry["skill"], "prj-discover")


if __name__ == "__main__":
    unittest.main()
