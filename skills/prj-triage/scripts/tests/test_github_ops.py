#!/usr/bin/env python3
"""Unit tests for github_ops.py.

All tests patch `_run_gh` so no real `gh` invocations leave the test process.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github_ops  # noqa: E402


class TestLabelFor(unittest.TestCase):
    def test_known_classes_map(self):
        self.assertEqual(github_ops.label_for("actionable"), "prj/triage:actionable")
        self.assertEqual(github_ops.label_for("definite-no"), "prj/triage:definite-no")
        self.assertEqual(
            github_ops.label_for("possible-duplicate"),
            "prj/triage:duplicate-candidate",
        )
        self.assertEqual(github_ops.label_for("needs-review"), "prj/triage:needs-review")

    def test_unknown_class_raises(self):
        with self.assertRaises(github_ops.GhOpsError):
            github_ops.label_for("garbage")

    def test_triage_labels_constant_is_complete(self):
        for klass in ("actionable", "definite-no", "possible-duplicate", "needs-review"):
            self.assertIn(klass, github_ops.TRIAGE_LABELS)


class TestApplyTriageLabel(unittest.TestCase):
    def test_command_construction_for_actionable(self):
        captured: dict = {}

        def fake_run(args, timeout=30):
            captured["args"] = args
            return (0, "label added", "")

        with patch.object(github_ops, "_run_gh", side_effect=fake_run):
            result = github_ops.apply_triage_label("octocat/hello", 42, "actionable")

        self.assertEqual(
            captured["args"],
            [
                "pr", "edit", "42",
                "--repo", "octocat/hello",
                "--add-label", "prj/triage:actionable",
            ],
        )
        self.assertEqual(result["label"], "prj/triage:actionable")
        self.assertEqual(result["pr_number"], 42)
        self.assertEqual(result["repo"], "octocat/hello")
        self.assertEqual(result["status"], "applied")

    def test_command_construction_for_possible_duplicate(self):
        captured: dict = {}

        def fake_run(args, timeout=30):
            captured["args"] = args
            return (0, "", "")

        with patch.object(github_ops, "_run_gh", side_effect=fake_run):
            github_ops.apply_triage_label("o/r", 7, "possible-duplicate")
        self.assertIn("prj/triage:duplicate-candidate", captured["args"])

    def test_gh_nonzero_exit_raises(self):
        with patch.object(
            github_ops, "_run_gh",
            return_value=(1, "", "label does not exist"),
        ):
            with self.assertRaises(github_ops.GhOpsError) as ctx:
                github_ops.apply_triage_label("o/r", 1, "actionable")
        self.assertIn("gh exited 1", str(ctx.exception))
        self.assertIn("label does not exist", str(ctx.exception))

    def test_gh_stderr_falls_back_to_stdout(self):
        with patch.object(
            github_ops, "_run_gh",
            return_value=(2, "stdout-payload-error", ""),
        ):
            with self.assertRaises(github_ops.GhOpsError) as ctx:
                github_ops.apply_triage_label("o/r", 1, "actionable")
        self.assertIn("stdout-payload-error", str(ctx.exception))

    def test_invalid_classification_raises(self):
        with self.assertRaises(github_ops.GhOpsError):
            github_ops.apply_triage_label("o/r", 1, "nonsense")

    def test_empty_repo_raises(self):
        with self.assertRaises(github_ops.GhOpsError):
            github_ops.apply_triage_label("", 1, "actionable")

    def test_zero_pr_number_raises(self):
        with self.assertRaises(github_ops.GhOpsError):
            github_ops.apply_triage_label("o/r", 0, "actionable")

    def test_negative_pr_number_raises(self):
        with self.assertRaises(github_ops.GhOpsError):
            github_ops.apply_triage_label("o/r", -3, "actionable")

    def test_non_int_pr_number_raises(self):
        with self.assertRaises(github_ops.GhOpsError):
            github_ops.apply_triage_label("o/r", "42", "actionable")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
