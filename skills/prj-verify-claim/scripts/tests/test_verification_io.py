#!/usr/bin/env python3
"""Unit tests for verification_io.py.

Each of the three verdicts must produce:
  - a verification.md with the verdict + strategy + rationale
  - the correct state-machine transition (phase + next_action)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import verification_io  # noqa: E402
# verification_io adds prj-orchestrator/scripts to sys.path at index 0; put
# our local SCRIPTS back at the front so future `import state_io` (already
# done by verification_io anyway) and any local module imports resolve in a
# deterministic order regardless of test discovery sequence.
sys.path.insert(0, str(_SCRIPTS))
import state_io  # noqa: E402


def _seed_state_with_pr(root: Path, pr_number: int, phase: str = "ClaimVerify"):
    state_io.init_state(root, "octocat/hello")
    state = state_io.load_state(root)
    state["prs"][str(pr_number)] = {
        "pr_number": pr_number,
        "phase": phase,
        "phase_entered_at": "2026-05-01T00:00:00+00:00",
        "last_action_at": "2026-05-01T00:00:00+00:00",
        "contributor_login": "alice",
        "needs_retriage": False,
        "new_comments_since_triage": 1,
        "next_action": {"skill": "prj-verify-claim", "mode": None},
    }
    state_io.save_state(root, state)
    return state


class TestRenderVerificationMd(unittest.TestCase):
    def test_renders_with_all_fields(self):
        body = verification_io.render_verification_md(
            pr_number=42,
            commenter="alice",
            claim="X breaks when Y",
            strategy="failing-test",
            observation="test passed instead of failing",
            verdict="not-verified",
            rationale="claim not reproducible from PR head",
            worktree_path=Path("/tmp/wt"),
            runner_command=["bun", "test"],
            runner_returncode=0,
            timestamp="2026-05-11T00:00:00+00:00",
        )
        for needle in (
            "# Verification: PR #42",
            "**Verdict:** `not-verified`",
            "**Strategy:** `failing-test`",
            "alice",
            "X breaks when Y",
            "test passed instead of failing",
            "claim not reproducible from PR head",
            "/tmp/wt",
            "bun test",
            "2026-05-11T00:00:00+00:00",
        ):
            self.assertIn(needle, body)

    def test_invalid_verdict_raises(self):
        with self.assertRaises(verification_io.VerificationError):
            verification_io.render_verification_md(
                pr_number=1,
                commenter="x",
                claim="c",
                strategy="failing-test",
                observation="o",
                verdict="bogus",
                rationale="r",
                worktree_path=None,
                runner_command=None,
                runner_returncode=None,
            )

    def test_invalid_strategy_raises(self):
        with self.assertRaises(verification_io.VerificationError):
            verification_io.render_verification_md(
                pr_number=1,
                commenter="x",
                claim="c",
                strategy="wing-it",
                observation="o",
                verdict="verified",
                rationale="r",
                worktree_path=None,
                runner_command=None,
                runner_returncode=None,
            )


class TestApplyVerdictToState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "_bmad").mkdir()
        _seed_state_with_pr(self.root, 42)

    def tearDown(self):
        self.tmp.cleanup()

    def test_verified_transitions_to_fixplan(self):
        state = state_io.load_state(self.root)
        verification_io.apply_verdict_to_state(state, 42, "verified")
        pr = state["prs"]["42"]
        self.assertEqual(pr["phase"], "FixPlan")
        self.assertEqual(pr["next_action"], {"skill": "prj-plan-fix", "mode": None})
        self.assertEqual(pr["last_verification_verdict"], "verified")
        # Stays clear of the PleaseAdvise flag
        self.assertNotIn("user_acknowledged_please_advise", pr)

    def test_not_verified_reverts_to_reviewed(self):
        state = state_io.load_state(self.root)
        verification_io.apply_verdict_to_state(state, 42, "not-verified")
        pr = state["prs"]["42"]
        self.assertEqual(pr["phase"], "Reviewed")
        self.assertIsNone(pr["next_action"])
        self.assertEqual(pr["last_verification_verdict"], "not-verified")

    def test_ambiguous_sets_pleaseadvise_and_flag(self):
        state = state_io.load_state(self.root)
        verification_io.apply_verdict_to_state(state, 42, "ambiguous")
        pr = state["prs"]["42"]
        self.assertEqual(pr["phase"], "PleaseAdvise")
        self.assertIsNone(pr["next_action"])
        self.assertEqual(pr["last_verification_verdict"], "ambiguous")
        # The orchestrator's priority logic gates on this flag.
        self.assertEqual(pr["user_acknowledged_please_advise"], False)

    def test_missing_pr_raises(self):
        state = state_io.load_state(self.root)
        with self.assertRaises(verification_io.VerificationError):
            verification_io.apply_verdict_to_state(state, 999, "verified")

    def test_unknown_verdict_raises(self):
        state = state_io.load_state(self.root)
        with self.assertRaises(verification_io.VerificationError):
            verification_io.apply_verdict_to_state(state, 42, "weird")


class TestPersist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "_bmad").mkdir()
        _seed_state_with_pr(self.root, 7)

    def tearDown(self):
        self.tmp.cleanup()

    def _persist(self, verdict: str, dry_run: bool = False):
        return verification_io.persist(
            project_root=self.root,
            pr_number=7,
            commenter="alice",
            claim="X is broken",
            strategy="existing-test",
            observation="ran the test; it passed",
            verdict=verdict,
            rationale="behavior matches docs",
            worktree_path=Path("/tmp/wt-7"),
            runner_command=["pytest", "tests/test_x.py"],
            runner_returncode=0,
            dry_run=dry_run,
        )

    def test_persist_verified_writes_md_and_state(self):
        extras = self._persist("verified")
        md_path = (
            self.root / "_bmad-output" / "pr-workflow" / "prs" / "7"
            / "verification.md"
        )
        self.assertTrue(md_path.exists())
        body = md_path.read_text(encoding="utf-8")
        self.assertIn("Verdict:** `verified`", body)
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["7"]["phase"], "FixPlan")
        self.assertEqual(extras["phase"], "FixPlan")
        self.assertEqual(extras["verdict"], "verified")

    def test_persist_not_verified_writes_md_and_state(self):
        self._persist("not-verified")
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["7"]["phase"], "Reviewed")

    def test_persist_ambiguous_writes_md_and_state(self):
        self._persist("ambiguous")
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["7"]["phase"], "PleaseAdvise")

    def test_persist_dry_run_writes_nothing(self):
        extras = self._persist("verified", dry_run=True)
        self.assertTrue(extras.get("dry_run"))
        md_path = (
            self.root / "_bmad-output" / "pr-workflow" / "prs" / "7"
            / "verification.md"
        )
        self.assertFalse(md_path.exists())
        state = state_io.load_state(self.root)
        # Unchanged in state too
        self.assertEqual(state["prs"]["7"]["phase"], "ClaimVerify")


if __name__ == "__main__":
    unittest.main()
