#!/usr/bin/env python3
"""Unit tests for select_next_action.py."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from select_next_action import select_next_action  # noqa: E402


DEFAULT_CONFIG = {"prj_discover_every_n": 3, "prj_report_hour_local": 8}
EARLY_MORNING = datetime(2026, 5, 9, 4, 0, tzinfo=timezone.utc)  # before 8am local UTC


def empty_state(repo: str = "owner/repo") -> dict:
    return {
        "version": "1.0",
        "repo": repo,
        "last_updated": "2026-05-09T00:00:00+00:00",
        "heartbeat_count": 0,
        "last_report_sent": None,
        "prs": {},
    }


class TestSelectNextAction(unittest.TestCase):
    def test_empty_queue_dispatches_discover(self):
        action = select_next_action(empty_state(), DEFAULT_CONFIG, now=EARLY_MORNING)
        self.assertEqual(action["action"], "dispatch")
        self.assertEqual(action["skill"], "prj-discover")
        self.assertEqual(action["priority"], 500)

    def test_no_op_when_terminal_only_and_cadence_not_hit(self):
        state = empty_state()
        state["heartbeat_count"] = 2  # 2 % 3 != 0
        state["prs"]["42"] = {
            "pr_number": 42,
            "phase": "ReadyToMerge",
            "phase_entered_at": "2026-05-09T00:00:00+00:00",
            "last_action_at": "2026-05-09T00:00:00+00:00",
            "contributor_login": "alice",
        }
        action = select_next_action(state, DEFAULT_CONFIG, now=EARLY_MORNING)
        self.assertEqual(action["action"], "noop")

    def test_discovery_cadence_triggers(self):
        state = empty_state()
        state["heartbeat_count"] = 3  # 3 % 3 == 0
        state["prs"]["42"] = {
            "pr_number": 42,
            "phase": "ReadyToMerge",
            "phase_entered_at": "2026-05-09T00:00:00+00:00",
            "last_action_at": "2026-05-09T00:00:00+00:00",
            "contributor_login": "alice",
        }
        action = select_next_action(state, DEFAULT_CONFIG, now=EARLY_MORNING)
        self.assertEqual(action["skill"], "prj-discover")

    def test_please_advise_outranks_normal(self):
        state = empty_state()
        state["heartbeat_count"] = 1
        state["prs"]["10"] = {
            "pr_number": 10,
            "phase": "Triaged",
            "phase_entered_at": "2026-05-09T00:00:00+00:00",
            "last_action_at": "2026-05-09T00:00:00+00:00",
            "contributor_login": "bob",
            "next_action": {"skill": "prj-review", "mode": None},
        }
        state["prs"]["42"] = {
            "pr_number": 42,
            "phase": "PleaseAdvise",
            "phase_entered_at": "2026-05-09T00:00:00+00:00",
            "last_action_at": "2026-05-09T00:00:00+00:00",
            "contributor_login": "alice",
            "please_advise_reason": "ambiguous claim",
            "user_acknowledged_please_advise": False,
        }
        action = select_next_action(state, DEFAULT_CONFIG, now=EARLY_MORNING)
        self.assertEqual(action["pr_number"], 42)
        self.assertEqual(action["priority"], 1000)

    def test_acknowledged_please_advise_does_not_rise(self):
        state = empty_state()
        state["heartbeat_count"] = 1
        state["prs"]["42"] = {
            "pr_number": 42,
            "phase": "PleaseAdvise",
            "phase_entered_at": "2026-05-09T00:00:00+00:00",
            "last_action_at": "2026-05-09T00:00:00+00:00",
            "contributor_login": "alice",
            "please_advise_reason": "ambiguous claim",
            "user_acknowledged_please_advise": True,
        }
        action = select_next_action(state, DEFAULT_CONFIG, now=EARLY_MORNING)
        self.assertEqual(action["action"], "noop")

    def test_fifo_tiebreak_older_wins(self):
        state = empty_state()
        state["heartbeat_count"] = 1
        state["prs"]["10"] = {
            "pr_number": 10,
            "phase": "Triaged",
            "phase_entered_at": "2026-05-07T00:00:00+00:00",  # 2 days older
            "last_action_at": "2026-05-07T00:00:00+00:00",
            "contributor_login": "bob",
            "next_action": {"skill": "prj-review", "mode": None},
        }
        state["prs"]["20"] = {
            "pr_number": 20,
            "phase": "Triaged",
            "phase_entered_at": "2026-05-09T00:00:00+00:00",
            "last_action_at": "2026-05-09T00:00:00+00:00",
            "contributor_login": "alice",
            "next_action": {"skill": "prj-review", "mode": None},
        }
        action = select_next_action(state, DEFAULT_CONFIG, now=EARLY_MORNING)
        self.assertEqual(action["pr_number"], 10)

    def test_new_comments_bump_priority(self):
        state = empty_state()
        state["heartbeat_count"] = 1
        state["prs"]["10"] = {
            "pr_number": 10,
            "phase": "Triaged",
            "phase_entered_at": "2026-05-09T00:00:00+00:00",
            "last_action_at": "2026-05-09T00:00:00+00:00",
            "contributor_login": "bob",
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-review", "mode": None},
        }
        state["prs"]["20"] = {
            "pr_number": 20,
            "phase": "Triaged",
            "phase_entered_at": "2026-05-09T00:00:00+00:00",
            "last_action_at": "2026-05-09T00:00:00+00:00",
            "contributor_login": "alice",
            "new_comments_since_triage": 3,
            "next_action": {"skill": "prj-review", "mode": None},
        }
        action = select_next_action(state, DEFAULT_CONFIG, now=EARLY_MORNING)
        self.assertEqual(action["pr_number"], 20)

    def test_daily_report_due(self):
        state = empty_state()
        state["heartbeat_count"] = 5
        # Build a `now` at 9am in the system's LOCAL timezone so the report-hour
        # comparison (which uses local-time semantics) is deterministic across hosts.
        local_tz = datetime.now().astimezone().tzinfo
        local_9am = datetime(2026, 5, 9, 9, 0, tzinfo=local_tz)
        # last_report_sent: yesterday in local terms, expressed as UTC ISO
        prev_local = datetime(2026, 5, 8, 8, 0, tzinfo=local_tz)
        state["last_report_sent"] = prev_local.astimezone(timezone.utc).isoformat()
        action = select_next_action(state, DEFAULT_CONFIG, now=local_9am)
        self.assertEqual(action["skill"], "prj-report-daily")

    def test_daily_report_not_due_after_send(self):
        state = empty_state()
        state["heartbeat_count"] = 5
        state["prs"]["10"] = {
            "pr_number": 10,
            "phase": "Triaged",
            "phase_entered_at": "2026-05-09T00:00:00+00:00",
            "last_action_at": "2026-05-09T00:00:00+00:00",
            "contributor_login": "bob",
            "next_action": {"skill": "prj-review", "mode": None},
        }
        local_tz = datetime.now().astimezone().tzinfo
        local_9am = datetime(2026, 5, 9, 9, 0, tzinfo=local_tz)
        report_sent_local = datetime(2026, 5, 9, 8, 5, tzinfo=local_tz)
        state["last_report_sent"] = report_sent_local.astimezone(timezone.utc).isoformat()
        action = select_next_action(state, DEFAULT_CONFIG, now=local_9am)
        # Should pick the PR review, not the report
        self.assertEqual(action["skill"], "prj-review")


if __name__ == "__main__":
    unittest.main()
