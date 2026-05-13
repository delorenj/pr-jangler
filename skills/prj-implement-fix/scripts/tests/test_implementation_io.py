#!/usr/bin/env python3
"""Unit tests for implementation_io.

Builds a fake project root with `_bmad/` + a seeded state + per-PR docs,
then exercises:
  - parse_adversarial_verdict / parse_claim_source / parse_fix_plan_summary
  - write_implementation_record emits frontmatter with all required fields
  - transition_to_ready_to_merge moves the PR phase and clears next_action
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import implementation_io  # noqa: E402
import state_io  # noqa: E402


class TestParsers(unittest.TestCase):
    def test_parse_adversarial_verdict_from_frontmatter(self):
        text = "---\nverdict: pass\nfindings: 0\n---\n# body\n"
        self.assertEqual(implementation_io.parse_adversarial_verdict(text), "pass")

    def test_parse_adversarial_verdict_lowercased(self):
        text = "---\nverdict: REJECT\n---\n"
        self.assertEqual(implementation_io.parse_adversarial_verdict(text), "reject")

    def test_parse_adversarial_verdict_missing_raises(self):
        with self.assertRaises(ValueError):
            implementation_io.parse_adversarial_verdict("no frontmatter here")

    def test_parse_claim_source_dashed_or_underscored(self):
        t1 = "---\nclaim_source: alice <a@b>\n---\n"
        t2 = "---\nclaim-source: bob <b@c>\n---\n"
        self.assertEqual(implementation_io.parse_claim_source(t1), "alice <a@b>")
        self.assertEqual(implementation_io.parse_claim_source(t2), "bob <b@c>")

    def test_parse_claim_source_empty_raises(self):
        with self.assertRaises(ValueError):
            implementation_io.parse_claim_source("---\nclaim_source:\n---\n")

    def test_parse_fix_plan_summary_frontmatter_wins(self):
        text = "---\nsummary: Add guard to foo\n---\n\n# Some Header\n"
        self.assertEqual(
            implementation_io.parse_fix_plan_summary(text),
            "Add guard to foo",
        )

    def test_parse_fix_plan_summary_falls_back_to_h1(self):
        text = "# Fix the foo bug\n\nbody\n"
        self.assertEqual(
            implementation_io.parse_fix_plan_summary(text),
            "Fix the foo bug",
        )

    def test_parse_fix_plan_summary_defaults_when_nothing(self):
        self.assertEqual(
            implementation_io.parse_fix_plan_summary("body only"),
            "apply verified fix",
        )


class TestImplementationIO(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "_bmad").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_state_with_pr(self, pr_number: int = 42) -> dict:
        state_io.init_state(self.root, "octocat/hello")
        state = state_io.load_state(self.root)
        now = datetime.now(timezone.utc).isoformat()
        state["prs"][str(pr_number)] = {
            "pr_number": pr_number,
            "phase": "FixImpl",
            "phase_entered_at": now,
            "last_action_at": now,
            "contributor_login": "alice",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "title": "Add foo",
            "contributor_head_ref": "alice:feat/foo",
            "next_action": {"skill": "prj-implement-fix", "mode": None},
        }
        state_io.save_state(self.root, state)
        return state

    def test_write_implementation_record_creates_file(self):
        self._seed_state_with_pr(42)
        out = implementation_io.write_implementation_record(
            self.root,
            42,
            path="tier-2-pr",
            url="https://github.com/octocat/hello/pull/99",
            branch="prj/auto-fix/42-add-foo",
            base_branch="alice:feat/foo",
            head_branch="prj/auto-fix/42-add-foo",
            commit_sha="deadbeef",
            test_summary={"status": "ok", "returncode": 0},
            title="[prj] add foo (re #42)",
            fallback_reason=None,
            fix_plan_excerpt="--- a\n+++ b\n",
        )
        self.assertTrue(out.exists())
        content = out.read_text(encoding="utf-8")
        self.assertIn("pr_number: 42", content)
        self.assertIn("path: tier-2-pr", content)
        self.assertIn("url: https://github.com/octocat/hello/pull/99", content)
        self.assertIn("commit_sha: deadbeef", content)
        self.assertIn("```json", content)
        self.assertIn("```diff", content)

    def test_write_implementation_record_emits_fallback_reason(self):
        self._seed_state_with_pr(7)
        out = implementation_io.write_implementation_record(
            self.root,
            7,
            path="tier-1-comment-fallback",
            url="https://github.com/octocat/hello/pull/7#comment-1",
            branch="prj/auto-fix/7-x",
            base_branch="alice:feat/x",
            head_branch="prj/auto-fix/7-x",
            commit_sha="cafecafe",
            test_summary={"status": "skipped"},
            title="[prj] x (re #7)",
            fallback_reason="branch is protected",
            fix_plan_excerpt="x",
        )
        content = out.read_text(encoding="utf-8")
        self.assertIn("fallback_reason: branch is protected", content)

    def test_transition_to_ready_to_merge_moves_phase(self):
        self._seed_state_with_pr(42)
        implementation_io.transition_to_ready_to_merge(
            self.root,
            42,
            implementation_url="https://github.com/octocat/hello/pull/99",
            commit_sha="deadbeef",
            path="tier-2-pr",
        )
        state = state_io.load_state(self.root)
        pr = state["prs"]["42"]
        self.assertEqual(pr["phase"], "ReadyToMerge")
        self.assertIsNone(pr["next_action"])
        # History entry recorded.
        self.assertIn("history", pr)
        last = pr["history"][-1]
        self.assertEqual(last["from"], "FixImpl")
        self.assertEqual(last["to"], "ReadyToMerge")
        self.assertEqual(last["by"], "prj-implement-fix")
        self.assertEqual(last["path"], "tier-2-pr")
        self.assertEqual(last["commit_sha"], "deadbeef")

    def test_transition_unknown_pr_raises(self):
        self._seed_state_with_pr(42)
        with self.assertRaises(KeyError):
            implementation_io.transition_to_ready_to_merge(
                self.root,
                999,
                implementation_url="x",
                commit_sha="y",
                path="tier-2-pr",
            )


if __name__ == "__main__":
    unittest.main()
