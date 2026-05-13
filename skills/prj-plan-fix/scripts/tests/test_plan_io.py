#!/usr/bin/env python3
"""Unit tests for plan_io.

Covers:
  - fix-plan schema validation (valid + multiple invalid forms)
  - write_plan refuses invalid markdown
  - write_plan writes atomically when valid
  - state transition FixPlan -> AdversarialCheck
  - record_adversarial_rejection bumps counter
  - should_escalate true once rejection_count >= PLAN_MAX_ATTEMPTS
  - escalate_please_advise transitions to PleaseAdvise
  - current_attempts reads the attempt number from the saved plan
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import plan_io  # noqa: E402
import state_io  # noqa: E402


VALID_PLAN = textwrap.dedent(
    """\
    # Fix Plan: PR #42

    ## Claim

    The `attach_data` tool drops the buffer on empty input.

    ## Failing Test

    ```python
    def test_attach_data_handles_empty_buffer():
        result = attach_data(b"")
        assert result.ok
    ```

    ## Proposed Diff

    ```diff
    - if buffer:
    + if buffer is not None:
    ```

    ## Rationale

    Empty bytes is a valid value distinct from None. Project convention is
    explicit None checks for optional parameters.

    ## Risk

    Could mask callers that previously relied on the empty-buffer drop
    behavior. None known in this codebase.

    ## Attempts

    - attempt: 1
    - previous_rejections: []
    """
)


def _seed_state_with_pr(project_root: Path, pr_number: int, phase: str = "FixPlan"):
    state_io.init_state(project_root, "octocat/hello")
    state = state_io.load_state(project_root)
    now = datetime.now(timezone.utc).isoformat()
    state["prs"][str(pr_number)] = {
        "pr_number": pr_number,
        "phase": phase,
        "phase_entered_at": now,
        "last_action_at": now,
        "contributor_login": "alice",
    }
    state_io.save_state(project_root, state)


class TestValidatePlan(unittest.TestCase):
    def test_valid_plan_returns_no_issues(self):
        self.assertEqual(plan_io.validate_plan(VALID_PLAN), [])

    def test_missing_heading_flagged(self):
        bad = VALID_PLAN.replace("## Risk", "## Hazards")
        issues = plan_io.validate_plan(bad)
        self.assertTrue(any("Risk" in i for i in issues))

    def test_failing_test_without_code_block_flagged(self):
        bad = textwrap.dedent(
            """\
            # Fix Plan: PR #42

            ## Claim

            something broke.

            ## Failing Test

            no fenced block here, just prose.

            ## Proposed Diff

            ```diff
            - a
            + b
            ```

            ## Rationale

            r

            ## Risk

            x

            ## Attempts

            - attempt: 1
            - previous_rejections: []
            """
        )
        issues = plan_io.validate_plan(bad)
        self.assertTrue(
            any("fenced code block" in i for i in issues),
            f"expected fenced-code-block complaint, got {issues}",
        )

    def test_attempts_section_requires_attempt_line(self):
        bad = VALID_PLAN.replace("- attempt: 1", "- attempt: not-an-int")
        issues = plan_io.validate_plan(bad)
        self.assertTrue(any("attempt:" in i for i in issues))

    def test_attempts_section_requires_previous_rejections(self):
        bad = VALID_PLAN.replace("- previous_rejections: []", "- other_thing: []")
        issues = plan_io.validate_plan(bad)
        self.assertTrue(any("previous_rejections" in i for i in issues))


class TestWritePlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "_bmad").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_valid_plan_persists_to_cache(self):
        path = plan_io.write_plan(self.root, 42, VALID_PLAN)
        self.assertTrue(path.exists())
        self.assertIn("# Fix Plan: PR #42", path.read_text(encoding="utf-8"))

    def test_write_invalid_plan_raises(self):
        with self.assertRaises(ValueError):
            plan_io.write_plan(self.root, 42, "not a plan")

    def test_current_attempts_reads_attempt_field(self):
        plan_io.write_plan(self.root, 42, VALID_PLAN)
        self.assertEqual(plan_io.current_attempts(self.root, 42), 1)

    def test_current_attempts_zero_when_no_plan(self):
        self.assertEqual(plan_io.current_attempts(self.root, 99), 0)


class TestStateTransitions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "_bmad").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_transition_to_adversarial_sets_phase(self):
        _seed_state_with_pr(self.root, 42, phase="FixPlan")
        plan_io.transition_to_adversarial(self.root, 42)
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "AdversarialCheck")
        self.assertEqual(
            state["prs"]["42"]["next_action"],
            {"skill": "prj-validate-adversarial", "mode": None},
        )

    def test_record_adversarial_rejection_increments_counter(self):
        _seed_state_with_pr(self.root, 42)
        plan_io.record_adversarial_rejection(self.root, 42, "scope-too-broad")
        plan_io.record_adversarial_rejection(self.root, 42, "no-AC-alignment")
        state = state_io.load_state(self.root)
        pr = state["prs"]["42"]
        self.assertEqual(pr["rejection_count"], 2)
        self.assertEqual(len(pr["adversarial_rejections"]), 2)
        self.assertEqual(pr["adversarial_rejections"][0]["summary"], "scope-too-broad")

    def test_should_escalate_after_two_rejections(self):
        _seed_state_with_pr(self.root, 42)
        self.assertFalse(plan_io.should_escalate(self.root, 42))
        plan_io.record_adversarial_rejection(self.root, 42, "x")
        self.assertFalse(plan_io.should_escalate(self.root, 42))
        plan_io.record_adversarial_rejection(self.root, 42, "y")
        self.assertTrue(plan_io.should_escalate(self.root, 42))

    def test_escalate_please_advise_transitions_phase(self):
        _seed_state_with_pr(self.root, 42)
        plan_io.escalate_please_advise(self.root, 42, "two rejects")
        state = state_io.load_state(self.root)
        pr = state["prs"]["42"]
        self.assertEqual(pr["phase"], "PleaseAdvise")
        self.assertEqual(pr["please_advise_reason"], "two rejects")
        self.assertIsNone(pr["next_action"])


if __name__ == "__main__":
    unittest.main()
