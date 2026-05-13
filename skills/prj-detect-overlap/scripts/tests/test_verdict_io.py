#!/usr/bin/env python3
"""Unit tests for verdict_io.py.

Covers pure-function logic (aggregation, phase transition, label building,
markdown render) plus filesystem and state.json effects of persist().
The `_run_gh` seam is patched out so apply_labels exercises its call
shape without touching the network.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# verdict_io patches sys.path with state_io's location, so import it before state_io.
import verdict_io  # noqa: E402
import state_io  # noqa: E402


def _pair(other_pr: int, verdict: str, shared=("a.py", "b.py"), rationale: str = "") -> verdict_io.PairVerdict:
    return verdict_io.PairVerdict(
        target_pr=100,
        other_pr=other_pr,
        shared_files=tuple(shared),
        overlap_count=len(shared),
        verdict=verdict,
        rationale=rationale,
    )


class TestAggregateStrongest(unittest.TestCase):
    def test_empty_returns_independent(self):
        self.assertEqual(verdict_io.aggregate_strongest_verdict([]), "independent")

    def test_severity_order(self):
        pairs = [
            _pair(1, "independent"),
            _pair(2, "complementary"),
            _pair(3, "conflicting"),
            _pair(4, "redundant"),
        ]
        self.assertEqual(verdict_io.aggregate_strongest_verdict(pairs), "redundant")

    def test_picks_strongest_when_mixed(self):
        pairs = [_pair(1, "independent"), _pair(2, "complementary")]
        self.assertEqual(verdict_io.aggregate_strongest_verdict(pairs), "complementary")


class TestResolvePhaseTransition(unittest.TestCase):
    def test_redundant_goes_to_rejected(self):
        t = verdict_io.resolve_phase_transition(
            "redundant", target_pr=100, target_phase_entered_at="",
            pairs=[_pair(50, "redundant")],
        )
        self.assertEqual(t.target_phase, "Rejected")
        self.assertIsNone(t.target_next_action)

    def test_independent_goes_to_review_pending(self):
        t = verdict_io.resolve_phase_transition(
            "independent", target_pr=100, target_phase_entered_at="",
            pairs=[_pair(50, "independent")],
        )
        self.assertEqual(t.target_phase, "ReviewPending")
        self.assertEqual(t.target_next_action, {"skill": "prj-review", "mode": None})

    def test_complementary_goes_to_review_pending(self):
        t = verdict_io.resolve_phase_transition(
            "complementary", target_pr=100,
            target_phase_entered_at="2026-05-11T00:00:00+00:00",
            pairs=[_pair(50, "complementary")],
            other_phase_entered_at_by_pr={50: "2026-05-01T00:00:00+00:00"},
        )
        self.assertEqual(t.target_phase, "ReviewPending")
        self.assertEqual(t.target_next_action, {"skill": "prj-review", "mode": None})

    def test_conflicting_target_newer_blocks_target(self):
        t = verdict_io.resolve_phase_transition(
            "conflicting", target_pr=100,
            target_phase_entered_at="2026-05-11T00:00:00+00:00",
            pairs=[_pair(50, "conflicting")],
            other_phase_entered_at_by_pr={50: "2026-04-01T00:00:00+00:00"},
        )
        self.assertEqual(t.target_phase, "Blocked")
        self.assertIsNone(t.target_next_action)
        self.assertEqual(t.target_blocking_on, 50)
        self.assertIsNone(t.other_pr)

    def test_conflicting_target_older_blocks_other(self):
        t = verdict_io.resolve_phase_transition(
            "conflicting", target_pr=100,
            target_phase_entered_at="2026-04-01T00:00:00+00:00",
            pairs=[_pair(200, "conflicting")],
            other_phase_entered_at_by_pr={200: "2026-05-11T00:00:00+00:00"},
        )
        self.assertEqual(t.target_phase, "ReviewPending")
        self.assertEqual(t.target_next_action, {"skill": "prj-review", "mode": None})
        self.assertEqual(t.other_pr, 200)
        self.assertEqual(t.other_phase, "Blocked")
        self.assertEqual(t.other_blocking_on, 100)


class TestBuildLabels(unittest.TestCase):
    def test_redundant_emits_duplicate_and_definite_no(self):
        labels = verdict_io.build_labels(
            target_pr=100, target_phase_entered_at="",
            pairs=[_pair(50, "redundant")],
        )
        self.assertIn("prj/overlap:duplicate", labels)
        self.assertIn("prj/triage:definite-no", labels)
        self.assertEqual(len(labels), 2)

    def test_conflicting_target_newer_gets_conflicts_with_label(self):
        labels = verdict_io.build_labels(
            target_pr=100, target_phase_entered_at="2026-05-11T00:00:00+00:00",
            pairs=[_pair(50, "conflicting")],
            other_phase_entered_at_by_pr={50: "2026-04-01T00:00:00+00:00"},
        )
        self.assertEqual(labels, ["prj/overlap:conflicts-with-50"])

    def test_conflicting_target_older_no_label(self):
        labels = verdict_io.build_labels(
            target_pr=100, target_phase_entered_at="2026-04-01T00:00:00+00:00",
            pairs=[_pair(200, "conflicting")],
            other_phase_entered_at_by_pr={200: "2026-05-11T00:00:00+00:00"},
        )
        self.assertEqual(labels, [])

    def test_complementary_target_newer_gets_depends_on_older(self):
        labels = verdict_io.build_labels(
            target_pr=100, target_phase_entered_at="2026-05-11T00:00:00+00:00",
            pairs=[_pair(50, "complementary")],
            other_phase_entered_at_by_pr={50: "2026-04-01T00:00:00+00:00"},
        )
        self.assertEqual(labels, ["prj/overlap:depends-on-50"])

    def test_complementary_target_older_no_label(self):
        labels = verdict_io.build_labels(
            target_pr=100, target_phase_entered_at="2026-04-01T00:00:00+00:00",
            pairs=[_pair(200, "complementary")],
            other_phase_entered_at_by_pr={200: "2026-05-11T00:00:00+00:00"},
        )
        self.assertEqual(labels, [])

    def test_independent_emits_no_labels(self):
        labels = verdict_io.build_labels(
            target_pr=100, target_phase_entered_at="",
            pairs=[_pair(50, "independent")],
        )
        self.assertEqual(labels, [])

    def test_dedup_and_sort(self):
        labels = verdict_io.build_labels(
            target_pr=100, target_phase_entered_at="2026-05-11T00:00:00+00:00",
            pairs=[
                _pair(50, "complementary"),
                _pair(60, "complementary"),
                _pair(70, "redundant"),
            ],
            other_phase_entered_at_by_pr={
                50: "2026-04-01T00:00:00+00:00",
                60: "2026-04-15T00:00:00+00:00",
            },
        )
        self.assertEqual(labels, sorted(labels))
        self.assertIn("prj/overlap:depends-on-50", labels)
        self.assertIn("prj/overlap:depends-on-60", labels)
        self.assertIn("prj/overlap:duplicate", labels)


class TestRenderOverlapMarkdown(unittest.TestCase):
    def test_no_pairs_message(self):
        body = verdict_io.render_overlap_markdown(
            target_pr=100, target_files=["a.py"], pairs=[],
            generated_at="2026-05-11T12:00:00+00:00",
        )
        self.assertIn("# Overlap report — PR #100", body)
        self.assertIn("No overlap pairs", body)

    def test_pairs_render_with_table(self):
        pairs = [
            _pair(50, "complementary", rationale="They build on each other."),
            _pair(60, "independent"),
        ]
        body = verdict_io.render_overlap_markdown(
            target_pr=100, target_files=["a.py", "b.py"], pairs=pairs,
        )
        self.assertIn("| Other PR |", body)
        self.assertIn("#50", body)
        self.assertIn("`complementary`", body)
        self.assertIn("They build on each other.", body)
        self.assertIn("#60", body)
        self.assertIn("`independent`", body)

    def test_pipes_in_rationale_escaped(self):
        pair = _pair(50, "independent", rationale="risky|stuff")
        body = verdict_io.render_overlap_markdown(
            target_pr=100, target_files=["a.py"], pairs=[pair],
        )
        self.assertIn("risky\\|stuff", body)

    def test_skipped_pairs_emit_secondary_table(self):
        pairs = [_pair(50, "independent")]
        skipped = [
            verdict_io.PairVerdict(
                target_pr=100, other_pr=99, shared_files=("a.py",),
                overlap_count=1, verdict="independent",
            )
        ]
        body = verdict_io.render_overlap_markdown(
            target_pr=100, target_files=["a.py"], pairs=pairs, skipped=skipped,
        )
        self.assertIn("Sub-threshold pairs", body)
        self.assertIn("#99", body)


class _PersistFixture:
    """Helper: builds a temp project root with seeded state for persist() tests."""

    def __init__(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "_bmad").mkdir()

    def cleanup(self):
        self.tmpdir.cleanup()

    def seed_state(self, target_pr: int = 100, others: list[int] | None = None,
                   target_entered: str = "2026-05-11T00:00:00+00:00",
                   other_entered: str = "2026-04-01T00:00:00+00:00"):
        state_io.init_state(self.root, "owner/repo")
        state = state_io.load_state(self.root)
        state["prs"][str(target_pr)] = {
            "pr_number": target_pr,
            "phase": "OverlapCheck",
            "phase_entered_at": target_entered,
            "last_action_at": target_entered,
            "contributor_login": "alice",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-detect-overlap", "mode": None},
        }
        for other in (others or []):
            state["prs"][str(other)] = {
                "pr_number": other,
                "phase": "ReviewPending",
                "phase_entered_at": other_entered,
                "last_action_at": other_entered,
                "contributor_login": "bob",
                "needs_retriage": False,
                "new_comments_since_triage": 0,
                "next_action": {"skill": "prj-review", "mode": None},
            }
        state_io.save_state(self.root, state)


class TestPersist(unittest.TestCase):
    def setUp(self):
        self.fx = _PersistFixture()

    def tearDown(self):
        self.fx.cleanup()

    def _persist(self, pairs, apply_gh_labels=False):
        """Persist with gh labelling disabled and any _run_gh patched anyway."""
        with patch.object(verdict_io, "_run_gh", return_value=""):
            return verdict_io.persist(
                self.fx.root,
                "owner/repo",
                pairs,
                skipped=[],
                target_pr=100,
                target_files=["a.py", "b.py"],
                apply_gh_labels=apply_gh_labels,
            )

    def test_independent_transitions_to_review_pending(self):
        self.fx.seed_state(others=[50])
        pairs = [_pair(50, "independent")]
        result = self._persist(pairs)

        state = state_io.load_state(self.fx.root)
        self.assertEqual(state["prs"]["100"]["phase"], "ReviewPending")
        self.assertEqual(
            state["prs"]["100"]["next_action"],
            {"skill": "prj-review", "mode": None},
        )
        self.assertNotIn("blocking_on", state["prs"]["100"])
        self.assertEqual(result["strongest_verdict"], "independent")
        self.assertEqual(result["labels_planned"], [])

        report = self.fx.root / "_bmad-output" / "pr-workflow" / "prs" / "100" / "overlap.md"
        self.assertTrue(report.exists())
        body = report.read_text(encoding="utf-8")
        self.assertIn("#50", body)
        self.assertIn("`independent`", body)
        notes = self.fx.root / "_bmad-output" / "pr-workflow" / "prs" / "100" / "overlap-notes.md"
        self.assertFalse(notes.exists())

    def test_complementary_transitions_review_pending_and_writes_note(self):
        # target_pr=100 is newer; 50 is older
        self.fx.seed_state(others=[50])
        pairs = [_pair(50, "complementary", rationale="50 first, then 100")]
        result = self._persist(pairs)

        state = state_io.load_state(self.fx.root)
        self.assertEqual(state["prs"]["100"]["phase"], "ReviewPending")
        self.assertEqual(result["strongest_verdict"], "complementary")
        self.assertIn("prj/overlap:depends-on-50", result["labels_planned"])

        notes = self.fx.root / "_bmad-output" / "pr-workflow" / "prs" / "100" / "overlap-notes.md"
        self.assertTrue(notes.exists())
        note_body = notes.read_text(encoding="utf-8")
        self.assertIn("PR #50", note_body)
        self.assertIn("older=#50", note_body)

    def test_conflicting_sets_blocking_on_when_target_newer(self):
        self.fx.seed_state(others=[50])
        pairs = [_pair(50, "conflicting")]
        result = self._persist(pairs)

        state = state_io.load_state(self.fx.root)
        self.assertEqual(state["prs"]["100"]["phase"], "Blocked")
        self.assertIsNone(state["prs"]["100"]["next_action"])
        self.assertEqual(state["prs"]["100"]["blocking_on"], 50)
        self.assertEqual(result["strongest_verdict"], "conflicting")
        self.assertIn("prj/overlap:conflicts-with-50", result["labels_planned"])

    def test_conflicting_target_older_blocks_other_pr(self):
        # Flip the dates: target (100) is older than the counterpart (200).
        self.fx.seed_state(
            others=[200],
            target_entered="2026-04-01T00:00:00+00:00",
            other_entered="2026-05-11T00:00:00+00:00",
        )
        pairs = [_pair(200, "conflicting")]
        result = self._persist(pairs)

        state = state_io.load_state(self.fx.root)
        # Target advances; counterpart is blocked
        self.assertEqual(state["prs"]["100"]["phase"], "ReviewPending")
        self.assertEqual(state["prs"]["200"]["phase"], "Blocked")
        self.assertEqual(state["prs"]["200"]["blocking_on"], 100)
        self.assertIsNone(state["prs"]["200"]["next_action"])
        self.assertEqual(result["phase_transition"]["other_pr"], 200)

    def test_redundant_transitions_to_rejected_and_applies_duplicate_label(self):
        self.fx.seed_state(others=[50])
        pairs = [_pair(50, "redundant")]
        result = self._persist(pairs)

        state = state_io.load_state(self.fx.root)
        self.assertEqual(state["prs"]["100"]["phase"], "Rejected")
        self.assertIsNone(state["prs"]["100"]["next_action"])
        self.assertEqual(result["strongest_verdict"], "redundant")
        self.assertIn("prj/overlap:duplicate", result["labels_planned"])
        self.assertIn("prj/triage:definite-no", result["labels_planned"])

    def test_apply_labels_calls_gh_per_label(self):
        self.fx.seed_state(others=[50])
        pairs = [_pair(50, "complementary")]
        with patch.object(verdict_io, "_run_gh", return_value="") as mock_run:
            verdict_io.persist(
                self.fx.root, "owner/repo", pairs, skipped=[], target_pr=100,
                target_files=["a.py", "b.py"], apply_gh_labels=True,
            )
        # One label: depends-on-50
        self.assertEqual(mock_run.call_count, 1)
        called_args = mock_run.call_args.args[0]
        self.assertIn("--add-label", called_args)
        self.assertIn("prj/overlap:depends-on-50", called_args)

    def test_missing_target_raises(self):
        self.fx.seed_state()  # target IS seeded
        # Now delete it
        state = state_io.load_state(self.fx.root)
        del state["prs"]["100"]
        state_io.save_state(self.fx.root, state)
        with self.assertRaises(verdict_io.VerdictIoError):
            self._persist([_pair(50, "independent")])


if __name__ == "__main__":
    unittest.main()
