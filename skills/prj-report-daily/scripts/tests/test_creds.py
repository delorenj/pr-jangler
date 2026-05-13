#!/usr/bin/env python3
"""Unit tests for creds.py."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import creds  # noqa: E402


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRunOp(unittest.TestCase):
    def test_success_returns_stripped_stdout(self):
        with patch.object(subprocess, "run", return_value=_FakeProc(stdout="hello\n")):
            self.assertEqual(creds._run_op("op://X/y"), "hello")

    def test_op_not_found_raises_creds_error(self):
        with patch.object(subprocess, "run", side_effect=FileNotFoundError("no op")):
            with self.assertRaises(creds.CredsError) as ctx:
                creds._run_op("op://X/y")
            self.assertIn("op", str(ctx.exception))

    def test_timeout_raises_creds_error(self):
        exc = subprocess.TimeoutExpired(cmd=["op"], timeout=5)
        with patch.object(subprocess, "run", side_effect=exc):
            with self.assertRaises(creds.CredsError) as ctx:
                creds._run_op("op://X/y")
            self.assertIn("timed out", str(ctx.exception))

    def test_nonzero_exit_raises_creds_error(self):
        with patch.object(subprocess, "run", return_value=_FakeProc(returncode=1, stderr="bad ref")):
            with self.assertRaises(creds.CredsError) as ctx:
                creds._run_op("op://X/y")
            self.assertIn("bad ref", str(ctx.exception))

    def test_empty_output_raises_creds_error(self):
        with patch.object(subprocess, "run", return_value=_FakeProc(stdout="")):
            with self.assertRaises(creds.CredsError):
                creds._run_op("op://X/y")


class TestResolveSmtpCreds(unittest.TestCase):
    def test_success_returns_user_pass_tuple(self):
        calls: list[str] = []

        def fake_runner(ref: str) -> str:
            calls.append(ref)
            if ref.endswith("/username"):
                return "smtp-user@example.com"
            if ref.endswith("/password"):
                return "supersecret"
            raise creds.CredsError(f"unexpected ref: {ref}")

        c = creds.resolve_smtp_creds("op://DeLoSecrets/gmail-smtp", runner=fake_runner)
        self.assertEqual(c.username, "smtp-user@example.com")
        self.assertEqual(c.password, "supersecret")
        self.assertEqual(len(calls), 2)
        self.assertTrue(any(ref.endswith("/username") for ref in calls))
        self.assertTrue(any(ref.endswith("/password") for ref in calls))

    def test_trailing_slash_in_ref_normalized(self):
        observed: list[str] = []

        def fake_runner(ref: str) -> str:
            observed.append(ref)
            return "v"

        creds.resolve_smtp_creds("op://X/y/", runner=fake_runner)
        self.assertEqual(observed[0], "op://X/y/username")
        self.assertEqual(observed[1], "op://X/y/password")

    def test_empty_ref_raises(self):
        with self.assertRaises(creds.CredsError):
            creds.resolve_smtp_creds("", runner=lambda r: "x")
        with self.assertRaises(creds.CredsError):
            creds.resolve_smtp_creds(None, runner=lambda r: "x")  # type: ignore[arg-type]

    def test_runner_error_propagates(self):
        def fake_runner(ref: str) -> str:
            raise creds.CredsError("nope")

        with self.assertRaises(creds.CredsError):
            creds.resolve_smtp_creds("op://X/y", runner=fake_runner)


if __name__ == "__main__":
    unittest.main()
