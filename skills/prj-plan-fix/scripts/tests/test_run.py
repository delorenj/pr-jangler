#!/usr/bin/env python3
"""End-to-end tests for prj-plan-fix/run.py.

The harness mocks the test runner to simulate the failing -> passing flow.
Worktree provisioning is bypassed by pre-staging the worktree directory.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import plan_io  # noqa: E402
import run as run_module  # noqa: E402
import state_io  # noqa: E402
import test_runner  # noqa: E402
import worktree as worktree_mod  # noqa: E402


VALID_PLAN = textwrap.dedent(
    """\
    # Fix Plan: PR #42

    ## Claim

    Empty buffer drops attachment.

    ## Failing Test

    ```python
    def test_empty():
        pass
    ```

    ## Proposed Diff

    ```diff
    - if x:
    + if x is not None:
    ```

    ## Rationale

    Explicit none check matches convention.

    ## Risk

    None known.

    ## Attempts

    - attempt: 1
    - previous_rejections: []
    """
)


def _make_result(exit_code: int, label: str = "") -> test_runner.RunResult:
    return test_runner.RunResult(
        command="bun test",
        cwd="/tmp",
        exit_code=exit_code,
        stdout=f"{label}-stdout",
        stderr=f"{label}-stderr",
        duration_ms=10,
    )


class TestRunEntry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "_bmad").mkdir()
        # Seed state with PR 42 at phase FixPlan
        state_io.init_state(self.root, "octocat/hello")
        now = datetime.now(timezone.utc).isoformat()
        state = state_io.load_state(self.root)
        state["prs"]["42"] = {
            "pr_number": 42,
            "phase": "FixPlan",
            "phase_entered_at": now,
            "last_action_at": now,
            "contributor_login": "alice",
        }
        state_io.save_state(self.root, state)
        # Pre-stage worktree so the worktree module reuses it.
        wt = worktree_mod.worktree_path(self.root, 42)
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /tmp/x", encoding="utf-8")
        self.wt = wt
        self.cache = (
            self.root / "_bmad-output" / "pr-workflow" / "prs" / "42"
        )
        self.cache.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_verification(self, verdict: str = "verified"):
        (self.cache / "verification.md").write_text(
            f"# Verification\n\nverdict: {verdict}\n",
            encoding="utf-8",
        )

    def _seed_inputs(self, plan: str = VALID_PLAN):
        (self.cache / "plan-draft.md").write_text(plan, encoding="utf-8")
        (self.cache / "failing-test.cmd").write_text("bun test", encoding="utf-8")
        (self.cache / "full-suite.cmd").write_text("bun test", encoding="utf-8")

    def _patch_runner(self, sequence):
        """Patch test_runner.run_tests to consume from a fixed sequence."""
        it = iter(sequence)

        def fake_run(command, cwd, timeout=300, _runner=None):
            return next(it)

        return patch.object(run_module.test_runner, "run_tests", side_effect=fake_run)

    def _patch_branch(self):
        return patch.object(worktree_mod, "_current_branch", return_value="pr/42")

    def test_refuses_when_not_verified(self):
        self._seed_verification(verdict="not-verified")
        self._seed_inputs()
        with self._patch_branch():
            rc = run_module.main(["--pr-number", "42", "--project-root", str(self.root)])
        self.assertEqual(rc, run_module.EXIT_NOT_VERIFIED)

    def test_refuses_when_verification_missing(self):
        # No verification.md at all.
        self._seed_inputs()
        with self._patch_branch():
            rc = run_module.main(["--pr-number", "42", "--project-root", str(self.root)])
        self.assertEqual(rc, run_module.EXIT_NOT_VERIFIED)

    def test_refuses_when_plan_draft_missing(self):
        self._seed_verification()
        (self.cache / "failing-test.cmd").write_text("bun test", encoding="utf-8")
        with self._patch_branch():
            rc = run_module.main(["--pr-number", "42", "--project-root", str(self.root)])
        self.assertEqual(rc, run_module.EXIT_TEST_NOT_WRITABLE)

    def test_refuses_when_test_passes_before_fix(self):
        self._seed_verification()
        self._seed_inputs()
        # Sequence: failing-test BEFORE fix = PASS (exit 0) -> bug not demonstrated
        sequence = [_make_result(0, "before")]
        with self._patch_branch(), self._patch_runner(sequence):
            rc = run_module.main(["--pr-number", "42", "--project-root", str(self.root)])
        self.assertEqual(rc, run_module.EXIT_TEST_NOT_FAILING)
        # Plan must NOT have been written
        self.assertFalse(plan_io.fix_plan_path(self.root, 42).exists())
        # Phase unchanged
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "FixPlan")

    def test_refuses_on_regression(self):
        self._seed_verification()
        self._seed_inputs()
        # before: FAIL (good), after-fix: PASS (good), full-suite: FAIL (regression)
        sequence = [
            _make_result(1, "before"),
            _make_result(0, "after"),
            _make_result(1, "suite"),
        ]
        with self._patch_branch(), self._patch_runner(sequence):
            rc = run_module.main(["--pr-number", "42", "--project-root", str(self.root)])
        self.assertEqual(rc, run_module.EXIT_REGRESSION)
        self.assertFalse(plan_io.fix_plan_path(self.root, 42).exists())
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "FixPlan")

    def test_invalid_plan_rejected(self):
        self._seed_verification()
        self._seed_inputs(plan="not actually a plan")
        # before: FAIL, after-fix: PASS, full-suite: PASS
        sequence = [
            _make_result(1, "before"),
            _make_result(0, "after"),
            _make_result(0, "suite"),
        ]
        with self._patch_branch(), self._patch_runner(sequence):
            rc = run_module.main(["--pr-number", "42", "--project-root", str(self.root)])
        self.assertEqual(rc, run_module.EXIT_INVALID_PLAN)

    def test_happy_path_writes_plan_and_transitions(self):
        self._seed_verification()
        self._seed_inputs()
        sequence = [
            _make_result(1, "before"),
            _make_result(0, "after"),
            _make_result(0, "suite"),
        ]
        with self._patch_branch(), self._patch_runner(sequence):
            rc = run_module.main(["--pr-number", "42", "--project-root", str(self.root)])
        self.assertEqual(rc, run_module.EXIT_OK)
        # Plan written
        self.assertTrue(plan_io.fix_plan_path(self.root, 42).exists())
        # Phase advanced
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "AdversarialCheck")
        # Runlog entry exists and tagged 'ok'
        log_path = state_io.runlog_path(self.root)
        self.assertTrue(log_path.exists())
        last = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        import json
        entry = json.loads(last)
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["skill"], "prj-plan-fix")
        self.assertEqual(entry["pr_number"], 42)

    def test_escalation_after_two_rejections(self):
        self._seed_verification()
        self._seed_inputs()
        # Pre-seed two adversarial rejections on the PR
        plan_io.record_adversarial_rejection(self.root, 42, "first")
        plan_io.record_adversarial_rejection(self.root, 42, "second")
        sequence = [
            _make_result(1, "before"),
            _make_result(0, "after"),
            _make_result(0, "suite"),
        ]
        with self._patch_branch(), self._patch_runner(sequence):
            rc = run_module.main(["--pr-number", "42", "--project-root", str(self.root)])
        self.assertEqual(rc, run_module.EXIT_ESCALATED)
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "PleaseAdvise")

    def test_dry_run_does_not_write_plan(self):
        self._seed_verification()
        self._seed_inputs()
        sequence = [
            _make_result(1, "before"),
            _make_result(0, "after"),
            _make_result(0, "suite"),
        ]
        with self._patch_branch(), self._patch_runner(sequence):
            rc = run_module.main([
                "--pr-number", "42", "--project-root", str(self.root), "--dry-run",
            ])
        self.assertEqual(rc, run_module.EXIT_OK)
        self.assertFalse(plan_io.fix_plan_path(self.root, 42).exists())
        # Phase unchanged in dry-run
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "FixPlan")


if __name__ == "__main__":
    unittest.main()
