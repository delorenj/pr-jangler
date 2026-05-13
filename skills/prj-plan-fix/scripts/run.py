#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""prj-plan-fix entry point.

Orchestrates the deterministic side of fix-planning:

  1. Refuse-or-proceed gate on prs/{n}/verification.md (must be verified)
  2. Reuse-or-provision worktree for the PR
  3. Run the failing-test gate (test must actually fail)
  4. Run fix + test gate (new test passes, full suite passes)
  5. Validate + write fix-plan.md
  6. Transition state FixPlan -> AdversarialCheck (or escalate to PleaseAdvise)
  7. Append a structured run-log entry

LLM-authored steps (failing test design, fix design, rationale, risk) happen
*outside* this script. The harness writes those artifacts to disk, then this
script is invoked to verify and commit them.

Inputs the harness must provide on disk before invocation:
  - {pr_cache}/plan-draft.md            full proposed fix-plan markdown
  - {pr_cache}/failing-test.cmd         shell command that runs the new test
  - {pr_cache}/full-suite.cmd           shell command that runs the full suite
  - {pr_cache}/apply-fix.sh   (optional) script that applies the fix to the worktree
  - {pr_cache}/revert-fix.sh  (optional) script that reverts the fix in the worktree

Exit codes:
  0  success (plan written, state transitioned)
  2  not-verified (verification.md missing or verdict not 'verified')
  3  test does not demonstrate bug (new test passed before the fix)
  4  regression detected (full suite failed after fix)
  5  plan invalid (fix-plan markdown failed validation)
  6  escalated to PleaseAdvise (2 consecutive adversarial rejections)
  7  test-not-writable (LLM declared the bug not testable)
  8  worktree provisioning failure
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Sibling import of state_io. plan_io has the path patch; importing it first
# guarantees state_io is reachable.
import plan_io
import worktree as worktree_mod
import test_runner

import state_io  # noqa: E402

# Failure exit codes (kept in sync with module docstring).
EXIT_OK = 0
EXIT_NOT_VERIFIED = 2
EXIT_TEST_NOT_FAILING = 3
EXIT_REGRESSION = 4
EXIT_INVALID_PLAN = 5
EXIT_ESCALATED = 6
EXIT_TEST_NOT_WRITABLE = 7
EXIT_WORKTREE_FAIL = 8


def _pr_cache(project_root: Path, pr_number: int) -> Path:
    return (
        project_root
        / "_bmad-output"
        / "pr-workflow"
        / "prs"
        / str(pr_number)
    )


def _verification_verdict(project_root: Path, pr_number: int) -> str | None:
    """Read verification.md and return the verdict string, or None."""
    path = _pr_cache(project_root, pr_number) / "verification.md"
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    # Look for `verdict: verified` line (case-insensitive value).
    import re
    m = re.search(r"^\s*[-*]?\s*verdict\s*:\s*(\S+)", content, re.MULTILINE | re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip().strip("`").lower()


def _read_command(path: Path) -> str | None:
    """Read a one-line command from a cmd file. Returns None if missing."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def _runlog_extras(run_id: str, pr_number: int) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "skill": "prj-plan-fix",
        "pr_number": pr_number,
    }


def _abort(
    project_root: Path,
    extras: dict[str, Any],
    status: str,
    exit_code: int,
    reason: str | None = None,
    duration_ms: int | None = None,
) -> int:
    extras["status"] = status
    if reason is not None:
        extras["reason"] = reason
    if duration_ms is not None:
        extras["duration_ms"] = duration_ms
    entry = {"action": "plan-fix"}
    entry.update(extras)
    state_io.append_runlog(project_root, entry)
    print(json.dumps(entry, sort_keys=True))
    return exit_code


def _run_apply(script_path: Path, cwd: Path, _runner=None) -> int:
    """Execute apply-fix.sh / revert-fix.sh in the worktree. Returns exit code.

    A missing script counts as a no-op success (0). This lets tests inject
    inline file mutations instead of having to materialize shell scripts.
    """
    if not script_path.exists():
        return 0
    import subprocess
    run = _runner or subprocess.run
    result = run(
        ["bash", str(script_path)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--project-root", help="Override project root", default=None)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the gates but do not write fix-plan.md or transition state",
    )
    parser.add_argument("--verbose", action="store_true", help="Diagnostics to stderr")
    parser.add_argument(
        "--test-runner-override",
        help="Override the project's test runner (e.g., 'pytest -q')",
        default=None,
    )
    args = parser.parse_args(argv)

    started = time.monotonic()
    run_id = uuid.uuid4().hex[:12]
    project_root = (
        Path(args.project_root).resolve()
        if args.project_root
        else state_io.find_project_root()
    )
    extras = _runlog_extras(run_id, args.pr_number)

    if args.verbose:
        print(f"[run {run_id}] project_root={project_root}", file=sys.stderr)

    # Verification gate
    verdict = _verification_verdict(project_root, args.pr_number)
    if verdict != "verified":
        duration_ms = int((time.monotonic() - started) * 1000)
        return _abort(
            project_root, extras,
            status="not-verified",
            exit_code=EXIT_NOT_VERIFIED,
            reason=f"verification.md verdict is {verdict!r}, expected 'verified'",
            duration_ms=duration_ms,
        )

    # Worktree
    try:
        wt = worktree_mod.ensure_worktree(project_root, args.pr_number)
    except worktree_mod.WorktreeError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return _abort(
            project_root, extras,
            status="worktree-failure",
            exit_code=EXIT_WORKTREE_FAIL,
            reason=str(exc),
            duration_ms=duration_ms,
        )
    extras["worktree_path"] = str(wt.path)
    extras["worktree_reused"] = wt.reused

    cache = _pr_cache(project_root, args.pr_number)
    plan_draft = cache / "plan-draft.md"
    failing_test_cmd = _read_command(cache / "failing-test.cmd")
    full_suite_cmd = _read_command(cache / "full-suite.cmd")
    apply_fix = cache / "apply-fix.sh"
    revert_fix = cache / "revert-fix.sh"

    if not plan_draft.exists():
        duration_ms = int((time.monotonic() - started) * 1000)
        return _abort(
            project_root, extras,
            status="test-not-writable",
            exit_code=EXIT_TEST_NOT_WRITABLE,
            reason=f"plan-draft.md missing at {plan_draft}; LLM did not produce a plan",
            duration_ms=duration_ms,
        )

    if not failing_test_cmd:
        duration_ms = int((time.monotonic() - started) * 1000)
        return _abort(
            project_root, extras,
            status="test-not-writable",
            exit_code=EXIT_TEST_NOT_WRITABLE,
            reason="failing-test.cmd missing or empty; cannot verify failing-test gate",
            duration_ms=duration_ms,
        )

    if not full_suite_cmd:
        full_suite_cmd = test_runner.detect_runner(
            wt.path, config_override=args.test_runner_override
        )

    # Failing-test gate: BEFORE applying fix the new test must FAIL.
    pre_result = test_runner.run_tests(failing_test_cmd, wt.path)
    extras["failing_test_before_fix"] = pre_result.to_dict()
    if pre_result.passed:
        duration_ms = int((time.monotonic() - started) * 1000)
        return _abort(
            project_root, extras,
            status="test-does-not-demonstrate-bug",
            exit_code=EXIT_TEST_NOT_FAILING,
            reason=(
                "New test passed before the fix was applied — it does not "
                "demonstrate the alleged bug. Refusing to advance."
            ),
            duration_ms=duration_ms,
        )

    # Apply the fix
    apply_rc = _run_apply(apply_fix, wt.path)
    if apply_rc != 0:
        duration_ms = int((time.monotonic() - started) * 1000)
        return _abort(
            project_root, extras,
            status="apply-fix-failed",
            exit_code=EXIT_REGRESSION,
            reason=f"apply-fix.sh exited {apply_rc}",
            duration_ms=duration_ms,
        )

    # New test must pass with fix
    post_result = test_runner.run_tests(failing_test_cmd, wt.path)
    extras["failing_test_after_fix"] = post_result.to_dict()
    if post_result.failed:
        _run_apply(revert_fix, wt.path)
        duration_ms = int((time.monotonic() - started) * 1000)
        return _abort(
            project_root, extras,
            status="fix-does-not-pass-test",
            exit_code=EXIT_REGRESSION,
            reason="Test still fails after applying the proposed diff.",
            duration_ms=duration_ms,
        )

    # Full suite must pass (no regression)
    suite_result = test_runner.run_tests(full_suite_cmd, wt.path)
    extras["full_suite_after_fix"] = suite_result.to_dict()
    if suite_result.failed:
        _run_apply(revert_fix, wt.path)
        duration_ms = int((time.monotonic() - started) * 1000)
        return _abort(
            project_root, extras,
            status="regression-detected",
            exit_code=EXIT_REGRESSION,
            reason="Full test suite regressed after applying the proposed diff.",
            duration_ms=duration_ms,
        )

    # Revert the fix in the worktree; implementation skill applies it for real later.
    _run_apply(revert_fix, wt.path)

    # Validate plan content
    plan_content = plan_draft.read_text(encoding="utf-8")
    issues = plan_io.validate_plan(plan_content)
    if issues:
        duration_ms = int((time.monotonic() - started) * 1000)
        extras["validation_issues"] = issues
        return _abort(
            project_root, extras,
            status="invalid-plan",
            exit_code=EXIT_INVALID_PLAN,
            reason="fix-plan markdown failed schema validation",
            duration_ms=duration_ms,
        )

    if args.dry_run:
        duration_ms = int((time.monotonic() - started) * 1000)
        return _abort(
            project_root, extras,
            status="dry-run",
            exit_code=EXIT_OK,
            duration_ms=duration_ms,
        )

    # Escalation check (run BEFORE we commit the new plan, so the rejection
    # count reflects what already happened).
    if plan_io.should_escalate(project_root, args.pr_number):
        plan_io.escalate_please_advise(
            project_root,
            args.pr_number,
            reason="2 consecutive adversarial rejections of fix-plan",
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        return _abort(
            project_root, extras,
            status="escalated-please-advise",
            exit_code=EXIT_ESCALATED,
            reason="rejection_count >= PLAN_MAX_ATTEMPTS",
            duration_ms=duration_ms,
        )

    # Commit plan + transition
    plan_io.write_plan(project_root, args.pr_number, plan_content)
    plan_io.transition_to_adversarial(project_root, args.pr_number)

    duration_ms = int((time.monotonic() - started) * 1000)
    extras["status"] = "ok"
    extras["fix_plan_path"] = str(plan_io.fix_plan_path(project_root, args.pr_number))
    extras["duration_ms"] = duration_ms
    entry = {"action": "plan-fix"}
    entry.update(extras)
    state_io.append_runlog(project_root, entry)
    print(json.dumps(entry, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
