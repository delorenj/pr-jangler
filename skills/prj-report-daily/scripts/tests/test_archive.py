#!/usr/bin/env python3
"""Unit tests for archive.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import archive  # noqa: E402


class TestArchivePathFor(unittest.TestCase):
    def test_path_uses_local_date_yyyy_mm_dd(self):
        now = datetime(2026, 5, 11, 12, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = archive.archive_path_for(root, now)
        self.assertTrue(str(p).endswith("/reports/2026-05-11.md") or
                        str(p).endswith("/reports/2026-05-10.md"))
        # Either local-date-of-UTC works; we just assert the suffix shape.
        self.assertEqual(p.parent.name, "reports")
        self.assertEqual(p.parent.parent.name, "pr-workflow")


class TestWriteArchive(unittest.TestCase):
    def test_writes_file_with_embedded_html_and_plain(self):
        now = datetime(2026, 5, 11, 12, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = archive.write_archive(
                root,
                subject="[PR Jangler] 2026-05-11: 1 need attention, 0 in progress",
                html_body="<html><body>HELLO</body></html>",
                plain_body="HELLO PLAIN",
                now=now,
            )
            self.assertTrue(path.exists())
            text = path.read_text(encoding="utf-8")
            self.assertIn("# [PR Jangler]", text)
            self.assertIn("HELLO PLAIN", text)
            self.assertIn("HELLO", text)
            self.assertIn("Plain-text body", text)
            self.assertIn("HTML body", text)
            self.assertTrue(str(path).endswith(".md"))

    def test_creates_parent_dirs(self):
        now = datetime(2026, 5, 11, 12, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "nested" / "deep"
            path = archive.write_archive(
                root, subject="s", html_body="<p>h</p>", plain_body="p", now=now,
            )
            self.assertTrue(path.exists())
            self.assertTrue(path.parent.is_dir())

    def test_overwrites_existing_file_on_resend(self):
        now = datetime(2026, 5, 11, 12, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive.write_archive(
                root, subject="first", html_body="<p>1</p>", plain_body="1", now=now,
            )
            path = archive.write_archive(
                root, subject="second", html_body="<p>2</p>", plain_body="2", now=now,
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("second", text)
            self.assertNotIn("first", text)


if __name__ == "__main__":
    unittest.main()
