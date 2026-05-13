#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Per-PR cache writes and phase-machine transitions for prj-triage.

Pure functions where possible: phase transition logic is data-driven via
PR_TRANSITIONS / COMMENT_TRANSITIONS. The two file-writing helpers are the
only side-effect surface in this module aside from the state writes that
flow through `state_io.save_state`.

Importable:
  PR_CLASSES, COMMENT_CLASSES,
  PR_TRANSITIONS, COMMENT_TRANSITIONS,
  apply_pr_classification, apply_comment_classification,
  write_triage_md, append_comments_triage_md, pr_cache_dir.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sibling import: state_io is owned by prj-orchestrator. Add its scripts dir
# to sys.path so we can `import state_io` cleanly without copy-paste. The path
# walks from this file: scripts/ -> prj-triage/ -> skills/ -> prj-orchestrator/scripts.
_SIBLING = Path(__file__).resolve().parent.parent.parent / "prj-orchestrator" / "scripts"
if _SIBLING.exists() and str(_SIBLING) not in sys.path:
    sys.path.insert(0, str(_SIBLING))

import state_io  # noqa: E402

PR_CLASSES = ("actionable", "definite-no", "possible-duplicate", "needs-review")
COMMENT_CLASSES = ("actionable", "advisory", "noise")

# Phase / next-action transitions, sourced from the rubric in SKILL.md.
PR_TRANSITIONS: dict[str, dict[str, Any]] = {
    "actionable": {
        "phase": "ReviewPending",
        "next_action": {"skill": "prj-review", "mode": None},
    },
    "possible-duplicate": {
        "phase": "OverlapCheck",
        "next_action": {"skill": "prj-detect-overlap", "mode": None},
    },
    "definite-no": {
        "phase": "Rejected",
        "next_action": None,
    },
    "needs-review": {
        "phase": "ReviewPending",
        "next_action": {"skill": "prj-review", "mode": None},
    },
}

COMMENT_TRANSITIONS: dict[str, dict[str, Any]] = {
    "actionable": {
        "phase": "ClaimVerify",
        "next_action": {"skill": "prj-verify-claim", "mode": None},
    },
    "advisory": {
        "phase": "Reviewed",
        "next_action": None,
    },
    "noise": {
        "phase": "Reviewed",
        "next_action": None,
    },
}


class PersistenceError(RuntimeError):
    """Raised on persistence-level failures (PR missing, invalid class, etc)."""


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def pr_cache_dir(project_root: Path, pr_number: int) -> Path:
    return project_root / "_bmad-output" / "pr-workflow" / "prs" / str(pr_number)


def _ensure_cache_dir(project_root: Path, pr_number: int) -> Path:
    cache = pr_cache_dir(project_root, pr_number)
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def write_triage_md(
    project_root: Path,
    pr_number: int,
    classification: str,
    rationale: str,
    now: datetime | None = None,
) -> Path:
    """Write the per-PR triage.md (overwrite each run; idempotent)."""
    if classification not in PR_CLASSES:
        raise PersistenceError(
            f"unknown PR classification: {classification!r}; expected one of {list(PR_CLASSES)}"
        )
    cache = _ensure_cache_dir(project_root, pr_number)
    path = cache / "triage.md"
    ts = _now_iso(now)
    body = (
        f"# PR #{pr_number} Triage\n\n"
        f"- Classification: `{classification}`\n"
        f"- Decided at: {ts}\n"
        f"- Rationale: {rationale.strip() or '(no rationale provided)'}\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


def append_comments_triage_md(
    project_root: Path,
    pr_number: int,
    classification: str,
    rationale: str,
    comment_id: str | None,
    now: datetime | None = None,
) -> Path:
    """Append one entry to comments-triage.md (append-only audit trail)."""
    if classification not in COMMENT_CLASSES:
        raise PersistenceError(
            f"unknown comment classification: {classification!r}; "
            f"expected one of {list(COMMENT_CLASSES)}"
        )
    cache = _ensure_cache_dir(project_root, pr_number)
    path = cache / "comments-triage.md"
    header_needed = not path.exists()
    ts = _now_iso(now)
    entry = (
        f"\n## {ts} — `{classification}`\n\n"
        f"- Comment ID: `{comment_id or 'unknown'}`\n"
        f"- Rationale: {rationale.strip() or '(no rationale provided)'}\n"
    )
    with path.open("a", encoding="utf-8") as fh:
        if header_needed:
            fh.write(f"# PR #{pr_number} Comment Triage Log\n")
        fh.write(entry)
    return path


def _apply_state_transition(
    state: dict[str, Any],
    pr_number: int,
    transition: dict[str, Any],
    *,
    reset_pr_triage_counters: bool,
    decrement_comment_counter: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply a phase + next_action transition to the PR entry in state.

    Returns the updated PR entry (also mutates `state` in place). Raises
    PersistenceError if the PR is not present.
    """
    key = str(pr_number)
    if key not in state.get("prs", {}):
        raise PersistenceError(f"PR {pr_number} not present in state.json")
    pr_entry = state["prs"][key]
    ts = _now_iso(now)
    new_phase = transition["phase"]
    if new_phase != pr_entry.get("phase"):
        pr_entry["phase"] = new_phase
        pr_entry["phase_entered_at"] = ts
    pr_entry["next_action"] = transition["next_action"]
    pr_entry["last_action_at"] = ts
    if reset_pr_triage_counters:
        pr_entry["new_comments_since_triage"] = 0
        pr_entry["needs_retriage"] = False
    if decrement_comment_counter:
        prior = int(pr_entry.get("new_comments_since_triage") or 0)
        pr_entry["new_comments_since_triage"] = max(0, prior - 1)
    return pr_entry


def apply_pr_classification(
    project_root: Path,
    pr_number: int,
    classification: str,
    rationale: str,
    *,
    persist: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist a PR-mode triage outcome: cache file + state transition.

    When `persist` is False this still computes the transition descriptor (for
    dry-run logging) but does not write the cache file or save state.
    """
    if classification not in PR_TRANSITIONS:
        raise PersistenceError(
            f"unknown PR classification: {classification!r}; expected one of {list(PR_CLASSES)}"
        )
    transition = PR_TRANSITIONS[classification]
    result: dict[str, Any] = {
        "mode": "pr",
        "pr_number": pr_number,
        "classification": classification,
        "phase": transition["phase"],
        "next_action": transition["next_action"],
        "persisted": persist,
    }
    if not persist:
        return result

    write_triage_md(project_root, pr_number, classification, rationale, now=now)
    state = state_io.load_state(project_root)
    _apply_state_transition(
        state,
        pr_number,
        transition,
        reset_pr_triage_counters=True,
        decrement_comment_counter=False,
        now=now,
    )
    state_io.save_state(project_root, state)
    return result


def apply_comment_classification(
    project_root: Path,
    pr_number: int,
    classification: str,
    rationale: str,
    *,
    comment_id: str | None = None,
    persist: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist a comment-mode triage outcome: append cache + state transition."""
    if classification not in COMMENT_TRANSITIONS:
        raise PersistenceError(
            f"unknown comment classification: {classification!r}; "
            f"expected one of {list(COMMENT_CLASSES)}"
        )
    transition = COMMENT_TRANSITIONS[classification]
    result: dict[str, Any] = {
        "mode": "comment",
        "pr_number": pr_number,
        "classification": classification,
        "comment_id": comment_id,
        "phase": transition["phase"],
        "next_action": transition["next_action"],
        "persisted": persist,
    }
    if not persist:
        return result

    append_comments_triage_md(
        project_root, pr_number, classification, rationale, comment_id, now=now,
    )
    state = state_io.load_state(project_root)
    # Record this comment as triaged so the orchestrator (or the next
    # comment-mode dispatch) can pick a different one via set difference
    # against seen_comment_ids.
    if comment_id:
        pr_entry = state.get("prs", {}).get(str(pr_number))
        if pr_entry is not None:
            triaged = list(pr_entry.get("triaged_comment_ids") or [])
            if comment_id not in triaged:
                triaged.append(comment_id)
                pr_entry["triaged_comment_ids"] = triaged
    _apply_state_transition(
        state,
        pr_number,
        transition,
        reset_pr_triage_counters=False,
        decrement_comment_counter=True,
        now=now,
    )
    state_io.save_state(project_root, state)
    return result


def next_unclassified_comment_id(state: dict[str, Any], pr_number: int) -> str | None:
    """Return the first comment_id in seen_comment_ids that has not yet been
    triaged for this PR. Returns None if all comments are triaged or the PR is
    not in state.

    Used by the orchestrator (or a higher-level driver) to decide which comment
    to dispatch comment-mode triage for next. Deterministic: respects
    seen_comment_ids ordering (which prj-discover persists sorted).
    """
    pr_entry = state.get("prs", {}).get(str(pr_number))
    if not pr_entry:
        return None
    seen = pr_entry.get("seen_comment_ids") or []
    triaged = set(pr_entry.get("triaged_comment_ids") or [])
    for cid in seen:
        if cid not in triaged:
            return cid
    return None


# ---------- CLI surface (debugging aid) ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, help="Project root")
    parser.add_argument("--mode", required=True, choices=["pr", "comment"])
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--classification", required=True)
    parser.add_argument("--rationale", default="(CLI debug)")
    parser.add_argument("--comment-id", default=None)
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Compute transition descriptor without writing cache or state",
    )
    args = parser.parse_args()
    project_root = Path(args.project_root).resolve()
    try:
        if args.mode == "pr":
            out = apply_pr_classification(
                project_root,
                args.pr_number,
                args.classification,
                args.rationale,
                persist=not args.no_persist,
            )
        else:
            out = apply_comment_classification(
                project_root,
                args.pr_number,
                args.classification,
                args.rationale,
                comment_id=args.comment_id,
                persist=not args.no_persist,
            )
    except PersistenceError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
