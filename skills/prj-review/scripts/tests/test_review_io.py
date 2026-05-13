#!/usr/bin/env python3
"""Unit tests for review_io.py.

Findings schema validation, review.md rendering, and state transition logic.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import review_io  # noqa: E402
import state_io  # noqa: E402  (on sys.path via review_io)


GOOD_FINDING = {
    "file": "src/foo.ts",
    "line": 42,
    "severity": "major",
    "category": "correctness",
    "claim": "Off-by-one in loop bound.",
    "suggested_fix": "Use `< len` not `<= len`.",
}


class TestValidateFindings(unittest.TestCase):
    def test_accepts_empty_list(self):
        self.assertEqual(review_io.validate_findings([]), [])

    def test_accepts_valid_list(self):
        out = review_io.validate_findings([dict(GOOD_FINDING)])
        self.assertEqual(out[0]["severity"], "major")

    def test_rejects_non_list(self):
        with self.assertRaises(review_io.ReviewIoError) as ctx:
            review_io.validate_findings({"file": "x"})
        self.assertIn("must be a list", str(ctx.exception))

    def test_rejects_non_dict_item(self):
        with self.assertRaises(review_io.ReviewIoError):
            review_io.validate_findings(["not a dict"])

    def test_rejects_missing_keys(self):
        bad = dict(GOOD_FINDING)
        bad.pop("suggested_fix")
        with self.assertRaises(review_io.ReviewIoError) as ctx:
            review_io.validate_findings([bad])
        self.assertIn("missing required keys", str(ctx.exception))
        self.assertIn("suggested_fix", str(ctx.exception))

    def test_rejects_extra_keys(self):
        bad = dict(GOOD_FINDING, extra_field="oops")
        with self.assertRaises(review_io.ReviewIoError) as ctx:
            review_io.validate_findings([bad])
        self.assertIn("unexpected keys", str(ctx.exception))

    def test_rejects_invalid_severity(self):
        bad = dict(GOOD_FINDING, severity="critical")
        with self.assertRaises(review_io.ReviewIoError) as ctx:
            review_io.validate_findings([bad])
        self.assertIn("invalid severity", str(ctx.exception))

    def test_accepts_all_four_severities(self):
        for sev in ("blocker", "major", "minor", "nit"):
            f = dict(GOOD_FINDING, severity=sev)
            review_io.validate_findings([f])  # Should not raise

    def test_rejects_zero_or_negative_line(self):
        for bad_line in (0, -1):
            bad = dict(GOOD_FINDING, line=bad_line)
            with self.assertRaises(review_io.ReviewIoError):
                review_io.validate_findings([bad])

    def test_rejects_non_int_line(self):
        bad = dict(GOOD_FINDING, line="42")
        with self.assertRaises(review_io.ReviewIoError):
            review_io.validate_findings([bad])

    def test_rejects_bool_line(self):
        # bool is a subclass of int in Python; we explicitly reject it.
        bad = dict(GOOD_FINDING, line=True)
        with self.assertRaises(review_io.ReviewIoError):
            review_io.validate_findings([bad])

    def test_rejects_blank_string_fields(self):
        for key in ("file", "category", "claim", "suggested_fix"):
            bad = dict(GOOD_FINDING)
            bad[key] = "   "
            with self.assertRaises(review_io.ReviewIoError) as ctx:
                review_io.validate_findings([bad])
            self.assertIn(key, str(ctx.exception))

    def test_error_message_includes_index(self):
        bad = dict(GOOD_FINDING)
        bad.pop("severity")
        with self.assertRaises(review_io.ReviewIoError) as ctx:
            review_io.validate_findings([dict(GOOD_FINDING), bad])
        self.assertIn("finding[1]", str(ctx.exception))


class TestRenderReviewMarkdown(unittest.TestCase):
    def test_renders_no_findings_section(self):
        md = review_io.render_review_markdown(
            pr_number=10,
            findings=[],
            summary="LGTM",
            pr_title="t",
            timestamp=datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc),
        )
        self.assertIn("# Review: t (#10)", md)
        self.assertIn("_No findings.", md)
        self.assertIn("blocker: 0", md)

    def test_sorts_by_severity_then_file_then_line(self):
        findings = [
            dict(GOOD_FINDING, severity="nit", file="src/b.ts", line=1),
            dict(GOOD_FINDING, severity="blocker", file="src/a.ts", line=5),
            dict(GOOD_FINDING, severity="blocker", file="src/a.ts", line=3),
        ]
        md = review_io.render_review_markdown(
            pr_number=1,
            findings=findings,
            summary="x",
            pr_title="t",
        )
        # First heading after Findings should be the blocker on a.ts:3
        first_blocker = md.find("[blocker]")
        second_blocker = md.find("[blocker]", first_blocker + 1)
        nit_pos = md.find("[nit]")
        self.assertLess(first_blocker, second_blocker)
        self.assertLess(second_blocker, nit_pos)
        self.assertIn("`src/a.ts:3`", md)

    def test_counts_correctly(self):
        findings = [
            dict(GOOD_FINDING, severity="blocker"),
            dict(GOOD_FINDING, severity="major"),
            dict(GOOD_FINDING, severity="major"),
            dict(GOOD_FINDING, severity="nit"),
        ]
        md = review_io.render_review_markdown(pr_number=1, findings=findings)
        self.assertIn("blocker: 1", md)
        self.assertIn("major: 2", md)
        self.assertIn("minor: 0", md)
        self.assertIn("nit: 1", md)

    def test_blank_summary_renders_placeholder(self):
        md = review_io.render_review_markdown(pr_number=1, findings=[])
        self.assertIn("_No summary provided._", md)

    def test_rejects_invalid_findings_before_rendering(self):
        with self.assertRaises(review_io.ReviewIoError):
            review_io.render_review_markdown(
                pr_number=1,
                findings=[dict(GOOD_FINDING, severity="critical")],
            )


class _StateFixture:
    """Helper to spin up a temp project root with a seeded state.json."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "_bmad").mkdir()

    def cleanup(self):
        self._tmp.cleanup()

    def seed_pr(self, pr_number: int, phase: str = "ReviewPending"):
        state_io.init_state(self.root, "octocat/hello")
        state = state_io.load_state(self.root)
        state["prs"][str(pr_number)] = {
            "pr_number": pr_number,
            "phase": phase,
            "phase_entered_at": "2026-05-10T10:00:00+00:00",
            "last_action_at": "2026-05-10T10:00:00+00:00",
            "contributor_login": "alice",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-review", "mode": None},
        }
        state_io.save_state(self.root, state)


class TestWriteReview(unittest.TestCase):
    def setUp(self):
        self.fixture = _StateFixture()

    def tearDown(self):
        self.fixture.cleanup()

    def test_writes_review_md_to_expected_path(self):
        findings = [dict(GOOD_FINDING)]
        path = review_io.write_review(
            project_root=self.fixture.root,
            pr_number=99,
            findings=findings,
            summary="ok",
            pr_title="My PR",
        )
        expected = (
            self.fixture.root / "_bmad-output" / "pr-workflow"
            / "prs" / "99" / "review.md"
        )
        self.assertEqual(path, expected)
        self.assertTrue(expected.exists())
        content = expected.read_text(encoding="utf-8")
        self.assertIn("# Review: My PR (#99)", content)
        self.assertIn("src/foo.ts:42", content)

    def test_invalid_findings_raise_before_write(self):
        path = (
            self.fixture.root / "_bmad-output" / "pr-workflow"
            / "prs" / "1" / "review.md"
        )
        with self.assertRaises(review_io.ReviewIoError):
            review_io.write_review(
                project_root=self.fixture.root,
                pr_number=1,
                findings=[{"bad": "object"}],
            )
        self.assertFalse(path.exists())


class TestTransitionToReviewed(unittest.TestCase):
    def setUp(self):
        self.fixture = _StateFixture()

    def tearDown(self):
        self.fixture.cleanup()

    def test_transitions_phase_and_sets_next_action(self):
        self.fixture.seed_pr(123, phase="ReviewPending")
        pr_entry = review_io.transition_to_reviewed(self.fixture.root, 123)
        self.assertEqual(pr_entry["phase"], "Reviewed")
        self.assertEqual(pr_entry["next_action"]["skill"], "prj-decision")
        # Reload and confirm persistence
        state = state_io.load_state(self.fixture.root)
        self.assertEqual(state["prs"]["123"]["phase"], "Reviewed")

    def test_raises_when_pr_missing(self):
        self.fixture.seed_pr(1)
        with self.assertRaises(review_io.ReviewIoError):
            review_io.transition_to_reviewed(self.fixture.root, 999)


if __name__ == "__main__":
    unittest.main()
