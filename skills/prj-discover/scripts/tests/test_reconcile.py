#!/usr/bin/env python3
"""Unit tests for reconcile.py.

Pure-function tests: no filesystem, no subprocess. We hand-build state
dicts and gh payloads and assert post-reconcile mutations.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reconcile  # noqa: E402


NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
RECENT_ISO = (NOW - timedelta(days=2)).isoformat()
OLD_ISO = (NOW - timedelta(days=30)).isoformat()


def _empty_state(repo: str = "owner/repo") -> dict:
    return {
        "version": "1.0",
        "repo": repo,
        "last_updated": NOW.isoformat(),
        "heartbeat_count": 0,
        "last_report_sent": None,
        "prs": {},
    }


def _open_pr(number: int, login: str = "alice", title: str = "T") -> dict:
    return {
        "number": number,
        "title": title,
        "author": {"login": login},
        "createdAt": (NOW - timedelta(days=1)).isoformat(),
        "headRefName": f"feature/{number}",
        "baseRefName": "main",
    }


def _view(comment_ids: list[str], review_ids: list[str] = (), thread_ids: list[str] = ()) -> dict:
    return {
        "comments": [{"id": cid} for cid in comment_ids],
        "reviews": [{"id": rid} for rid in review_ids],
        "reviewThreads": [{"comments": [{"id": tid} for tid in thread_ids]}] if thread_ids else [],
    }


class TestReconcile(unittest.TestCase):
    def test_new_pr_added_at_discovered_phase(self):
        state = _empty_state()
        open_prs = [_open_pr(101, login="bob")]
        details = {101: _view(["c1"])}

        report = reconcile.reconcile(state, open_prs, details, now=NOW)

        self.assertEqual(report.new_prs, [101])
        self.assertIn("101", state["prs"])
        entry = state["prs"]["101"]
        self.assertEqual(entry["phase"], "Discovered")
        self.assertEqual(entry["contributor_login"], "bob")
        self.assertEqual(entry["next_action"], {"skill": "prj-triage", "mode": "pr"})
        self.assertEqual(entry["new_comments_since_triage"], 0)
        self.assertEqual(entry["seen_comment_ids"], ["c1"])
        self.assertEqual(entry["phase_entered_at"], NOW.isoformat())
        self.assertEqual(entry["last_action_at"], NOW.isoformat())

    def test_closed_pr_archived(self):
        state = _empty_state()
        state["prs"]["77"] = {
            "pr_number": 77,
            "phase": "ReviewPending",
            "phase_entered_at": RECENT_ISO,
            "last_action_at": RECENT_ISO,
            "contributor_login": "alice",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-review", "mode": None},
            "seen_comment_ids": [],
        }

        # Empty open-PR list → 77 is no longer open
        report = reconcile.reconcile(state, open_prs=[], details_by_number={}, now=NOW)

        self.assertEqual(report.archived_prs, [77])
        self.assertEqual(state["prs"]["77"]["phase"], "Archived")
        self.assertIsNone(state["prs"]["77"]["next_action"])

    def test_new_comment_bumps_count_and_sets_comment_triage(self):
        state = _empty_state()
        state["prs"]["55"] = {
            "pr_number": 55,
            "phase": "Reviewed",
            "phase_entered_at": RECENT_ISO,
            "last_action_at": RECENT_ISO,
            "contributor_login": "alice",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-decision", "mode": None},
            "seen_comment_ids": ["c1", "c2"],
        }
        open_prs = [_open_pr(55)]
        details = {55: _view(["c1", "c2", "c3"], review_ids=["r1"])}

        report = reconcile.reconcile(state, open_prs, details, now=NOW)

        self.assertEqual(report.prs_with_new_comments, [55])
        # 2 new ids (c3 + r1) appeared since last sweep
        self.assertEqual(state["prs"]["55"]["new_comments_since_triage"], 2)
        self.assertEqual(
            state["prs"]["55"]["next_action"],
            {"skill": "prj-triage", "mode": "comment"},
        )
        self.assertEqual(state["prs"]["55"]["last_action_at"], NOW.isoformat())
        # seen_comment_ids should now be the fresh union
        self.assertEqual(
            sorted(state["prs"]["55"]["seen_comment_ids"]),
            ["c1", "c2", "c3", "r1"],
        )

    def test_new_comment_on_discovered_pr_does_not_flip_to_comment_mode(self):
        """An untriaged PR with fresh comments stays on pr-triage mode."""
        state = _empty_state()
        state["prs"]["33"] = {
            "pr_number": 33,
            "phase": "Discovered",
            "phase_entered_at": RECENT_ISO,
            "last_action_at": RECENT_ISO,
            "contributor_login": "alice",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-triage", "mode": "pr"},
            "seen_comment_ids": [],
        }
        open_prs = [_open_pr(33)]
        details = {33: _view(["c1"])}

        report = reconcile.reconcile(state, open_prs, details, now=NOW)

        self.assertEqual(report.prs_with_new_comments, [33])
        self.assertEqual(
            state["prs"]["33"]["next_action"],
            {"skill": "prj-triage", "mode": "pr"},
        )

    def test_idle_pr_flagged_for_retriage(self):
        state = _empty_state()
        state["prs"]["20"] = {
            "pr_number": 20,
            "phase": "Reviewed",
            "phase_entered_at": OLD_ISO,
            "last_action_at": OLD_ISO,  # 30 days ago
            "contributor_login": "alice",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-decision", "mode": None},
            "seen_comment_ids": [],
        }
        open_prs = [_open_pr(20)]
        details = {20: _view([])}

        report = reconcile.reconcile(state, open_prs, details, now=NOW, stale_days=14)

        self.assertEqual(report.retriaged_for_idle, [20])
        self.assertTrue(state["prs"]["20"]["needs_retriage"])

    def test_recently_active_pr_not_flagged_for_retriage(self):
        state = _empty_state()
        state["prs"]["21"] = {
            "pr_number": 21,
            "phase": "Reviewed",
            "phase_entered_at": RECENT_ISO,
            "last_action_at": RECENT_ISO,  # 2 days ago
            "contributor_login": "alice",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-decision", "mode": None},
            "seen_comment_ids": [],
        }
        report = reconcile.reconcile(state, [_open_pr(21)], {21: _view([])}, now=NOW)
        self.assertEqual(report.retriaged_for_idle, [])
        self.assertFalse(state["prs"]["21"]["needs_retriage"])

    def test_terminal_phase_not_flagged_idle(self):
        state = _empty_state()
        state["prs"]["22"] = {
            "pr_number": 22,
            "phase": "Archived",
            "phase_entered_at": OLD_ISO,
            "last_action_at": OLD_ISO,
            "contributor_login": "alice",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": None,
            "seen_comment_ids": [],
        }
        # PR 22 is not in open list and is already Archived; should be no-op
        report = reconcile.reconcile(state, [], {}, now=NOW)
        self.assertEqual(report.archived_prs, [])
        self.assertEqual(report.retriaged_for_idle, [])

    def test_report_to_dict_shape(self):
        report = reconcile.ReconcileReport(
            new_prs=[3, 1, 2],
            archived_prs=[],
            prs_with_new_comments=[7],
            retriaged_for_idle=[],
        )
        out = report.to_dict()
        self.assertEqual(out["new_prs"], [1, 2, 3])  # sorted
        self.assertEqual(out["totals"]["new"], 3)
        self.assertEqual(out["totals"]["new_comments"], 1)

    def test_new_pr_initializes_triaged_comment_ids_empty(self):
        state = _empty_state()
        open_prs = [_open_pr(101)]
        details = {101: _view(["c1", "c2"])}
        reconcile.reconcile(state, open_prs, details, now=NOW)
        entry = state["prs"]["101"]
        self.assertEqual(entry["triaged_comment_ids"], [])
        self.assertEqual(sorted(entry["seen_comment_ids"]), ["c1", "c2"])

    def test_new_pr_records_maintainer_activity_when_configured(self):
        state = _empty_state()
        open_prs = [_open_pr(102)]
        # comment from a maintainer + a non-maintainer
        details = {
            102: {
                "comments": [
                    {"id": "c1", "author": {"login": "alice"}, "createdAt": "2026-05-01T10:00:00Z"},
                    {"id": "c2", "author": {"login": "bob"}, "createdAt": "2026-05-09T10:00:00Z"},
                ],
                "reviews": [
                    {"id": "r1", "author": {"login": "alice"}, "submittedAt": "2026-05-05T10:00:00Z"},
                ],
            }
        }
        reconcile.reconcile(state, open_prs, details, now=NOW, maintainer_logins=["alice"])
        entry = state["prs"]["102"]
        # alice is the maintainer; latest of her two activities is the review on 2026-05-05
        self.assertEqual(entry["last_maintainer_activity_at"], "2026-05-05T10:00:00Z")

    def test_no_maintainer_config_yields_null_field(self):
        state = _empty_state()
        open_prs = [_open_pr(103)]
        details = {
            103: {
                "comments": [{"id": "c1", "author": {"login": "alice"}, "createdAt": "2026-05-01T10:00:00Z"}],
                "reviews": [],
            }
        }
        reconcile.reconcile(state, open_prs, details, now=NOW)  # no maintainer_logins
        entry = state["prs"]["103"]
        self.assertIsNone(entry["last_maintainer_activity_at"])

    def test_update_refreshes_maintainer_activity_each_sweep(self):
        state = _empty_state()
        state["prs"]["104"] = {
            "pr_number": 104,
            "phase": "Reviewed",
            "phase_entered_at": RECENT_ISO,
            "last_action_at": RECENT_ISO,
            "contributor_login": "carol",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-decision", "mode": None},
            "seen_comment_ids": ["c1"],
            "triaged_comment_ids": [],
            "last_maintainer_activity_at": None,  # initially absent
        }
        details = {
            104: {
                "comments": [
                    {"id": "c1", "author": {"login": "carol"}, "createdAt": "2026-05-01T10:00:00Z"},
                    {"id": "c2", "author": {"login": "alice"}, "createdAt": "2026-05-10T10:00:00Z"},
                ],
                "reviews": [],
            }
        }
        reconcile.reconcile(state, [_open_pr(104)], details, now=NOW, maintainer_logins=["alice"])
        # New maintainer comment c2 should set last_maintainer_activity_at on existing PR
        self.assertEqual(state["prs"]["104"]["last_maintainer_activity_at"], "2026-05-10T10:00:00Z")

    def test_no_changes_returns_empty_report(self):
        state = _empty_state()
        state["prs"]["50"] = {
            "pr_number": 50,
            "phase": "Reviewed",
            "phase_entered_at": RECENT_ISO,
            "last_action_at": RECENT_ISO,
            "contributor_login": "alice",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-decision", "mode": None},
            "seen_comment_ids": ["c1"],
        }
        report = reconcile.reconcile(
            state, [_open_pr(50)], {50: _view(["c1"])}, now=NOW
        )
        self.assertEqual(report.to_dict()["totals"],
                         {"new": 0, "archived": 0, "new_comments": 0, "idle_flagged": 0})


if __name__ == "__main__":
    unittest.main()
