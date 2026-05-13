#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Fix-plan I/O + retry/escalation logic for prj-plan-fix.

Owns:
  - validating the fix-plan markdown structure
  - writing fix-plan.md to the per-PR cache
  - transitioning state FixPlan -> AdversarialCheck (or PleaseAdvise on escalation)
  - incrementing the retry counter on redo after adversarial rejection
  - escalating to PleaseAdvise after 2 consecutive rejections

CLI:
  python3 plan_io.py validate --pr-number 42 --plan-md <path>
  python3 plan_io.py write --pr-number 42 --plan-md <path>
  python3 plan_io.py transition --pr-number 42

Importable:
  validate_plan(content) -> list[str]   # returns list of issues; [] means valid
  write_plan(project_root, pr_number, content) -> Path
  read_plan(project_root, pr_number) -> str
  fix_plan_path(project_root, pr_number) -> Path
  transition_to_adversarial(project_root, pr_number) -> dict
  current_attempts(project_root, pr_number) -> int
  record_adversarial_rejection(project_root, pr_number, summary) -> dict
  escalate_please_advise(project_root, pr_number, reason) -> dict
  PLAN_MAX_ATTEMPTS

Schema for the fix-plan markdown (validated by validate_plan):

  # Fix Plan: PR #{n}
  ## Claim
  ## Failing Test
  ## Proposed Diff
  ## Rationale
  ## Risk
  ## Attempts
    - attempt: <int>
    - previous_rejections: [list]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sibling import of state_io from prj-orchestrator. Path walks from this file:
# scripts/ -> prj-plan-fix/ -> skills/ -> prj-orchestrator/scripts.
_SIBLING = (
    Path(__file__).resolve().parent.parent.parent
    / "prj-orchestrator"
    / "scripts"
)
if _SIBLING.exists() and str(_SIBLING) not in sys.path:
    sys.path.insert(0, str(_SIBLING))

import state_io  # noqa: E402

PLAN_MAX_ATTEMPTS = 2

REQUIRED_HEADINGS = [
    "# Fix Plan: PR #",
    "## Claim",
    "## Failing Test",
    "## Proposed Diff",
    "## Rationale",
    "## Risk",
    "## Attempts",
]


def fix_plan_path(project_root: Path, pr_number: int) -> Path:
    return (
        project_root
        / "_bmad-output"
        / "pr-workflow"
        / "prs"
        / str(pr_number)
        / "fix-plan.md"
    )


def _pr_cache_dir(project_root: Path, pr_number: int) -> Path:
    return (
        project_root
        / "_bmad-output"
        / "pr-workflow"
        / "prs"
        / str(pr_number)
    )


def validate_plan(content: str) -> list[str]:
    """Return a list of issues with the plan content. Empty list = valid.

    Checks:
      - All required headings appear in order
      - Failing Test section contains a fenced code block
      - Proposed Diff section contains a fenced ```diff block
      - Attempts section has an integer `attempt:` line
    """
    issues: list[str] = []

    # Required headings present
    last_pos = -1
    for heading in REQUIRED_HEADINGS:
        pos = content.find(heading)
        if pos == -1:
            issues.append(f"missing required heading: {heading!r}")
        elif pos < last_pos:
            issues.append(f"heading {heading!r} appears out of order")
        else:
            last_pos = pos

    # Failing Test fenced code block
    test_section = _section_body(content, "## Failing Test", "## Proposed Diff")
    if test_section is not None and "```" not in test_section:
        issues.append("Failing Test section must contain a fenced code block")

    # Proposed Diff fenced diff block
    diff_section = _section_body(content, "## Proposed Diff", "## Rationale")
    if diff_section is not None:
        if "```diff" not in diff_section and "```" not in diff_section:
            issues.append("Proposed Diff section must contain a fenced code block")

    # Attempts section schema
    attempts_section = _section_body(content, "## Attempts", None)
    if attempts_section is not None:
        m = re.search(r"^\s*-\s*attempt:\s*(\d+)\s*$", attempts_section, re.MULTILINE)
        if not m:
            issues.append("Attempts section must contain `- attempt: <int>`")
        if not re.search(
            r"^\s*-\s*previous_rejections:\s*\[.*\]\s*$",
            attempts_section,
            re.MULTILINE,
        ):
            issues.append(
                "Attempts section must contain `- previous_rejections: [...]`"
            )

    return issues


def _section_body(content: str, start_heading: str, end_heading: str | None) -> str | None:
    """Return the text between two headings, or None if start not found."""
    start = content.find(start_heading)
    if start == -1:
        return None
    start = start + len(start_heading)
    if end_heading:
        end = content.find(end_heading, start)
        if end == -1:
            return content[start:]
        return content[start:end]
    return content[start:]


def write_plan(project_root: Path, pr_number: int, content: str) -> Path:
    """Validate then write fix-plan.md atomically. Returns the path written."""
    issues = validate_plan(content)
    if issues:
        raise ValueError(f"fix-plan invalid: {issues}")
    cache = _pr_cache_dir(project_root, pr_number)
    cache.mkdir(parents=True, exist_ok=True)
    path = fix_plan_path(project_root, pr_number)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return path


def read_plan(project_root: Path, pr_number: int) -> str:
    path = fix_plan_path(project_root, pr_number)
    if not path.exists():
        raise FileNotFoundError(f"fix-plan.md not found at {path}")
    return path.read_text(encoding="utf-8")


def current_attempts(project_root: Path, pr_number: int) -> int:
    """Read the current attempt count from fix-plan.md if it exists, else 0."""
    path = fix_plan_path(project_root, pr_number)
    if not path.exists():
        return 0
    content = path.read_text(encoding="utf-8")
    m = re.search(r"^\s*-\s*attempt:\s*(\d+)\s*$", content, re.MULTILINE)
    return int(m.group(1)) if m else 0


def _update_pr_in_state(
    project_root: Path,
    pr_number: int,
    *,
    phase: str,
    next_action: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> dict:
    """Mutate state.json for one PR. Returns the updated PR record."""
    state = state_io.load_state(project_root)
    key = str(pr_number)
    if key not in state["prs"]:
        raise KeyError(f"PR {pr_number} not in state.prs")
    pr = state["prs"][key]
    pr["phase"] = phase
    pr["phase_entered_at"] = datetime.now(timezone.utc).isoformat()
    pr["last_action_at"] = pr["phase_entered_at"]
    pr["next_action"] = next_action
    if extra:
        pr.update(extra)
    state_io.save_state(project_root, state)
    return pr


def transition_to_adversarial(project_root: Path, pr_number: int) -> dict:
    """Advance PR from FixPlan to AdversarialCheck."""
    return _update_pr_in_state(
        project_root,
        pr_number,
        phase="AdversarialCheck",
        next_action={"skill": "prj-validate-adversarial", "mode": None},
    )


def escalate_please_advise(
    project_root: Path,
    pr_number: int,
    reason: str,
) -> dict:
    """Promote PR to PleaseAdvise. Used after 2 consecutive adversarial rejects
    or when a failing test cannot be written."""
    return _update_pr_in_state(
        project_root,
        pr_number,
        phase="PleaseAdvise",
        next_action=None,
        extra={"please_advise_reason": reason},
    )


def record_adversarial_rejection(
    project_root: Path,
    pr_number: int,
    summary: str,
) -> dict:
    """Append a rejection note to the PR's state and bump rejection_count.

    Returns the updated PR record. Does NOT transition phase here; the
    orchestrator/run.py decides whether to retry or escalate.
    """
    state = state_io.load_state(project_root)
    key = str(pr_number)
    if key not in state["prs"]:
        raise KeyError(f"PR {pr_number} not in state.prs")
    pr = state["prs"][key]
    rejections = pr.get("adversarial_rejections") or []
    rejections.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
    })
    pr["adversarial_rejections"] = rejections
    pr["rejection_count"] = len(rejections)
    pr["last_action_at"] = datetime.now(timezone.utc).isoformat()
    state_io.save_state(project_root, state)
    return pr


def should_escalate(project_root: Path, pr_number: int) -> bool:
    """True when consecutive rejections have hit PLAN_MAX_ATTEMPTS."""
    state = state_io.load_state(project_root)
    pr = state["prs"].get(str(pr_number), {})
    return int(pr.get("rejection_count", 0)) >= PLAN_MAX_ATTEMPTS


# ---------- CLI surface ----------

def _cmd_validate(args: argparse.Namespace) -> int:
    content = Path(args.plan_md).read_text(encoding="utf-8")
    issues = validate_plan(content)
    if issues:
        print(json.dumps({"status": "invalid", "issues": issues}))
        return 1
    print(json.dumps({"status": "ok"}))
    return 0


def _cmd_write(args: argparse.Namespace) -> int:
    project_root = (
        Path(args.project_root).resolve()
        if args.project_root
        else state_io.find_project_root()
    )
    content = Path(args.plan_md).read_text(encoding="utf-8")
    try:
        path = write_plan(project_root, args.pr_number, content)
    except ValueError as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "path": str(path)}))
    return 0


def _cmd_transition(args: argparse.Namespace) -> int:
    project_root = (
        Path(args.project_root).resolve()
        if args.project_root
        else state_io.find_project_root()
    )
    pr = transition_to_adversarial(project_root, args.pr_number)
    print(json.dumps({"status": "ok", "pr": pr}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="Validate a fix-plan markdown file")
    p_val.add_argument("--plan-md", required=True, help="Path to plan markdown")
    p_val.add_argument("--pr-number", type=int, required=True)
    p_val.set_defaults(func=_cmd_validate)

    p_write = sub.add_parser("write", help="Validate then write fix-plan.md")
    p_write.add_argument("--plan-md", required=True, help="Path to plan markdown")
    p_write.add_argument("--pr-number", type=int, required=True)
    p_write.add_argument("--project-root", help="Override project root", default=None)
    p_write.set_defaults(func=_cmd_write)

    p_trans = sub.add_parser(
        "transition", help="Transition PR phase FixPlan -> AdversarialCheck"
    )
    p_trans.add_argument("--pr-number", type=int, required=True)
    p_trans.add_argument("--project-root", help="Override project root", default=None)
    p_trans.set_defaults(func=_cmd_transition)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
