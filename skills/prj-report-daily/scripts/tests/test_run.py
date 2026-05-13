#!/usr/bin/env python3
"""Integration tests for prj-report-daily/run.py.

Each test builds a temporary project root, mocks `op` and SMTP, runs
`run.main()`, and asserts state mutations and runlog entries.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import aggregate  # noqa: E402,F401  (ensures state_io on sys.path)
import creds as creds_module  # noqa: E402
import run as run_module  # noqa: E402
import smtp_send  # noqa: E402
import state_io  # noqa: E402


class _FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=30):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[tuple] = []
        _FakeSMTP.instances.append(self)

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def sendmail(self, sender, recipients, message):
        self.calls.append(("sendmail", sender, tuple(recipients), len(message)))

    def quit(self):
        self.calls.append(("quit",))


class _BoomSMTP(_FakeSMTP):
    def login(self, user, password):
        raise Exception("auth failed")


def _fake_op_runner(ref: str) -> str:
    if ref.endswith("/username"):
        return "smtp-user@example.com"
    if ref.endswith("/password"):
        return "topsecret"
    raise creds_module.CredsError(f"unexpected ref: {ref}")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "_bmad").mkdir()
        _FakeSMTP.instances.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_config(self, **overrides):
        cfg = {
            "prj_repo": "octocat/hello",
            "prj_email_to": "you@example.com",
            "prj_email_from": "bot@example.com",
            "prj_smtp_host": "smtp.example.com",
            "prj_smtp_port": 587,
            "prj_smtp_creds_ref": "op://Vault/smtp",
        }
        cfg.update(overrides)
        lines = ["[modules.prj]"]
        for k, v in cfg.items():
            if v is None:
                continue
            if isinstance(v, int):
                lines.append(f"{k} = {v}")
            else:
                lines.append(f'{k} = "{v}"')
        (self.root / "_bmad" / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _seed_state(self):
        state_io.init_state(self.root, "octocat/hello")

    def _last_runlog(self) -> dict:
        path = state_io.runlog_path(self.root)
        self.assertTrue(path.exists())
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(lines)
        return json.loads(lines[-1])

    def _invoke(self, extra=None, smtp_factory=_FakeSMTP, op_runner=_fake_op_runner):
        argv = ["run.py", "--project-root", str(self.root)]
        if extra:
            argv.extend(extra)
        with patch.object(sys, "argv", argv), \
             patch.object(smtp_send.smtplib, "SMTP", smtp_factory), \
             patch.object(creds_module, "_run_op", op_runner):
            return run_module.main()


class TestRunHappyPath(_Base):
    def test_send_updates_last_report_sent_only_on_success(self):
        self._write_config()
        self._seed_state()
        rc = self._invoke()
        self.assertEqual(rc, 0)
        state = state_io.load_state(self.root)
        self.assertIsNotNone(state["last_report_sent"])
        # SMTP got the canonical sequence.
        self.assertEqual(len(_FakeSMTP.instances), 1)
        sequence = [c[0] for c in _FakeSMTP.instances[0].calls]
        self.assertEqual(sequence, ["starttls", "login", "sendmail", "quit"])
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "ok")
        self.assertIn("subject", entry)
        self.assertIn("archive_path", entry)
        self.assertTrue(Path(entry["archive_path"]).exists())

    def test_all_quiet_subject_when_empty(self):
        self._write_config()
        self._seed_state()
        rc = self._invoke()
        self.assertEqual(rc, 0)
        entry = self._last_runlog()
        self.assertTrue(entry["all_quiet"])
        self.assertIn("all quiet", entry["subject"])

    def test_active_subject_with_please_advise(self):
        self._write_config()
        self._seed_state()
        state = state_io.load_state(self.root)
        state["prs"]["42"] = {
            "pr_number": 42,
            "phase": "PleaseAdvise",
            "phase_entered_at": "2026-05-10T00:00:00+00:00",
            "last_action_at": "2026-05-11T00:00:00+00:00",
            "contributor_login": "alice",
        }
        state_io.save_state(self.root, state)
        rc = self._invoke()
        self.assertEqual(rc, 0)
        entry = self._last_runlog()
        self.assertFalse(entry["all_quiet"])
        self.assertIn("need attention", entry["subject"])


class TestRunDryRun(_Base):
    def test_dry_run_does_not_send_or_update_last_report_sent(self):
        self._write_config()
        self._seed_state()
        before = state_io.load_state(self.root).get("last_report_sent")
        rc = self._invoke(extra=["--dry-run"])
        self.assertEqual(rc, 0)
        # No SMTP factory ever instantiated.
        self.assertEqual(_FakeSMTP.instances, [])
        after = state_io.load_state(self.root).get("last_report_sent")
        self.assertEqual(before, after)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "dry-run")
        self.assertTrue(Path(entry["archive_path"]).exists())


class TestRunMisconfigured(_Base):
    def test_missing_required_key_exits_2(self):
        self._write_config(prj_email_to=None)
        rc = self._invoke()
        self.assertEqual(rc, 2)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "misconfigured")
        self.assertIn("prj_email_to", entry["reason"])


class TestRunCredsError(_Base):
    def test_op_failure_exits_4(self):
        self._write_config()
        self._seed_state()

        def boom(ref):
            raise creds_module.CredsError("op vault locked")

        rc = self._invoke(op_runner=boom)
        self.assertEqual(rc, 4)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "creds-error")
        self.assertIn("op vault locked", entry["error"])


class TestRunSmtpFailure(_Base):
    def test_smtp_failure_bumps_counter_and_does_not_update_last_report_sent(self):
        self._write_config()
        self._seed_state()
        rc = self._invoke(smtp_factory=_BoomSMTP)
        self.assertEqual(rc, 5)
        state = state_io.load_state(self.root)
        self.assertIsNone(state["last_report_sent"])
        self.assertEqual(state.get("report_send_failures"), 1)
        self.assertNotIn("0", state["prs"])
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "smtp-error")
        self.assertEqual(entry["report_send_failures"], 1)
        self.assertFalse(entry["escalated_to_please_advise"])

    def test_three_consecutive_failures_escalate_to_please_advise(self):
        self._write_config()
        self._seed_state()
        # Pre-seed counter to 2: this run will be the 3rd failure.
        state = state_io.load_state(self.root)
        state["report_send_failures"] = 2
        state_io.save_state(self.root, state)

        rc = self._invoke(smtp_factory=_BoomSMTP)
        self.assertEqual(rc, 5)
        state = state_io.load_state(self.root)
        self.assertIn("0", state["prs"])
        self.assertEqual(state["prs"]["0"]["phase"], "PleaseAdvise")
        # Counter resets so we don't escalate again on the very next failure.
        self.assertEqual(state.get("report_send_failures"), 0)
        entry = self._last_runlog()
        self.assertTrue(entry["escalated_to_please_advise"])


class TestRunArchiveAlwaysWritten(_Base):
    def test_archive_present_even_on_smtp_failure(self):
        self._write_config()
        self._seed_state()
        rc = self._invoke(smtp_factory=_BoomSMTP)
        self.assertEqual(rc, 5)
        # Archive was written before the SMTP attempt.
        now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        archive_file = self.root / "_bmad-output" / "pr-workflow" / "reports" / f"{now}.md"
        self.assertTrue(archive_file.exists())


if __name__ == "__main__":
    unittest.main()
