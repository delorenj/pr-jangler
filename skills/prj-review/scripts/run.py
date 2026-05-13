#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""prj-review entry point.

Two modes:

  --fetch-only
      Phase 1: print the PR review context (pr, diff, files, AC, conventions)
      as JSON on stdout. The LLM consumes this and produces findings.

  --persist
      Phase 4: read findings JSON from stdin (or --findings-file), validate
      against the strict schema, write review.md, transition state to
      Reviewed, optionally post a Comment-type review on GitHub (gated by
      prj_post_review_comment), and append a runlog entry.

Exit codes:
  0  success
  2  misconfigured (prj_repo missing, etc.)
  3  gh CLI failure during fetch
  4  findings schema validation failure during persist
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Sibling imports: state_io + select_next_action both live in prj-orchestrator/scripts.
# Touch review_io first because it performs the same sys.path patch.
import review_io
from github_fetch import GhFetcher, GhFetchError
from github_post import GhPostError, post_review_comment
from hindsight_lookup import lookup_conventions

# state_io is now on sys.path via review_io's patch.
import state_io  # noqa: E402
from select_next_action import load_prj_config  # noqa: E402


def _load_repo_and_post_flag(project_root: Path) -> tuple[str, bool]:
    config = load_prj_config(project_root)
    repo_value = config.get("prj_repo")
    repo = repo_value.strip() if isinstance(repo_value, str) else ""
    post_flag_raw = config.get("prj_post_review_comment", False)
    if isinstance(post_flag_raw, str):
        post_flag = post_flag_raw.strip().lower() in {"1", "true", "yes", "on"}
    else:
        post_flag = bool(post_flag_raw)
    return repo, post_flag


def _read_findings_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.findings_file:
        raw = Path(args.findings_file).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("no findings payload provided (stdin empty and no --findings-file)")
    return json.loads(raw)


def _fetch_only(
    project_root: Path,
    pr_number: int,
    repo: str,
    run_id: str,
    fetcher_factory: Any | None = None,
) -> tuple[int, dict[str, Any]]:
    started = time.monotonic()
    extras: dict[str, Any] = {
        "run_id": run_id,
        "skill": "prj-review",
        "phase": "fetch",
        "pr_number": pr_number,
        "repo": repo,
    }
    factory = fetcher_factory or GhFetcher
    try:
        fetcher = factory(repo)
        context = fetcher.fetch_all(pr_number)
    except GhFetchError as exc:
        extras["status"] = "gh-error"
        extras["error"] = str(exc)
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        return 3, extras

    # Best-effort hindsight lookup keyed off the PR title and changed files.
    title = context.pr.get("title", "")
    file_paths = [f.get("path", "") for f in context.files]
    query = (
        f"code review conventions for {title}; files: {', '.join(file_paths[:5])}"
    )
    hindsight_result = lookup_conventions(query)

    payload = context.to_dict()
    payload["conventions"] = hindsight_result.excerpts
    payload["conventions_meta"] = {
        "available": hindsight_result.available,
        "bank": hindsight_result.bank,
        "warning": hindsight_result.warning,
    }

    extras["status"] = "ok"
    extras["files_count"] = len(context.files)
    extras["conventions_available"] = hindsight_result.available
    extras["duration_ms"] = int((time.monotonic() - started) * 1000)
    return 0, {"runlog": extras, "payload": payload}


def _persist(
    project_root: Path,
    pr_number: int,
    repo: str,
    post_flag: bool,
    findings_payload: dict[str, Any],
    run_id: str,
    poster: Any = None,
) -> tuple[int, dict[str, Any]]:
    # Resolve the poster at call time so patch.object(run_module, "post_review_comment", ...)
    # works for tests. Looking up the module attribute (not a default-arg-bound
    # reference) is what lets the test seam swap.
    if poster is None:
        # Look up via the current module globals so test patches via
        # `patch.object(run_module, "post_review_comment", ...)` win.
        poster = sys.modules[__name__].post_review_comment
    started = time.monotonic()
    extras: dict[str, Any] = {
        "run_id": run_id,
        "skill": "prj-review",
        "phase": "persist",
        "pr_number": pr_number,
        "repo": repo,
        "post_enabled": post_flag,
    }

    findings = findings_payload.get("findings", [])
    summary = findings_payload.get("summary", "")
    pr_title = findings_payload.get("pr_title", "")
    try:
        path = review_io.write_review(
            project_root=project_root,
            pr_number=pr_number,
            findings=findings,
            summary=summary,
            pr_title=pr_title,
        )
    except review_io.ReviewIoError as exc:
        extras["status"] = "invalid-findings"
        extras["error"] = str(exc)
        extras["duration_ms"] = int((time.monotonic() - started) * 1000)
        return 4, extras

    extras["review_path"] = str(path)
    extras["findings_count"] = len(findings)

    try:
        pr_entry = review_io.transition_to_reviewed(project_root, pr_number)
        extras["new_phase"] = pr_entry["phase"]
    except review_io.ReviewIoError as exc:
        # Cache was written; just log the transition issue without losing the review.
        extras["transition_warning"] = str(exc)

    try:
        post_result = poster(
            repo=repo,
            pr_number=pr_number,
            body_file=path,
            enabled=post_flag,
        )
        extras["post"] = post_result
    except GhPostError as exc:
        extras["post"] = {"status": "error", "error": str(exc)}

    extras["status"] = "ok"
    extras["duration_ms"] = int((time.monotonic() - started) * 1000)
    return 0, extras


def _abort_misconfigured(project_root: Path, run_id: str, reason: str) -> int:
    entry = {
        "run_id": run_id,
        "action": "review",
        "skill": "prj-review",
        "status": "misconfigured",
        "reason": reason,
    }
    state_io.append_runlog(project_root, entry)
    print(json.dumps(entry, sort_keys=True), file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="Override project root", default=None)
    parser.add_argument("--pr", "--pr-number", dest="pr", required=True, type=int, help="PR number to review")
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Phase 1: emit JSON review context to stdout, do not persist anything",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Phase 4: read findings JSON from stdin (or --findings-file) and persist",
    )
    parser.add_argument(
        "--findings-file",
        help="Path to JSON with {findings, summary, pr_title} (alternative to stdin)",
        default=None,
    )
    args = parser.parse_args()

    if args.fetch_only == args.persist:
        print(
            json.dumps({
                "status": "error",
                "error": "exactly one of --fetch-only or --persist is required",
            }),
            file=sys.stderr,
        )
        return 2

    run_id = uuid.uuid4().hex[:12]
    project_root = (
        Path(args.project_root) if args.project_root else state_io.find_project_root()
    )

    repo, post_flag = _load_repo_and_post_flag(project_root)
    if not repo:
        return _abort_misconfigured(
            project_root, run_id,
            "prj_repo not set in [modules.prj] of _bmad/config.toml",
        )

    # Ensure state exists so persist can update it (fetch_only does not need it,
    # but having state initialized is harmless and matches the prj-discover pattern).
    if not state_io.state_path(project_root).exists():
        state_io.init_state(project_root, repo)

    if args.fetch_only:
        exit_code, result = _fetch_only(project_root, args.pr, repo, run_id)
        if exit_code != 0:
            state_io.append_runlog(project_root, result)
            print(json.dumps(result, sort_keys=True), file=sys.stderr)
            return exit_code
        state_io.append_runlog(project_root, result["runlog"])
        print(json.dumps(result["payload"], indent=2, sort_keys=True))
        return 0

    # --persist branch
    try:
        findings_payload = _read_findings_payload(args)
    except (ValueError, json.JSONDecodeError) as exc:
        entry = {
            "run_id": run_id,
            "action": "review",
            "skill": "prj-review",
            "status": "invalid-payload",
            "error": str(exc),
        }
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 4

    exit_code, extras = _persist(
        project_root, args.pr, repo, post_flag, findings_payload, run_id,
    )
    log_entry: dict[str, Any] = {"action": "review-persist"}
    log_entry.update(extras)
    state_io.append_runlog(project_root, log_entry)
    print(json.dumps(log_entry, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
