import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prj_agentd.selector import (  # noqa: E402
    Selection,
    SelectorError,
    _parse_selection,
    run_selector,
)


class TestParseSelection(unittest.TestCase):
    def test_round_trip(self):
        payload = {
            "status": "action-selected",
            "repo": "owner/repo",
            "pr": 42,
            "phase": "ReviewPending",
            "skill": "prj-review",
            "mode": None,
            "priority": 130,
            "reason": "next_action",
            "state_sha": "abc",
        }
        sel = _parse_selection(payload)
        self.assertEqual(sel.skill, "prj-review")
        self.assertEqual(sel.pr, 42)
        self.assertTrue(sel.is_actionable)
        self.assertFalse(sel.is_system_action)

    def test_system_action_flagged(self):
        payload = {
            "status": "action-selected", "repo": "owner/repo", "pr": None,
            "phase": None, "skill": "prj-discover", "mode": None,
            "priority": 500, "reason": "queue empty", "state_sha": "no-state",
        }
        sel = _parse_selection(payload)
        self.assertTrue(sel.is_system_action)
        self.assertTrue(sel.is_actionable)

    def test_rejects_missing_keys(self):
        with self.assertRaises(SelectorError):
            _parse_selection({"status": "idle"})

    def test_rejects_unknown_status(self):
        payload = {
            "status": "exploded", "repo": None, "pr": None, "phase": None,
            "skill": None, "mode": None, "priority": 0, "reason": "",
            "state_sha": "x",
        }
        with self.assertRaises(SelectorError):
            _parse_selection(payload)


class TestRunSelectorIntegration(unittest.TestCase):
    """Drive run_selector against a stand-in run.py script."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake_run = self.root / "skills" / "prj-orchestrator" / "scripts" / "run.py"
        self.fake_run.parent.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_fake_runner(self, payload: dict, exit_code: int = 0):
        # Embed the JSON payload as a Python string literal (via repr) so the
        # generated script just prints it verbatim — no Python<->JSON null/None
        # conversion needed.
        payload_json = json.dumps(payload, sort_keys=True)
        self.fake_run.write_text(
            f"import sys\nsys.stdout.write({payload_json!r})\nsys.stdout.write('\\n')\nsys.exit({exit_code})\n",
            encoding="utf-8",
        )

    def test_returns_action_selected(self):
        self._write_fake_runner({
            "status": "action-selected", "repo": "owner/repo", "pr": None,
            "phase": None, "skill": "prj-discover", "mode": None,
            "priority": 500, "reason": "queue empty", "state_sha": "no-state",
        })
        sel = run_selector(self.root, orchestrator_run_py=self.fake_run)
        self.assertEqual(sel.skill, "prj-discover")
        self.assertEqual(sel.status, "action-selected")

    def test_misconfigured_exit2_is_not_an_error(self):
        self._write_fake_runner({
            "status": "misconfigured", "repo": None, "pr": None, "phase": None,
            "skill": None, "mode": None, "priority": 0,
            "reason": "no prj_repo", "state_sha": "no-state",
        }, exit_code=2)
        sel = run_selector(self.root, orchestrator_run_py=self.fake_run)
        self.assertEqual(sel.status, "misconfigured")

    def test_missing_run_py_raises(self):
        with self.assertRaises(SelectorError):
            run_selector(self.root, orchestrator_run_py=self.root / "nope.py")

    def test_non_json_stdout_raises(self):
        self.fake_run.write_text("print('not json')\n", encoding="utf-8")
        with self.assertRaises(SelectorError):
            run_selector(self.root, orchestrator_run_py=self.fake_run)


if __name__ == "__main__":
    unittest.main()
