#!/usr/bin/env python3
"""Unit tests for prj-decision/scripts/rubric.py.

Covers each decision class:
  - ready-to-merge:    blockers clean, tests pass, overlap ok, adversarial pass
  - request-changes:   blockers present, or adversarial reject, or tests failing
  - close-as-not-now:  triple gate enforced (must be ALL three)
  - ambiguous cases:   confidence falls to low when no actionable signal
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import aggregate  # noqa: E402
import rubric  # noqa: E402


def _signals(**kwargs) -> aggregate.Signals:
    """Helper: build a Signals object from kwargs with sensible defaults."""
    base = dict(pr_number=1, has_review=True)
    base.update(kwargs)
    return aggregate.Signals(**base)


class TestReadyToMerge(unittest.TestCase):
    def test_clean_review_no_implementation_needed(self):
        s = _signals(blocker_count=0, major_count=0, has_implementation=False)
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "ready-to-merge")
        self.assertEqual(result.confidence, "high")
        self.assertIn("no-implementation-needed", " ".join(result.gates_passed))

    def test_implementation_tests_passing(self):
        s = _signals(
            blocker_count=1, major_count=0,
            has_implementation=True, tests_status="pass", regressions="none",
            adversarial_verdict="pass", overlap_verdict="independent",
        )
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "ready-to-merge")

    def test_ambiguity_drops_confidence_to_medium(self):
        s = _signals(
            blocker_count=0, has_implementation=False,
            ambiguity_signals=["single-adversarial-reject"],
        )
        # Empty ambiguity does NOT disqualify ready-to-merge but lowers confidence.
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "ready-to-merge")
        self.assertEqual(result.confidence, "medium")

    def test_complementary_overlap_still_ready(self):
        s = _signals(blocker_count=0, overlap_verdict="complementary")
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "ready-to-merge")


class TestRequestChanges(unittest.TestCase):
    def test_blocker_findings_block_ready(self):
        s = _signals(blocker_count=2, has_implementation=False)
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "request-changes")
        self.assertEqual(result.confidence, "high")

    def test_adversarial_reject_blocks_ready(self):
        s = _signals(
            blocker_count=0, has_implementation=False,
            adversarial_verdict="reject", has_fix_plan=True,
        )
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "request-changes")
        self.assertIn("adversarial-reject", " ".join(result.gates_passed))

    def test_overlap_conflicting_blocks_ready(self):
        s = _signals(overlap_verdict="conflicting")
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "request-changes")

    def test_failing_tests_block_ready(self):
        s = _signals(
            blocker_count=0,
            has_implementation=True, tests_status="fail", regressions="present",
        )
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "request-changes")

    def test_low_confidence_when_no_signal(self):
        # Edge case: overlap blocks the ready path but there is no other
        # actionable signal pointing to request-changes. The rubric should
        # report low confidence so the LLM can route to please-advise.
        # We construct: ready-to-merge falsified by single-reject adversarial,
        # but the request_changes_signals path catches that. So we need to
        # falsify ready AND keep request-changes signals empty. The only way:
        # overlap=redundant (falsifies ready), no blockers/majors/adv/etc,
        # AND fail the close-as-not-now gate (no maintainer-inactivity).
        s = _signals(
            blocker_count=0, major_count=0,
            overlap_verdict="redundant",
            days_since_maintainer=5,
        )
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "request-changes")
        # overlap-redundant IS a request-changes signal via the gates path,
        # so we explicitly check no signals appear when even overlap is None
        # but ready is falsified some other way → trickier; assert at least
        # the confidence lookup logic returns 'low' when zero rc signals.
        # Build a clearly-no-signal case directly:
        s2 = _signals(
            blocker_count=0, major_count=0,
            has_implementation=False,
            adversarial_verdict="pass",
            overlap_verdict="independent",
            # ready-to-merge would PASS here. We need to falsify ready while
            # leaving zero rc signals. Forced via verification ambiguous:
            verification_verdict="ambiguous",
        )
        # ready still passes here because verification ambiguity is not in
        # the ready-gates. But ambiguity_signals contains a flag, dropping
        # confidence to medium. That's medium, not low. Drop ready by setting
        # overlap conflict:
        s3 = aggregate.Signals(
            pr_number=1, has_review=True,
            overlap_verdict="conflicting",
        )
        # request_changes_signals will see overlap-conflicting -> 1 signal -> medium.
        # To get low: zero signals AND ready falsified.
        # That happens only if adversarial=escalate (not in rc signals, not in ready).
        s4 = aggregate.Signals(
            pr_number=1, has_review=True,
            adversarial_verdict="escalate",
        )
        result4 = rubric.evaluate(s4)
        self.assertEqual(result4.decision, "request-changes")
        self.assertEqual(result4.confidence, "low")


class TestCloseAsNotNow(unittest.TestCase):
    def test_triple_gate_definite_no(self):
        s = _signals(
            days_since_maintainer=45,
            triage_class="definite-no",
        )
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "close-as-not-now")
        self.assertEqual(result.confidence, "high")

    def test_triple_gate_overlap_redundant(self):
        s = _signals(
            days_since_maintainer=60,
            overlap_verdict="redundant",
        )
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "close-as-not-now")

    def test_triple_gate_two_adversarial_escalations(self):
        s = _signals(
            days_since_maintainer=40,
            adversarial_escalation_count=2,
        )
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "close-as-not-now")

    def test_inactive_alone_does_not_close(self):
        # Gate (a) satisfied but (b) is not. Must NOT close.
        s = _signals(
            days_since_maintainer=90,
            blocker_count=1,
        )
        result = rubric.evaluate(s)
        self.assertNotEqual(result.decision, "close-as-not-now")
        self.assertEqual(result.decision, "request-changes")

    def test_definite_no_alone_does_not_close(self):
        # Gate (b) satisfied, (a) not (maintainer was active recently). Must NOT close.
        s = _signals(
            days_since_maintainer=5,
            triage_class="definite-no",
        )
        result = rubric.evaluate(s)
        self.assertNotEqual(result.decision, "close-as-not-now")

    def test_one_adversarial_escalation_does_not_close(self):
        # Single escalation is one short of the gate.
        s = _signals(
            days_since_maintainer=60,
            adversarial_escalation_count=1,
        )
        result = rubric.evaluate(s)
        self.assertNotEqual(result.decision, "close-as-not-now")

    def test_no_review_evidence_does_not_close(self):
        # Evidence gate (c): we refuse to close un-reviewed PRs.
        s = aggregate.Signals(
            pr_number=1,
            days_since_maintainer=60,
            adversarial_escalation_count=2,
            has_review=False,
            triage_class=None,
        )
        result = rubric.evaluate(s)
        self.assertNotEqual(result.decision, "close-as-not-now")

    def test_inactive_threshold_exactly_30_does_not_qualify(self):
        # ">30" means strictly greater. 30 exactly should fall through.
        s = _signals(
            days_since_maintainer=30,
            triage_class="definite-no",
        )
        result = rubric.evaluate(s)
        self.assertNotEqual(result.decision, "close-as-not-now")

    def test_inactive_threshold_31_qualifies(self):
        s = _signals(
            days_since_maintainer=31,
            triage_class="definite-no",
        )
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "close-as-not-now")


class TestRubricOrderingAndContract(unittest.TestCase):
    def test_close_outranks_ready(self):
        # Triple gate met AND review is clean. Close wins.
        s = _signals(
            blocker_count=0, has_implementation=False,
            days_since_maintainer=60,
            adversarial_escalation_count=2,
        )
        result = rubric.evaluate(s)
        self.assertEqual(result.decision, "close-as-not-now")

    def test_reasoning_hints_present(self):
        s = _signals(blocker_count=0, has_implementation=False)
        result = rubric.evaluate(s)
        self.assertTrue(result.reasoning_hints)

    def test_decision_classes_match_documented_set(self):
        self.assertEqual(
            set(rubric.DECISION_CLASSES),
            {"ready-to-merge", "request-changes", "close-as-not-now"},
        )

    def test_close_threshold_constant(self):
        self.assertEqual(rubric.CLOSE_AS_NOT_NOW_DAYS, 30)


if __name__ == "__main__":
    unittest.main()
