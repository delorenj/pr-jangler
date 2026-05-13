#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""prj-detect-overlap entry point.

Two modes:

  --scan
    Step 1: fetch the target PR's changed-files list, fetch the same for every
    other non-terminal open PR in state.json, compute file-overlap pairs at the
    2-shared-files threshold. Emit JSON on stdout. No state writes.

  --verdicts-json '{"<other_pr>": "<verdict>"}'
    Step 2: take per-pair verdicts from the LLM step, apply labels via gh,
    write prs/{n}/overlap.md, transition state.json, append a runlog entry.

Both modes share config loading, project-root resolution, and runlog plumbing.

Exit codes:
  0  success (including 'dry-run' and 'no-pairs')
  2  misconfigured (prj_repo missing) or verdicts arg malformed
  3  gh CLI failure (transport error reaching GitHub)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Sibling import: verdict_io patches sys.path with state_io's location, so we
# import it first and then import state_io.
import overlap_scan
import verdict_io

import state_io  # noqa: E402

# select_next_action.load_prj_config lives in prj-orchestrator/scripts; reuse
# the single config-loading contract instead of duplicating it.
from select_next_action import load_prj_config  # noqa: E402


def _parse_verdicts_arg(raw: str) -> dict[int, str]:
    """Parse the --verdicts-json arg into {other_pr: verdict}. Validates verdicts."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise verdict_io.VerdictIoError(f"--verdicts-json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise verdict_io.VerdictIoError("--verdicts-json must be an object {pr: verdict}")
    out: dict[int, str] = {}
    for key, value in data.items():
        try:
            pr_num = int(key)
        except (TypeError, ValueError) as exc:
            raise verdict_io.VerdictIoError(
                f"--verdicts-json key must be a PR number, got {key!r}"
            ) from exc
        if not isinstance(value, str) or value not in verdict_io.VERDICTS:
            raise verdict_io.VerdictIoError(
                f"--verdicts-json value for PR {pr_num} must be one of "
                f"{', '.join(verdict_io.VERDICTS)}, got {value!r}"
            )
        out[pr_num] = value
    return out


def _parse_rationales_arg(raw: str | None) -> dict[int, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise verdict_io.VerdictIoError(
            f"--rationale-json is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise verdict_io.VerdictIoError("--rationale-json must be an object {pr: rationale}")
    out: dict[int, str] = {}
    for key, value in data.items():
        try:
            pr_num = int(key)
        except (TypeError, ValueError):
            continue
        out[pr_num] = str(value or "")
    return out


def _do_scan(
    project_root: Path,
    repo: str,
    target_pr: int,
    state: dict[str, Any],
    run_id: str,
    verbose: bool,
) -> tuple[int, dict[str, Any]]:
    """Step 1: scan only. Emit pairs report as runlog extras."""
    started = time.monotonic()
    extras: dict[str, Any] = {
        "run_id": run_id, "skill": "prj-detect-overlap",
        "mode": "scan", "pr_number": target_pr, "repo": repo,
    }
    others = overlap_scan.collect_other_pr_numbers(state, target_pr)
    extras["others_checked"] = others
    try:
        report = overlap_scan.scan(repo, target_pr, others)
    except overlap_scan.OverlapScanError as exc:
        extras["status"] = "gh-error"
        extras["error"] = str(exc)
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        return (3, extras)
    extras["status"] = "scan-ok"
    extras["report"] = report.to_dict()
    extras["duration_ms"] = int((time.monotonic() - started) * 1000)
    if verbose:
        print(
            f"[run {run_id}] scan: {len(report.pairs)} pair(s) above threshold "
            f"({len(report.skipped)} skipped)",
            file=sys.stderr,
        )
    return (0, extras)


def _pair_verdicts_from_scan(
    scan_report: overlap_scan.ScanReport,
    verdicts: dict[int, str],
    rationales: dict[int, str],
) -> list[verdict_io.PairVerdict]:
    """Map scan pairs into PairVerdict using the supplied verdicts. Pairs with
    no verdict are dropped (treated as not-yet-classified)."""
    out: list[verdict_io.PairVerdict] = []
    for pair in scan_report.pairs:
        verdict = verdicts.get(pair.other_pr)
        if verdict is None:
            continue
        out.append(
            verdict_io.PairVerdict(
                target_pr=pair.target_pr,
                other_pr=pair.other_pr,
                shared_files=pair.shared_files,
                overlap_count=pair.overlap_count,
                verdict=verdict,
                rationale=rationales.get(pair.other_pr, ""),
            )
        )
    return out


def _skipped_from_scan(scan_report: overlap_scan.ScanReport) -> list[verdict_io.PairVerdict]:
    """Carry sub-threshold pairs into the report as informational rows. Their
    'verdict' field is unused for state transitions but tagged 'independent'
    so the schema is consistent."""
    out: list[verdict_io.PairVerdict] = []
    for pair in scan_report.skipped:
        out.append(
            verdict_io.PairVerdict(
                target_pr=pair.target_pr,
                other_pr=pair.other_pr,
                shared_files=pair.shared_files,
                overlap_count=pair.overlap_count,
                verdict="independent",
            )
        )
    return out


def _do_persist(
    project_root: Path,
    repo: str,
    target_pr: int,
    state: dict[str, Any],
    verdicts: dict[int, str],
    rationales: dict[int, str],
    dry_run: bool,
    skip_label: bool,
    run_id: str,
    verbose: bool,
) -> tuple[int, dict[str, Any]]:
    """Step 2: persist verdicts. Runs the scan internally so the script is
    self-contained even when invoked without a prior --scan run."""
    started = time.monotonic()
    extras: dict[str, Any] = {
        "run_id": run_id, "skill": "prj-detect-overlap",
        "mode": "persist", "pr_number": target_pr, "repo": repo,
    }
    others = overlap_scan.collect_other_pr_numbers(state, target_pr)
    extras["others_checked"] = others
    try:
        scan_report = overlap_scan.scan(repo, target_pr, others)
    except overlap_scan.OverlapScanError as exc:
        extras["status"] = "gh-error"
        extras["error"] = str(exc)
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        return (3, extras)

    pair_verdicts = _pair_verdicts_from_scan(scan_report, verdicts, rationales)
    skipped = _skipped_from_scan(scan_report)
    extras["pair_count"] = len(pair_verdicts)
    extras["scan_report"] = scan_report.to_dict()

    if dry_run:
        extras["status"] = "dry-run"
        # Compute the would-be aggregation for runlog visibility but don't write.
        if pair_verdicts:
            extras["strongest_verdict"] = verdict_io.aggregate_strongest_verdict(
                pair_verdicts
            )
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        return (0, extras)

    try:
        result = verdict_io.persist(
            project_root,
            repo,
            pair_verdicts,
            skipped,
            target_pr,
            list(scan_report.target_files),
            apply_gh_labels=(not skip_label),
        )
    except verdict_io.VerdictIoError as exc:
        extras["status"] = "verdict-error"
        extras["error"] = str(exc)
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        return (3, extras)

    extras["status"] = "ok"
    extras.update(result)
    extras["duration_ms"] = int((time.monotonic() - started) * 1000)
    if verbose:
        print(
            f"[run {run_id}] persist: strongest={extras.get('strongest_verdict')} "
            f"labels={extras.get('labels_planned')}",
            file=sys.stderr,
        )
    return (0, extras)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="Override project root", default=None)
    parser.add_argument("--pr-number", type=int, required=True, help="Target PR number")
    parser.add_argument(
        "--scan", action="store_true",
        help="Step 1: scan for overlap pairs only, emit JSON, no state writes",
    )
    parser.add_argument(
        "--verdicts-json",
        help='JSON object mapping other_pr -> verdict (independent|complementary|conflicting|redundant)',
    )
    parser.add_argument(
        "--rationale-json",
        help="JSON object mapping other_pr -> one-paragraph rationale (optional)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute everything, log intent, no gh, no state writes, no overlap.md",
    )
    parser.add_argument(
        "--skip-label", action="store_true",
        help="Persist state + cache but skip the gh label call",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Emit progress diagnostics to stderr",
    )
    args = parser.parse_args()

    if not args.scan and args.verdicts_json is None:
        parser.error("either --scan or --verdicts-json is required")

    run_id = uuid.uuid4().hex[:12]
    project_root = (
        Path(args.project_root) if args.project_root else state_io.find_project_root()
    )
    if args.verbose:
        print(f"[run {run_id}] project_root={project_root}", file=sys.stderr)

    config = load_prj_config(project_root)
    repo_value = config.get("prj_repo")
    repo = repo_value.strip() if isinstance(repo_value, str) else ""
    if not repo:
        entry = {
            "run_id": run_id,
            "action": "abort",
            "skill": "prj-detect-overlap",
            "pr_number": args.pr_number,
            "status": "misconfigured",
            "reason": "prj_repo not set in [modules.prj] of _bmad/config.toml",
        }
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 2

    if not state_io.state_path(project_root).exists():
        state_io.init_state(project_root, repo)
    state = state_io.load_state(project_root)

    if args.scan:
        exit_code, extras = _do_scan(
            project_root, repo, args.pr_number, state, run_id, args.verbose,
        )
    else:
        try:
            verdicts = _parse_verdicts_arg(args.verdicts_json or "")
            rationales = _parse_rationales_arg(args.rationale_json)
        except verdict_io.VerdictIoError as exc:
            entry = {
                "run_id": run_id,
                "action": "abort",
                "skill": "prj-detect-overlap",
                "pr_number": args.pr_number,
                "status": "bad-args",
                "error": str(exc),
            }
            state_io.append_runlog(project_root, entry)
            print(json.dumps(entry, sort_keys=True), file=sys.stderr)
            return 2
        exit_code, extras = _do_persist(
            project_root, repo, args.pr_number, state,
            verdicts, rationales,
            args.dry_run, args.skip_label,
            run_id, args.verbose,
        )

    log_entry: dict[str, Any] = {"action": "detect-overlap"}
    log_entry.update(extras)
    state_io.append_runlog(project_root, log_entry)
    print(json.dumps(log_entry, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
