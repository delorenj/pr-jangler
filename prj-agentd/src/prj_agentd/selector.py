"""Selector adapter.

Subprocess-invokes `python3 skills/prj-orchestrator/scripts/run.py --select-only`,
parses its JSON output, and returns a typed Selection. This is the seam:
PR Jangler chooses the action, prj-agentd executes it (or doesn't).
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


SelectionStatus = Literal["action-selected", "idle", "misconfigured"]


@dataclass(frozen=True)
class Selection:
    """One pure-selector result from PR Jangler."""

    status: SelectionStatus
    repo: str | None
    pr: int | None
    phase: str | None
    skill: str | None
    mode: str | None
    priority: int
    reason: str
    state_sha: str

    @property
    def is_actionable(self) -> bool:
        return self.status == "action-selected" and self.skill is not None

    @property
    def is_system_action(self) -> bool:
        return self.is_actionable and self.pr is None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "repo": self.repo,
            "pr": self.pr,
            "phase": self.phase,
            "skill": self.skill,
            "mode": self.mode,
            "priority": self.priority,
            "reason": self.reason,
            "state_sha": self.state_sha,
        }


class SelectorError(RuntimeError):
    """Raised when the selector subprocess fails in an unrecoverable way
    (non-JSON stdout, missing run.py, etc.)."""


def run_selector(
    project_root: Path,
    *,
    orchestrator_run_py: Path | None = None,
    timeout_seconds: int = 30,
) -> Selection:
    """Invoke the selector subprocess and return its parsed result.

    Misconfigured (exit 2) is NOT an error here — the selector emits a
    well-formed JSON object with status='misconfigured', which we return so
    the daemon can decide what to do (typically: log and skip this tick).
    """
    run_py = orchestrator_run_py or (
        project_root / "skills" / "prj-orchestrator" / "scripts" / "run.py"
    )
    if not run_py.exists():
        raise SelectorError(f"orchestrator run.py not found at {run_py}")

    cmd = [sys.executable, str(run_py), "--project-root", str(project_root), "--select-only"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise SelectorError(f"selector subprocess timed out after {timeout_seconds}s") from exc

    if result.returncode not in (0, 2):
        raise SelectorError(
            f"selector exited {result.returncode}: stderr={result.stderr.strip()[-500:]}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        raise SelectorError(
            f"selector returned no stdout (exit {result.returncode}): "
            f"stderr={result.stderr.strip()[-500:]}"
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SelectorError(f"selector stdout was not JSON: {stdout!r}") from exc

    return _parse_selection(payload)


def _parse_selection(payload: dict) -> Selection:
    required = {"status", "repo", "pr", "phase", "skill", "mode",
                "priority", "reason", "state_sha"}
    missing = required - payload.keys()
    if missing:
        raise SelectorError(f"selector payload missing keys: {sorted(missing)}")
    status = payload["status"]
    if status not in ("action-selected", "idle", "misconfigured"):
        raise SelectorError(f"selector returned unknown status: {status!r}")
    return Selection(
        status=status,  # type: ignore[arg-type]
        repo=payload["repo"],
        pr=payload["pr"],
        phase=payload["phase"],
        skill=payload["skill"],
        mode=payload["mode"],
        priority=int(payload["priority"]),
        reason=str(payload["reason"]),
        state_sha=str(payload["state_sha"]),
    )
