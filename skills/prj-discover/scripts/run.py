#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""prj-discover entry point.

One discovery sweep:
  1. Load config + state.
  2. Check `gh api rate_limit`. If < min_required remaining, defer + log + exit 0.
  3. List open PRs via gh; fetch per-PR comment payload.
  4. Reconcile against state (new, archived, new-comments, idle).
  5. Capture per-PR cache (meta.json with files-changed) for newly-discovered PRs.
  6. Atomically persist state via state_io.save_state.
  7. Append a structured run-log entry.

Exit codes:
  0  success (including 'deferred' due to rate-limit)
  2  misconfigured (prj_repo missing)
  3  gh CLI failure (transport error reaching GitHub)
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Sibling import of state_io. The reconcile module performs the same path
# manipulation, so importing reconcile first ensures state_io is on sys.path.
import reconcile
from gh_client import GhClient, GhClientError

# state_io comes via reconcile's sys.path patch; import after reconcile.
import state_io  # noqa: E402

# select_next_action lives in prj-orchestrator/scripts; we use it solely for
# load_prj_config so the config loading contract stays single-sourced.
from select_next_action import load_prj_config  # noqa: E402


def _pr_cache_dir(project_root: Path, pr_number: int) -> Path:
    return project_root / "_bmad-output" / "pr-workflow" / "prs" / str(pr_number)


def _write_pr_meta(
    project_root: Path,
    pr_number: int,
    pr_summary: dict[str, Any],
    files_changed: int,
) -> Path:
    """Create the per-PR cache directory and write meta.json for a new PR."""
    cache = _pr_cache_dir(project_root, pr_number)
    cache.mkdir(parents=True, exist_ok=True)
    meta_path = cache / "meta.json"
    meta = {
        "pr_number": pr_number,
        "contributor_login": _author_login(pr_summary),
        "title": pr_summary.get("title", ""),
        "head_ref": pr_summary.get("headRefName", ""),
        "base_ref": pr_summary.get("baseRefName", ""),
        "files_changed": files_changed,
        "first_seen_at": pr_summary.get("createdAt", ""),
    }
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return meta_path


def _author_login(pr: dict[str, Any]) -> str:
    author = pr.get("author")
    if isinstance(author, dict):
        return str(author.get("login") or "unknown")
    if isinstance(author, str):
        return author
    return "unknown"


def _build_action_summary(report: reconcile.ReconcileReport) -> str:
    parts: list[str] = []
    if report.new_prs:
        parts.append(f"new={len(report.new_prs)}")
    if report.archived_prs:
        parts.append(f"archived={len(report.archived_prs)}")
    if report.prs_with_new_comments:
        parts.append(f"new-comments={len(report.prs_with_new_comments)}")
    if report.retriaged_for_idle:
        parts.append(f"idle-flagged={len(report.retriaged_for_idle)}")
    return ", ".join(parts) if parts else "no changes"


def _run_sweep(
    project_root: Path,
    repo: str,
    client: GhClient,
    dry_run: bool,
    verbose: bool,
    run_id: str,
    maintainer_logins: list[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Core sweep. Returns (exit_code, runlog_entry_extras)."""
    started = time.monotonic()
    extras: dict[str, Any] = {"run_id": run_id, "skill": "prj-discover", "repo": repo}

    # Rate-limit gate
    try:
        rate = client.rate_limit()
    except GhClientError as exc:
        extras["status"] = "gh-error"
        extras["error"] = str(exc)
        return (3, extras)

    extras["rate_remaining"] = rate.remaining
    extras["rate_limit"] = rate.limit
    if rate.deferred:
        extras["status"] = "deferred-rate-limit"
        extras["reason"] = (
            f"gh rate-limit remaining={rate.remaining} < min_required=100; deferring sweep"
        )
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        return (0, extras)

    # Pull GitHub state
    try:
        open_prs = client.list_open()
        details: dict[int, dict[str, Any]] = {}
        for pr in open_prs:
            number = int(pr["number"])
            details[number] = client.view(number)
    except GhClientError as exc:
        extras["status"] = "gh-error"
        extras["error"] = str(exc)
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        return (3, extras)

    # Load state, compute diff
    state = state_io.load_state(project_root)
    if dry_run:
        working = copy.deepcopy(state)
    else:
        working = state
    report = reconcile.reconcile(working, open_prs, details, maintainer_logins=maintainer_logins)

    # Per-PR cache for new PRs (skipped in dry-run)
    if not dry_run:
        for pr in open_prs:
            number = int(pr["number"])
            if number in report.new_prs:
                stats = client.diff_stats(number)
                _write_pr_meta(project_root, number, pr, stats.get("files_changed", 0))

    extras["status"] = "dry-run" if dry_run else "ok"
    extras["report"] = report.to_dict()
    extras["summary"] = _build_action_summary(report)

    if not dry_run:
        state_io.save_state(project_root, working)
    extras["duration_ms"] = int((time.monotonic() - started) * 1000)
    if verbose:
        print(
            f"[run {run_id}] {extras['status']}: {extras['summary']}",
            file=sys.stderr,
        )
    return (0, extras)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="Override project root", default=None)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute reconciliation but do not write state or cache directories",
    )
    parser.add_argument("--verbose", action="store_true", help="Emit progress diagnostics to stderr")
    args = parser.parse_args()

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
            "skill": "prj-discover",
            "status": "misconfigured",
            "reason": "prj_repo not set in [modules.prj] of _bmad/config.toml",
        }
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 2

    # Ensure state exists so reconciliation has something to read.
    if not state_io.state_path(project_root).exists():
        state_io.init_state(project_root, repo)

    client = GhClient(repo)
    maintainer_logins_raw = config.get("prj_maintainer_logins") or []
    maintainer_logins = [str(m) for m in maintainer_logins_raw] if isinstance(maintainer_logins_raw, list) else []
    exit_code, extras = _run_sweep(
        project_root, repo, client, args.dry_run, args.verbose, run_id,
        maintainer_logins=maintainer_logins,
    )

    log_entry: dict[str, Any] = {"action": "discover-sweep"}
    log_entry.update(extras)
    state_io.append_runlog(project_root, log_entry)
    print(json.dumps(log_entry, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
