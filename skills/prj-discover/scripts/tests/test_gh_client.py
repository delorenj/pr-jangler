#!/usr/bin/env python3
"""Unit tests for gh_client.py.

All tests patch `_run_gh` so no real `gh` invocations leave the test process.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gh_client  # noqa: E402


class TestRateLimit(unittest.TestCase):
    def test_remaining_above_min_is_not_deferred(self):
        payload = {"resources": {"core": {"remaining": 4500, "limit": 5000}}}
        with patch.object(gh_client, "_run_gh", return_value=json.dumps(payload)):
            status = gh_client.get_rate_limit_remaining(min_required=100)
        self.assertEqual(status.remaining, 4500)
        self.assertEqual(status.limit, 5000)
        self.assertFalse(status.deferred)

    def test_remaining_below_min_is_deferred(self):
        payload = {"resources": {"core": {"remaining": 50, "limit": 5000}}}
        with patch.object(gh_client, "_run_gh", return_value=json.dumps(payload)):
            status = gh_client.get_rate_limit_remaining(min_required=100)
        self.assertTrue(status.deferred)
        self.assertEqual(status.remaining, 50)

    def test_remaining_exactly_at_threshold_is_not_deferred(self):
        payload = {"resources": {"core": {"remaining": 100, "limit": 5000}}}
        with patch.object(gh_client, "_run_gh", return_value=json.dumps(payload)):
            status = gh_client.get_rate_limit_remaining(min_required=100)
        self.assertFalse(status.deferred)

    def test_non_object_response_raises(self):
        with patch.object(gh_client, "_run_gh", return_value="[]"):
            with self.assertRaises(gh_client.GhClientError):
                gh_client.get_rate_limit_remaining()

    def test_to_dict_round_trips(self):
        s = gh_client.RateLimitStatus(remaining=10, limit=20, deferred=True)
        self.assertEqual(
            s.to_dict(),
            {"remaining": 10, "limit": 20, "deferred": True},
        )


class TestListOpenPrs(unittest.TestCase):
    def test_parses_pr_list_payload(self):
        payload = [
            {
                "number": 1,
                "title": "First",
                "author": {"login": "alice"},
                "createdAt": "2026-05-01T00:00:00Z",
                "headRefName": "feature/x",
                "baseRefName": "main",
            }
        ]
        with patch.object(gh_client, "_run_gh", return_value=json.dumps(payload)):
            prs = gh_client.list_open_prs("owner/repo")
        self.assertEqual(len(prs), 1)
        self.assertEqual(prs[0]["number"], 1)
        self.assertEqual(prs[0]["author"]["login"], "alice")

    def test_empty_response_returns_empty_list(self):
        with patch.object(gh_client, "_run_gh", return_value=""):
            prs = gh_client.list_open_prs("owner/repo")
        self.assertEqual(prs, [])

    def test_invocation_uses_repo_and_filters_open(self):
        captured = {}

        def fake_run(args, timeout=60):
            captured["args"] = args
            return "[]"

        with patch.object(gh_client, "_run_gh", side_effect=fake_run):
            gh_client.list_open_prs("octocat/hello")

        self.assertIn("--repo", captured["args"])
        repo_idx = captured["args"].index("--repo")
        self.assertEqual(captured["args"][repo_idx + 1], "octocat/hello")
        self.assertIn("--state", captured["args"])
        state_idx = captured["args"].index("--state")
        self.assertEqual(captured["args"][state_idx + 1], "open")

    def test_empty_repo_raises(self):
        with self.assertRaises(gh_client.GhClientError):
            gh_client.list_open_prs("")

    def test_non_list_response_raises(self):
        with patch.object(gh_client, "_run_gh", return_value='{"oops": "object"}'):
            with self.assertRaises(gh_client.GhClientError):
                gh_client.list_open_prs("owner/repo")


class TestFetchPrDetails(unittest.TestCase):
    def test_parses_view_payload(self):
        payload = {
            "number": 42,
            "state": "OPEN",
            "comments": [{"id": "c1"}, {"id": "c2"}],
            "reviews": [{"id": "r1"}],
            "reviewThreads": [{"comments": [{"id": "t1"}]}],
        }
        with patch.object(gh_client, "_run_gh", return_value=json.dumps(payload)):
            details = gh_client.fetch_pr_details("owner/repo", 42)
        self.assertEqual(details["number"], 42)
        self.assertEqual(len(details["comments"]), 2)

    def test_collect_comment_ids_merges_sources(self):
        details = {
            "comments": [{"id": "c1"}, {"id": "c2"}],
            "reviews": [{"id": "r1"}],
            "reviewThreads": [
                {"comments": [{"id": "t1"}, {"id": "t2"}]},
            ],
        }
        ids = gh_client.collect_comment_ids(details)
        self.assertEqual(sorted(ids), ["c1", "c2", "r1", "t1", "t2"])

    def test_collect_comment_ids_handles_missing_keys(self):
        self.assertEqual(gh_client.collect_comment_ids({}), [])
        self.assertEqual(
            gh_client.collect_comment_ids({"comments": None, "reviews": None}),
            [],
        )

    def test_collect_comment_ids_coerces_numeric_ids_to_string(self):
        details = {"comments": [{"databaseId": 12345}]}
        self.assertEqual(gh_client.collect_comment_ids(details), ["12345"])


class TestFetchPrDiffStats(unittest.TestCase):
    def test_extracts_files_changed_count(self):
        stat = (
            "src/foo.ts | 12 ++++++++++++\n"
            "src/bar.ts |  3 ++-\n"
            "2 files changed, 13 insertions(+), 2 deletions(-)\n"
        )
        with patch.object(gh_client, "_run_gh", return_value=stat):
            result = gh_client.fetch_pr_diff_stats("owner/repo", 1)
        self.assertEqual(result["files_changed"], 2)

    def test_singular_files_changed(self):
        stat = "README.md | 1 +\n1 file changed, 1 insertion(+)\n"
        with patch.object(gh_client, "_run_gh", return_value=stat):
            result = gh_client.fetch_pr_diff_stats("owner/repo", 2)
        self.assertEqual(result["files_changed"], 1)

    def test_empty_diff_falls_back_to_zero(self):
        with patch.object(gh_client, "_run_gh", return_value=""):
            result = gh_client.fetch_pr_diff_stats("owner/repo", 3)
        self.assertEqual(result["files_changed"], 0)

    def test_gh_error_falls_back_to_zero(self):
        def boom(args, timeout=60):
            raise gh_client.GhClientError("subprocess died")

        with patch.object(gh_client, "_run_gh", side_effect=boom):
            result = gh_client.fetch_pr_diff_stats("owner/repo", 4)
        self.assertEqual(result["files_changed"], 0)


class TestGhClientFacade(unittest.TestCase):
    def test_requires_repo(self):
        with self.assertRaises(gh_client.GhClientError):
            gh_client.GhClient("")

    def test_facade_delegates(self):
        client = gh_client.GhClient("owner/repo", min_required=50)
        payload = [{"number": 1, "title": "t", "author": {"login": "a"},
                    "createdAt": "2026-05-01T00:00:00Z",
                    "headRefName": "f", "baseRefName": "main"}]
        with patch.object(gh_client, "_run_gh", return_value=json.dumps(payload)):
            self.assertEqual(len(client.list_open()), 1)


if __name__ == "__main__":
    unittest.main()
