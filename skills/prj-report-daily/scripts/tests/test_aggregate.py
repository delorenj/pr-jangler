#!/usr/bin/env python3
"""Unit tests for aggregate.py."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import aggregate  # noqa: E402


def _ts(now: datetime, hours_ago: float) -> str:
    return (now - timedelta(hours=hours_ago)).isoformat()


def _pr(num: int, phase: str, now: datetime, hours_ago_last_action: float = 1.0,
        hours_ago_phase: float = 1.0, contributor: str = "alice",
        title: str = "Add feature") -> dict:
    return {
        "pr_number": num,
        "phase": phase,
        "phase_entered_at": _ts(now, hours_ago_phase),
        "last_action_at": _ts(now, hours_ago_last_action),
        "contributor_login": contributor,
        "title": title,
    }


class TestGroupPRs(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)

    def test_please_advise_routes_to_needs_attention(self):
        state = {"prs": {"5": _pr(5, "PleaseAdvise", self.now)}}
        groups = aggregate.group_prs(state, self.now)
        self.assertEqual(len(groups["needs_attention"]), 1)
        self.assertEqual(groups["needs_attention"][0]["pr_number"], 5)
        self.assertFalse(groups["in_progress"])
        self.assertFalse(groups["resolved_today"])
        self.assertFalse(groups["no_change"])

    def test_fix_impl_routes_to_needs_attention(self):
        state = {"prs": {"7": _pr(7, "FixImpl", self.now)}}
        groups = aggregate.group_prs(state, self.now)
        self.assertEqual(len(groups["needs_attention"]), 1)
        self.assertEqual(groups["needs_attention"][0]["pr_number"], 7)

    def test_in_progress_recent_activity(self):
        state = {"prs": {"3": _pr(3, "ReviewPending", self.now, hours_ago_last_action=2)}}
        groups = aggregate.group_prs(state, self.now)
        self.assertEqual(len(groups["in_progress"]), 1)
        self.assertEqual(groups["in_progress"][0]["pr_number"], 3)
        self.assertFalse(groups["no_change"])

    def test_in_progress_idle_routes_to_no_change(self):
        state = {"prs": {"3": _pr(3, "ReviewPending", self.now, hours_ago_last_action=48)}}
        groups = aggregate.group_prs(state, self.now)
        self.assertFalse(groups["in_progress"])
        self.assertEqual(len(groups["no_change"]), 1)

    def test_resolved_today_within_24h(self):
        state = {"prs": {
            "9": _pr(9, "ReadyToMerge", self.now, hours_ago_last_action=5),
            "10": _pr(10, "Rejected", self.now, hours_ago_last_action=20),
        }}
        groups = aggregate.group_prs(state, self.now)
        nums = {p["pr_number"] for p in groups["resolved_today"]}
        self.assertEqual(nums, {9, 10})

    def test_terminal_phase_older_than_24h_dropped(self):
        state = {"prs": {"9": _pr(9, "ReadyToMerge", self.now, hours_ago_last_action=48)}}
        groups = aggregate.group_prs(state, self.now)
        self.assertFalse(groups["resolved_today"])
        # Terminal-phase PRs older than 24h are not in any group (they're done).
        self.assertFalse(groups["needs_attention"])
        self.assertFalse(groups["in_progress"])
        self.assertFalse(groups["no_change"])

    def test_synthetic_system_entry_routed_to_needs_attention(self):
        state = {"prs": {"0": {
            "pr_number": 0, "phase": "PleaseAdvise",
            "phase_entered_at": _ts(self.now, 1),
            "last_action_at": _ts(self.now, 1),
            "contributor_login": "prj-report-daily",
            "title": "SMTP failed",
        }}}
        groups = aggregate.group_prs(state, self.now)
        self.assertEqual(len(groups["needs_attention"]), 1)

    def test_needs_attention_sorts_please_advise_before_fix_impl(self):
        state = {"prs": {
            "1": _pr(1, "FixImpl", self.now, hours_ago_phase=1),
            "2": _pr(2, "PleaseAdvise", self.now, hours_ago_phase=10),
        }}
        groups = aggregate.group_prs(state, self.now)
        self.assertEqual(groups["needs_attention"][0]["pr_number"], 2)
        self.assertEqual(groups["needs_attention"][1]["pr_number"], 1)


class TestShortlist(unittest.TestCase):
    def test_cap_at_5_with_overflow(self):
        now = datetime(2026, 5, 11, tzinfo=timezone.utc)
        items = [_pr(i, "PleaseAdvise", now) for i in range(1, 9)]
        sl = aggregate.build_shortlist(items)
        self.assertEqual(len(sl["items"]), 5)
        self.assertEqual(sl["overflow"], 3)

    def test_no_overflow_when_under_cap(self):
        now = datetime(2026, 5, 11, tzinfo=timezone.utc)
        items = [_pr(i, "PleaseAdvise", now) for i in range(1, 4)]
        sl = aggregate.build_shortlist(items)
        self.assertEqual(len(sl["items"]), 3)
        self.assertEqual(sl["overflow"], 0)

    def test_empty_shortlist(self):
        sl = aggregate.build_shortlist([])
        self.assertEqual(sl["items"], [])
        self.assertEqual(sl["overflow"], 0)


class TestAllQuiet(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 5, 11, tzinfo=timezone.utc)
        self.empty_groups = {
            "needs_attention": [], "in_progress": [],
            "resolved_today": [], "no_change": [],
        }

    def test_truly_quiet(self):
        self.assertTrue(aggregate.is_all_quiet(self.empty_groups, []))

    def test_needs_attention_breaks_quiet(self):
        groups = dict(self.empty_groups)
        groups["needs_attention"] = [_pr(1, "PleaseAdvise", self.now)]
        self.assertFalse(aggregate.is_all_quiet(groups, []))

    def test_resolved_today_breaks_quiet(self):
        groups = dict(self.empty_groups)
        groups["resolved_today"] = [_pr(1, "ReadyToMerge", self.now)]
        self.assertFalse(aggregate.is_all_quiet(groups, []))

    def test_runlog_per_pr_dispatch_breaks_quiet(self):
        runlog = [
            {"action": "dispatch", "skill": "prj-review", "status": "ok",
             "ts": self.now.isoformat()},
        ]
        self.assertFalse(aggregate.is_all_quiet(self.empty_groups, runlog))

    def test_runlog_system_dispatch_does_not_break_quiet(self):
        runlog = [
            {"action": "dispatch", "skill": "prj-discover", "status": "ok",
             "ts": self.now.isoformat()},
            {"action": "dispatch", "skill": "prj-report-daily", "status": "ok",
             "ts": self.now.isoformat()},
        ]
        self.assertTrue(aggregate.is_all_quiet(self.empty_groups, runlog))

    def test_runlog_failed_dispatch_does_not_break_quiet(self):
        runlog = [
            {"action": "dispatch", "skill": "prj-review", "status": "stub: not built",
             "ts": self.now.isoformat()},
        ]
        self.assertTrue(aggregate.is_all_quiet(self.empty_groups, runlog))


class TestBuildAggregate(unittest.TestCase):
    def test_top_level_smoke(self):
        now = datetime(2026, 5, 11, tzinfo=timezone.utc)
        state = {"prs": {
            "1": _pr(1, "PleaseAdvise", now, hours_ago_phase=24),
            "2": _pr(2, "ReviewPending", now, hours_ago_last_action=2),
            "3": _pr(3, "ReadyToMerge", now, hours_ago_last_action=10),
        }}
        agg = aggregate.build_aggregate(state, [], None, now)
        self.assertEqual(len(agg["groups"]["needs_attention"]), 1)
        self.assertEqual(len(agg["groups"]["in_progress"]), 1)
        self.assertEqual(len(agg["groups"]["resolved_today"]), 1)
        self.assertEqual(len(agg["shortlist"]["items"]), 1)
        self.assertFalse(agg["all_quiet"])

    def test_all_quiet_path(self):
        now = datetime(2026, 5, 11, tzinfo=timezone.utc)
        agg = aggregate.build_aggregate({"prs": {}}, [], None, now)
        self.assertTrue(agg["all_quiet"])


if __name__ == "__main__":
    unittest.main()
