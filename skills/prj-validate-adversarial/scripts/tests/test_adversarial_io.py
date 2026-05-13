#!/usr/bin/env python3
"""Unit tests for adversarial_io.apply_transition and render_adversarial_md."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import adversarial_io  # noqa: E402


def _seed_state(pr_number: int, reject_count: int = 0) -> dict:
    """Build a minimal state structure with one PR in AdversarialCheck phase."""
    key = str(pr_number)
    return {
        "version": "1.0",
        "repo": "octo/hello",
        "last_updated": "2026-05-09T00:00:00+00:00",
        "heartbeat_count": 1,
        "prs": {
            key: {
                "pr_number": pr_number,
                "phase": "AdversarialCheck",
                "phase_entered_at": "2026-05-09T00:00:00+00:00",
                "last_action_at": "2026-05-09T00:00:00+00:00",
                "contributor_login": "alice",
                "needs_retriage": False,
                "new_comments_since_triage": 0,
                "next_action": {"skill": "prj-validate-adversarial", "mode": None},
                "adversarial_reject_count": reject_count,
            }
        },
    }


class TestApplyTransition(unittest.TestCase):
    def test_pass_moves_to_fix_impl_resets_counter(self):
        state = _seed_state(101, reject_count=1)
        out = adversarial_io.apply_transition(state, 101, "pass")
        pr = state["prs"]["101"]
        self.assertEqual(pr["phase"], "FixImpl")
        self.assertEqual(pr["next_action"], {"skill": "prj-implement-fix", "mode": None})
        self.assertEqual(pr["adversarial_reject_count"], 0)
        self.assertEqual(out["effective_verdict"], "pass")
        self.assertEqual(out["next_phase"], "FixImpl")

    def test_first_reject_goes_back_to_fix_plan_and_increments(self):
        state = _seed_state(202, reject_count=0)
        out = adversarial_io.apply_transition(state, 202, "reject")
        pr = state["prs"]["202"]
        self.assertEqual(pr["phase"], "FixPlan")
        self.assertEqual(pr["next_action"], {"skill": "prj-plan-fix", "mode": None})
        self.assertEqual(pr["adversarial_reject_count"], 1)
        self.assertEqual(out["effective_verdict"], "reject")
        self.assertEqual(out["next_phase"], "FixPlan")

    def test_second_reject_escalates_to_please_advise(self):
        state = _seed_state(303, reject_count=1)
        out = adversarial_io.apply_transition(state, 303, "reject")
        pr = state["prs"]["303"]
        self.assertEqual(pr["phase"], "PleaseAdvise")
        self.assertIsNone(pr["next_action"])
        self.assertEqual(pr["adversarial_reject_count"], 0)
        self.assertEqual(out["effective_verdict"], "escalate")
        self.assertFalse(pr["user_acknowledged_please_advise"])

    def test_escalate_moves_to_please_advise_resets_counter(self):
        state = _seed_state(404, reject_count=1)
        out = adversarial_io.apply_transition(state, 404, "escalate")
        pr = state["prs"]["404"]
        self.assertEqual(pr["phase"], "PleaseAdvise")
        self.assertIsNone(pr["next_action"])
        self.assertEqual(pr["adversarial_reject_count"], 0)
        self.assertEqual(out["effective_verdict"], "escalate")

    def test_unknown_verdict_raises(self):
        state = _seed_state(505)
        with self.assertRaises(ValueError):
            adversarial_io.apply_transition(state, 505, "abstain")

    def test_missing_pr_raises_keyerror(self):
        state = _seed_state(606)
        with self.assertRaises(KeyError):
            adversarial_io.apply_transition(state, 999, "pass")


class TestRenderAndWriteReport(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_render_includes_verdict_and_findings(self):
        findings = [
            {"item": "test_validity", "passes": True, "finding": "covers user-observable behavior"},
            {"item": "scope", "passes": False, "finding": "touches unrelated config"},
            {"item": "side_effects", "passes": True, "finding": "caller-local"},
            {"item": "regressions", "passes": True, "finding": "regression count = 0"},
            {"item": "ac_alignment", "passes": True, "finding": "matches AC"},
            {"item": "worst_case_probe", "passes": True, "finding": "skipping fix breaks X"},
        ]
        regression = {
            "command": "pytest",
            "status": "ok",
            "exit_code": 0,
            "regressions": 0,
            "duration_ms": 1234,
            "stdout_tail": "passed",
            "stderr_tail": "",
        }
        content = adversarial_io.render_adversarial_md(
            pr_number=42,
            verdict="reject",
            summary="scope creep",
            findings=findings,
            concerns=["touches an unrelated path"],
            regression=regression,
        )
        self.assertIn("# Adversarial Validation Report — PR #42", content)
        self.assertIn("LLM verdict: `reject`", content)
        self.assertIn("scope — FAIL", content)
        self.assertIn("touches unrelated config", content)
        self.assertIn("Concerns", content)
        self.assertIn("regression count = 0", content)

    def test_write_report_atomic_and_creates_parents(self):
        path = adversarial_io.write_adversarial_report(self.root, 7, "hello\n")
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), "hello\n")
        self.assertEqual(
            path,
            self.root / "_bmad-output" / "pr-workflow" / "prs" / "7" / "adversarial.md",
        )


if __name__ == "__main__":
    unittest.main()
