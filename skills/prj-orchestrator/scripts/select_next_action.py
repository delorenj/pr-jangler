#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Compute the highest-priority next action for the PR Jangler heartbeat.

Outputs a JSON action descriptor on stdout:

    {"action": "dispatch|noop",
     "skill": "prj-x" or null,
     "pr_number": N or null,
     "mode": "pr|comment" or null,
     "priority": N,
     "reason": "string"}

Importable: select_next_action(state, config, now).
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from state_io import (
    TERMINAL_PHASES,
    find_project_root,
    load_state,
)

DEFAULTS = {
    "prj_discover_every_n": 3,
    "prj_report_hour_local": 8,
}


def load_prj_config(project_root: Path) -> dict[str, Any]:
    """Read [modules.prj] from _bmad/config.toml. Fill missing keys with defaults."""
    path = project_root / "_bmad" / "config.toml"
    cfg = dict(DEFAULTS)
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        cfg.update(data.get("modules", {}).get("prj", {}))
    return cfg


def _days_since(iso_ts: str, now: datetime) -> int:
    if not iso_ts:
        return 0
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return 0
    delta = now - ts
    return max(0, delta.days)


def _pr_priority(pr: dict[str, Any], now: datetime) -> int:
    """Compute priority for one PR. Returns -1 if it has no eligible action."""
    if pr["phase"] in TERMINAL_PHASES:
        return -1
    if pr["phase"] == "PleaseAdvise" and not pr.get("user_acknowledged_please_advise"):
        return 1000
    if not pr.get("next_action"):
        return -1
    score = 100
    if pr.get("new_comments_since_triage", 0) > 0:
        score += 50
    if pr.get("needs_retriage"):
        score += 30
    score += min(_days_since(pr.get("phase_entered_at", ""), now), 30)
    return score


def select_next_action(
    state: dict[str, Any],
    config: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Determine the highest-priority next action across the queue."""
    now = now or datetime.now(timezone.utc)

    # System action: daily report.
    # Suppressed on fresh projects (no PRs ever discovered AND no report ever sent)
    # so the first heartbeats prioritize discovery over an empty "all quiet" email.
    report_hour = int(config.get("prj_report_hour_local", DEFAULTS["prj_report_hour_local"]))
    local_now = now.astimezone()
    today = local_now.date().isoformat()
    last_report = state.get("last_report_sent")
    last_report_date: str | None = None
    if last_report:
        try:
            last_report_date = (
                datetime.fromisoformat(last_report.replace("Z", "+00:00"))
                .astimezone()
                .date()
                .isoformat()
            )
        except ValueError:
            pass
    queue_has_history = bool(state.get("prs")) or last_report is not None
    if queue_has_history and local_now.hour >= report_hour and last_report_date != today:
        return {
            "action": "dispatch",
            "skill": "prj-report-daily",
            "pr_number": None,
            "mode": None,
            "priority": 700,
            "reason": f"daily report due at local hour >= {report_hour}",
        }

    # System action: discovery cadence or empty queue
    discover_every_n = int(config.get("prj_discover_every_n", DEFAULTS["prj_discover_every_n"]))
    heartbeat = state.get("heartbeat_count", 0)
    queue_empty = not state.get("prs")
    if queue_empty or (heartbeat > 0 and heartbeat % discover_every_n == 0):
        reason = "queue empty" if queue_empty else f"discovery cadence (heartbeat {heartbeat} % {discover_every_n} == 0)"
        return {
            "action": "dispatch",
            "skill": "prj-discover",
            "pr_number": None,
            "mode": None,
            "priority": 500,
            "reason": reason,
        }

    # Per-PR action: pick highest priority with FIFO tiebreak
    candidates: list[tuple[int, str, str]] = []
    for pr_key, pr in state["prs"].items():
        score = _pr_priority(pr, now)
        if score > 0 and (pr.get("next_action") or pr["phase"] == "PleaseAdvise"):
            candidates.append((score, pr.get("phase_entered_at", ""), pr_key))

    if not candidates:
        return {
            "action": "noop",
            "skill": None,
            "pr_number": None,
            "mode": None,
            "priority": 0,
            "reason": "no actionable PRs and no system actions due",
        }

    # priority desc, phase_entered_at asc (older first), pr_key asc
    candidates.sort(key=lambda t: (-t[0], t[1], int(t[2])))
    priority, _, pr_key = candidates[0]
    pr = state["prs"][pr_key]
    na = pr.get("next_action") or {"skill": "prj-please-advise-handler", "mode": None}
    return {
        "action": "dispatch",
        "skill": na["skill"],
        "pr_number": pr["pr_number"],
        "mode": na.get("mode"),
        "priority": priority,
        "reason": f"highest-priority PR action (PR {pr_key}, phase {pr['phase']})",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="Override project root", default=None)
    parser.add_argument("--state", help="Path to state.json (overrides default)", default=None)
    parser.add_argument("--now", help="ISO timestamp for testing", default=None)
    args = parser.parse_args()

    project_root = Path(args.project_root) if args.project_root else find_project_root()
    if args.state:
        state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    else:
        state = load_state(project_root)
    config = load_prj_config(project_root)
    now = datetime.fromisoformat(args.now) if args.now else None
    action = select_next_action(state, config, now)
    print(json.dumps(action, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
