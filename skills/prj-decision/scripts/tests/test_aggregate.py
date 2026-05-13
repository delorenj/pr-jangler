#!/usr/bin/env python3
"""Unit tests for prj-decision/scripts/aggregate.py.

Covers:
  - Missing cache files surface as empty signals (not raised).
  - Review parsing handles frontmatter and body-heuristic paths.
  - Implementation parsing handles tests/regressions frontmatter.
  - Overlap, triage, adversarial, verification frontmatter parsers.
  - decisions.log JSONL load + adversarial-escalation count.
  - days_since uses the injected `now` clock.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import aggregate  # noqa: E402


class TestParsers(unittest.TestCase):
    def test_parse_frontmatter_basic(self):
        text = "---\nfoo: bar\nbaz: \"quoted\"\n---\n\nbody"
        fm = aggregate.parse_frontmatter(text)
        self.assertEqual(fm["foo"], "bar")
        self.assertEqual(fm["baz"], "quoted")

    def test_parse_frontmatter_absent(self):
        self.assertEqual(aggregate.parse_frontmatter(""), {})
        self.assertEqual(aggregate.parse_frontmatter("no frontmatter here"), {})

    def test_parse_review_frontmatter_explicit_counts(self):
        text = "---\nblockers: 2\nmajors: 1\nminors: 4\n---\nbody"
        self.assertEqual(aggregate.parse_review(text), (2, 1, 4))

    def test_parse_review_findings_summary(self):
        text = "---\nfindings: blocker=3, major=0, minor=7\n---\nbody"
        self.assertEqual(aggregate.parse_review(text), (3, 0, 7))

    def test_parse_review_body_heuristic(self):
        text = "## Findings\n- severity: blocker\n- severity: Major\n- severity: minor\n- severity: minor"
        b, m, mi = aggregate.parse_review(text)
        self.assertEqual(b, 1)
        self.assertEqual(m, 1)
        self.assertEqual(mi, 2)

    def test_parse_implementation_valid_and_invalid(self):
        text = "---\ntests: pass\nregressions: none\n---\n"
        self.assertEqual(aggregate.parse_implementation(text), ("pass", "none"))
        text2 = "---\ntests: bogus\nregressions: maybe\n---\n"
        self.assertEqual(aggregate.parse_implementation(text2), ("unknown", "unknown"))
        self.assertEqual(aggregate.parse_implementation(""), ("unknown", "unknown"))

    def test_parse_overlap_verdicts(self):
        for verdict in ("independent", "complementary", "conflicting", "redundant"):
            text = f"---\nverdict: {verdict}\n---\n"
            self.assertEqual(aggregate.parse_overlap(text), verdict)
        self.assertIsNone(aggregate.parse_overlap("---\nverdict: nonsense\n---\n"))
        self.assertIsNone(aggregate.parse_overlap(""))

    def test_parse_triage(self):
        text = "---\nclassification: definite-no\n---\n"
        self.assertEqual(aggregate.parse_triage(text), "definite-no")
        text2 = "---\nclass: actionable\n---\n"
        self.assertEqual(aggregate.parse_triage(text2), "actionable")
        self.assertIsNone(aggregate.parse_triage(""))

    def test_parse_adversarial(self):
        self.assertEqual(
            aggregate.parse_adversarial("---\nverdict: pass\n---\n"), "pass",
        )
        self.assertEqual(
            aggregate.parse_adversarial("---\nverdict: reject\n---\n"), "reject",
        )
        self.assertIsNone(aggregate.parse_adversarial(""))

    def test_parse_verification(self):
        self.assertEqual(
            aggregate.parse_verification("---\nverdict: verified\n---\n"), "verified",
        )

    def test_days_since(self):
        now = datetime(2026, 5, 11, tzinfo=timezone.utc)
        self.assertEqual(
            aggregate.days_since("2026-04-01T00:00:00+00:00", now), 40,
        )
        # Z suffix
        self.assertEqual(
            aggregate.days_since("2026-05-01T00:00:00Z", now), 10,
        )
        # No tz → assumed UTC
        self.assertEqual(
            aggregate.days_since("2026-05-10T00:00:00", now), 1,
        )
        self.assertIsNone(aggregate.days_since(None, now))
        self.assertIsNone(aggregate.days_since("not-a-date", now))


class TestLoadDecisionsLog(unittest.TestCase):
    def test_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "decisions.log"
            self.assertEqual(aggregate.load_decisions_log(path), [])

    def test_jsonl_roundtrip_and_skip_malformed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "decisions.log"
            path.write_text(
                '{"kind": "decision", "decision": "ready-to-merge"}\n'
                '   \n'
                'not json at all\n'
                '{"kind": "adversarial-escalation"}\n',
                encoding="utf-8",
            )
            entries = aggregate.load_decisions_log(path)
            self.assertEqual(len(entries), 2)
            self.assertEqual(
                aggregate.count_adversarial_escalations(entries), 1,
            )


class TestAggregate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_all_files_returns_empty_bundle(self):
        sigs = aggregate.aggregate(self.cache, 1)
        self.assertEqual(sigs.pr_number, 1)
        self.assertFalse(sigs.has_review)
        self.assertFalse(sigs.has_implementation)
        self.assertEqual(sigs.cache_paths, [])
        self.assertEqual(sigs.blocker_count, 0)
        self.assertIsNone(sigs.overlap_verdict)
        self.assertIsNone(sigs.days_since_maintainer)

    def test_full_cache_populates_signals(self):
        (self.cache / "review.md").write_text(
            "---\nblockers: 1\nmajors: 2\nminors: 0\n---\n", encoding="utf-8",
        )
        (self.cache / "implementation.md").write_text(
            "---\ntests: pass\nregressions: none\n---\n", encoding="utf-8",
        )
        (self.cache / "overlap.md").write_text(
            "---\nverdict: independent\n---\n", encoding="utf-8",
        )
        (self.cache / "triage.md").write_text(
            "---\nclassification: actionable\n---\n", encoding="utf-8",
        )
        (self.cache / "adversarial.md").write_text(
            "---\nverdict: pass\n---\n", encoding="utf-8",
        )
        (self.cache / "verification.md").write_text(
            "---\nverdict: verified\n---\n", encoding="utf-8",
        )
        (self.cache / "fix-plan.md").write_text("plan content", encoding="utf-8")
        (self.cache / "decisions.log").write_text(
            '{"kind": "adversarial-escalation"}\n'
            '{"kind": "adversarial-escalation"}\n',
            encoding="utf-8",
        )

        sigs = aggregate.aggregate(
            self.cache, 42,
            pr_state_entry={"last_maintainer_activity_at": "2026-03-01T00:00:00Z"},
            now=datetime(2026, 5, 11, tzinfo=timezone.utc),
        )
        self.assertTrue(sigs.has_review)
        self.assertEqual(sigs.blocker_count, 1)
        self.assertEqual(sigs.major_count, 2)
        self.assertTrue(sigs.has_implementation)
        self.assertEqual(sigs.tests_status, "pass")
        self.assertEqual(sigs.regressions, "none")
        self.assertEqual(sigs.overlap_verdict, "independent")
        self.assertEqual(sigs.triage_class, "actionable")
        self.assertEqual(sigs.adversarial_verdict, "pass")
        self.assertEqual(sigs.verification_verdict, "verified")
        self.assertTrue(sigs.has_fix_plan)
        self.assertEqual(sigs.adversarial_escalation_count, 2)
        self.assertEqual(sigs.days_since_maintainer, 71)
        self.assertIn("review.md", sigs.cache_paths)
        self.assertIn("decisions.log", sigs.cache_paths)

    def test_ambiguity_signals_recorded(self):
        # major findings + fix-plan + no implementation → flagged
        (self.cache / "review.md").write_text(
            "---\nblockers: 0\nmajors: 1\n---\n", encoding="utf-8",
        )
        (self.cache / "fix-plan.md").write_text("plan", encoding="utf-8")
        sigs = aggregate.aggregate(self.cache, 5)
        self.assertTrue(any(
            "majors-without-implementation" in s for s in sigs.ambiguity_signals
        ))

    def test_to_dict_roundtrip(self):
        sigs = aggregate.aggregate(self.cache, 99)
        data = sigs.to_dict()
        # Sanity: it must be a flat-ish dict of JSON-serialisable values.
        json.dumps(data)
        self.assertEqual(data["pr_number"], 99)


if __name__ == "__main__":
    unittest.main()
