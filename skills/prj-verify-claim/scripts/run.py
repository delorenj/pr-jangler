#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""prj-verify-claim entry point.

One verification pass:
  1. Load config + state.
  2. Locate the latest actionable claim (or accept --claim verbatim).
  3. Render verification.md, apply phase transition, save state.
  4. If verdict == 'not-verified', render and post the pushback comment.
  5. If verdict == 'verified', clean up the worktree.
  6. Append a structured run-log entry.

The judgment (strategy choice, verdict adjudication, observation text) is the
LLM's job and is passed in as CLI flags. This script is the deterministic
plumbing: persistence, comment posting, worktree cleanup, runlog.

Exit codes:
  0  success (any of verified, not-verified, ambiguous, or --dry-run)
  1  unexpected error (state inconsistent, template missing, etc.)
  2  misconfigured (prj_repo missing)
  3  gh subprocess failure when posting the pushback comment
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Sibling imports: state_io lives in prj-orchestrator/scripts. Importing
# verification_io first patches sys.path with state_io's location.
import verification_io
import comment_post
import worktree as worktree_mod

# state_io was placed on sys.path by verification_io's import-time side effect.
import state_io  # noqa: E402

# load_prj_config is single-sourced from prj-orchestrator.
from select_next_action import load_prj_config  # noqa: E402


def _pr_cache_dir(project_root: Path, pr_number: int) -> Path:
    return project_root / "_bmad-output" / "pr-workflow" / "prs" / str(pr_number)


def _comments_triage_path(project_root: Path, pr_number: int) -> Path:
    return _pr_cache_dir(project_root, pr_number) / "comments-triage.md"


def _read_latest_actionable_claim(
    project_root: Path, pr_number: int,
) -> tuple[str, str]:
    """Read the latest actionable comment from comments-triage.md.

    The triage skill appends entries to this file; the format is intentionally
    loose ("a section per classified comment") and we use a simple heuristic:
    the last block that contains the word `actionable` and a quoted claim
    excerpt wins. Returns (commenter, claim_text). Both may be empty strings
    if no actionable entry is found.
    """
    path = _comments_triage_path(project_root, pr_number)
    if not path.exists():
        return ("", "")
    text = path.read_text(encoding="utf-8")
    # Split on top-level headings (`## `) since prj-triage uses one per comment.
    blocks = ["## " + b for b in text.split("\n## ") if b.strip()]
    if blocks and not text.lstrip().startswith("## "):
        # First block had no leading marker; restore raw form.
        blocks[0] = blocks[0][len("## "):]
    chosen: str | None = None
    for block in reversed(blocks):
        if "actionable" in block.lower():
            chosen = block
            break
    if chosen is None:
        return ("", "")
    commenter = ""
    claim_lines: list[str] = []
    for raw in chosen.splitlines():
        line = raw.rstrip()
        low = line.lower()
        # Extract the value after the bolded label. We strip the leading
        # list-marker / bold markers so the colon-split sees `value` cleanly.
        if "**commenter:**" in low:
            value = line.split("**Commenter:**", 1)[-1] if "**Commenter:**" in line else line.split("**commenter:**", 1)[-1]
            commenter = value.strip().lstrip("@")
            continue
        if "**claim:**" in low:
            value = line.split("**Claim:**", 1)[-1] if "**Claim:**" in line else line.split("**claim:**", 1)[-1]
            claim_lines.append(value.strip())
            continue
        if line.startswith(">"):
            claim_lines.append(line.lstrip("> ").rstrip())
    return commenter, "\n".join(c for c in claim_lines if c).strip()


def _post_pushback_if_needed(
    *,
    verdict: str,
    repo: str,
    pr_number: int,
    claim: str,
    commenter: str,
    role: str,
    strategy: str,
    worktree_path: Path,
    runner_command: list[str] | None,
    observation: str,
    skip_comment: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """For verdict='not-verified', render and post the pushback comment.

    Returns a dict for inclusion in the runlog. For other verdicts or for
    skip/dry runs, the dict's `status` reflects why no comment was posted.
    """
    if verdict != "not-verified":
        return {"status": "skipped-not-applicable"}
    if dry_run:
        return {"status": "skipped-dry-run"}
    if skip_comment:
        return {"status": "skipped-flag"}
    body = comment_post.render_pushback(
        claim=claim,
        commenter=commenter,
        strategy=strategy,
        worktree=str(worktree_path),
        command=" ".join(runner_command) if runner_command else "<not run>",
        observation=observation,
        role=role,
    )
    return comment_post.post_pr_comment(repo, pr_number, body)


def _cleanup_if_verified(
    project_root: Path,
    pr_number: int,
    verdict: str,
    dry_run: bool,
) -> dict[str, Any]:
    if verdict != "verified":
        return {"status": "retained", "reason": f"verdict={verdict}"}
    if dry_run:
        return {"status": "skipped-dry-run"}
    return worktree_mod.cleanup_worktree(project_root, pr_number)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="Override project root", default=None)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument(
        "--strategy", required=True,
        choices=list(verification_io.VALID_STRATEGIES),
    )
    parser.add_argument(
        "--verdict", required=True,
        choices=list(verification_io.VALID_VERDICTS),
    )
    parser.add_argument("--observation", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--claim", default=None,
                        help="Claim text; if omitted, read from comments-triage.md")
    parser.add_argument("--commenter", default=None,
                        help="Commenter login; if omitted, read from comments-triage.md")
    parser.add_argument("--commenter-role", default="contributor",
                        choices=["maintainer", "contributor", "first-timer", "bot"])
    parser.add_argument("--worktree", default=None,
                        help="Worktree path; defaults to the conventional location")
    parser.add_argument("--runner-command", default=None,
                        help="Space-separated runner command for the verification record")
    parser.add_argument("--runner-returncode", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-comment", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    started = time.monotonic()
    run_id = uuid.uuid4().hex[:12]
    project_root = (
        Path(args.project_root) if args.project_root else state_io.find_project_root()
    )
    if args.verbose:
        print(f"[run {run_id}] project_root={project_root}", file=sys.stderr)

    base_entry: dict[str, Any] = {
        "action": "verify-claim",
        "skill": "prj-verify-claim",
        "run_id": run_id,
        "pr_number": args.pr_number,
        "verdict": args.verdict,
        "strategy": args.strategy,
    }

    config = load_prj_config(project_root)
    repo_value = config.get("prj_repo")
    repo = repo_value.strip() if isinstance(repo_value, str) else ""
    if not repo:
        entry = dict(base_entry)
        entry["status"] = "misconfigured"
        entry["reason"] = "prj_repo not set in [modules.prj] of _bmad/config.toml"
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 2

    claim_text = args.claim
    commenter = args.commenter
    if not claim_text or not commenter:
        triage_commenter, triage_claim = _read_latest_actionable_claim(
            project_root, args.pr_number,
        )
        if not claim_text:
            claim_text = triage_claim
        if not commenter:
            commenter = triage_commenter or "unknown"

    worktree_path = (
        Path(args.worktree) if args.worktree
        else worktree_mod.worktree_path(project_root, args.pr_number)
    )
    runner_command = args.runner_command.split() if args.runner_command else None

    try:
        extras = verification_io.persist(
            project_root=project_root,
            pr_number=args.pr_number,
            commenter=commenter or "unknown",
            claim=claim_text or "",
            strategy=args.strategy,
            observation=args.observation,
            verdict=args.verdict,
            rationale=args.rationale,
            worktree_path=worktree_path,
            runner_command=runner_command,
            runner_returncode=args.runner_returncode,
            dry_run=args.dry_run,
        )
    except verification_io.VerificationError as exc:
        entry = dict(base_entry)
        entry["status"] = "error"
        entry["error"] = str(exc)
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 1

    # Pushback comment (verdict=not-verified only)
    try:
        comment_result = _post_pushback_if_needed(
            verdict=args.verdict,
            repo=repo,
            pr_number=args.pr_number,
            claim=claim_text or "",
            commenter=commenter or "unknown",
            role=args.commenter_role,
            strategy=args.strategy,
            worktree_path=worktree_path,
            runner_command=runner_command,
            observation=args.observation,
            skip_comment=args.skip_comment,
            dry_run=args.dry_run,
        )
    except comment_post.CommentError as exc:
        entry = dict(base_entry)
        entry.update(extras)
        entry["status"] = "gh-error"
        entry["error"] = str(exc)
        entry["duration_ms"] = int((time.monotonic() - started) * 1000)
        state_io.append_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 3

    cleanup_result = _cleanup_if_verified(
        project_root, args.pr_number, args.verdict, args.dry_run,
    )

    entry = dict(base_entry)
    entry.update(extras)
    entry["status"] = "dry-run" if args.dry_run else "ok"
    entry["comment"] = comment_result
    entry["worktree_cleanup"] = cleanup_result
    entry["duration_ms"] = int((time.monotonic() - started) * 1000)
    state_io.append_runlog(project_root, entry)
    if args.verbose:
        print(
            f"[run {run_id}] verdict={args.verdict} phase={extras.get('phase')}",
            file=sys.stderr,
        )
    print(json.dumps(entry, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
