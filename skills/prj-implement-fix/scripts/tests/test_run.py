#!/usr/bin/env python3
"""Integration tests for prj-implement-fix/run.py.

Builds a tmp project root with config + state + per-PR cache. Mocks the git
seam (`branch_ops._run_git`), the gh seam (`pr_create._run_gh`), and the
credential resolver (`creds.resolve_reference`) so no real network or git is
needed.

Covers:
  - end-to-end happy path
  - fallback path on contributor veto
  - refuse when adversarial verdict != pass
  - refuse when fix-plan has no diff fence
  - refuse when claim_source is missing
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

import branch_ops  # noqa: E402,F401
import implementation_io  # noqa: E402,F401
import pr_create  # noqa: E402
import run as run_module  # noqa: E402
import state_io  # noqa: E402


CONFIG_TOML = """\
[modules.prj]
prj_repo = "octocat/hello"
prj_bot_user = "prj-bot"
prj_bot_token_ref = "env://_TEST_TOKEN"
"""

FIX_PLAN = """\
---
summary: Guard against nil
---

# Fix

## Rationale

The guard is required because foo can be None when bar is missing.

## Risks

None enumerated beyond cache layer interactions.

## Diff

```diff
--- a/src/foo.ts
+++ b/src/foo.ts
@@ -1,3 +1,4 @@
 export function foo() {
+  if (!bar) return;
   return bar;
 }
```
"""

VERIFICATION = """\
---
verdict: verified
claim_source: alice <alice@users.noreply.github.com>
---

Reproduced.
"""

ADVERSARIAL_PASS = """\
---
verdict: pass
findings: 0
---

LGTM
"""

ADVERSARIAL_REJECT = """\
---
verdict: reject
findings: 2
---

Issues.
"""


class _GitRecorder:
    """Records every _run_git invocation and returns deterministic OK results."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, args, cwd=None, env=None, stdin_text=None, timeout=120):
        self.calls.append({
            "args": list(args),
            "cwd": cwd,
            "env": env,
            "stdin": stdin_text,
        })
        # rev-parse HEAD -> sha; everything else returns ok stdout.
        if args[0:2] == ["rev-parse", "HEAD"]:
            return branch_ops.GitResult(0, "abc123def456\n", "")
        return branch_ops.GitResult(0, "", "")


class _GhScripted:
    def __init__(self, plan):
        self.plan = list(plan)
        self.calls: list[dict] = []

    def __call__(self, args, timeout: int = 60, stdin_text=None):
        self.calls.append({"args": list(args), "stdin": stdin_text})
        joined = " ".join(args)
        for marker, result in self.plan:
            if marker in joined:
                return result
        return (0, "ok\n", "")


class TestRunHappyPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "_bmad").mkdir()
        (self.root / "_bmad" / "config.toml").write_text(CONFIG_TOML, encoding="utf-8")

        state_io.init_state(self.root, "octocat/hello")
        state = state_io.load_state(self.root)
        now = datetime.now(timezone.utc).isoformat()
        state["prs"]["42"] = {
            "pr_number": 42,
            "phase": "FixImpl",
            "phase_entered_at": now,
            "last_action_at": now,
            "contributor_login": "alice",
            "needs_retriage": False,
            "new_comments_since_triage": 0,
            "title": "Add foo guard",
            "contributor_head_ref": "alice:feat/foo",
            "next_action": {"skill": "prj-implement-fix", "mode": None},
        }
        state_io.save_state(self.root, state)

        cache = self.root / "_bmad-output" / "pr-workflow" / "prs" / "42"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "fix-plan.md").write_text(FIX_PLAN, encoding="utf-8")
        (cache / "verification.md").write_text(VERIFICATION, encoding="utf-8")
        (cache / "adversarial.md").write_text(ADVERSARIAL_PASS, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _last_runlog(self) -> dict:
        log = state_io.runlog_path(self.root)
        self.assertTrue(log.exists(), "runlog should exist")
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def _invoke(self, *, gh_plan, extra_args=None):
        recorder = _GitRecorder()
        gh = _GhScripted(gh_plan)
        argv = [
            "--pr-number", "42",
            "--project-root", str(self.root),
        ]
        if extra_args:
            argv.extend(extra_args)

        with patch.object(branch_ops, "_run_git", side_effect=recorder), \
             patch.object(pr_create, "_run_gh", side_effect=gh), \
             patch.dict("os.environ", {"_TEST_TOKEN": "ghp_fake"}):
            rc = run_module.run(argv)
        return rc, recorder, gh

    def test_happy_path_opens_pr_and_transitions_state(self):
        rc, recorder, gh = self._invoke(gh_plan=[
            ("pr create", (0, "https://github.com/octocat/hello/pull/200\n", "")),
        ])
        self.assertEqual(rc, 0)

        # Run-log entry.
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["path"], "tier-2-pr")
        self.assertEqual(entry["pr_number"], 42)
        self.assertEqual(entry["url"], "https://github.com/octocat/hello/pull/200")
        self.assertTrue(entry["branch"].startswith("prj/auto-fix/42-"))
        self.assertEqual(entry["base_branch"], "alice:feat/foo")
        self.assertEqual(entry["commit_sha"], "abc123def456")

        # State transition.
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "ReadyToMerge")
        self.assertIsNone(state["prs"]["42"]["next_action"])

        # implementation.md written.
        impl = (
            self.root / "_bmad-output" / "pr-workflow" / "prs" / "42" / "implementation.md"
        )
        self.assertTrue(impl.exists())
        content = impl.read_text(encoding="utf-8")
        self.assertIn("path: tier-2-pr", content)
        self.assertIn("url: https://github.com/octocat/hello/pull/200", content)

        # Git calls: checkout -b, apply, commit, rev-parse, push. No --force, no --amend.
        git_argvs = [c["args"] for c in recorder.calls]
        flat = " ".join(" ".join(a) for a in git_argvs)
        self.assertIn("checkout -b prj/auto-fix/42-", flat)
        self.assertIn("apply --index", flat)
        self.assertIn("commit", flat)
        self.assertIn("push --set-upstream origin prj/auto-fix/42-", flat)
        self.assertNotIn("--force", flat)
        self.assertNotIn("--amend", flat)
        self.assertNotIn("--no-verify", flat)

        # gh calls: pr create + label create + pr edit
        gh_flat = " | ".join(" ".join(c["args"]) for c in gh.calls)
        self.assertIn("pr create", gh_flat)
        self.assertIn("label create", gh_flat)
        self.assertIn("pr edit 42", gh_flat)

    def test_fallback_to_comment_on_branch_protected(self):
        rc, recorder, gh = self._invoke(gh_plan=[
            ("pr create", (1, "", "Error: branch is protected and rejects PRs")),
            ("pr comment", (0, "https://github.com/octocat/hello/pull/42#issuecomment-7\n", "")),
        ])
        self.assertEqual(rc, 0)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["path"], "tier-1-comment-fallback")
        self.assertEqual(entry["fallback_reason"], "branch is protected")

        # State still transitioned (we did surface the fix).
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "ReadyToMerge")

        # Comment body had a ```diff fence.
        comment_call = next(
            c for c in gh.calls if "pr comment" in " ".join(c["args"])
        )
        self.assertIn("```diff", comment_call["stdin"])

    def test_refuses_when_adversarial_verdict_not_pass(self):
        (self.root / "_bmad-output" / "pr-workflow" / "prs" / "42" / "adversarial.md").write_text(
            ADVERSARIAL_REJECT, encoding="utf-8"
        )
        rc, recorder, gh = self._invoke(gh_plan=[])
        self.assertEqual(rc, 4)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "not-validated")
        # No git, no gh calls.
        self.assertEqual(recorder.calls, [])
        self.assertEqual(gh.calls, [])
        # State unchanged.
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "FixImpl")

    def test_refuses_when_adversarial_missing(self):
        (self.root / "_bmad-output" / "pr-workflow" / "prs" / "42" / "adversarial.md").unlink()
        rc, _, _ = self._invoke(gh_plan=[])
        self.assertEqual(rc, 4)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "not-validated")

    def test_refuses_when_fix_plan_lacks_diff_fence(self):
        cache = self.root / "_bmad-output" / "pr-workflow" / "prs" / "42"
        bad_plan = "---\nsummary: x\n---\n\n# no fence here\n"
        (cache / "fix-plan.md").write_text(bad_plan, encoding="utf-8")
        rc, _, _ = self._invoke(gh_plan=[])
        self.assertEqual(rc, 5)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "bad-fix-plan")

    def test_refuses_when_claim_source_missing(self):
        cache = self.root / "_bmad-output" / "pr-workflow" / "prs" / "42"
        bad_verify = "---\nverdict: verified\n---\n\nreproduced\n"
        (cache / "verification.md").write_text(bad_verify, encoding="utf-8")
        rc, _, _ = self._invoke(gh_plan=[])
        self.assertEqual(rc, 5)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "bad-fix-plan")

    def test_missing_required_config_returns_misconfigured(self):
        (self.root / "_bmad" / "config.toml").write_text(
            "[modules.prj]\nprj_repo = \"octocat/hello\"\n",
            encoding="utf-8",
        )
        rc, _, _ = self._invoke(gh_plan=[])
        self.assertEqual(rc, 2)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "misconfigured")
        self.assertIn("prj_bot_user", entry["missing_config"])
        self.assertIn("prj_bot_token_ref", entry["missing_config"])

    def test_dry_run_skips_writes_but_logs_dry_run(self):
        rc, recorder, gh = self._invoke(
            gh_plan=[],  # dry-run shouldn't call gh at all
            extra_args=["--dry-run"],
        )
        self.assertEqual(rc, 0)
        entry = self._last_runlog()
        self.assertEqual(entry["status"], "dry-run")
        # No git side-effects.
        self.assertEqual(recorder.calls, [])
        # No gh side-effects (dry-run short-circuits in pr_create.create_fix_pr too).
        self.assertEqual(gh.calls, [])
        # State unchanged (dry-run does not transition).
        state = state_io.load_state(self.root)
        self.assertEqual(state["prs"]["42"]["phase"], "FixImpl")
        # implementation.md still recorded for audit.
        impl = (
            self.root / "_bmad-output" / "pr-workflow" / "prs" / "42" / "implementation.md"
        )
        self.assertTrue(impl.exists())
        self.assertIn("path: dry-run", impl.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
