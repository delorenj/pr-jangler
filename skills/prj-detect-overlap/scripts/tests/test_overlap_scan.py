#!/usr/bin/env python3
"""Unit tests for overlap_scan.py.

Pure-function tests for compute_pairs + collect_other_pr_numbers, plus
mocked-subprocess tests for fetch_changed_files and the end-to-end scan().
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import overlap_scan  # noqa: E402


class TestComputePairs(unittest.TestCase):
    def test_no_overlap_returns_empty(self):
        report = overlap_scan.compute_pairs(
            target_pr=10,
            target_files=["a.py", "b.py"],
            other_files_by_pr={20: ["c.py"], 30: ["d.py"]},
        )
        self.assertEqual(report.pairs, [])
        self.assertEqual(report.skipped, [])
        self.assertEqual(report.target_pr, 10)
        self.assertEqual(report.target_files, ("a.py", "b.py"))

    def test_single_file_overlap_below_threshold_skipped(self):
        report = overlap_scan.compute_pairs(
            target_pr=10,
            target_files=["a.py", "b.py"],
            other_files_by_pr={20: ["a.py", "z.py"]},
            threshold=2,
        )
        self.assertEqual(len(report.pairs), 0)
        self.assertEqual(len(report.skipped), 1)
        self.assertEqual(report.skipped[0].other_pr, 20)
        self.assertEqual(report.skipped[0].overlap_count, 1)
        self.assertEqual(report.skipped[0].shared_files, ("a.py",))

    def test_two_file_overlap_hits_threshold(self):
        report = overlap_scan.compute_pairs(
            target_pr=10,
            target_files=["a.py", "b.py", "c.py"],
            other_files_by_pr={20: ["a.py", "b.py", "z.py"]},
        )
        self.assertEqual(len(report.pairs), 1)
        self.assertEqual(len(report.skipped), 0)
        pair = report.pairs[0]
        self.assertEqual(pair.target_pr, 10)
        self.assertEqual(pair.other_pr, 20)
        self.assertEqual(pair.overlap_count, 2)
        self.assertEqual(pair.shared_files, ("a.py", "b.py"))

    def test_multiple_pairs_sorted_by_overlap_desc_then_pr_asc(self):
        report = overlap_scan.compute_pairs(
            target_pr=10,
            target_files=["a.py", "b.py", "c.py", "d.py"],
            other_files_by_pr={
                40: ["a.py", "b.py"],          # overlap 2
                30: ["a.py", "b.py", "c.py"],  # overlap 3
                20: ["a.py", "b.py", "d.py"],  # overlap 3
                50: ["e.py"],                  # overlap 0 -> dropped
            },
        )
        self.assertEqual([p.other_pr for p in report.pairs], [20, 30, 40])
        self.assertEqual([p.overlap_count for p in report.pairs], [3, 3, 2])

    def test_custom_threshold_three_files(self):
        report = overlap_scan.compute_pairs(
            target_pr=10,
            target_files=["a.py", "b.py", "c.py"],
            other_files_by_pr={
                20: ["a.py", "b.py"],          # 2
                30: ["a.py", "b.py", "c.py"],  # 3
            },
            threshold=3,
        )
        self.assertEqual(len(report.pairs), 1)
        self.assertEqual(report.pairs[0].other_pr, 30)
        self.assertEqual(len(report.skipped), 1)
        self.assertEqual(report.skipped[0].other_pr, 20)

    def test_to_dict_shape(self):
        report = overlap_scan.compute_pairs(
            target_pr=10,
            target_files=["a.py", "b.py"],
            other_files_by_pr={20: ["a.py", "b.py"]},
        )
        d = report.to_dict()
        self.assertEqual(d["target_pr"], 10)
        self.assertEqual(d["target_files"], ["a.py", "b.py"])
        self.assertEqual(d["pairs"][0]["other_pr"], 20)
        self.assertEqual(d["totals"]["pairs_over_threshold"], 1)
        self.assertEqual(d["totals"]["pairs_skipped"], 0)


class TestCollectOtherPrNumbers(unittest.TestCase):
    def _state(self, prs: dict[str, str]) -> dict:
        return {
            "version": "1.0",
            "repo": "owner/repo",
            "last_updated": "2026-05-11T00:00:00+00:00",
            "heartbeat_count": 0,
            "prs": {
                key: {
                    "pr_number": int(key),
                    "phase": phase,
                    "phase_entered_at": "2026-05-11T00:00:00+00:00",
                    "last_action_at": "2026-05-11T00:00:00+00:00",
                    "contributor_login": "alice",
                }
                for key, phase in prs.items()
            },
        }

    def test_excludes_target(self):
        state = self._state({"10": "ReviewPending", "20": "ReviewPending"})
        result = overlap_scan.collect_other_pr_numbers(state, target_pr=10)
        self.assertEqual(result, [20])

    def test_excludes_terminal_phases(self):
        state = self._state({
            "10": "ReviewPending",
            "20": "Archived",
            "30": "Rejected",
            "40": "Blocked",
            "50": "ReadyToMerge",
            "60": "ReviewPending",
        })
        result = overlap_scan.collect_other_pr_numbers(state, target_pr=10)
        self.assertEqual(result, [60])

    def test_sorted_ascending(self):
        state = self._state({"99": "Reviewed", "5": "ReviewPending", "55": "Triaged"})
        result = overlap_scan.collect_other_pr_numbers(state, target_pr=999)
        self.assertEqual(result, [5, 55, 99])

    def test_empty_state(self):
        state = self._state({})
        result = overlap_scan.collect_other_pr_numbers(state, target_pr=1)
        self.assertEqual(result, [])


class TestFetchChangedFiles(unittest.TestCase):
    def test_strips_blanks_and_whitespace(self):
        raw = "src/foo.ts\n\n  src/bar.ts  \n\nsrc/baz.ts\n"
        with patch.object(overlap_scan, "_run_gh", return_value=raw) as mock_run:
            result = overlap_scan.fetch_changed_files("owner/repo", 42)
        self.assertEqual(result, ["src/foo.ts", "src/bar.ts", "src/baz.ts"])
        mock_run.assert_called_once()
        called_args = mock_run.call_args.args[0]
        self.assertIn("pr", called_args)
        self.assertIn("diff", called_args)
        self.assertIn("42", called_args)
        self.assertIn("--name-only", called_args)
        self.assertIn("--repo", called_args)
        self.assertIn("owner/repo", called_args)

    def test_empty_repo_raises(self):
        with self.assertRaises(overlap_scan.OverlapScanError):
            overlap_scan.fetch_changed_files("", 1)

    def test_gh_failure_propagates(self):
        def boom(*_args, **_kw):
            raise overlap_scan.OverlapScanError("gh exited 1: not found")

        with patch.object(overlap_scan, "_run_gh", side_effect=boom):
            with self.assertRaises(overlap_scan.OverlapScanError):
                overlap_scan.fetch_changed_files("owner/repo", 1)


class TestScanEndToEnd(unittest.TestCase):
    def test_scan_returns_sorted_pairs(self):
        # gh is called once per PR (target + each other). Different responses
        # are keyed off the pr_number arg position in the args list.
        files_by_pr = {
            10: "a.py\nb.py\nc.py\n",
            20: "a.py\nb.py\nz.py\n",  # overlap 2 with 10
            30: "a.py\nc.py\nq.py\n",  # overlap 2 with 10
            40: "x.py\n",               # overlap 0
        }

        def fake_run(args, timeout=60):
            # args = ["pr","diff",N,"--repo","owner/repo","--name-only"]
            number = int(args[2])
            return files_by_pr[number]

        with patch.object(overlap_scan, "_run_gh", side_effect=fake_run):
            report = overlap_scan.scan(
                "owner/repo", target_pr=10, other_pr_numbers=[20, 30, 40],
            )
        self.assertEqual(len(report.pairs), 2)
        other_prs = [p.other_pr for p in report.pairs]
        # Both pairs have overlap=2, tie-break by other_pr asc
        self.assertEqual(other_prs, [20, 30])
        self.assertEqual(report.target_files, ("a.py", "b.py", "c.py"))

    def test_scan_no_others(self):
        with patch.object(overlap_scan, "_run_gh", return_value="a.py\n"):
            report = overlap_scan.scan("owner/repo", target_pr=10, other_pr_numbers=[])
        self.assertEqual(report.pairs, [])
        self.assertEqual(report.target_files, ("a.py",))


if __name__ == "__main__":
    unittest.main()
