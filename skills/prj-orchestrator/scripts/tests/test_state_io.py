#!/usr/bin/env python3
"""Unit tests for state_io.py."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import state_io  # noqa: E402


class TestStateIO(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "_bmad").mkdir()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_init_creates_empty_state(self):
        state = state_io.init_state(self.root, "owner/repo")
        self.assertEqual(state["version"], "1.0")
        self.assertEqual(state["repo"], "owner/repo")
        self.assertEqual(state["prs"], {})
        self.assertEqual(state["heartbeat_count"], 0)
        self.assertTrue(state_io.state_path(self.root).exists())

    def test_init_is_idempotent(self):
        state1 = state_io.init_state(self.root, "owner/repo")
        state1["heartbeat_count"] = 5
        state_io.save_state(self.root, state1)
        state2 = state_io.init_state(self.root, "owner/different")
        self.assertEqual(state2["heartbeat_count"], 5)
        self.assertEqual(state2["repo"], "owner/repo")

    def test_save_and_load_roundtrip(self):
        state = state_io.init_state(self.root, "owner/repo")
        state["prs"]["42"] = {
            "pr_number": 42,
            "phase": "Discovered",
            "phase_entered_at": "2026-05-09T00:00:00+00:00",
            "last_action_at": "2026-05-09T00:00:00+00:00",
            "contributor_login": "alice",
        }
        state_io.save_state(self.root, state)
        loaded = state_io.load_state(self.root)
        self.assertIn("42", loaded["prs"])
        self.assertEqual(loaded["prs"]["42"]["phase"], "Discovered")

    def test_invalid_state_rejected_atomically(self):
        state_io.init_state(self.root, "owner/repo")
        bad = state_io.load_state(self.root)
        bad["version"] = "broken"
        with self.assertRaises(ValueError):
            state_io.save_state(self.root, bad)
        # Original state still loads cleanly (no partial overwrite)
        good = state_io.load_state(self.root)
        self.assertEqual(good["version"], "1.0")

    def test_validate_rejects_invalid_phase(self):
        state_io.init_state(self.root, "owner/repo")
        s = state_io.load_state(self.root)
        s["prs"]["42"] = {
            "pr_number": 42,
            "phase": "InvalidPhase",
            "phase_entered_at": "2026-05-09T00:00:00+00:00",
            "last_action_at": "2026-05-09T00:00:00+00:00",
            "contributor_login": "alice",
        }
        with self.assertRaises(ValueError):
            state_io.save_state(self.root, s)

    def test_validate_rejects_non_digit_pr_key(self):
        state_io.init_state(self.root, "owner/repo")
        s = state_io.load_state(self.root)
        s["prs"]["abc"] = {
            "pr_number": 1,
            "phase": "Discovered",
            "phase_entered_at": "2026-05-09T00:00:00+00:00",
            "last_action_at": "2026-05-09T00:00:00+00:00",
            "contributor_login": "alice",
        }
        with self.assertRaises(ValueError):
            state_io.save_state(self.root, s)

    def test_append_runlog_creates_and_appends(self):
        state_io.append_runlog(self.root, {"action": "test", "status": "ok"})
        log = state_io.runlog_path(self.root)
        self.assertTrue(log.exists())
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["action"], "test")
        self.assertIn("ts", entry)
        state_io.append_runlog(self.root, {"action": "test2"})
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_find_project_root_walks_up(self):
        nested = self.root / "skills" / "prj-orchestrator" / "scripts"
        nested.mkdir(parents=True)
        found = state_io.find_project_root(nested)
        self.assertEqual(found, self.root)


if __name__ == "__main__":
    unittest.main()
