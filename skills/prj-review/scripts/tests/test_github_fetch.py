#!/usr/bin/env python3
"""Unit tests for github_fetch.py.

All tests patch the `_run_gh` seam so no real gh CLI calls leave the test.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github_fetch  # noqa: E402


class TestExtractAcceptanceCriteria(unittest.TestCase):
    def test_returns_empty_when_body_blank(self):
        self.assertEqual(github_fetch.extract_acceptance_criteria(""), [])

    def test_returns_empty_when_no_ac_section(self):
        body = "# Title\n\nSome description with no AC heading."
        self.assertEqual(github_fetch.extract_acceptance_criteria(body), [])

    def test_parses_dash_bullets(self):
        body = (
            "# Title\n\n"
            "## Acceptance Criteria\n"
            "- the foo must be bar\n"
            "- baz handles unicode\n\n"
            "## Notes\n"
            "- this should NOT be parsed\n"
        )
        items = github_fetch.extract_acceptance_criteria(body)
        self.assertEqual(items, ["the foo must be bar", "baz handles unicode"])

    def test_parses_numbered_bullets_case_insensitive_heading(self):
        body = (
            "## acceptance-criteria\n"
            "1. first\n"
            "2) second\n"
        )
        self.assertEqual(github_fetch.extract_acceptance_criteria(body), ["first", "second"])

    def test_short_heading_AC_works(self):
        body = "## AC\n- one"
        self.assertEqual(github_fetch.extract_acceptance_criteria(body), ["one"])


class TestFetchPrView(unittest.TestCase):
    def test_parses_view_payload(self):
        payload = {
            "number": 42,
            "title": "Add foo",
            "body": "Description\n## Acceptance Criteria\n- works",
            "baseRefName": "main",
            "headRefName": "feat/foo",
            "author": {"login": "alice"},
            "files": [{"path": "src/foo.ts", "status": "modified"}],
            "state": "OPEN",
            "url": "https://github.com/x/y/pull/42",
        }
        with patch.object(github_fetch, "_run_gh", return_value=json.dumps(payload)):
            view = github_fetch.fetch_pr_view("octocat/hello", 42)
        self.assertEqual(view["number"], 42)
        self.assertEqual(view["files"][0]["path"], "src/foo.ts")

    def test_requires_repo(self):
        with self.assertRaises(github_fetch.GhFetchError):
            github_fetch.fetch_pr_view("", 1)

    def test_non_object_response_raises(self):
        with patch.object(github_fetch, "_run_gh", return_value="[]"):
            with self.assertRaises(github_fetch.GhFetchError):
                github_fetch.fetch_pr_view("octocat/hello", 1)

    def test_invocation_includes_required_json_fields(self):
        captured: dict = {}

        def fake_run(args, timeout=60):
            captured["args"] = args
            return "{}"

        with patch.object(github_fetch, "_run_gh", side_effect=fake_run):
            try:
                github_fetch.fetch_pr_view("octocat/hello", 5)
            except github_fetch.GhFetchError:
                # empty {} fails non-object check? actually {} is an object, ok
                pass

        self.assertIn("--json", captured["args"])
        idx = captured["args"].index("--json")
        json_fields = captured["args"][idx + 1]
        for field in ("number", "title", "body", "baseRefName", "headRefName", "author", "files"):
            self.assertIn(field, json_fields)


class TestFetchPrDiff(unittest.TestCase):
    def test_returns_raw_stdout(self):
        sample_diff = "diff --git a/foo b/foo\n+++ b/foo\n@@ -1 +1 @@\n-a\n+b\n"
        with patch.object(github_fetch, "_run_gh", return_value=sample_diff):
            out = github_fetch.fetch_pr_diff("octocat/hello", 42)
        self.assertEqual(out, sample_diff)

    def test_requires_repo(self):
        with self.assertRaises(github_fetch.GhFetchError):
            github_fetch.fetch_pr_diff("", 1)


class TestFetchChangedFiles(unittest.TestCase):
    def test_skips_binary_extensions(self):
        files = [{"path": "image.png", "status": "added"}]
        result = github_fetch.fetch_changed_files("o/r", 1, "main", files)
        self.assertEqual(result, [{"path": "image.png", "status": "added", "content": None}])

    def test_skips_removed_files(self):
        files = [{"path": "src/old.ts", "status": "removed"}]
        result = github_fetch.fetch_changed_files("o/r", 1, "main", files)
        self.assertEqual(result, [{"path": "src/old.ts", "status": "removed", "content": None}])

    def test_pulls_text_content_via_gh_api(self):
        files = [{"path": "src/foo.ts", "status": "modified"}]
        with patch.object(github_fetch, "_run_gh", return_value="const x = 1\n"):
            result = github_fetch.fetch_changed_files("o/r", 1, "feat/x", files)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["path"], "src/foo.ts")
        self.assertEqual(result[0]["content"], "const x = 1\n")

    def test_records_fetch_error_without_raising(self):
        files = [{"path": "src/foo.ts", "status": "modified"}]
        with patch.object(github_fetch, "_run_gh", side_effect=github_fetch.GhFetchError("boom")):
            result = github_fetch.fetch_changed_files("o/r", 1, "feat/x", files)
        self.assertIsNone(result[0]["content"])
        self.assertEqual(result[0]["fetch_error"], "boom")

    def test_truncates_oversized_files(self):
        big = "x" * (300_000)
        files = [{"path": "src/big.ts", "status": "modified"}]
        with patch.object(github_fetch, "_run_gh", return_value=big):
            result = github_fetch.fetch_changed_files("o/r", 1, "main", files)
        self.assertTrue(result[0]["content"].endswith("[truncated]\n"))


class TestGhFetcher(unittest.TestCase):
    def test_fetch_all_assembles_context(self):
        view_payload = {
            "number": 7,
            "title": "Fix bar",
            "body": "## Acceptance Criteria\n- bar fixed",
            "headRefName": "fix/bar",
            "baseRefName": "main",
            "author": {"login": "carol"},
            "files": [{"path": "README.md", "status": "modified"}],
        }
        call_count = {"i": 0}

        def fake_run(args, timeout=60):
            call_count["i"] += 1
            if "view" in args:
                return json.dumps(view_payload)
            if "diff" in args:
                return "diff --git a/README.md b/README.md\n"
            if "api" in args:
                return "# Project\n"
            return ""

        with patch.object(github_fetch, "_run_gh", side_effect=fake_run):
            ctx = github_fetch.GhFetcher("octocat/hello").fetch_all(7)
        self.assertEqual(ctx.pr["number"], 7)
        self.assertEqual(ctx.acceptance_criteria, ["bar fixed"])
        self.assertEqual(ctx.files[0]["path"], "README.md")
        self.assertIn("README.md", ctx.diff)

    def test_facade_requires_repo(self):
        with self.assertRaises(github_fetch.GhFetchError):
            github_fetch.GhFetcher("")


if __name__ == "__main__":
    unittest.main()
