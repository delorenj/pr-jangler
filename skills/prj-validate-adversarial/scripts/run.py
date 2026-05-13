#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""prj-validate-adversarial entry point.

One validation pass for a single PR's fix-plan:

  1. Load config + state. Validate PR is in AdversarialCheck phase.
  2. Gather context: fix-plan.md, verification.md, review.md (read-through).
  3. Run the full regression suite via regression_run.py. If regressions > 0,
     short-circuit to verdict=reject with item=regressions, passes=false.
  4. Otherwise gather a context bundle for the LLM and either:
       a. Print the bundle and the adversarial prompt to stdout for an LLM
          step to consume (default).
       b. Accept a finalized adversarial response via --finalize-response
          (JSON path or `-` for stdin), validate via checklist_io, persist
          via adversarial_io, and write state + run-log.

Two-phase flow: the script is idempotent across the two phases because the
state isn't mutated until --finalize-response. The hard regression gate is the
exception: a regression auto-rejects in phase 1 with a single invocation.

Exit codes:
  0  success (pass, reject, escalate, gather-only)
  2  misconfigured (prj_repo missing or PR not in AdversarialCheck)
  3  PR cache missing (fix-plan / verification / review absent)
  4  invalid adversarial response from LLM
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Sibling imports: state_io lives in prj-orchestrator/scripts.
_SIBLING = Path(__file__).resolve().parent.parent.parent / "prj-orchestrator" / "scripts"
if _SIBLING.exists() and str(_SIBLING) not in sys.path:
    sys.path.insert(0, str(_SIBLING))

import state_io  # noqa: E402
from select_next_action import load_prj_config  # noqa: E402

# Local imports from this skill's scripts/.
_SELF = Path(__file__).resolve().parent
if str(_SELF) not in sys.path:
    sys.path.insert(0, str(_SELF))

import adversarial_io  # noqa: E402
import checklist_io  # noqa: E402
import regression_run  # noqa: E402


REQUIRED_PHASE = "AdversarialCheck"


def _pr_cache_dir(project_root: Path, pr_number: int) -> Path:
    return project_root / "_bmad-output" / "pr-workflow" / "prs" / str(pr_number)


def _read_optional(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _gather_inputs(project_root: Path, pr_number: int) -> dict[str, Any]:
    """Read fix-plan, verification, review. Return a dict with their text."""
    cache = _pr_cache_dir(project_root, pr_number)
    fix_plan = cache / "fix-plan.md"
    verification = cache / "verification.md"
    review = cache / "review.md"
    missing = [p.name for p in (fix_plan, verification) if not p.exists()]
    return {
        "cache_dir": str(cache),
        "fix_plan": _read_optional(fix_plan),
        "verification": _read_optional(verification),
        "review": _read_optional(review),
        "missing": missing,
    }


def _regression_auto_reject_response(regression: dict[str, Any]) -> dict[str, Any]:
    """Build a canonical reject response triggered by the structural gate."""
    findings = []
    auto_finding = (
        f"Regression-suite hard gate: command `{regression.get('command', '')}` "
        f"exited with code {regression.get('exit_code', '?')} and reports "
        f"{regression.get('regressions', 0)} regression(s) on the fix branch."
    )
    placeholder = (
        "Skipped: regression gate short-circuited the validator. "
        "Re-evaluate this item after the regression is resolved."
    )
    for item in checklist_io.REQUIRED_ITEMS:
        if item == "regressions":
            findings.append({"item": item, "passes": False, "finding": auto_finding})
        else:
            findings.append({"item": item, "passes": False, "finding": placeholder})
    return {
        "verdict": "reject",
        "summary": "Auto-reject: regression-suite hard gate triggered.",
        "findings": findings,
        "concerns": [
            f"Regression count = {regression.get('regressions', 0)} on fix branch. Resolve before re-validating.",
        ],
    }


def _finalize(
    project_root: Path,
    pr_number: int,
    response: dict[str, Any],
    regression: dict[str, Any],
    run_id: str,
    verbose: bool,
) -> tuple[int, dict[str, Any]]:
    """Validate response, persist state + report, return (exit_code, extras)."""
    try:
        normalized = checklist_io.validate_response(response)
    except checklist_io.ValidationError as exc:
        extras = {
            "run_id": run_id,
            "skill": "prj-validate-adversarial",
            "pr_number": pr_number,
            "status": "invalid-response",
            "reasons": exc.reasons,
        }
        return (4, extras)

    state = state_io.load_state(project_root)
    if str(pr_number) not in state.get("prs", {}):
        extras = {
            "run_id": run_id,
            "skill": "prj-validate-adversarial",
            "pr_number": pr_number,
            "status": "pr-not-in-state",
        }
        return (2, extras)

    transition = adversarial_io.apply_transition(state, pr_number, normalized["verdict"])
    effective = transition["effective_verdict"]

    content = adversarial_io.render_adversarial_md(
        pr_number=pr_number,
        verdict=normalized["verdict"],
        summary=normalized["summary"],
        findings=normalized["findings"],
        concerns=normalized["concerns"],
        regression=regression,
        effective_verdict=effective,
        reject_count=transition["reject_count"],
    )
    report_path = adversarial_io.write_adversarial_report(project_root, pr_number, content)
    state_io.save_state(project_root, state)

    if verbose:
        print(
            f"[run {run_id}] verdict={normalized['verdict']} effective={effective} "
            f"next_phase={transition['next_phase']}",
            file=sys.stderr,
        )

    extras = {
        "run_id": run_id,
        "skill": "prj-validate-adversarial",
        "pr_number": pr_number,
        "status": "ok",
        "verdict": normalized["verdict"],
        "effective_verdict": effective,
        "next_phase": transition["next_phase"],
        "reject_count": transition["reject_count"],
        "regression_status": regression.get("status"),
        "regressions": regression.get("regressions", 0),
        "report_path": str(report_path),
    }
    return (0, extras)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", "--pr-number", dest="pr", type=int, required=True, help="PR number to validate")
    parser.add_argument("--project-root", help="Override project root", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip regression run + state writes; emit the would-be context bundle",
    )
    parser.add_argument(
        "--finalize-response",
        help=(
            "Path to a JSON file with the adversarial response, or `-` for stdin. "
            "When provided, the script runs the regression gate, validates the response, "
            "and persists state. When omitted, the script prints the gather bundle and exits."
        ),
        default=None,
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose stderr diagnostics")
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    project_root = (
        Path(args.project_root).resolve()
        if args.project_root
        else state_io.find_project_root()
    )
    if args.verbose:
        print(f"[run {run_id}] project_root={project_root}", file=sys.stderr)

    config = load_prj_config(project_root)
    repo_value = config.get("prj_repo")
    repo = repo_value.strip() if isinstance(repo_value, str) else ""
    if not repo:
        entry = {
            "run_id": run_id,
            "skill": "prj-validate-adversarial",
            "action": "abort",
            "status": "misconfigured",
            "reason": "prj_repo not set in [modules.prj] of _bmad/config.toml",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 2

    if not state_io.state_path(project_root).exists():
        state_io.init_state(project_root, repo)

    # Verify PR is in the expected phase before doing real work.
    state = state_io.load_state(project_root)
    pr_entry = state.get("prs", {}).get(str(args.pr))
    if pr_entry is None:
        entry = {
            "run_id": run_id,
            "skill": "prj-validate-adversarial",
            "pr_number": args.pr,
            "action": "abort",
            "status": "pr-not-in-state",
            "reason": f"PR {args.pr} not present in state.json",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 2

    if pr_entry.get("phase") != REQUIRED_PHASE:
        entry = {
            "run_id": run_id,
            "skill": "prj-validate-adversarial",
            "pr_number": args.pr,
            "action": "abort",
            "status": "wrong-phase",
            "reason": f"PR {args.pr} phase={pr_entry.get('phase')!r}, expected {REQUIRED_PHASE!r}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 2

    inputs = _gather_inputs(project_root, args.pr)
    if inputs["missing"]:
        entry = {
            "run_id": run_id,
            "skill": "prj-validate-adversarial",
            "pr_number": args.pr,
            "action": "abort",
            "status": "cache-missing",
            "reason": f"missing per-PR cache files: {inputs['missing']}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 3

    # Dry-run: report what would happen and exit cleanly.
    if args.dry_run:
        entry = {
            "run_id": run_id,
            "skill": "prj-validate-adversarial",
            "pr_number": args.pr,
            "action": "validate-adversarial",
            "status": "dry-run",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        state_io.append_runlog(project_root, entry)
        print(json.dumps({"status": "dry-run", "inputs": {k: bool(v) if isinstance(v, str) else v for k, v in inputs.items()}}, sort_keys=True))
        return 0

    # Run the regression gate.
    regression = regression_run.run_regression(project_root).to_dict()
    if args.verbose:
        print(
            f"[run {run_id}] regression status={regression['status']} "
            f"regressions={regression['regressions']}",
            file=sys.stderr,
        )

    # Hard structural gate: any regression auto-rejects, LLM is not consulted.
    if regression.get("regressions", 0) > 0:
        response = _regression_auto_reject_response(regression)
        exit_code, extras = _finalize(
            project_root, args.pr, response, regression, run_id, args.verbose
        )
        extras["gate"] = "regression-auto-reject"
        extras["action"] = "validate-adversarial"
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        state_io.append_runlog(project_root, extras)
        print(json.dumps(extras, sort_keys=True))
        return exit_code

    # If a finalized response is supplied, validate + persist now.
    if args.finalize_response:
        if args.finalize_response == "-":
            payload = sys.stdin.read()
        else:
            payload = Path(args.finalize_response).read_text(encoding="utf-8")
        try:
            response = json.loads(payload)
        except json.JSONDecodeError as exc:
            entry = {
                "run_id": run_id,
                "skill": "prj-validate-adversarial",
                "pr_number": args.pr,
                "action": "validate-adversarial",
                "status": "invalid-response",
                "reasons": [f"response is not valid JSON: {exc.msg}"],
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            state_io.append_runlog(project_root, entry)
            print(json.dumps(entry, sort_keys=True), file=sys.stderr)
            return 4

        exit_code, extras = _finalize(
            project_root, args.pr, response, regression, run_id, args.verbose
        )
        extras["action"] = "validate-adversarial"
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        state_io.append_runlog(project_root, extras)
        print(json.dumps(extras, sort_keys=True))
        return exit_code

    # Gather-only mode: emit the context bundle for an LLM to consume.
    bundle = {
        "run_id": run_id,
        "skill": "prj-validate-adversarial",
        "pr_number": args.pr,
        "phase": pr_entry.get("phase"),
        "prior_reject_count": int(pr_entry.get("adversarial_reject_count") or 0),
        "prompt_asset": "assets/prompt-adversarial.md",
        "regression": regression,
        "inputs": inputs,
    }
    entry = {
        "run_id": run_id,
        "skill": "prj-validate-adversarial",
        "pr_number": args.pr,
        "action": "validate-adversarial",
        "status": "gathered",
        "regressions": regression.get("regressions", 0),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    state_io.append_runlog(project_root, entry)
    print(json.dumps(bundle, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
