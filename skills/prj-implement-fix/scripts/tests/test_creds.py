#!/usr/bin/env python3
"""Unit tests for creds.resolve_reference.

Patches the `_resolve_via_op` seam so no real `op` is required. Asserts:
  - op:// path delegates to the seam
  - env:// path reads from os.environ
  - empty / non-prefixed / non-string refs raise CredentialError
  - secret value is never echoed by the CLI surface (length-only output)
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import creds  # noqa: E402


class TestResolveReference(unittest.TestCase):
    def test_op_scheme_delegates_to_op_seam(self):
        with patch.object(creds, "_resolve_via_op", return_value="ghp_secret"):
            self.assertEqual(
                creds.resolve_reference("op://Vault/Item/Field"),
                "ghp_secret",
            )

    def test_env_scheme_reads_environ(self):
        with patch.dict("os.environ", {"_CREDS_TEST_VAR": "env_secret"}):
            self.assertEqual(
                creds.resolve_reference("env://_CREDS_TEST_VAR"),
                "env_secret",
            )

    def test_env_scheme_missing_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(creds.CredentialError):
                creds.resolve_reference("env://_DEFINITELY_NOT_SET_VAR")

    def test_unsupported_scheme_raises(self):
        with self.assertRaises(creds.CredentialError):
            creds.resolve_reference("file:///etc/secret")

    def test_empty_ref_raises(self):
        with self.assertRaises(creds.CredentialError):
            creds.resolve_reference("")

    def test_non_string_ref_raises(self):
        with self.assertRaises(creds.CredentialError):
            creds.resolve_reference(None)  # type: ignore[arg-type]


class TestCLIDoesNotLeakSecret(unittest.TestCase):
    def test_cli_emits_length_only_no_value(self):
        with patch.object(creds, "_resolve_via_op", return_value="topsecret123"), \
             patch.object(sys, "argv", ["creds.py", "resolve", "--ref", "op://A/B/C"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = creds.main()
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["length"], len("topsecret123"))
        # The literal secret value must NOT appear anywhere in stdout.
        self.assertNotIn("topsecret123", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
