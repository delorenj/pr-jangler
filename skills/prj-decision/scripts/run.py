#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""prj-decision entry point.

One decision pass over a single PR:
  1. Load config + state.
  2. Verify PR exists in state and is in a decidable phase.
  3. Aggregate per-PR cache artifacts into a Signals bundle.
  4. Apply the deterministic rubric to produce a decision class.
  5. Render the decision comment from assets/template-decision-comment.md.
  6. Apply the `prj/decision:{class}` label, post comment, append decisions.log,
     transition state. All four mutations are atomic-per-PR (we tolerate
     partial GitHub failures but always persist state + log).
  7. Append a structured run-log entry.

Exit codes:
  0  success (including 'already-decided' no-op and 'dry-run')
  2  misconfigured (prj_repo missing) or bad-args (--pr missing)
  3  gh CLI failure
  4  no cache for the requested PR
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sibling import: state_io is owned by prj-orchestrator.
_SIBLING = (
    Path(__file__).resolve().parent.parent.parent / "prj-orchestrator" / "scripts"
)
if _SIBLING.exists() and str(_SIBLING) not in sys.path:
    sys.path.insert(0, str(_SIBLING))

# Add this skill's scripts directory so aggregate / rubric / decision_io resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import state_io  # noqa: E402
from select_next_action import load_prj_config  # noqa: E402

import aggregate  # noqa: E402
import decision_io  # noqa: E402
import rubric  # noqa: E402


SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = SKILL_ROOT / "assets" / "template-decision-comment.md"


def _pr_cache_dir(project_root: Path, pr_number: int) -> Path:
    return project_root / "_bmad-output" / "pr-workflow" / "prs" / str(pr_number)


def _build_summary(decision: str, result: rubric.RubricResult) -> str:
    if decision == "ready-to-merge":
        return "All structural gates passed. This PR is ready for the maintainer to merge."
    if decision == "close-as-not-now":
        return (
            "Triple gate met (no maintainer activity >30d, plus definite-no | "
            "overlap-redundant | >=2 adversarial escalations). Labeling for closure; "
            "the maintainer must close this PR manually if they agree."
        )
    return (
        "Outstanding actionable findings or unresolved verification. "
        "Requesting changes; this PR stays in the queue for the next round."
    )


def _bullets(items: list[str], empty: str = "None.") -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)


def _build_findings_block(result: rubric.RubricResult, signals: aggregate.Signals) -> str:
    parts: list[str] = []
    if signals.blocker_count:
        parts.append(f"Blockers: {signals.blocker_count}")
    if signals.major_count:
        parts.append(f"Majors: {signals.major_count}")
    if signals.minor_count:
        parts.append(f"Minors: {signals.minor_count}")
    if signals.adversarial_verdict and signals.adversarial_verdict != "pass":
        parts.append(f"Adversarial verdict: {signals.adversarial_verdict}")
    if signals.overlap_verdict and signals.overlap_verdict not in (None, "independent"):
        parts.append(f"Overlap verdict: {signals.overlap_verdict}")
    if signals.verification_verdict and signals.verification_verdict != "verified":
        parts.append(f"Verification: {signals.verification_verdict}")
    return _bullets(parts)


def _build_outstanding(result: rubric.RubricResult) -> str:
    items: list[str] = []
    if result.decision == "close-as-not-now":
        items.append("Close this PR manually if you agree with the assessment.")
    elif result.decision == "request-changes":
        items.extend(result.gates_passed)
        items.extend(result.ambiguity_signals)
    return _bullets(items)


def _build_recommendation(result: rubric.RubricResult) -> str:
    base = " ".join(result.reasoning_hints) if result.reasoning_hints else ""
    if not base:
        return "No further recommendation."
    return base


def _build_cache_paths(signals: aggregate.Signals) -> str:
    if not signals.cache_paths:
        return "(no cache artifacts found)"
    return _bullets([f"`prs/{signals.pr_number}/{name}`" for name in signals.cache_paths])


def _comment_context(
    pr_number: int,
    signals: aggregate.Signals,
    result: rubric.RubricResult,
    run_id: str,
) -> dict[str, str]:
    return {
        "decision_class": result.decision,
        "summary": _build_summary(result.decision, result),
        "findings_block": _build_findings_block(result, signals),
        "outstanding_items": _build_outstanding(result),
        "recommendation": _build_recommendation(result),
        "cache_paths": _build_cache_paths(signals),
        "run_id": run_id,
    }


def _emit(entry: dict[str, Any], verbose: bool) -> None:
    print(json.dumps(entry, sort_keys=True))
    if verbose:
        print(
            f"[run {entry.get('run_id')}] {entry.get('status')}: "
            f"pr={entry.get('pr_number')} decision={entry.get('decision')}",
            file=sys.stderr,
        )


def _decide_one(
    project_root: Path,
    repo: str,
    pr_number: int,
    dry_run: bool,
    verbose: bool,
    run_id: str,
) -> tuple[int, dict[str, Any]]:
    started = time.monotonic()
    extras: dict[str, Any] = {
        "run_id": run_id,
        "skill": "prj-decision",
        "repo": repo,
        "pr_number": pr_number,
    }

    state = state_io.load_state(project_root)
    pr_entry = state.get("prs", {}).get(str(pr_number))
    if pr_entry is None:
        extras["status"] = "unknown-pr"
        extras["error"] = f"PR {pr_number} not in state.json"
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        return (4, extras)

    cache_dir = _pr_cache_dir(project_root, pr_number)
    if not cache_dir.exists():
        extras["status"] = "no-cache"
        extras["error"] = f"per-PR cache missing at {cache_dir}"
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        return (4, extras)

    signals = aggregate.aggregate(cache_dir, pr_number, pr_entry)
    result = rubric.evaluate(signals)
    extras["decision"] = result.decision
    extras["confidence"] = result.confidence
    extras["gates_passed"] = result.gates_passed
    extras["gates_failed"] = result.gates_failed
    extras["cache_paths"] = signals.cache_paths

    # Build comment context regardless of dry-run so its rendering is exercised.
    context = _comment_context(pr_number, signals, result, run_id)
    template = TEMPLATE_PATH.read_text(encoding="utf-8") if TEMPLATE_PATH.exists() else ""
    body = decision_io.render_comment(template, context) if template else ""

    if dry_run:
        extras["status"] = "dry-run"
        extras["comment_preview_chars"] = len(body)
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        return (0, extras)

    # Side effects in order: label → comment → log → state.
    try:
        decision_io.apply_label(repo, pr_number, result.decision)
        extras["label_applied"] = f"prj/decision:{result.decision}"
    except decision_io.GhError as exc:
        extras["status"] = "gh-error"
        extras["error"] = f"apply-label: {exc}"
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        return (3, extras)

    try:
        decision_io.post_comment(repo, pr_number, body)
        extras["comment_posted"] = True
    except decision_io.GhError as exc:
        extras["status"] = "gh-error"
        extras["error"] = f"post-comment: {exc}"
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        return (3, extras)

    log_entry = {
        "run_id": run_id,
        "kind": "decision",
        "decision": result.decision,
        "confidence": result.confidence,
        "gates_passed": result.gates_passed,
        "gates_failed": result.gates_failed,
        "cache_paths": signals.cache_paths,
    }
    decision_io.append_decisions_log(cache_dir, log_entry)

    state, transition_status = decision_io.transition_state(
        state, pr_number, result.decision,
    )
    state_io.save_state(project_root, state)
    extras["transition"] = transition_status
    extras["status"] = "ok"
    extras["duration_ms"] = int((time.monotonic() - started) * 1000)
    return (0, extras)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="Override project root", default=None)
    parser.add_argument("--pr", "--pr-number", dest="pr", type=int, help="PR number to decide on (required)")
    parser.add_argument("--dry-run", action="store_true", help="Do not apply label/comment/state")
    parser.add_argument("--verbose", action="store_true", help="Diagnostics to stderr")
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:12]
    project_root = (
        Path(args.project_root) if args.project_root else state_io.find_project_root()
    )
    if args.verbose:
        print(f"[run {run_id}] project_root={project_root}", file=sys.stderr)

    if args.pr is None:
        entry = {
            "run_id": run_id,
            "action": "abort",
            "skill": "prj-decision",
            "status": "bad-args",
            "reason": "--pr <number> is required",
        }
        state_io.append_runlog(project_root, entry)
        _emit(entry, args.verbose)
        return 2

    config = load_prj_config(project_root)
    repo_value = config.get("prj_repo")
    repo = repo_value.strip() if isinstance(repo_value, str) else ""
    if not repo:
        entry = {
            "run_id": run_id,
            "action": "abort",
            "skill": "prj-decision",
            "status": "misconfigured",
            "pr_number": args.pr,
            "reason": "prj_repo not set in [modules.prj] of _bmad/config.toml",
        }
        state_io.append_runlog(project_root, entry)
        _emit(entry, args.verbose)
        return 2

    # Ensure state exists; init from repo if missing so the next checks behave.
    if not state_io.state_path(project_root).exists():
        state_io.init_state(project_root, repo)

    exit_code, extras = _decide_one(
        project_root, repo, args.pr, args.dry_run, args.verbose, run_id,
    )

    log_entry: dict[str, Any] = {"action": "decision-pass"}
    log_entry.update(extras)
    state_io.append_runlog(project_root, log_entry)
    _emit(log_entry, args.verbose)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
