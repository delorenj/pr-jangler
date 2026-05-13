#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""prj-triage entry point.

One classification per invocation. The classification itself is the agent's
decision (passed in via --classification); this script handles the
deterministic plumbing:

  1. Load config + state, validate inputs.
  2. (PR mode) apply GitHub label via github_ops.apply_triage_label.
  3. Write per-PR cache (triage.md for PR mode, comments-triage.md append for
     comment mode).
  4. Atomically transition the PR's phase + next_action via persistence.py.
  5. Append a structured run-log entry.

Exit codes:
  0  success (including dry-run)
  2  misconfigured (prj_repo missing, PR not in state, invalid classification)
  3  gh CLI failure (when applying labels)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Sibling import of state_io. persistence.py performs the same path
# manipulation, so importing persistence first ensures state_io is on sys.path.
import persistence
from github_ops import GhOpsError, apply_triage_label

# state_io comes via persistence's sys.path patch; import after persistence.
import state_io  # noqa: E402

# select_next_action lives in prj-orchestrator/scripts; we use it solely for
# load_prj_config so the config loading contract stays single-sourced.
from select_next_action import load_prj_config  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="Override project root", default=None)
    parser.add_argument(
        "--mode",
        required=True,
        choices=["pr", "comment"],
        help="Classification mode",
    )
    parser.add_argument(
        "--pr-number",
        required=True,
        type=int,
        help="Target PR number",
    )
    parser.add_argument(
        "--classification",
        required=True,
        help="Chosen category (rubric in SKILL.md)",
    )
    parser.add_argument(
        "--rationale",
        required=True,
        help="One-sentence justification recorded in cache + runlog",
    )
    parser.add_argument(
        "--comment-id",
        default=None,
        help="Comment ID being classified (mode=comment only). If omitted, "
             "the script records 'unknown' and leaves comment selection to "
             "the next comment-mode invocation.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute outcome and log intent; do NOT apply label or write state",
    )
    parser.add_argument(
        "--skip-label",
        action="store_true",
        help="Persist state and cache, but skip the gh label call",
    )
    parser.add_argument("--verbose", action="store_true", help="Diagnostics to stderr")
    return parser


def _validate_classification(mode: str, classification: str) -> None:
    if mode == "pr":
        if classification not in persistence.PR_CLASSES:
            raise ValueError(
                f"invalid PR classification {classification!r}; "
                f"expected one of {list(persistence.PR_CLASSES)}"
            )
    else:
        if classification not in persistence.COMMENT_CLASSES:
            raise ValueError(
                f"invalid comment classification {classification!r}; "
                f"expected one of {list(persistence.COMMENT_CLASSES)}"
            )


def _runlog_entry(**fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"action": "triage"}
    entry.update(fields)
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    run_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    project_root = (
        Path(args.project_root) if args.project_root else state_io.find_project_root()
    )
    if args.verbose:
        print(f"[run {run_id}] project_root={project_root}", file=sys.stderr)

    # Config check
    config = load_prj_config(project_root)
    repo_value = config.get("prj_repo")
    repo = repo_value.strip() if isinstance(repo_value, str) else ""
    if not repo:
        entry = _runlog_entry(
            run_id=run_id,
            skill="prj-triage",
            mode=args.mode,
            pr_number=args.pr_number,
            status="misconfigured",
            reason="prj_repo not set in [modules.prj] of _bmad/config.toml",
        )
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 2

    # Validate classification before touching anything
    try:
        _validate_classification(args.mode, args.classification)
    except ValueError as exc:
        entry = _runlog_entry(
            run_id=run_id,
            skill="prj-triage",
            mode=args.mode,
            pr_number=args.pr_number,
            classification=args.classification,
            status="invalid-classification",
            error=str(exc),
        )
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 2

    # Confirm PR exists in state
    try:
        state = state_io.load_state(project_root)
    except FileNotFoundError:
        entry = _runlog_entry(
            run_id=run_id,
            skill="prj-triage",
            mode=args.mode,
            pr_number=args.pr_number,
            status="missing-state",
            reason="state.json not found; run prj-discover first",
        )
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 2
    if str(args.pr_number) not in state.get("prs", {}):
        entry = _runlog_entry(
            run_id=run_id,
            skill="prj-triage",
            mode=args.mode,
            pr_number=args.pr_number,
            classification=args.classification,
            status="missing-pr",
            reason=f"PR {args.pr_number} not present in state.json",
        )
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 2

    persist = not args.dry_run
    label_result: dict[str, Any] | None = None
    label_skipped_reason: str | None = None

    # PR mode: apply the GitHub label (unless dry-run or skip-label)
    if args.mode == "pr":
        if args.dry_run:
            label_skipped_reason = "dry-run"
        elif args.skip_label:
            label_skipped_reason = "skip-label"
        else:
            try:
                label_result = apply_triage_label(repo, args.pr_number, args.classification)
            except GhOpsError as exc:
                entry = _runlog_entry(
                    run_id=run_id,
                    skill="prj-triage",
                    mode="pr",
                    pr_number=args.pr_number,
                    classification=args.classification,
                    repo=repo,
                    status="gh-error",
                    error=str(exc),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                state_io.append_runlog(project_root, entry)
                print(json.dumps(entry, sort_keys=True), file=sys.stderr)
                return 3

    # Persist via persistence.py
    try:
        if args.mode == "pr":
            persist_result = persistence.apply_pr_classification(
                project_root,
                args.pr_number,
                args.classification,
                args.rationale,
                persist=persist,
            )
        else:
            persist_result = persistence.apply_comment_classification(
                project_root,
                args.pr_number,
                args.classification,
                args.rationale,
                comment_id=args.comment_id,
                persist=persist,
            )
    except persistence.PersistenceError as exc:
        entry = _runlog_entry(
            run_id=run_id,
            skill="prj-triage",
            mode=args.mode,
            pr_number=args.pr_number,
            classification=args.classification,
            status="persist-error",
            error=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 2

    entry = _runlog_entry(
        run_id=run_id,
        skill="prj-triage",
        mode=args.mode,
        pr_number=args.pr_number,
        classification=args.classification,
        rationale=args.rationale,
        repo=repo,
        status="dry-run" if args.dry_run else "ok",
        phase=persist_result["phase"],
        next_action=persist_result["next_action"],
        label_applied=label_result,
        label_skipped_reason=label_skipped_reason,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    if args.mode == "comment":
        entry["comment_id"] = args.comment_id

    state_io.append_runlog(project_root, entry)
    if args.verbose:
        print(
            f"[run {run_id}] {entry['status']}: pr={args.pr_number} "
            f"class={args.classification} -> phase={persist_result['phase']}",
            file=sys.stderr,
        )
    print(json.dumps(entry, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
