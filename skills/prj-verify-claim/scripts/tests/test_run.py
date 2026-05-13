#!/usr/bin/env python3
"""End-to-end integration tests for prj-verify-claim/run.py.

Each test builds a temporary project root, seeds state with a PR in
ClaimVerify phase, mocks the gh-comment subprocess seam, and exercises
run.main() for each verdict.
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

# Touch verification_io first so it patches sys.path with state_io's
# location. That patch lands BEFORE our SCRIPTS insert in some loader
# orderings, so we re-insert SCRIPTS at position 0 afterwards to keep the
# local `run`, `worktree`, `comment_post` modules ahead of any same-named
# siblings under prj-orchestrator/scripts on sys.path.
import verification_io  # noqa: E402,F401
sys.path.insert(0, str(SCRIPTS))

import run as run_module  # noqa: E402
import state_io  # noqa: E402
import worktree as worktree_mod  # noqa: E402
import comment_post  # noqa: E402


def _seed(root: Path, pr_number: int = 42, with_triage: bool = True):
    """Seed the temp project root with a state.json and (optionally) a
    comments-triage.md so run.py has a claim to read."""
    (root / "_bmad").mkdir(exist_ok=True)
    (root / "_bmad" / "config.toml").write_text(
        '[modules.prj]\nprj_repo = "octocat/hello"\n',
        encoding="utf-8",
    )
    state_io.init_state(root, "octocat/hello")
    state = state_io.load_state(root)
    state["prs"][str(pr_number)] = {
        "pr_number": pr_number,
        "phase": "ClaimVerify",
        "phase_entered_at": "2026-05-01T00:00:00+00:00",
        "last_action_at": "2026-05-01T00:00:00+00:00",
        "contributor_login": "alice",
        "needs_retriage": False,
        "new_comments_since_triage": 1,
        "next_action": {"skill": "prj-verify-claim", "mode": None},
    }
    state_io.save_state(root, state)

    if with_triage:
        pr_dir = root / "_bmad-output" / "pr-workflow" / "prs" / str(pr_number)
        pr_dir.mkdir(parents=True, exist_ok=True)
        (pr_dir / "comments-triage.md").write_text(
            "## Comment c1\n"
            "- **Commenter:** alice\n"
            "- **Classification:** actionable\n"
            "- **Claim:** Calling foo() with empty args is supposed to throw, but it returns None.\n"
            "\n"
            "Rationale: maintainer explicitly tested.\n",
            encoding="utf-8",
        )


class TestRunVerdicts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _seed(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _invoke(self, argv_extra: list[str]) -> int:
        argv = [
            "run.py",
            "--project-root", str(self.root),
            "--pr-number", "42",
        ] + argv_extra
        with patch.object(sys, "argv", argv):
            return run_module.main()

    def _last_runlog(self) -> dict:
        log = state_io.runlog_path(self.root)
        self.assertTrue(log.exists())
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def test_misconfigured_when_prj_repo_missing(self):
        # Overwrite config with no prj_repo
        (self.root / "_bmad" / "config.toml").write_text(
            "[modules.prj]\n", encoding="utf-8",
        )
        rc = self._invoke([
            "--strategy", "existing-test",
            "--verdict", "verified",
            "--observation", "x",
            "--rationale", "y",
        ])
        self.assertEqual(rc, 2)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "misconfigured")

    def test_verified_writes_md_transitions_to_fixplan_and_cleans_worktree(self):
        # Provision a fake worktree so cleanup has something to remove
        wt_path = worktree_mod.worktree_path(self.root, 42)
        wt_path.mkdir(parents=True)
        (wt_path / ".git").write_text("ok", encoding="utf-8")

        # gh comment must NOT be called for verified.
        with patch.object(
            comment_post, "_run_gh_with_stdin",
            side_effect=AssertionError("must not post comment on verified"),
        ):
            rc = self._invoke([
                "--strategy", "failing-test",
                "--verdict", "verified",
                "--observation", "test fails as expected",
                "--rationale", "claim reproduced cleanly",
            ])
        self.assertEqual(rc, 0)

        # Phase advances to FixPlan
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "FixPlan")
        self.assertEqual(
            state["prs"]["42"]["next_action"],
            {"skill": "prj-plan-fix", "mode": None},
        )

        # verification.md exists
        md = self.root / "_bmad-output" / "pr-workflow" / "prs" / "42" / "verification.md"
        self.assertTrue(md.exists())
        self.assertIn("verified", md.read_text(encoding="utf-8"))

        # Worktree cleaned up
        self.assertFalse(wt_path.exists())

        entry = self._last_runlog()
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["verdict"], "verified")
        self.assertEqual(entry["worktree_cleanup"]["status"], "removed")
        self.assertEqual(entry["comment"]["status"], "skipped-not-applicable")

    def test_not_verified_reverts_to_reviewed_and_posts_comment(self):
        # Provision a worktree so we can confirm it's retained.
        wt_path = worktree_mod.worktree_path(self.root, 42)
        wt_path.mkdir(parents=True)
        (wt_path / ".git").write_text("ok", encoding="utf-8")

        posted_bodies: list[str] = []

        def fake_gh(args, stdin_text, timeout=30):
            posted_bodies.append(stdin_text)
            return (0, "https://github.com/o/r/issues/42#c1\n", "")

        with patch.object(comment_post, "_run_gh_with_stdin", side_effect=fake_gh):
            rc = self._invoke([
                "--strategy", "existing-test",
                "--verdict", "not-verified",
                "--observation", "all tests pass on HEAD; the claimed failure is not visible",
                "--rationale", "behavior matches the documented contract",
            ])
        self.assertEqual(rc, 0)

        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "Reviewed")
        self.assertIsNone(state["prs"]["42"]["next_action"])

        # Worktree retained for inspection
        self.assertTrue(wt_path.exists())

        # Exactly one polite comment was posted, mentioning the worktree
        self.assertEqual(len(posted_bodies), 1)
        body = posted_bodies[0]
        self.assertIn("@alice", body)
        self.assertIn("worktree", body.lower())
        self.assertIn("PR Jangler bot", body)

        entry = self._last_runlog()
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["verdict"], "not-verified")
        self.assertEqual(entry["comment"]["status"], "posted")
        self.assertEqual(entry["worktree_cleanup"]["status"], "retained")

    def test_ambiguous_moves_to_pleaseadvise_no_comment(self):
        wt_path = worktree_mod.worktree_path(self.root, 42)
        wt_path.mkdir(parents=True)
        (wt_path / ".git").write_text("ok", encoding="utf-8")

        with patch.object(
            comment_post, "_run_gh_with_stdin",
            side_effect=AssertionError("must not post comment on ambiguous"),
        ):
            rc = self._invoke([
                "--strategy", "manual-exercise",
                "--verdict", "ambiguous",
                "--observation", "ran the script but the output was inconclusive",
                "--rationale", "claim too vague to ground a single experiment",
            ])
        self.assertEqual(rc, 0)

        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "PleaseAdvise")
        self.assertEqual(state["prs"]["42"]["user_acknowledged_please_advise"], False)
        self.assertTrue(wt_path.exists(), "ambiguous must retain worktree")

        entry = self._last_runlog()
        self.assertEqual(entry["verdict"], "ambiguous")
        self.assertEqual(entry["comment"]["status"], "skipped-not-applicable")

    def test_skip_comment_flag_blocks_post_on_not_verified(self):
        with patch.object(
            comment_post, "_run_gh_with_stdin",
            side_effect=AssertionError("must not post comment when --skip-comment"),
        ):
            rc = self._invoke([
                "--strategy", "existing-test",
                "--verdict", "not-verified",
                "--observation", "tests pass",
                "--rationale", "no bug",
                "--skip-comment",
            ])
        self.assertEqual(rc, 0)

        entry = self._last_runlog()
        self.assertEqual(entry["comment"]["status"], "skipped-flag")

    def test_dry_run_writes_nothing(self):
        wt_path = worktree_mod.worktree_path(self.root, 42)
        wt_path.mkdir(parents=True)
        (wt_path / ".git").write_text("ok", encoding="utf-8")

        rc = self._invoke([
            "--strategy", "failing-test",
            "--verdict", "verified",
            "--observation", "z",
            "--rationale", "z",
            "--dry-run",
        ])
        self.assertEqual(rc, 0)
        # Phase unchanged
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "ClaimVerify")
        # verification.md not written
        md = self.root / "_bmad-output" / "pr-workflow" / "prs" / "42" / "verification.md"
        self.assertFalse(md.exists())
        # Worktree retained because --dry-run skips cleanup
        self.assertTrue(wt_path.exists())

        entry = self._last_runlog()
        self.assertEqual(entry["status"], "dry-run")

    def test_gh_comment_failure_returns_3(self):
        def fake_gh(args, stdin_text, timeout=30):
            return (1, "", "auth required")

        with patch.object(comment_post, "_run_gh_with_stdin", side_effect=fake_gh):
            rc = self._invoke([
                "--strategy", "existing-test",
                "--verdict", "not-verified",
                "--observation", "x",
                "--rationale", "y",
            ])
        self.assertEqual(rc, 3)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "gh-error")
        self.assertIn("auth required", entry["error"])

    def test_runlog_carries_required_fields(self):
        wt_path = worktree_mod.worktree_path(self.root, 42)
        wt_path.mkdir(parents=True)
        (wt_path / ".git").write_text("ok", encoding="utf-8")

        rc = self._invoke([
            "--strategy", "existing-test",
            "--verdict", "verified",
            "--observation", "x",
            "--rationale", "y",
        ])
        self.assertEqual(rc, 0)
        entry = self._last_runlog()
        for key in (
            "ts", "action", "skill", "status", "duration_ms",
            "run_id", "pr_number", "verdict", "strategy", "phase",
        ):
            self.assertIn(key, entry, f"runlog missing key: {key}")
        self.assertEqual(entry["action"], "verify-claim")
        self.assertEqual(entry["skill"], "prj-verify-claim")

    def test_claim_read_from_comments_triage_when_omitted(self):
        wt_path = worktree_mod.worktree_path(self.root, 42)
        wt_path.mkdir(parents=True)
        (wt_path / ".git").write_text("ok", encoding="utf-8")

        rc = self._invoke([
            "--strategy", "existing-test",
            "--verdict", "verified",
            "--observation", "ok",
            "--rationale", "ok",
        ])
        self.assertEqual(rc, 0)
        md = (
            self.root / "_bmad-output" / "pr-workflow" / "prs" / "42"
            / "verification.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Calling foo() with empty args", md)


if __name__ == "__main__":
    unittest.main()
