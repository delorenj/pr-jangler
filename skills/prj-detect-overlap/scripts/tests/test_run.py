#!/usr/bin/env python3
"""Integration tests for prj-detect-overlap/run.py.

End-to-end exercise of main() with mocked gh subprocess calls. State writes,
overlap.md, runlog, and label application are asserted against the temp
filesystem.
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

# Import order matters: verdict_io inserts prj-orchestrator/scripts at sys.path[0]
# (so it can import state_io), which would shadow our own run.py when `import run`
# resolves. Touch verdict_io first to trigger that side-effect, then re-prepend
# our scripts dir so `import run` binds to *our* run.py.
import verdict_io  # noqa: E402,F401
import overlap_scan  # noqa: E402,F401
sys.path.insert(0, str(SCRIPTS))
import run as run_module  # noqa: E402
import state_io  # noqa: E402


def _seed_state(root: Path, target_pr: int, others: list[int]) -> None:
    state_io.init_state(root, "owner/repo")
    state = state_io.load_state(root)
    state["prs"][str(target_pr)] = {
        "pr_number": target_pr,
        "phase": "OverlapCheck",
        "phase_entered_at": "2026-05-11T00:00:00+00:00",
        "last_action_at": "2026-05-11T00:00:00+00:00",
        "contributor_login": "alice",
        "needs_retriage": False,
        "new_comments_since_triage": 0,
        "next_action": {"skill": "prj-detect-overlap", "mode": None},
    }
    for o in others:
        state["prs"][str(o)] = {
            "pr_number": o,
            "phase": "ReviewPending",
            "phase_entered_at": "2026-04-01T00:00:00+00:00",
            "last_action_at": "2026-04-01T00:00:00+00:00",
            "contributor_login": "bob",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-review", "mode": None},
        }
    state_io.save_state(root, state)


class TestRunEndToEnd(unittest.TestCase):
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

    def _last_runlog(self) -> dict:
        log = state_io.runlog_path(self.root)
        self.assertTrue(log.exists(), "runlog should exist after run")
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def _invoke(self, argv: list[str]) -> int:
        full = ["run.py", "--project-root", str(self.root), *argv]
        with patch.object(sys, "argv", full):
            return run_module.main()

    def test_missing_prj_repo_returns_misconfigured(self):
        self._write_config(prj_repo=None)
        rc = self._invoke(["--pr-number", "100", "--scan"])
        self.assertEqual(rc, 2)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "misconfigured")

    def test_scan_emits_pairs_for_overlapping_pr(self):
        self._write_config()
        _seed_state(self.root, target_pr=100, others=[50, 60])

        files_by_pr = {
            100: "src/a.ts\nsrc/b.ts\nsrc/c.ts\n",
            50: "src/a.ts\nsrc/b.ts\nsrc/z.ts\n",  # overlap 2
            60: "src/x.ts\n",                        # overlap 0
        }

        def fake_run(args, timeout=60):
            number = int(args[2])
            return files_by_pr[number]

        with patch.object(overlap_scan, "_run_gh", side_effect=fake_run):
            rc = self._invoke(["--pr-number", "100", "--scan"])
        self.assertEqual(rc, 0)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "scan-ok")
        self.assertEqual(entry["mode"], "scan")
        self.assertIn("report", entry)
        pairs = entry["report"]["pairs"]
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["other_pr"], 50)
        self.assertEqual(pairs[0]["overlap_count"], 2)

    def test_dry_run_persist_writes_no_state_and_no_overlap_md(self):
        self._write_config()
        _seed_state(self.root, target_pr=100, others=[50])

        files_by_pr = {
            100: "src/a.ts\nsrc/b.ts\n",
            50: "src/a.ts\nsrc/b.ts\n",
        }

        def fake_run(args, timeout=60):
            number = int(args[2])
            return files_by_pr[number]

        with patch.object(overlap_scan, "_run_gh", side_effect=fake_run):
            rc = self._invoke([
                "--pr-number", "100",
                "--verdicts-json", '{"50": "complementary"}',
                "--dry-run",
            ])
        self.assertEqual(rc, 0)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "dry-run")
        # No overlap.md
        overlap_md = self.root / "_bmad-output" / "pr-workflow" / "prs" / "100" / "overlap.md"
        self.assertFalse(overlap_md.exists())
        # State unchanged
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["100"]["phase"], "OverlapCheck")

    def test_persist_writes_state_overlap_md_and_skips_label_when_skip_label_set(self):
        self._write_config()
        _seed_state(self.root, target_pr=100, others=[50])

        files_by_pr = {
            100: "src/a.ts\nsrc/b.ts\nsrc/c.ts\n",
            50: "src/a.ts\nsrc/b.ts\nsrc/z.ts\n",
        }

        scan_calls = []

        def fake_scan_run(args, timeout=60):
            scan_calls.append(list(args))
            number = int(args[2])
            return files_by_pr[number]

        # Patch overlap_scan's seam (used during scan) AND verdict_io's seam
        # (used when applying labels). With --skip-label the verdict_io seam
        # should NOT be invoked.
        label_calls = []

        def fake_label_run(args, timeout=60):
            label_calls.append(list(args))
            return ""

        with patch.object(overlap_scan, "_run_gh", side_effect=fake_scan_run), \
             patch.object(verdict_io, "_run_gh", side_effect=fake_label_run):
            rc = self._invoke([
                "--pr-number", "100",
                "--verdicts-json", '{"50": "independent"}',
                "--rationale-json", '{"50": "Different concerns despite overlap."}',
                "--skip-label",
            ])
        self.assertEqual(rc, 0)
        # Labels were not applied
        self.assertEqual(label_calls, [])

        entry = self._last_runlog()
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["strongest_verdict"], "independent")

        # State transitioned
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["100"]["phase"], "ReviewPending")
        self.assertEqual(
            state["prs"]["100"]["next_action"],
            {"skill": "prj-review", "mode": None},
        )

        # overlap.md written
        overlap_md = self.root / "_bmad-output" / "pr-workflow" / "prs" / "100" / "overlap.md"
        self.assertTrue(overlap_md.exists())
        body = overlap_md.read_text(encoding="utf-8")
        self.assertIn("#50", body)
        self.assertIn("`independent`", body)
        self.assertIn("Different concerns despite overlap.", body)

    def test_persist_redundant_marks_rejected_and_applies_labels(self):
        self._write_config()
        _seed_state(self.root, target_pr=100, others=[50])

        files_by_pr = {
            100: "src/a.ts\nsrc/b.ts\n",
            50: "src/a.ts\nsrc/b.ts\n",
        }

        label_calls = []

        def fake_scan_run(args, timeout=60):
            return files_by_pr[int(args[2])]

        def fake_label_run(args, timeout=60):
            label_calls.append(list(args))
            return ""

        with patch.object(overlap_scan, "_run_gh", side_effect=fake_scan_run), \
             patch.object(verdict_io, "_run_gh", side_effect=fake_label_run):
            rc = self._invoke([
                "--pr-number", "100",
                "--verdicts-json", '{"50": "redundant"}',
            ])
        self.assertEqual(rc, 0)
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["100"]["phase"], "Rejected")
        # gh called twice: once for prj/overlap:duplicate, once for prj/triage:definite-no
        self.assertEqual(len(label_calls), 2)
        applied = {call[-1] for call in label_calls}
        self.assertEqual(applied, {"prj/overlap:duplicate", "prj/triage:definite-no"})

    def test_bad_verdicts_arg_returns_2(self):
        self._write_config()
        _seed_state(self.root, target_pr=100, others=[50])
        rc = self._invoke([
            "--pr-number", "100",
            "--verdicts-json", '{"50": "bogus-verdict"}',
        ])
        self.assertEqual(rc, 2)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "bad-args")

    def test_gh_failure_during_scan_returns_3(self):
        self._write_config()
        _seed_state(self.root, target_pr=100, others=[50])

        def boom(args, timeout=60):
            raise overlap_scan.OverlapScanError("gh exited 1: not authorized")

        with patch.object(overlap_scan, "_run_gh", side_effect=boom):
            rc = self._invoke(["--pr-number", "100", "--scan"])
        self.assertEqual(rc, 3)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "gh-error")
        self.assertIn("not authorized", entry["error"])

    def test_runlog_has_required_fields(self):
        self._write_config()
        _seed_state(self.root, target_pr=100, others=[])

        # No others -> empty scan
        with patch.object(overlap_scan, "_run_gh", return_value="src/a.ts\n"):
            rc = self._invoke(["--pr-number", "100", "--scan"])
        self.assertEqual(rc, 0)
        entry = self._last_runlog()
        for key in ("ts", "action", "skill", "status", "duration_ms", "run_id"):
            self.assertIn(key, entry, f"runlog missing key: {key}")
        self.assertEqual(entry["action"], "detect-overlap")
        self.assertEqual(entry["skill"], "prj-detect-overlap")


if __name__ == "__main__":
    unittest.main()
