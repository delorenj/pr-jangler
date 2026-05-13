#!/usr/bin/env python3
"""End-to-end-ish tests for run.py.

Each test builds a temp project root and patches the GhFetcher + post_review_comment
seams via run_module attributes. Findings are stubbed.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

THIS = Path(__file__).resolve()
SCRIPTS = THIS.parent.parent
sys.path.insert(0, str(SCRIPTS))

# Touch review_io first to patch sys.path with state_io's location.
import review_io  # noqa: E402,F401
import run as run_module  # noqa: E402
import state_io  # noqa: E402
import github_fetch  # noqa: E402
import hindsight_lookup  # noqa: E402


class _FakeFetcher:
    """Minimal stand-in for GhFetcher."""

    def __init__(self, repo: str, context: "github_fetch.PrContext | None" = None):
        self.repo = repo
        self._context = context or github_fetch.PrContext(
            pr={
                "number": 11,
                "title": "Fix",
                "body": "## Acceptance Criteria\n- works",
                "headRefName": "fix/x",
                "baseRefName": "main",
                "author": {"login": "alice"},
                "files": [{"path": "src/foo.ts", "status": "modified"}],
            },
            diff="diff --git a/src/foo.ts b/src/foo.ts\n",
            files=[{"path": "src/foo.ts", "status": "modified", "content": "x"}],
            acceptance_criteria=["works"],
        )

    def fetch_all(self, pr_number: int):
        return self._context


GOOD_FINDING = {
    "file": "src/foo.ts",
    "line": 42,
    "severity": "major",
    "category": "correctness",
    "claim": "Off-by-one bug.",
    "suggested_fix": "Use < not <=.",
}


class _RunTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "_bmad").mkdir()
        # Default Hindsight: unavailable, returns empty excerpts.
        self._hindsight_patch = patch.object(
            run_module, "lookup_conventions",
            return_value=hindsight_lookup.HindsightLookupResult(
                bank="prj", query="", excerpts=[], available=False,
                warning="patched: unavailable",
            ),
        )
        self._hindsight_patch.start()

    def tearDown(self):
        self._hindsight_patch.stop()
        self.tmp.cleanup()

    def _write_config(self, repo: str | None = "octocat/hello", post: bool = False):
        toml = "[modules.prj]\n"
        if repo is not None:
            toml += f'prj_repo = "{repo}"\n'
        toml += f"prj_post_review_comment = {'true' if post else 'false'}\n"
        (self.root / "_bmad" / "config.toml").write_text(toml, encoding="utf-8")

    def _last_runlog(self) -> dict:
        log = state_io.runlog_path(self.root)
        self.assertTrue(log.exists(), "runlog should exist")
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def _seed_pr(self, pr_number: int, phase: str = "ReviewPending"):
        state_io.init_state(self.root, "octocat/hello")
        state = state_io.load_state(self.root)
        state["prs"][str(pr_number)] = {
            "pr_number": pr_number,
            "phase": phase,
            "phase_entered_at": "2026-05-10T10:00:00+00:00",
            "last_action_at": "2026-05-10T10:00:00+00:00",
            "contributor_login": "alice",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "next_action": {"skill": "prj-review", "mode": None},
        }
        state_io.save_state(self.root, state)

    def _invoke(self, argv: list[str], poster_fn=None) -> tuple[int, str]:
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["run.py", *argv]), \
             patch.object(run_module, "GhFetcher", _FakeFetcher), \
             patch.object(sys, "stdout", buffer):
            if poster_fn is not None:
                with patch.object(run_module, "post_review_comment", side_effect=poster_fn):
                    rc = run_module.main()
            else:
                rc = run_module.main()
        return rc, buffer.getvalue()


class TestFetchOnly(_RunTestBase):
    def test_missing_repo_returns_2(self):
        self._write_config(repo=None)
        rc, _ = self._invoke([
            "--project-root", str(self.root),
            "--pr", "1", "--fetch-only",
        ])
        self.assertEqual(rc, 2)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "misconfigured")

    def test_fetch_only_emits_payload_and_logs_ok(self):
        self._write_config()
        rc, out = self._invoke([
            "--project-root", str(self.root),
            "--pr", "11", "--fetch-only",
        ])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["pr"]["number"], 11)
        self.assertEqual(payload["acceptance_criteria"], ["works"])
        self.assertIn("conventions", payload)
        # Hindsight stub returns unavailable; conventions list is empty.
        self.assertEqual(payload["conventions"], [])
        self.assertFalse(payload["conventions_meta"]["available"])

        entry = self._last_runlog()
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["phase"], "fetch")
        self.assertEqual(entry["files_count"], 1)

    def test_requires_one_of_fetch_only_or_persist(self):
        self._write_config()
        argv = ["run.py", "--project-root", str(self.root), "--pr", "1"]
        with patch.object(sys, "argv", argv):
            rc = run_module.main()
        self.assertEqual(rc, 2)


class TestPersist(_RunTestBase):
    def _make_findings_file(self, findings: list[dict]) -> Path:
        path = self.root / "findings.json"
        path.write_text(
            json.dumps({"findings": findings, "summary": "ok", "pr_title": "Fix"}),
            encoding="utf-8",
        )
        return path

    def test_persist_writes_review_md_and_transitions_state(self):
        self._write_config(post=False)
        self._seed_pr(11)
        ff = self._make_findings_file([dict(GOOD_FINDING)])

        # Poster should NEVER be invoked when prj_post_review_comment=false.
        def poster_should_not_be_called(**kwargs):
            self.fail("poster was invoked despite enabled=False")

        # Use the real post_review_comment to exercise the skip path.
        rc, out = self._invoke([
            "--project-root", str(self.root),
            "--pr", "11", "--persist",
            "--findings-file", str(ff),
        ])
        self.assertEqual(rc, 0)
        entry = json.loads(out)
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["findings_count"], 1)
        self.assertFalse(entry["post_enabled"])
        self.assertEqual(entry["post"]["status"], "skipped")

        # State transitioned
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["11"]["phase"], "Reviewed")
        self.assertEqual(state["prs"]["11"]["next_action"]["skill"], "prj-decision")

        # review.md exists
        review_path = (
            self.root / "_bmad-output" / "pr-workflow"
            / "prs" / "11" / "review.md"
        )
        self.assertTrue(review_path.exists())
        body = review_path.read_text(encoding="utf-8")
        self.assertIn("src/foo.ts:42", body)
        self.assertIn("[major]", body)

    def test_persist_calls_poster_when_enabled(self):
        self._write_config(post=True)
        self._seed_pr(11)
        ff = self._make_findings_file([dict(GOOD_FINDING)])

        captured = {}

        def fake_poster(*, repo, pr_number, body_file, enabled):
            captured["repo"] = repo
            captured["pr_number"] = pr_number
            captured["body_file"] = body_file
            captured["enabled"] = enabled
            return {"status": "posted", "pr_number": pr_number}

        rc, out = self._invoke(
            [
                "--project-root", str(self.root),
                "--pr", "11", "--persist",
                "--findings-file", str(ff),
            ],
            poster_fn=fake_poster,
        )
        self.assertEqual(rc, 0)
        entry = json.loads(out)
        self.assertTrue(entry["post_enabled"])
        self.assertEqual(entry["post"]["status"], "posted")
        self.assertEqual(captured["pr_number"], 11)
        self.assertTrue(captured["enabled"])
        self.assertEqual(captured["repo"], "octocat/hello")
        # body_file should point at the cached review.md
        self.assertTrue(Path(captured["body_file"]).exists())
        self.assertEqual(Path(captured["body_file"]).name, "review.md")

    def test_persist_rejects_invalid_findings_with_rc_4(self):
        self._write_config()
        self._seed_pr(11)
        bad = dict(GOOD_FINDING, severity="critical")  # Not in VALID_SEVERITIES
        ff = self._make_findings_file([bad])

        rc, _ = self._invoke([
            "--project-root", str(self.root),
            "--pr", "11", "--persist",
            "--findings-file", str(ff),
        ])
        self.assertEqual(rc, 4)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "invalid-findings")
        # State should NOT have transitioned because validation failed first
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["11"]["phase"], "ReviewPending")

    def test_persist_zero_findings_still_writes_cache(self):
        self._write_config()
        self._seed_pr(11)
        ff = self._make_findings_file([])

        rc, out = self._invoke([
            "--project-root", str(self.root),
            "--pr", "11", "--persist",
            "--findings-file", str(ff),
        ])
        self.assertEqual(rc, 0)
        review_path = (
            self.root / "_bmad-output" / "pr-workflow"
            / "prs" / "11" / "review.md"
        )
        self.assertTrue(review_path.exists())
        entry = json.loads(out)
        self.assertEqual(entry["findings_count"], 0)


if __name__ == "__main__":
    unittest.main()
