#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Verdict persistence + phase-transition logic for the adversarial validator.

Responsibilities:

  - Write `prs/{n}/adversarial.md` with verdict, six checklist findings, and a
    regression-suite summary.
  - Apply the per-verdict state transition on the loaded state object:
      pass     -> AdversarialCheck moves to FixImpl, reset reject counter
      reject   -> AdversarialCheck moves back to FixPlan, increment counter;
                  on the second consecutive reject, escalate to PleaseAdvise.
      escalate -> AdversarialCheck moves to PleaseAdvise, reset counter
  - Surface the retry counter via `adversarial_reject_count` on the PR entry.

This module does NOT load or save state.json itself; the caller is `run.py`,
which already owns state I/O via `state_io`. Pure functions here keep the
transition logic testable.

CLI: not exposed (this module exists for import). For visibility in the
catalog, a `--describe` flag prints the transition table as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REJECT_ESCALATION_THRESHOLD = 2  # two consecutive rejects -> escalate


def adversarial_md_path(project_root: Path, pr_number: int) -> Path:
    return (
        project_root
        / "_bmad-output"
        / "pr-workflow"
        / "prs"
        / str(pr_number)
        / "adversarial.md"
    )


def render_adversarial_md(
    pr_number: int,
    verdict: str,
    summary: str,
    findings: list[dict[str, Any]],
    concerns: list[str],
    regression: dict[str, Any],
    effective_verdict: str | None = None,
    reject_count: int = 0,
    now: datetime | None = None,
) -> str:
    """Render the adversarial.md content. Pure function for testability."""
    now = now or datetime.now(timezone.utc)
    effective_verdict = effective_verdict or verdict

    lines: list[str] = []
    lines.append(f"# Adversarial Validation Report — PR #{pr_number}")
    lines.append("")
    lines.append(f"- Generated: {now.isoformat()}")
    lines.append(f"- LLM verdict: `{verdict}`")
    lines.append(f"- Effective verdict (post structural gates): `{effective_verdict}`")
    lines.append(f"- Consecutive rejects (incl. this one if applicable): `{reject_count}`")
    if summary:
        lines.append(f"- Summary: {summary}")
    lines.append("")
    lines.append("## Regression-suite summary")
    lines.append("")
    lines.append(f"- Command: `{regression.get('command', '')}`")
    lines.append(f"- Status: `{regression.get('status', '')}`")
    lines.append(f"- Exit code: `{regression.get('exit_code', '?')}`")
    lines.append(f"- Regressions detected: `{regression.get('regressions', 0)}`")
    lines.append(f"- Duration (ms): `{regression.get('duration_ms', 0)}`")
    stdout_tail = regression.get("stdout_tail") or ""
    stderr_tail = regression.get("stderr_tail") or ""
    if stdout_tail:
        lines.append("")
        lines.append("<details><summary>stdout (tail)</summary>")
        lines.append("")
        lines.append("```")
        lines.append(stdout_tail)
        lines.append("```")
        lines.append("")
        lines.append("</details>")
    if stderr_tail:
        lines.append("")
        lines.append("<details><summary>stderr (tail)</summary>")
        lines.append("")
        lines.append("```")
        lines.append(stderr_tail)
        lines.append("```")
        lines.append("")
        lines.append("</details>")

    lines.append("")
    lines.append("## Checklist findings")
    lines.append("")
    for entry in findings:
        marker = "PASS" if entry.get("passes") else "FAIL"
        lines.append(f"### {entry.get('item', '?')} — {marker}")
        lines.append("")
        lines.append((entry.get("finding") or "").strip())
        lines.append("")

    if concerns:
        lines.append("## Concerns")
        lines.append("")
        for c in concerns:
            lines.append(f"- {c}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_adversarial_report(
    project_root: Path,
    pr_number: int,
    content: str,
) -> Path:
    """Atomically write adversarial.md. Creates parent dirs as needed."""
    path = adversarial_md_path(project_root, pr_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return path


def apply_transition(
    state: dict[str, Any],
    pr_number: int,
    verdict: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mutate `state` to reflect the verdict.

    Returns a transition summary:
      {effective_verdict, prior_phase, next_phase, reject_count}

    The rule:
      - pass:     phase -> FixImpl, next_action prj-implement-fix, reset count.
      - reject:   increment adversarial_reject_count. If new count >= THRESHOLD,
                  escalate to PleaseAdvise. Otherwise go back to FixPlan.
      - escalate: phase -> PleaseAdvise, reset count.
    """
    now = now or datetime.now(timezone.utc)
    key = str(pr_number)
    pr = state.setdefault("prs", {}).get(key)
    if pr is None:
        raise KeyError(f"PR {pr_number} not in state.prs")

    prior_phase = pr.get("phase")
    prior_count = int(pr.get("adversarial_reject_count") or 0)
    effective = verdict

    if verdict == "pass":
        new_count = 0
        next_phase = "FixImpl"
        next_action: dict[str, Any] | None = {"skill": "prj-implement-fix", "mode": None}
    elif verdict == "reject":
        new_count = prior_count + 1
        if new_count >= REJECT_ESCALATION_THRESHOLD:
            effective = "escalate"
            next_phase = "PleaseAdvise"
            next_action = None
            new_count = 0
        else:
            next_phase = "FixPlan"
            next_action = {"skill": "prj-plan-fix", "mode": None}
    elif verdict == "escalate":
        new_count = 0
        next_phase = "PleaseAdvise"
        next_action = None
    else:
        raise ValueError(f"unknown verdict: {verdict!r}")

    iso = now.isoformat()
    pr["phase"] = next_phase
    pr["phase_entered_at"] = iso
    pr["last_action_at"] = iso
    pr["adversarial_reject_count"] = new_count
    pr["next_action"] = next_action
    if next_phase == "PleaseAdvise":
        # Tag for the orchestrator's PleaseAdvise priority bump
        pr["user_acknowledged_please_advise"] = False

    return {
        "effective_verdict": effective,
        "prior_phase": prior_phase,
        "next_phase": next_phase,
        "reject_count": new_count if effective == "reject" else prior_count + (1 if verdict == "reject" else 0),
    }


def _cmd_describe(args: argparse.Namespace) -> int:
    table = {
        "pass": {"next_phase": "FixImpl", "next_action": "prj-implement-fix", "reset_counter": True},
        "reject (first)": {"next_phase": "FixPlan", "next_action": "prj-plan-fix", "reset_counter": False},
        "reject (>=threshold)": {
            "next_phase": "PleaseAdvise",
            "next_action": None,
            "reset_counter": True,
            "note": f"escalation triggers at reject_count >= {REJECT_ESCALATION_THRESHOLD}",
        },
        "escalate": {"next_phase": "PleaseAdvise", "next_action": None, "reset_counter": True},
    }
    print(json.dumps({"transitions": table, "threshold": REJECT_ESCALATION_THRESHOLD}, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_desc = sub.add_parser("describe", help="Print the verdict-to-transition table as JSON")
    p_desc.set_defaults(func=_cmd_describe)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
