#!/usr/bin/env python3
"""End-to-end tests for run.py.

Each test builds a tmp project root with `_bmad/config.toml`, seeds a PR in
AdversarialCheck phase, writes the required per-PR cache files, mocks the
regression-suite runner via patch.object, and exercises run.main(). State
mutations, adversarial.md content, and runlog entries are asserted against the
temp filesystem.
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

# Importing run wires up sys.path for state_io as a side effect, so import
# state_io after run.
import run as run_module  # noqa: E402

import adversarial_io  # noqa: E402
import checklist_io  # noqa: E402
import regression_run  # noqa: E402
import state_io  # noqa: E402


def _all_pass_findings() -> list[dict]:
    return [
        {"item": item, "passes": True, "finding": f"{item} passes per evidence."}
        for item in checklist_io.REQUIRED_ITEMS
    ]


def _mixed_findings(failed_item: str) -> list[dict]:
    out = []
    for item in checklist_io.REQUIRED_ITEMS:
        out.append({
            "item": item,
            "passes": item != failed_item,
            "finding": f"{item} ok" if item != failed_item else f"{item} fails per cite.",
        })
    return out


class TestRunValidation(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "_bmad").mkdir()
        self.pr_number = 101
        self._write_config()
        self._seed_state(self.pr_number)
        self._seed_pr_cache(self.pr_number)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_config(self):
        (self.root / "_bmad" / "config.toml").write_text(
            "[modules.prj]\nprj_repo = \"octo/hello\"\n",
            encoding="utf-8",
        )

    def _seed_state(self, pr_number: int, phase: str = "AdversarialCheck", reject_count: int = 0):
        state_io.init_state(self.root, "octo/hello")
        state = state_io.load_state(self.root)
        state["prs"][str(pr_number)] = {
            "pr_number": pr_number,
            "phase": phase,
            "phase_entered_at": "2026-05-09T00:00:00+00:00",
            "last_action_at": "2026-05-09T00:00:00+00:00",
            "contributor_login": "alice",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-validate-adversarial", "mode": None},
            "adversarial_reject_count": reject_count,
        }
        state_io.save_state(self.root, state)

    def _seed_pr_cache(self, pr_number: int):
        cache = self.root / "_bmad-output" / "pr-workflow" / "prs" / str(pr_number)
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "fix-plan.md").write_text("# fix-plan\n\nfailing test + diff\n", encoding="utf-8")
        (cache / "verification.md").write_text("# verification\n\nreproduced.\n", encoding="utf-8")
        (cache / "review.md").write_text("# review\n\nnotes.\n", encoding="utf-8")

    def _last_runlog(self) -> dict:
        log = state_io.runlog_path(self.root)
        self.assertTrue(log.exists(), "runlog should exist after run")
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def _invoke(self, extra_args, regression_result=None):
        argv = ["run.py", "--project-root", str(self.root), "--pr", str(self.pr_number)] + list(extra_args)
        rr = regression_result or regression_run.RegressionResult(
            status="ok",
            command="echo ok",
            exit_code=0,
            returncode_indicates_pass=True,
            stdout_tail="ok",
            stderr_tail="",
            regressions=0,
            duration_ms=10,
        )
        with patch.object(sys, "argv", argv), \
             patch.object(run_module.regression_run, "run_regression", return_value=rr):
            return run_module.main()

    # --- verdict paths ---

    def test_pass_verdict_advances_to_fix_impl(self):
        response_path = self.root / "response.json"
        response_path.write_text(
            json.dumps({"verdict": "pass", "findings": _all_pass_findings()}),
            encoding="utf-8",
        )
        rc = self._invoke(["--finalize-response", str(response_path)])
        self.assertEqual(rc, 0)

        state = state_io.load_state(self.root)
        pr = state["prs"][str(self.pr_number)]
        self.assertEqual(pr["phase"], "FixImpl")
        self.assertEqual(pr["adversarial_reject_count"], 0)
        self.assertEqual(pr["next_action"], {"skill": "prj-implement-fix", "mode": None})

        report = (
            self.root / "_bmad-output" / "pr-workflow" / "prs"
            / str(self.pr_number) / "adversarial.md"
        )
        self.assertTrue(report.exists())
        self.assertIn("LLM verdict: `pass`", report.read_text(encoding="utf-8"))

        entry = self._last_runlog()
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["verdict"], "pass")
        self.assertEqual(entry["effective_verdict"], "pass")
        self.assertEqual(entry["next_phase"], "FixImpl")

    def test_first_reject_routes_to_fix_plan(self):
        response = {
            "verdict": "reject",
            "summary": "scope creep",
            "findings": _mixed_findings("scope"),
            "concerns": ["touches unrelated config"],
        }
        response_path = self.root / "response.json"
        response_path.write_text(json.dumps(response), encoding="utf-8")
        rc = self._invoke(["--finalize-response", str(response_path)])
        self.assertEqual(rc, 0)

        state = state_io.load_state(self.root)
        pr = state["prs"][str(self.pr_number)]
        self.assertEqual(pr["phase"], "FixPlan")
        self.assertEqual(pr["adversarial_reject_count"], 1)
        self.assertEqual(pr["next_action"], {"skill": "prj-plan-fix", "mode": None})

        entry = self._last_runlog()
        self.assertEqual(entry["verdict"], "reject")
        self.assertEqual(entry["effective_verdict"], "reject")

    def test_second_reject_escalates_to_please_advise(self):
        # Pre-seed the PR with a prior reject already on the books.
        self._seed_state(self.pr_number, phase="AdversarialCheck", reject_count=1)

        response = {
            "verdict": "reject",
            "findings": _mixed_findings("ac_alignment"),
        }
        response_path = self.root / "response.json"
        response_path.write_text(json.dumps(response), encoding="utf-8")
        rc = self._invoke(["--finalize-response", str(response_path)])
        self.assertEqual(rc, 0)

        state = state_io.load_state(self.root)
        pr = state["prs"][str(self.pr_number)]
        self.assertEqual(pr["phase"], "PleaseAdvise")
        self.assertIsNone(pr["next_action"])
        self.assertEqual(pr["adversarial_reject_count"], 0)

        entry = self._last_runlog()
        self.assertEqual(entry["effective_verdict"], "escalate")
        self.assertEqual(entry["next_phase"], "PleaseAdvise")

    def test_escalate_verdict_goes_to_please_advise(self):
        response = {
            "verdict": "escalate",
            "findings": _mixed_findings("worst_case_probe"),
            "summary": "needs a human",
        }
        response_path = self.root / "response.json"
        response_path.write_text(json.dumps(response), encoding="utf-8")
        rc = self._invoke(["--finalize-response", str(response_path)])
        self.assertEqual(rc, 0)

        state = state_io.load_state(self.root)
        pr = state["prs"][str(self.pr_number)]
        self.assertEqual(pr["phase"], "PleaseAdvise")
        self.assertIsNone(pr["next_action"])

        entry = self._last_runlog()
        self.assertEqual(entry["effective_verdict"], "escalate")

    # --- regression auto-reject path ---

    def test_regression_auto_rejects_without_consulting_llm(self):
        # A finalize-response would normally apply, but the regression gate
        # must short-circuit it. We do NOT supply a response; if the gate
        # works, the verdict is still written to disk.
        rr = regression_run.RegressionResult(
            status="ok",
            command="pytest",
            exit_code=1,
            returncode_indicates_pass=False,
            stdout_tail="FAILED test_foo",
            stderr_tail="",
            regressions=2,
            duration_ms=900,
        )
        rc = self._invoke([], regression_result=rr)
        self.assertEqual(rc, 0)

        state = state_io.load_state(self.root)
        pr = state["prs"][str(self.pr_number)]
        # First reject: should land at FixPlan, not escalate yet.
        self.assertEqual(pr["phase"], "FixPlan")
        self.assertEqual(pr["adversarial_reject_count"], 1)

        report = (
            self.root / "_bmad-output" / "pr-workflow" / "prs"
            / str(self.pr_number) / "adversarial.md"
        )
        self.assertTrue(report.exists())
        body = report.read_text(encoding="utf-8")
        self.assertIn("Auto-reject: regression-suite hard gate", body)
        self.assertIn("Regressions detected: `2`", body)

        entry = self._last_runlog()
        self.assertEqual(entry["gate"], "regression-auto-reject")
        self.assertEqual(entry["verdict"], "reject")

    def test_regression_auto_reject_escalates_on_second_consecutive(self):
        self._seed_state(self.pr_number, phase="AdversarialCheck", reject_count=1)
        rr = regression_run.RegressionResult(
            status="ok",
            command="pytest",
            exit_code=1,
            returncode_indicates_pass=False,
            stdout_tail="",
            stderr_tail="boom",
            regressions=1,
            duration_ms=10,
        )
        rc = self._invoke([], regression_result=rr)
        self.assertEqual(rc, 0)

        state = state_io.load_state(self.root)
        pr = state["prs"][str(self.pr_number)]
        self.assertEqual(pr["phase"], "PleaseAdvise")
        self.assertEqual(pr["adversarial_reject_count"], 0)

    # --- error paths ---

    def test_missing_prj_repo_returns_misconfigured(self):
        (self.root / "_bmad" / "config.toml").write_text("[modules.prj]\n", encoding="utf-8")
        rc = self._invoke([])
        self.assertEqual(rc, 2)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "misconfigured")

    def test_pr_not_in_state_returns_2(self):
        # Validate a PR that doesn't exist in state.
        argv = ["run.py", "--project-root", str(self.root), "--pr", "9999"]
        rr = regression_run.RegressionResult(
            status="ok", command="x", exit_code=0,
            returncode_indicates_pass=True, stdout_tail="", stderr_tail="",
            regressions=0, duration_ms=1,
        )
        with patch.object(sys, "argv", argv), \
             patch.object(run_module.regression_run, "run_regression", return_value=rr):
            rc = run_module.main()
        self.assertEqual(rc, 2)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "pr-not-in-state")

    def test_wrong_phase_returns_2(self):
        self._seed_state(self.pr_number, phase="ReviewPending")
        rc = self._invoke([])
        self.assertEqual(rc, 2)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "wrong-phase")

    def test_missing_cache_returns_3(self):
        # Erase fix-plan.md so cache is incomplete.
        (self.root / "_bmad-output" / "pr-workflow" / "prs" / str(self.pr_number) / "fix-plan.md").unlink()
        rc = self._invoke([])
        self.assertEqual(rc, 3)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "cache-missing")

    def test_invalid_response_returns_4(self):
        # Regression gate clean; supply malformed response.
        response_path = self.root / "response.json"
        response_path.write_text(json.dumps({"verdict": "pass", "findings": []}), encoding="utf-8")
        rc = self._invoke(["--finalize-response", str(response_path)])
        self.assertEqual(rc, 4)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "invalid-response")

    def test_invalid_response_json_returns_4(self):
        response_path = self.root / "response.json"
        response_path.write_text("not json", encoding="utf-8")
        rc = self._invoke(["--finalize-response", str(response_path)])
        self.assertEqual(rc, 4)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "invalid-response")

    def test_dry_run_does_not_mutate_state(self):
        rc = self._invoke(["--dry-run"])
        self.assertEqual(rc, 0)
        state = state_io.load_state(self.root)
        pr = state["prs"][str(self.pr_number)]
        self.assertEqual(pr["phase"], "AdversarialCheck")
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "dry-run")

    def test_gather_only_mode_does_not_mutate_state(self):
        # Regression clean, no finalize-response: should emit gathered bundle.
        rc = self._invoke([])
        self.assertEqual(rc, 0)
        state = state_io.load_state(self.root)
        pr = state["prs"][str(self.pr_number)]
        self.assertEqual(pr["phase"], "AdversarialCheck")
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "gathered")


if __name__ == "__main__":
    unittest.main()
