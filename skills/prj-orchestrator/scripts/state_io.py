#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""State I/O for the PR Jangler queue.

CLI subcommands: init, load, save, append-runlog, find-root.
Importable: load_state, save_state, init_state, append_runlog, find_project_root,
state_path, runlog_path, VALID_PHASES, TERMINAL_PHASES.

Atomic save semantics: write to {path}.tmp, fsync, rename. Validation is
structural (light) since the state schema is small enough to keep stdlib-only.
For richer validation see assets/state-schema.json.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_VERSION = "1.0"

VALID_PHASES = frozenset({
    "Discovered", "Triaged", "OverlapCheck", "ReviewPending", "Reviewed",
    "CommentsTriage", "ClaimVerify", "FixPlan", "AdversarialCheck",
    "FixImpl", "ReadyToMerge", "Blocked", "Rejected", "Archived",
    "PleaseAdvise",
})

TERMINAL_PHASES = frozenset({"ReadyToMerge", "Blocked", "Rejected", "Archived"})


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from start to find the first directory containing `_bmad/`.

    Falls back to `git rev-parse --show-toplevel` if `_bmad/` is not found.
    """
    here = (start or Path(__file__).resolve()).resolve()
    for parent in [here, *here.parents]:
        if (parent / "_bmad").is_dir():
            return parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(result.stdout.strip())
    except Exception as exc:
        raise RuntimeError(
            "Could not locate project root. Expected `_bmad/` directory in an ancestor."
        ) from exc


def state_path(project_root: Path) -> Path:
    return project_root / "_bmad-output" / "pr-workflow" / "state.json"


def runlog_path(project_root: Path, date_str: str | None = None) -> Path:
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return project_root / "_bmad-output" / "pr-workflow" / "logs" / f"{date_str}.jsonl"


def _validate_state(state: dict[str, Any]) -> None:
    """Light structural validation. Raises ValueError on malformed state."""
    if state.get("version") != STATE_VERSION:
        raise ValueError(f"state.version must be {STATE_VERSION!r}, got {state.get('version')!r}")
    for key in ("repo", "last_updated", "heartbeat_count", "prs"):
        if key not in state:
            raise ValueError(f"state missing required key: {key}")
    if not isinstance(state["prs"], dict):
        raise ValueError("state.prs must be an object")
    for pr_key, pr in state["prs"].items():
        if not pr_key.isdigit():
            raise ValueError(f"state.prs key must be digits, got {pr_key!r}")
        for key in ("pr_number", "phase", "phase_entered_at", "last_action_at", "contributor_login"):
            if key not in pr:
                raise ValueError(f"PR {pr_key} missing key: {key}")
        if pr["phase"] not in VALID_PHASES:
            raise ValueError(f"PR {pr_key} has invalid phase: {pr['phase']!r}")


def init_state(project_root: Path, repo: str) -> dict[str, Any]:
    """Initialize state.json if missing. Returns the in-memory state.

    Idempotent: if state already exists, returns it unchanged (repo arg ignored).
    """
    path = state_path(project_root)
    if path.exists():
        return load_state(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "version": STATE_VERSION,
        "repo": repo,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "heartbeat_count": 0,
        "last_report_sent": None,
        "prs": {},
    }
    save_state(project_root, state)
    return state


def load_state(project_root: Path) -> dict[str, Any]:
    path = state_path(project_root)
    if not path.exists():
        raise FileNotFoundError(f"state.json not found at {path}. Call init first.")
    with path.open("r", encoding="utf-8") as fh:
        state = json.load(fh)
    _validate_state(state)
    return state


def save_state(project_root: Path, state: dict[str, Any]) -> None:
    """Atomic write: validate, write to .tmp, fsync, rename."""
    _validate_state(state)
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    path = state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def append_runlog(project_root: Path, entry: dict[str, Any]) -> None:
    """Append a single JSON line to today's runlog. Adds `ts` if not set."""
    if "ts" not in entry:
        entry["ts"] = datetime.now(timezone.utc).isoformat()
    path = runlog_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True))
        fh.write("\n")


# ---------- CLI surface ----------

def _cmd_init(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root) if args.project_root else find_project_root()
    state = init_state(project_root, args.repo)
    print(json.dumps({
        "status": "ok",
        "state_path": str(state_path(project_root)),
        "repo": state["repo"],
        "heartbeat_count": state["heartbeat_count"],
    }))
    return 0


def _cmd_load(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root) if args.project_root else find_project_root()
    try:
        state = load_state(project_root)
    except FileNotFoundError as exc:
        print(json.dumps({"status": "missing", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


def _cmd_save(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root) if args.project_root else find_project_root()
    state = json.load(sys.stdin)
    save_state(project_root, state)
    print(json.dumps({"status": "ok", "state_path": str(state_path(project_root))}))
    return 0


def _cmd_append_runlog(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root) if args.project_root else find_project_root()
    entry = json.loads(args.entry) if args.entry else json.load(sys.stdin)
    append_runlog(project_root, entry)
    print(json.dumps({"status": "ok", "runlog_path": str(runlog_path(project_root))}))
    return 0


def _cmd_find_root(args: argparse.Namespace) -> int:
    print(str(find_project_root()))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="Override project root path", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize state.json (idempotent)")
    p_init.add_argument("--repo", required=True, help="Repo in owner/name format")
    p_init.set_defaults(func=_cmd_init)

    p_load = sub.add_parser("load", help="Load state.json and emit JSON to stdout")
    p_load.set_defaults(func=_cmd_load)

    p_save = sub.add_parser("save", help="Read state JSON from stdin and atomically save")
    p_save.set_defaults(func=_cmd_save)

    p_log = sub.add_parser("append-runlog", help="Append a structured run-log entry")
    p_log.add_argument("--entry", help="JSON string for entry (or pipe via stdin)")
    p_log.set_defaults(func=_cmd_append_runlog)

    p_root = sub.add_parser("find-root", help="Print resolved project root path")
    p_root.set_defaults(func=_cmd_find_root)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
