#!/usr/bin/env python3
"""End-to-end tests for prj-triage/run.py.

Each test builds a temporary project root with `_bmad/config.toml`, mocks the
github_ops label call (via patch on apply_triage_label in run.py's namespace),
and exercises run.main() with explicit argv. State writes, per-PR cache, and
runlog entries are asserted against the temp filesystem.
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

# persistence patches sys.path so state_io is importable
import persistence  # noqa: E402,F401
import run as run_module  # noqa: E402
import github_ops  # noqa: E402
import state_io  # noqa: E402


def _seed_pr(state: dict, pr_number: int, phase: str = "Discovered") -> None:
    state["prs"][str(pr_number)] = {
        "pr_number": pr_number,
        "phase": phase,
        "phase_entered_at": "2026-05-01T00:00:00+00:00",
        "last_action_at": "2026-05-01T00:00:00+00:00",
        "contributor_login": "alice",
        "needs_retriage": False,
        "new_comments_since_triage": 1,
        "next_action": {"skill": "prj-triage", "mode": "pr"},
        "seen_comment_ids": ["c1"],
    }


class _RunFixture:
    """Helper: spin up a temp project root with config + state."""

    def __init__(self, *, with_repo: bool = True):
        self.with_repo = with_repo

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "_bmad").mkdir()
        if self.with_repo:
            toml = '[modules.prj]\nprj_repo = "octocat/hello"\n'
        else:
            toml = "[modules.prj]\n"
        (self.root / "_bmad" / "config.toml").write_text(toml, encoding="utf-8")
        state_io.init_state(self.root, "octocat/hello")
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()

    def seed(self, pr_number: int = 101, phase: str = "Discovered") -> None:
        state = state_io.load_state(self.root)
        _seed_pr(state, pr_number, phase=phase)
        state_io.save_state(self.root, state)

    def state(self) -> dict:
        return state_io.load_state(self.root)

    def last_runlog(self) -> dict:
        log = state_io.runlog_path(self.root)
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])


def _argv(fx: _RunFixture, *args: str) -> list[str]:
    return ["--project-root", str(fx.root), *args]


class TestMisconfiguredAndValidation(unittest.TestCase):
    def test_missing_prj_repo_returns_misconfigured(self):
        with _RunFixture(with_repo=False) as fx:
            fx.seed()
            rc = run_module.main(_argv(
                fx,
                "--mode", "pr",
                "--pr-number", "101",
                "--classification", "actionable",
                "--rationale", "x",
            ))
            self.assertEqual(rc, 2)
            entry = fx.last_runlog()
            self.assertEqual(entry["status"], "misconfigured")
            self.assertEqual(entry["skill"], "prj-triage")

    def test_invalid_pr_class_returns_2(self):
        with _RunFixture() as fx:
            fx.seed()
            rc = run_module.main(_argv(
                fx,
                "--mode", "pr",
                "--pr-number", "101",
                "--classification", "garbage",
                "--rationale", "x",
            ))
            self.assertEqual(rc, 2)
            entry = fx.last_runlog()
            self.assertEqual(entry["status"], "invalid-classification")

    def test_invalid_comment_class_returns_2(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=22, phase="Reviewed")
            rc = run_module.main(_argv(
                fx,
                "--mode", "comment",
                "--pr-number", "22",
                "--classification", "wrong",
                "--rationale", "x",
            ))
            self.assertEqual(rc, 2)
            entry = fx.last_runlog()
            self.assertEqual(entry["status"], "invalid-classification")

    def test_missing_pr_in_state_returns_2(self):
        with _RunFixture() as fx:
            rc = run_module.main(_argv(
                fx,
                "--mode", "pr",
                "--pr-number", "9999",
                "--classification", "actionable",
                "--rationale", "x",
            ))
            self.assertEqual(rc, 2)
            entry = fx.last_runlog()
            self.assertEqual(entry["status"], "missing-pr")


class TestPrMode(unittest.TestCase):
    def _ok_label_stub(self, repo, pr_number, classification):
        return {
            "label": f"prj/triage:{classification}",
            "pr_number": pr_number,
            "repo": repo,
            "status": "applied",
        }

    def test_actionable_applies_label_writes_cache_and_state(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=101)
            with patch.object(run_module, "apply_triage_label", side_effect=self._ok_label_stub) as label:
                rc = run_module.main(_argv(
                    fx,
                    "--mode", "pr",
                    "--pr-number", "101",
                    "--classification", "actionable",
                    "--rationale", "looks like a real fix",
                ))
            self.assertEqual(rc, 0)
            label.assert_called_once_with("octocat/hello", 101, "actionable")
            entry = fx.last_runlog()
            self.assertEqual(entry["status"], "ok")
            self.assertEqual(entry["phase"], "ReviewPending")
            self.assertEqual(entry["next_action"], {"skill": "prj-review", "mode": None})
            self.assertEqual(entry["label_applied"]["label"], "prj/triage:actionable")
            triage = fx.root / "_bmad-output" / "pr-workflow" / "prs" / "101" / "triage.md"
            self.assertTrue(triage.exists())
            self.assertEqual(fx.state()["prs"]["101"]["phase"], "ReviewPending")

    def test_definite_no_terminal_no_next_action(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=12)
            with patch.object(run_module, "apply_triage_label", side_effect=self._ok_label_stub):
                rc = run_module.main(_argv(
                    fx,
                    "--mode", "pr",
                    "--pr-number", "12",
                    "--classification", "definite-no",
                    "--rationale", "edits build/",
                ))
            self.assertEqual(rc, 0)
            entry = fx.last_runlog()
            self.assertEqual(entry["phase"], "Rejected")
            self.assertIsNone(entry["next_action"])
            self.assertEqual(fx.state()["prs"]["12"]["phase"], "Rejected")

    def test_possible_duplicate_routes_to_overlap_check(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=82)
            with patch.object(run_module, "apply_triage_label", side_effect=self._ok_label_stub):
                rc = run_module.main(_argv(
                    fx,
                    "--mode", "pr",
                    "--pr-number", "82",
                    "--classification", "possible-duplicate",
                    "--rationale", "shares files with #81",
                ))
            self.assertEqual(rc, 0)
            entry = fx.last_runlog()
            self.assertEqual(entry["phase"], "OverlapCheck")
            self.assertEqual(
                entry["next_action"],
                {"skill": "prj-detect-overlap", "mode": None},
            )

    def test_needs_review_routes_to_review_pending(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=77)
            with patch.object(run_module, "apply_triage_label", side_effect=self._ok_label_stub):
                rc = run_module.main(_argv(
                    fx,
                    "--mode", "pr",
                    "--pr-number", "77",
                    "--classification", "needs-review",
                    "--rationale", "substantive but unclear",
                ))
            self.assertEqual(rc, 0)
            entry = fx.last_runlog()
            self.assertEqual(entry["phase"], "ReviewPending")
            self.assertEqual(entry["next_action"], {"skill": "prj-review", "mode": None})

    def test_dry_run_skips_label_and_persistence(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=101)
            with patch.object(run_module, "apply_triage_label") as label:
                rc = run_module.main(_argv(
                    fx,
                    "--mode", "pr",
                    "--pr-number", "101",
                    "--classification", "actionable",
                    "--rationale", "x",
                    "--dry-run",
                ))
            self.assertEqual(rc, 0)
            label.assert_not_called()
            entry = fx.last_runlog()
            self.assertEqual(entry["status"], "dry-run")
            self.assertEqual(entry["label_skipped_reason"], "dry-run")
            # State unchanged (phase still Discovered)
            self.assertEqual(fx.state()["prs"]["101"]["phase"], "Discovered")
            triage = fx.root / "_bmad-output" / "pr-workflow" / "prs" / "101" / "triage.md"
            self.assertFalse(triage.exists())

    def test_skip_label_persists_without_calling_gh(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=101)
            with patch.object(run_module, "apply_triage_label") as label:
                rc = run_module.main(_argv(
                    fx,
                    "--mode", "pr",
                    "--pr-number", "101",
                    "--classification", "actionable",
                    "--rationale", "x",
                    "--skip-label",
                ))
            self.assertEqual(rc, 0)
            label.assert_not_called()
            entry = fx.last_runlog()
            self.assertEqual(entry["status"], "ok")
            self.assertEqual(entry["label_skipped_reason"], "skip-label")
            self.assertIsNone(entry["label_applied"])
            # State DID change
            self.assertEqual(fx.state()["prs"]["101"]["phase"], "ReviewPending")

    def test_gh_label_failure_returns_3_and_does_not_persist(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=101)

            def boom(*a, **kw):
                raise github_ops.GhOpsError("rate limited")

            with patch.object(run_module, "apply_triage_label", side_effect=boom):
                rc = run_module.main(_argv(
                    fx,
                    "--mode", "pr",
                    "--pr-number", "101",
                    "--classification", "actionable",
                    "--rationale", "x",
                ))
            self.assertEqual(rc, 3)
            entry = fx.last_runlog()
            self.assertEqual(entry["status"], "gh-error")
            self.assertIn("rate limited", entry["error"])
            # State unchanged
            self.assertEqual(fx.state()["prs"]["101"]["phase"], "Discovered")


class TestCommentMode(unittest.TestCase):
    def test_actionable_transitions_to_claim_verify(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=42, phase="Reviewed")
            rc = run_module.main(_argv(
                fx,
                "--mode", "comment",
                "--pr-number", "42",
                "--classification", "actionable",
                "--rationale", "maintainer says it breaks",
                "--comment-id", "c123",
            ))
            self.assertEqual(rc, 0)
            entry = fx.last_runlog()
            self.assertEqual(entry["status"], "ok")
            self.assertEqual(entry["phase"], "ClaimVerify")
            self.assertEqual(
                entry["next_action"],
                {"skill": "prj-verify-claim", "mode": None},
            )
            self.assertEqual(entry["comment_id"], "c123")
            self.assertIsNone(entry["label_applied"])
            log = fx.root / "_bmad-output" / "pr-workflow" / "prs" / "42" / "comments-triage.md"
            self.assertTrue(log.exists())
            self.assertEqual(fx.state()["prs"]["42"]["phase"], "ClaimVerify")

    def test_advisory_reverts_to_reviewed(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=42, phase="Reviewed")
            rc = run_module.main(_argv(
                fx,
                "--mode", "comment",
                "--pr-number", "42",
                "--classification", "advisory",
                "--rationale", "opinion",
                "--comment-id", "c1",
            ))
            self.assertEqual(rc, 0)
            entry = fx.last_runlog()
            self.assertEqual(entry["phase"], "Reviewed")
            self.assertIsNone(entry["next_action"])

    def test_noise_reverts_to_reviewed(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=42, phase="Reviewed")
            rc = run_module.main(_argv(
                fx,
                "--mode", "comment",
                "--pr-number", "42",
                "--classification", "noise",
                "--rationale", "bot ping",
            ))
            self.assertEqual(rc, 0)
            entry = fx.last_runlog()
            self.assertEqual(entry["phase"], "Reviewed")
            # comment_id omitted -> stays None / null
            self.assertIsNone(entry["comment_id"])

    def test_comment_mode_does_not_call_label(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=42, phase="Reviewed")
            with patch.object(run_module, "apply_triage_label") as label:
                rc = run_module.main(_argv(
                    fx,
                    "--mode", "comment",
                    "--pr-number", "42",
                    "--classification", "actionable",
                    "--rationale", "x",
                    "--comment-id", "c9",
                ))
            self.assertEqual(rc, 0)
            label.assert_not_called()

    def test_dry_run_comment_does_not_persist(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=42, phase="Reviewed")
            rc = run_module.main(_argv(
                fx,
                "--mode", "comment",
                "--pr-number", "42",
                "--classification", "actionable",
                "--rationale", "x",
                "--comment-id", "c9",
                "--dry-run",
            ))
            self.assertEqual(rc, 0)
            entry = fx.last_runlog()
            self.assertEqual(entry["status"], "dry-run")
            self.assertEqual(fx.state()["prs"]["42"]["phase"], "Reviewed")
            log = fx.root / "_bmad-output" / "pr-workflow" / "prs" / "42" / "comments-triage.md"
            self.assertFalse(log.exists())


class TestRunlogShape(unittest.TestCase):
    def test_runlog_entry_has_required_fields(self):
        with _RunFixture() as fx:
            fx.seed(pr_number=101)
            with patch.object(
                run_module, "apply_triage_label",
                return_value={"label": "x", "pr_number": 101, "repo": "o/r", "status": "applied"},
            ):
                run_module.main(_argv(
                    fx,
                    "--mode", "pr",
                    "--pr-number", "101",
                    "--classification", "actionable",
                    "--rationale", "x",
                ))
            entry = fx.last_runlog()
            for key in ("ts", "action", "skill", "status", "duration_ms", "run_id",
                        "mode", "pr_number", "classification"):
                self.assertIn(key, entry, f"runlog missing key: {key}")
            self.assertEqual(entry["action"], "triage")
            self.assertEqual(entry["skill"], "prj-triage")


if __name__ == "__main__":
    unittest.main()
