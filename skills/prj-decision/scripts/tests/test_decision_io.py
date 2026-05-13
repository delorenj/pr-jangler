#!/usr/bin/env python3
"""Unit tests for prj-decision/scripts/decision_io.py.

Covers:
  - render_comment substitutes known tokens, leaves unknown intact
  - apply_label builds the correct gh argv (no subprocess actually invoked)
  - post_comment passes a tmp body-file path (file removed on exit)
  - append_decisions_log writes JSONL line with `ts`
  - transition_state matrix for each decision class and edge cases
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

import decision_io  # noqa: E402


class TestRenderComment(unittest.TestCase):
    def test_basic_substitution(self):
        tpl = "Decision: {decision_class}\nSummary: {summary}\n"
        out = decision_io.render_comment(tpl, {
            "decision_class": "ready-to-merge",
            "summary": "All clear.",
        })
        self.assertIn("Decision: ready-to-merge", out)
        self.assertIn("Summary: All clear.", out)

    def test_unknown_token_preserved(self):
        tpl = "Hello {name}, status: {status}"
        out = decision_io.render_comment(tpl, {"name": "Jarad"})
        self.assertIn("Hello Jarad", out)
        self.assertIn("{status}", out)

    def test_empty_template(self):
        self.assertEqual(decision_io.render_comment("", {"k": "v"}), "")


class TestApplyLabel(unittest.TestCase):
    def test_label_command_shape(self):
        captured: list[list[str]] = []

        def fake_runner(argv: list[str]) -> str:
            captured.append(argv)
            return "ok\n"

        out = decision_io.apply_label(
            "octocat/hello", 7, "ready-to-merge", runner=fake_runner,
        )
        self.assertEqual(out, "ok\n")
        self.assertEqual(captured, [[
            "pr", "edit", "7",
            "--repo", "octocat/hello",
            "--add-label", "prj/decision:ready-to-merge",
        ]])

    def test_invalid_decision_class_raises(self):
        with self.assertRaises(ValueError):
            decision_io.apply_label("o/r", 1, "garbage")


class TestPostComment(unittest.TestCase):
    def test_body_file_passed_and_cleaned(self):
        captured: list[list[str]] = []
        captured_body: list[str] = []

        def fake_runner(argv: list[str]) -> str:
            captured.append(argv)
            # Read the body file before the function unlinks it
            body_path = argv[argv.index("--body-file") + 1]
            captured_body.append(Path(body_path).read_text(encoding="utf-8"))
            return "https://github.com/o/r/pull/7#issuecomment-1\n"

        out = decision_io.post_comment(
            "octocat/hello", 7, "hello\n", runner=fake_runner,
        )
        self.assertIn("issuecomment", out)
        self.assertEqual(len(captured), 1)
        argv = captured[0]
        self.assertEqual(argv[0:3], ["pr", "comment", "7"])
        self.assertIn("--repo", argv)
        self.assertIn("--body-file", argv)
        self.assertEqual(captured_body[0], "hello\n")
        # The tmp file should be cleaned up after post_comment returns
        body_path = Path(argv[argv.index("--body-file") + 1])
        self.assertFalse(body_path.exists(), "tmp body file should be cleaned")


class TestAppendDecisionsLog(unittest.TestCase):
    def test_creates_file_and_appends_with_ts(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "cache"
            entry = {"kind": "decision", "decision": "ready-to-merge"}
            path = decision_io.append_decisions_log(cache, entry)
            self.assertTrue(path.exists())
            line = path.read_text(encoding="utf-8").strip()
            obj = json.loads(line)
            self.assertEqual(obj["decision"], "ready-to-merge")
            self.assertIn("ts", obj)

    def test_appends_to_existing_file(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            decision_io.append_decisions_log(cache, {"kind": "first"})
            decision_io.append_decisions_log(cache, {"kind": "second"})
            lines = (cache / "decisions.log").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["kind"], "first")
            self.assertEqual(json.loads(lines[1])["kind"], "second")


class TestTransitionState(unittest.TestCase):
    def _state_with(self, phase: str) -> dict:
        return {
            "version": "1.0",
            "repo": "octocat/hello",
            "last_updated": "2026-05-11T00:00:00+00:00",
            "heartbeat_count": 0,
            "prs": {
                "42": {
                    "pr_number": 42,
                    "phase": phase,
                    "phase_entered_at": "2026-05-10T00:00:00+00:00",
                    "last_action_at": "2026-05-10T00:00:00+00:00",
                    "contributor_login": "alice",
                    "next_action": {"skill": "prj-decision", "mode": None},
                },
            },
        }

    def test_ready_to_merge_transitions(self):
        state = self._state_with("Reviewed")
        now = datetime(2026, 5, 11, 12, tzinfo=timezone.utc)
        state, status = decision_io.transition_state(state, 42, "ready-to-merge", now)
        self.assertEqual(status, "transitioned")
        self.assertEqual(state["prs"]["42"]["phase"], "ReadyToMerge")
        self.assertIsNone(state["prs"]["42"]["next_action"])

    def test_close_as_not_now_lands_at_rejected(self):
        state = self._state_with("Reviewed")
        state, status = decision_io.transition_state(state, 42, "close-as-not-now")
        self.assertEqual(status, "transitioned")
        self.assertEqual(state["prs"]["42"]["phase"], "Rejected")

    def test_close_from_blocked_allowed(self):
        state = self._state_with("Blocked")
        state, status = decision_io.transition_state(state, 42, "close-as-not-now")
        self.assertEqual(status, "transitioned")
        self.assertEqual(state["prs"]["42"]["phase"], "Rejected")

    def test_request_changes_no_transition(self):
        state = self._state_with("Reviewed")
        state, status = decision_io.transition_state(state, 42, "request-changes")
        self.assertEqual(status, "cleared-next-action")
        self.assertEqual(state["prs"]["42"]["phase"], "Reviewed")
        self.assertIsNone(state["prs"]["42"]["next_action"])

    def test_already_decided_terminal_phase(self):
        state = self._state_with("ReadyToMerge")
        state, status = decision_io.transition_state(state, 42, "ready-to-merge")
        self.assertEqual(status, "already-decided")
        # Phase preserved
        self.assertEqual(state["prs"]["42"]["phase"], "ReadyToMerge")

    def test_unknown_pr(self):
        state = self._state_with("Reviewed")
        state, status = decision_io.transition_state(state, 999, "ready-to-merge")
        self.assertEqual(status, "unknown-pr")

    def test_decision_to_phase_map(self):
        self.assertEqual(decision_io.DECISION_TO_PHASE["ready-to-merge"], "ReadyToMerge")
        self.assertEqual(decision_io.DECISION_TO_PHASE["close-as-not-now"], "Rejected")
        self.assertNotIn("request-changes", decision_io.DECISION_TO_PHASE)


if __name__ == "__main__":
    unittest.main()
