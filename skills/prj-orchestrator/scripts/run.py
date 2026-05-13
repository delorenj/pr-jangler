#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""prj-orchestrator main heartbeat.

Reads state, picks next action, dispatches (or stub-logs), persists state,
appends a structured run-log entry, exits. Idempotent. Atomic state writes.
Never silently fails.

Exit codes:
  0 success (including stub: not built, noop, and dispatch-failed)
  2 misconfigured (prj_repo missing)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from state_io import (
    STATE_VERSION,
    append_runlog,
    find_project_root,
    init_state,
    load_state,
    save_state,
    state_path,
)
from select_next_action import load_prj_config, select_next_action


def _phase_skill_root(project_root: Path, skill_name: str) -> Path:
    return project_root / "skills" / skill_name


def _skill_is_installed(project_root: Path, skill_name: str) -> bool:
    return (_phase_skill_root(project_root, skill_name) / "SKILL.md").exists()


def _direct_invoke_cmd(project_root: Path, skill: str, pr_number: int | None, mode: str | None) -> list[str]:
    """Fallback to direct Python invocation of the sibling skill's run.py.

    Used when the `bmad` CLI is not on PATH. The contract: every phase skill
    exposes its entry at `skills/{name}/scripts/run.py` with `--project-root`,
    optionally `--pr`, and optionally `--mode`.
    """
    cmd = [sys.executable, str(_phase_skill_root(project_root, skill) / "scripts" / "run.py"),
           "--project-root", str(project_root)]
    if pr_number is not None:
        cmd.extend(["--pr-number", str(pr_number)])
    if mode:
        cmd.extend(["--mode", mode])
    return cmd


def _dispatch(skill: str, pr_number: int | None, mode: str | None, verbose: bool, run_id: str,
              project_root: Path | None = None) -> tuple[str, dict[str, Any]]:
    """Dispatch via `bmad run`, falling back to direct Python invocation when the CLI is absent."""
    extra: dict[str, Any] = {}
    cmd = ["bmad", "run", skill]
    if pr_number is not None:
        cmd.extend(["--pr-number", str(pr_number)])
    if mode:
        cmd.extend(["--mode", mode])
    if verbose:
        print(f"[run {run_id}] dispatching: {' '.join(cmd)}", file=sys.stderr)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        extra["dispatch_exit"] = result.returncode
        if result.stderr:
            extra["dispatch_stderr_tail"] = result.stderr[-500:]
        return ("dispatched" if result.returncode == 0 else "dispatch-failed", extra)
    except FileNotFoundError:
        # `bmad` CLI missing: fall back to direct Python invocation of the sibling skill.
        if project_root is None:
            return ("no-bmad-cli", {"hint": "no project_root for fallback; pass it through"})
        fallback_cmd = _direct_invoke_cmd(project_root, skill, pr_number, mode)
        if verbose:
            print(f"[run {run_id}] bmad CLI missing, fallback: {' '.join(fallback_cmd)}", file=sys.stderr)
        try:
            result = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=300)
            extra["dispatch_mode"] = "direct-python"
            extra["dispatch_exit"] = result.returncode
            if result.stderr:
                extra["dispatch_stderr_tail"] = result.stderr[-500:]
            return ("dispatched" if result.returncode == 0 else "dispatch-failed", extra)
        except subprocess.TimeoutExpired:
            return ("dispatch-timeout", {"timeout_s": 300, "dispatch_mode": "direct-python"})
        except Exception as e:
            return ("dispatch-failed", {"dispatch_mode": "direct-python", "error": str(e)})
    except subprocess.TimeoutExpired:
        return ("dispatch-timeout", {"timeout_s": 300})


def _state_sha(project_root: Path) -> str:
    """SHA-256 of state.json bytes on disk, or 'no-state' if absent.

    Used as an idempotency token by external daemons (e.g. prj-agentd) that
    select an action and later need to detect whether the underlying state
    changed before they execute it.
    """
    path = state_path(project_root)
    if not path.exists():
        return "no-state"
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ephemeral_state(repo: str) -> dict[str, Any]:
    """Build an in-memory empty state for --select-only when state.json is absent.

    Mirrors init_state's structure but does NOT touch disk. Used so the selector
    can return a useful action ('queue empty -> prj-discover') on a fresh
    project without forcing a write.
    """
    return {
        "version": STATE_VERSION,
        "repo": repo,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "heartbeat_count": 0,
        "last_report_sent": None,
        "prs": {},
    }


def _select_only(project_root: Path, config: dict[str, Any], repo: str) -> dict[str, Any]:
    """Pure selector: read-only, no runlog, no dispatch. Returns the action descriptor.

    Output schema (one JSON object):
        status: "action-selected" | "idle" | "misconfigured"
        repo: "owner/name" or null
        pr: int or null
        phase: PR phase string or null (null for system actions and idle)
        skill: target skill or null
        mode: "pr" | "comment" | null
        priority: int
        reason: human-readable string
        state_sha: "no-state" or 64-char hex
    """
    if state_path(project_root).exists():
        state = load_state(project_root)
    else:
        state = _ephemeral_state(repo)

    action = select_next_action(state, config)

    pr_number = action.get("pr_number")
    phase: str | None = None
    if pr_number is not None:
        pr_record = state["prs"].get(str(pr_number))
        if pr_record is not None:
            phase = pr_record.get("phase")

    status = "idle" if action["action"] == "noop" else "action-selected"
    return {
        "status": status,
        "repo": state.get("repo") or None,
        "pr": pr_number,
        "phase": phase,
        "skill": action.get("skill"),
        "mode": action.get("mode"),
        "priority": action.get("priority", 0),
        "reason": action.get("reason", ""),
        "state_sha": _state_sha(project_root),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="Override project root", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Compute and log but do not dispatch")
    parser.add_argument(
        "--select-only",
        action="store_true",
        help=(
            "Pure selector mode for external daemons (e.g. prj-agentd): read-only, "
            "no state mutation, no runlog, no dispatch. Emits one JSON object to "
            "stdout with the selected action plus a state_sha idempotency token."
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose stderr diagnostics")
    parser.add_argument("--once", action="store_true", help="Cron-clarity flag (always single-shot)")
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    project_root = Path(args.project_root) if args.project_root else find_project_root()
    if args.verbose:
        print(f"[run {run_id}] project_root={project_root}", file=sys.stderr)

    config = load_prj_config(project_root)
    repo = config.get("prj_repo", "").strip() if isinstance(config.get("prj_repo"), str) else ""
    if not repo:
        msg = "prj_repo not configured in [modules.prj] of _bmad/config.toml. Refusing to run."
        if args.select_only:
            # Pure selector: no runlog side-effects. Still surface the misconfig as JSON
            # so daemons can react without parsing stderr or relying on exit codes.
            print(json.dumps({
                "status": "misconfigured",
                "repo": None,
                "pr": None,
                "phase": None,
                "skill": None,
                "mode": None,
                "priority": 0,
                "reason": msg,
                "state_sha": _state_sha(project_root),
            }, sort_keys=True))
            return 2
        entry = {
            "run_id": run_id,
            "action": "abort",
            "status": "misconfigured",
            "reason": msg,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        append_runlog(project_root, entry)
        print(json.dumps({"status": "abort", "reason": msg}), file=sys.stderr)
        return 2

    if args.select_only:
        result = _select_only(project_root, config, repo)
        if args.verbose:
            print(f"[run {run_id}] select-only result={json.dumps(result)}", file=sys.stderr)
        print(json.dumps(result, sort_keys=True))
        return 0

    # Ensure state exists
    if not state_path(project_root).exists():
        init_state(project_root, repo)

    state = load_state(project_root)
    state["heartbeat_count"] = state.get("heartbeat_count", 0) + 1

    action = select_next_action(state, config)
    if args.verbose:
        print(f"[run {run_id}] action={json.dumps(action)}", file=sys.stderr)

    # Persist incremented heartbeat BEFORE dispatch so subprocesses see the new
    # value and can mutate state without a write-race against this process.
    # After dispatch, this process MUST NOT re-save state; the dispatched skill
    # is the canonical writer for whatever fields it touched.
    save_state(project_root, state)

    log_entry: dict[str, Any] = {
        "run_id": run_id,
        "action": action["action"],
        "skill": action.get("skill"),
        "pr_number": action.get("pr_number"),
        "mode": action.get("mode"),
        "priority": action.get("priority"),
        "reason": action.get("reason"),
        "heartbeat_count": state["heartbeat_count"],
    }

    status: str
    next_action_hint: str | None = None

    if action["action"] == "noop":
        status = "idle"
    elif args.dry_run:
        status = "dry-run"
        next_action_hint = f"would dispatch {action['skill']}"
    elif not _skill_is_installed(project_root, action["skill"]):
        status = "stub: not built"
        next_action_hint = f"{action['skill']} not installed; logged only"
    else:
        status, extra = _dispatch(
            skill=action["skill"],
            pr_number=action.get("pr_number"),
            mode=action.get("mode"),
            verbose=args.verbose,
            run_id=run_id,
            project_root=project_root,
        )
        log_entry.update(extra)

    log_entry["status"] = status
    if next_action_hint:
        log_entry["next_action_hint"] = next_action_hint
    log_entry["duration_ms"] = int((time.monotonic() - started) * 1000)

    # State already persisted pre-dispatch; do NOT re-save here.
    append_runlog(project_root, log_entry)

    print(json.dumps(log_entry, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
