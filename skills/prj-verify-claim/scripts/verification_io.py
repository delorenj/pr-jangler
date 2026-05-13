#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Verification document writes + state-machine transitions for prj-verify-claim.

`verification.md` is the per-PR cache artifact this skill owns. It contains:
  - the claim that was verified
  - the chosen reproduction strategy
  - what was observed when the strategy ran
  - the adjudicated verdict
  - the rationale for that verdict

This module also encodes the state-machine transitions for the three verdicts:
  - verified      -> phase=FixPlan,    next_action=prj-plan-fix
  - not-verified  -> phase=Reviewed,   next_action=null
  - ambiguous     -> phase=PleaseAdvise, next_action=null

Importable:
  - VERDICT_TRANSITIONS, VALID_VERDICTS, VALID_STRATEGIES
  - VerificationError
  - render_verification_md (pure)
  - apply_verdict_to_state (pure)
  - write_verification (filesystem)
  - persist (orchestration glue: render + write + transition)

The module imports state_io from prj-orchestrator/scripts via sys.path; that
path is added at module import time the same way prj-discover/reconcile.py
does it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sibling import: state_io lives in prj-orchestrator/scripts. Walk up from
# this file: scripts/ -> prj-verify-claim/ -> skills/ -> prj-orchestrator/scripts.
_SIBLING = Path(__file__).resolve().parent.parent.parent / "prj-orchestrator" / "scripts"
if _SIBLING.exists() and str(_SIBLING) not in sys.path:
    sys.path.insert(0, str(_SIBLING))

import state_io  # noqa: E402


VALID_VERDICTS = ("verified", "not-verified", "ambiguous")
VALID_STRATEGIES = ("failing-test", "existing-test", "manual-exercise")


# Phase + next-action transitions per verdict. `next_action` is None to encode
# a terminal-for-this-cycle transition; the orchestrator will pick up the PR
# again when discovery surfaces a new comment or a maintainer override.
VERDICT_TRANSITIONS: dict[str, dict[str, Any]] = {
    "verified": {
        "phase": "FixPlan",
        "next_action": {"skill": "prj-plan-fix", "mode": None},
        "user_acknowledged_please_advise": None,
    },
    "not-verified": {
        "phase": "Reviewed",
        "next_action": None,
        "user_acknowledged_please_advise": None,
    },
    "ambiguous": {
        "phase": "PleaseAdvise",
        "next_action": None,
        # PleaseAdvise rises to priority 1000 until user_acknowledged is set.
        "user_acknowledged_please_advise": False,
    },
}


class VerificationError(RuntimeError):
    """Raised for invalid verdict, missing PR in state, or I/O failure."""


def _verification_path(project_root: Path, pr_number: int) -> Path:
    return (
        project_root / "_bmad-output" / "pr-workflow" / "prs"
        / str(pr_number) / "verification.md"
    )


def render_verification_md(
    pr_number: int,
    commenter: str,
    claim: str,
    strategy: str,
    observation: str,
    verdict: str,
    rationale: str,
    worktree_path: Path | str | None,
    runner_command: list[str] | None,
    runner_returncode: int | None,
    timestamp: str | None = None,
) -> str:
    """Compose the verification.md body. Pure function (no I/O)."""
    if verdict not in VALID_VERDICTS:
        raise VerificationError(
            f"invalid verdict {verdict!r}; expected one of {VALID_VERDICTS}"
        )
    if strategy not in VALID_STRATEGIES:
        raise VerificationError(
            f"invalid strategy {strategy!r}; expected one of {VALID_STRATEGIES}"
        )
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    rc_text = "n/a" if runner_returncode is None else str(runner_returncode)
    cmd_text = " ".join(runner_command) if runner_command else "n/a"
    wt_text = str(worktree_path) if worktree_path else "n/a"
    body = f"""# Verification: PR #{pr_number}

- **Verdict:** `{verdict}`
- **Strategy:** `{strategy}`
- **Commenter:** {commenter or "unknown"}
- **Timestamp:** {ts}
- **Worktree:** `{wt_text}`
- **Runner command:** `{cmd_text}`
- **Runner exit code:** `{rc_text}`

## Claim

{claim.strip() if claim else "_(claim text not provided)_"}

## Observation

{observation.strip() if observation else "_(no observation recorded)_"}

## Rationale

{rationale.strip() if rationale else "_(no rationale recorded)_"}
"""
    return body


def write_verification(
    project_root: Path,
    pr_number: int,
    body: str,
) -> Path:
    """Write verification.md (overwriting any prior content)."""
    path = _verification_path(project_root, pr_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def apply_verdict_to_state(
    state: dict[str, Any],
    pr_number: int,
    verdict: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mutate `state` to reflect the verdict's phase transition.

    Returns the same state object for convenience. Raises VerificationError
    if the PR is not in state or the verdict is unknown.
    """
    if verdict not in VERDICT_TRANSITIONS:
        raise VerificationError(
            f"invalid verdict {verdict!r}; expected one of {sorted(VERDICT_TRANSITIONS)}"
        )
    key = str(pr_number)
    pr = state.get("prs", {}).get(key)
    if pr is None:
        raise VerificationError(
            f"PR #{pr_number} not present in state; cannot transition"
        )
    transition = VERDICT_TRANSITIONS[verdict]
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    pr["phase"] = transition["phase"]
    pr["phase_entered_at"] = now_iso
    pr["last_action_at"] = now_iso
    pr["next_action"] = transition["next_action"]
    # Resetting the comment counter is the responsibility of prj-triage; we
    # do not clear it here. But we do record the most recent verification
    # outcome so downstream skills can read it without re-opening verification.md.
    pr["last_verification_verdict"] = verdict
    if transition["user_acknowledged_please_advise"] is not None:
        pr["user_acknowledged_please_advise"] = transition[
            "user_acknowledged_please_advise"
        ]
    return state


def persist(
    project_root: Path,
    pr_number: int,
    commenter: str,
    claim: str,
    strategy: str,
    observation: str,
    verdict: str,
    rationale: str,
    worktree_path: Path | str | None,
    runner_command: list[str] | None,
    runner_returncode: int | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """End-to-end persistence: render markdown, write file, update state.

    Returns a dict suitable for inclusion in the run-log entry. When
    `dry_run=True`, computes the same content but neither writes
    verification.md nor saves state.
    """
    body = render_verification_md(
        pr_number=pr_number,
        commenter=commenter,
        claim=claim,
        strategy=strategy,
        observation=observation,
        verdict=verdict,
        rationale=rationale,
        worktree_path=worktree_path,
        runner_command=runner_command,
        runner_returncode=runner_returncode,
    )
    md_path = _verification_path(project_root, pr_number)
    extras: dict[str, Any] = {
        "verdict": verdict,
        "strategy": strategy,
        "verification_md": str(md_path),
        "phase": VERDICT_TRANSITIONS[verdict]["phase"],
        "next_action": VERDICT_TRANSITIONS[verdict]["next_action"],
    }
    if dry_run:
        extras["dry_run"] = True
        return extras

    write_verification(project_root, pr_number, body)
    state = state_io.load_state(project_root)
    apply_verdict_to_state(state, pr_number, verdict)
    state_io.save_state(project_root, state)
    return extras


# ---------- CLI surface ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, help="Project root path")
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--commenter", default="unknown")
    parser.add_argument("--claim", required=True)
    parser.add_argument("--strategy", required=True, choices=list(VALID_STRATEGIES))
    parser.add_argument("--observation", required=True)
    parser.add_argument("--verdict", required=True, choices=list(VALID_VERDICTS))
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--worktree", default=None)
    parser.add_argument("--runner-command", default=None,
                        help="Space-separated command string, for logging only")
    parser.add_argument("--runner-returncode", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    runner_cmd = args.runner_command.split() if args.runner_command else None
    try:
        extras = persist(
            project_root=Path(args.project_root),
            pr_number=args.pr_number,
            commenter=args.commenter,
            claim=args.claim,
            strategy=args.strategy,
            observation=args.observation,
            verdict=args.verdict,
            rationale=args.rationale,
            worktree_path=args.worktree,
            runner_command=runner_cmd,
            runner_returncode=args.runner_returncode,
            dry_run=args.dry_run,
        )
    except VerificationError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(extras, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
