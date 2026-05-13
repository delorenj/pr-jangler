#!/usr/bin/env python3
"""Unit tests for persistence.py.

Tests use tempfile.TemporaryDirectory + state_io.init_state to set up an
isolated project root and exercise:
  - write_triage_md (PR mode cache write)
  - append_comments_triage_md (comment-mode cache append)
  - apply_pr_classification (all 4 PR classes -> phase transitions)
  - apply_comment_classification (all 3 comment classes -> phase transitions)
  - error paths (PR missing, invalid class)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

# Importing persistence places state_io on sys.path
import persistence  # noqa: E402
import state_io  # noqa: E402


def _seed_pr(state: dict, pr_number: int, phase: str = "Discovered") -> None:
    """Insert a fully-formed PR entry into state.prs."""
    state["prs"][str(pr_number)] = {
        "pr_number": pr_number,
        "phase": phase,
        "phase_entered_at": "2026-05-01T00:00:00+00:00",
        "last_action_at": "2026-05-01T00:00:00+00:00",
        "contributor_login": "alice",
        "needs_retriage": True,
        "new_comments_since_triage": 2,
        "next_action": {"skill": "prj-triage", "mode": "pr"},
        "seen_comment_ids": ["c1", "c2"],
        "triaged_comment_ids": [],
    }


class _ProjectFixture:
    """Helper: spin up a temp project root with initialized state."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "_bmad").mkdir()
        state_io.init_state(self.root, "octocat/hello")
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()

    def load(self) -> dict:
        return state_io.load_state(self.root)


class TestPrCacheWrite(unittest.TestCase):
    def test_write_triage_md_creates_file_with_required_sections(self):
        with _ProjectFixture() as fx:
            path = persistence.write_triage_md(
                fx.root, 42, "actionable", "looks like a real bug fix",
            )
            content = path.read_text(encoding="utf-8")
            self.assertIn("PR #42 Triage", content)
            self.assertIn("`actionable`", content)
            self.assertIn("looks like a real bug fix", content)
            self.assertTrue(path.name == "triage.md")
            # File lives under prs/42/
            self.assertEqual(path.parent.name, "42")

    def test_write_triage_md_overwrites_on_rerun(self):
        with _ProjectFixture() as fx:
            persistence.write_triage_md(fx.root, 1, "actionable", "first rationale")
            path = persistence.write_triage_md(fx.root, 1, "needs-review", "second rationale")
            content = path.read_text(encoding="utf-8")
            self.assertIn("needs-review", content)
            self.assertIn("second rationale", content)
            self.assertNotIn("first rationale", content)

    def test_write_triage_md_empty_rationale_records_placeholder(self):
        with _ProjectFixture() as fx:
            path = persistence.write_triage_md(fx.root, 9, "actionable", "")
            self.assertIn("(no rationale provided)", path.read_text(encoding="utf-8"))

    def test_write_triage_md_rejects_invalid_class(self):
        with _ProjectFixture() as fx:
            with self.assertRaises(persistence.PersistenceError):
                persistence.write_triage_md(fx.root, 1, "garbage", "x")


class TestCommentsCacheAppend(unittest.TestCase):
    def test_first_append_writes_header(self):
        with _ProjectFixture() as fx:
            path = persistence.append_comments_triage_md(
                fx.root, 17, "advisory", "opinion only", "c5",
            )
            self.assertTrue(path.exists())
            self.assertTrue(path.name == "comments-triage.md")
            content = path.read_text(encoding="utf-8")
            self.assertIn("PR #17 Comment Triage Log", content)
            self.assertIn("`advisory`", content)
            self.assertIn("c5", content)

    def test_subsequent_append_does_not_repeat_header(self):
        with _ProjectFixture() as fx:
            persistence.append_comments_triage_md(
                fx.root, 17, "advisory", "first", "c1",
            )
            persistence.append_comments_triage_md(
                fx.root, 17, "actionable", "second", "c2",
            )
            content = (fx.root / "_bmad-output" / "pr-workflow" / "prs" / "17"
                       / "comments-triage.md").read_text(encoding="utf-8")
            # Exactly one header
            self.assertEqual(content.count("PR #17 Comment Triage Log"), 1)
            # Both entries present
            self.assertIn("first", content)
            self.assertIn("second", content)
            self.assertIn("c1", content)
            self.assertIn("c2", content)

    def test_append_with_missing_comment_id_records_unknown(self):
        with _ProjectFixture() as fx:
            persistence.append_comments_triage_md(
                fx.root, 5, "noise", "bot ping", None,
            )
            content = (fx.root / "_bmad-output" / "pr-workflow" / "prs" / "5"
                       / "comments-triage.md").read_text(encoding="utf-8")
            self.assertIn("unknown", content)

    def test_append_rejects_invalid_class(self):
        with _ProjectFixture() as fx:
            with self.assertRaises(persistence.PersistenceError):
                persistence.append_comments_triage_md(
                    fx.root, 1, "wrong-bucket", "x", "c1",
                )


class TestPrClassificationTransitions(unittest.TestCase):
    def _setup(self, fx: _ProjectFixture, pr_number: int = 42) -> None:
        state = fx.load()
        _seed_pr(state, pr_number, phase="Discovered")
        state_io.save_state(fx.root, state)

    def test_actionable_transitions_to_review_pending(self):
        with _ProjectFixture() as fx:
            self._setup(fx)
            result = persistence.apply_pr_classification(
                fx.root, 42, "actionable", "valid bug fix",
            )
            self.assertEqual(result["phase"], "ReviewPending")
            self.assertEqual(result["next_action"], {"skill": "prj-review", "mode": None})
            state = fx.load()
            entry = state["prs"]["42"]
            self.assertEqual(entry["phase"], "ReviewPending")
            self.assertEqual(entry["next_action"], {"skill": "prj-review", "mode": None})
            # Triage counters reset
            self.assertEqual(entry["new_comments_since_triage"], 0)
            self.assertFalse(entry["needs_retriage"])

    def test_possible_duplicate_transitions_to_overlap_check(self):
        with _ProjectFixture() as fx:
            self._setup(fx)
            result = persistence.apply_pr_classification(
                fx.root, 42, "possible-duplicate", "touches same files as #81",
            )
            self.assertEqual(result["phase"], "OverlapCheck")
            self.assertEqual(
                result["next_action"],
                {"skill": "prj-detect-overlap", "mode": None},
            )
            state = fx.load()
            self.assertEqual(state["prs"]["42"]["phase"], "OverlapCheck")

    def test_definite_no_transitions_to_rejected_terminal(self):
        with _ProjectFixture() as fx:
            self._setup(fx)
            result = persistence.apply_pr_classification(
                fx.root, 42, "definite-no", "edits build/ output",
            )
            self.assertEqual(result["phase"], "Rejected")
            self.assertIsNone(result["next_action"])
            state = fx.load()
            self.assertEqual(state["prs"]["42"]["phase"], "Rejected")
            self.assertIsNone(state["prs"]["42"]["next_action"])

    def test_needs_review_transitions_to_review_pending(self):
        with _ProjectFixture() as fx:
            self._setup(fx)
            result = persistence.apply_pr_classification(
                fx.root, 42, "needs-review", "ambiguous",
            )
            self.assertEqual(result["phase"], "ReviewPending")
            self.assertEqual(result["next_action"], {"skill": "prj-review", "mode": None})

    def test_pr_classification_writes_triage_md(self):
        with _ProjectFixture() as fx:
            self._setup(fx, pr_number=99)
            persistence.apply_pr_classification(
                fx.root, 99, "actionable", "reasoning text",
            )
            triage = fx.root / "_bmad-output" / "pr-workflow" / "prs" / "99" / "triage.md"
            self.assertTrue(triage.exists())
            self.assertIn("reasoning text", triage.read_text(encoding="utf-8"))

    def test_dry_run_does_not_write_state_or_cache(self):
        with _ProjectFixture() as fx:
            self._setup(fx, pr_number=55)
            result = persistence.apply_pr_classification(
                fx.root, 55, "actionable", "x", persist=False,
            )
            self.assertFalse(result["persisted"])
            state = fx.load()
            # state remained at Discovered (the seed) because we did not persist
            self.assertEqual(state["prs"]["55"]["phase"], "Discovered")
            triage = fx.root / "_bmad-output" / "pr-workflow" / "prs" / "55" / "triage.md"
            self.assertFalse(triage.exists())

    def test_pr_missing_in_state_raises(self):
        with _ProjectFixture() as fx:
            with self.assertRaises(persistence.PersistenceError):
                persistence.apply_pr_classification(
                    fx.root, 12345, "actionable", "x",
                )

    def test_invalid_class_raises(self):
        with _ProjectFixture() as fx:
            self._setup(fx)
            with self.assertRaises(persistence.PersistenceError):
                persistence.apply_pr_classification(
                    fx.root, 42, "nonsense", "x",
                )


class TestCommentClassificationTransitions(unittest.TestCase):
    def _setup(self, fx: _ProjectFixture, pr_number: int = 7) -> None:
        state = fx.load()
        _seed_pr(state, pr_number, phase="Reviewed")
        state_io.save_state(fx.root, state)

    def test_actionable_transitions_to_claim_verify(self):
        with _ProjectFixture() as fx:
            self._setup(fx)
            result = persistence.apply_comment_classification(
                fx.root, 7, "actionable", "reproduction shown", comment_id="c100",
            )
            self.assertEqual(result["phase"], "ClaimVerify")
            self.assertEqual(
                result["next_action"],
                {"skill": "prj-verify-claim", "mode": None},
            )
            state = fx.load()
            entry = state["prs"]["7"]
            self.assertEqual(entry["phase"], "ClaimVerify")
            # Counter was 2; should drop by 1 -> 1
            self.assertEqual(entry["new_comments_since_triage"], 1)

    def test_advisory_reverts_to_reviewed(self):
        with _ProjectFixture() as fx:
            self._setup(fx)
            result = persistence.apply_comment_classification(
                fx.root, 7, "advisory", "opinion", comment_id="c200",
            )
            self.assertEqual(result["phase"], "Reviewed")
            self.assertIsNone(result["next_action"])

    def test_noise_reverts_to_reviewed(self):
        with _ProjectFixture() as fx:
            self._setup(fx)
            result = persistence.apply_comment_classification(
                fx.root, 7, "noise", "any update?",
            )
            self.assertEqual(result["phase"], "Reviewed")
            self.assertIsNone(result["next_action"])

    def test_counter_floors_at_zero(self):
        with _ProjectFixture() as fx:
            state = fx.load()
            _seed_pr(state, 8, phase="Reviewed")
            state["prs"]["8"]["new_comments_since_triage"] = 0
            state_io.save_state(fx.root, state)
            persistence.apply_comment_classification(
                fx.root, 8, "advisory", "x", comment_id="c1",
            )
            state = fx.load()
            self.assertEqual(state["prs"]["8"]["new_comments_since_triage"], 0)

    def test_comment_classification_appends_cache(self):
        with _ProjectFixture() as fx:
            self._setup(fx, pr_number=44)
            persistence.apply_comment_classification(
                fx.root, 44, "noise", "ci ping", comment_id="cbot",
            )
            log = fx.root / "_bmad-output" / "pr-workflow" / "prs" / "44" / "comments-triage.md"
            self.assertTrue(log.exists())
            content = log.read_text(encoding="utf-8")
            self.assertIn("noise", content)
            self.assertIn("cbot", content)

    def test_dry_run_does_not_write_state_or_cache(self):
        with _ProjectFixture() as fx:
            self._setup(fx, pr_number=33)
            result = persistence.apply_comment_classification(
                fx.root, 33, "actionable", "x", comment_id="c1", persist=False,
            )
            self.assertFalse(result["persisted"])
            state = fx.load()
            self.assertEqual(state["prs"]["33"]["phase"], "Reviewed")
            log = fx.root / "_bmad-output" / "pr-workflow" / "prs" / "33" / "comments-triage.md"
            self.assertFalse(log.exists())

    def test_pr_missing_in_state_raises(self):
        with _ProjectFixture() as fx:
            with self.assertRaises(persistence.PersistenceError):
                persistence.apply_comment_classification(
                    fx.root, 999, "actionable", "x", comment_id="c1",
                )

    def test_invalid_class_raises(self):
        with _ProjectFixture() as fx:
            self._setup(fx)
            with self.assertRaises(persistence.PersistenceError):
                persistence.apply_comment_classification(
                    fx.root, 7, "wrong", "x", comment_id="c1",
                )


class TestPhaseTransitionTables(unittest.TestCase):
    """Round-trip sanity: ensure rubric tables cover every declared class."""

    def test_pr_transitions_cover_all_classes(self):
        self.assertEqual(set(persistence.PR_TRANSITIONS), set(persistence.PR_CLASSES))

    def test_comment_transitions_cover_all_classes(self):
        self.assertEqual(
            set(persistence.COMMENT_TRANSITIONS),
            set(persistence.COMMENT_CLASSES),
        )

    def test_all_target_phases_are_valid(self):
        for transition in persistence.PR_TRANSITIONS.values():
            self.assertIn(transition["phase"], state_io.VALID_PHASES)
        for transition in persistence.COMMENT_TRANSITIONS.values():
            self.assertIn(transition["phase"], state_io.VALID_PHASES)


class TestTriagedCommentIdsCursor(unittest.TestCase):
    """Verify triaged_comment_ids tracking + the next_unclassified_comment_id helper."""

    def test_apply_comment_classification_appends_to_triaged_ids(self):
        with _ProjectFixture() as fx:
            state = fx.load()
            _seed_pr(state, 9, phase="Reviewed")
            state_io.save_state(fx.root, state)

            persistence.apply_comment_classification(
                fx.root, 9, "advisory", "opinion", comment_id="c1",
            )
            state = fx.load()
            self.assertEqual(state["prs"]["9"]["triaged_comment_ids"], ["c1"])

            persistence.apply_comment_classification(
                fx.root, 9, "noise", "off-topic", comment_id="c2",
            )
            state = fx.load()
            self.assertEqual(state["prs"]["9"]["triaged_comment_ids"], ["c1", "c2"])

    def test_apply_comment_classification_skips_append_when_id_missing(self):
        with _ProjectFixture() as fx:
            state = fx.load()
            _seed_pr(state, 10, phase="Reviewed")
            state_io.save_state(fx.root, state)

            persistence.apply_comment_classification(
                fx.root, 10, "noise", "no id given", comment_id=None,
            )
            state = fx.load()
            self.assertEqual(state["prs"]["10"]["triaged_comment_ids"], [])

    def test_next_unclassified_returns_first_untriaged(self):
        state = {"prs": {"42": {
            "seen_comment_ids": ["a", "b", "c"],
            "triaged_comment_ids": ["a"],
        }}}
        self.assertEqual(persistence.next_unclassified_comment_id(state, 42), "b")

    def test_next_unclassified_returns_none_when_all_done(self):
        state = {"prs": {"42": {
            "seen_comment_ids": ["a", "b"],
            "triaged_comment_ids": ["a", "b"],
        }}}
        self.assertIsNone(persistence.next_unclassified_comment_id(state, 42))

    def test_next_unclassified_returns_none_for_missing_pr(self):
        self.assertIsNone(persistence.next_unclassified_comment_id({"prs": {}}, 99))


if __name__ == "__main__":
    unittest.main()
