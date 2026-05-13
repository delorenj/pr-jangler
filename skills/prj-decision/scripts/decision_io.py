#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Side-effect surface for prj-decision: label, comment, log, transition.

Each function is small and unit-testable with a subprocess seam (`_run_gh`)
and a state-transition seam that operates purely on dict state. The orchestrating
script (run.py) composes these into one atomic-feeling pass.

Importable:
  - render_comment(template_text, context)
  - apply_label(repo, pr_number, decision_class, ...)
  - post_comment(repo, pr_number, body, ...)
  - append_decisions_log(cache_dir, entry)
  - transition_state(state, pr_number, decision_class)  (pure)
  - DECISION_TO_PHASE
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Sibling-import state_io from prj-orchestrator.
_SIBLING = (
    Path(__file__).resolve().parent.parent.parent / "prj-orchestrator" / "scripts"
)
if _SIBLING.exists() and str(_SIBLING) not in sys.path:
    sys.path.insert(0, str(_SIBLING))

import state_io  # noqa: E402


class GhError(RuntimeError):
    """Raised when a gh CLI invocation fails. Includes captured stderr tail."""


# Per state-transition matrix in SKILL.md. close-as-not-now lands at Rejected.
DECISION_TO_PHASE = {
    "ready-to-merge": "ReadyToMerge",
    "close-as-not-now": "Rejected",
    # request-changes intentionally not mapped: PR stays in current phase.
}


# ---------- gh subprocess seam ----------

def _run_gh(args: list[str], timeout: int = 30) -> str:
    """Execute gh and return stdout. Raises GhError on non-zero exit.

    This is the single seam tests patch.
    """
    binary = shutil.which("gh") or "gh"
    cmd = [binary, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise GhError(f"gh CLI not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh timed out after {timeout}s: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise GhError(f"gh exited {proc.returncode}: {tail}")
    return proc.stdout


# ---------- label application ----------

def apply_label(
    repo: str,
    pr_number: int,
    decision_class: str,
    runner: Any = None,
) -> str:
    """Apply the `prj/decision:{class}` label to the PR.

    Returns the gh stdout for the caller's run-log record. The `runner`
    parameter defaults to this module's `_run_gh` (resolved lazily so tests
    that patch `decision_io._run_gh` are honored).
    """
    if decision_class not in {"ready-to-merge", "request-changes", "close-as-not-now"}:
        raise ValueError(f"invalid decision class: {decision_class!r}")
    if runner is None:
        runner = _run_gh
    label = f"prj/decision:{decision_class}"
    return runner([
        "pr", "edit", str(pr_number),
        "--repo", repo,
        "--add-label", label,
    ])


# ---------- comment rendering & posting ----------

_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}", re.IGNORECASE)


def render_comment(template_text: str, context: dict[str, str]) -> str:
    """Substitute {placeholder} tokens in template_text from context.

    Unknown placeholders are left untouched (safer than KeyError in production
    runs over evolving cache files).
    """

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        return str(context[key]) if key in context else match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, template_text)


def post_comment(
    repo: str,
    pr_number: int,
    body: str,
    runner: Any = None,
) -> str:
    """Post a comment using `gh pr comment --body-file <tmp>`.

    A tmp file is used to avoid shell-escaping headaches with multi-line content.
    Caller is responsible for the tmp dir lifetime; this function creates and
    removes its own. `runner` defaults to this module's `_run_gh`, resolved
    lazily so tests can patch `decision_io._run_gh`.
    """
    if runner is None:
        runner = _run_gh
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".md", prefix=f"prj-decision-{pr_number}-",
        encoding="utf-8",
    ) as fh:
        fh.write(body)
        tmp_path = fh.name
    try:
        return runner([
            "pr", "comment", str(pr_number),
            "--repo", repo,
            "--body-file", tmp_path,
        ])
    finally:
        try:
            Path(tmp_path).unlink()
        except FileNotFoundError:
            pass


# ---------- decisions.log JSONL append ----------

def append_decisions_log(cache_dir: Path, entry: dict[str, Any]) -> Path:
    """Append a JSONL line to decisions.log under cache_dir.

    Adds `ts` (UTC ISO) if missing. The file is created if it does not exist.
    Returns the path written.
    """
    if "ts" not in entry:
        entry["ts"] = datetime.now(timezone.utc).isoformat()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "decisions.log"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True))
        fh.write("\n")
    return path


# ---------- state transition (pure) ----------

def transition_state(
    state: dict[str, Any],
    pr_number: int,
    decision_class: str,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    """Mutate state for one PR per the decision-to-phase matrix.

    Returns (state, transition_status) where transition_status is one of:
      - 'transitioned'        phase changed
      - 'cleared-next-action' request-changes, no phase change
      - 'already-decided'     PR already in a terminal phase
      - 'unknown-pr'          PR not in state.prs

    State is mutated in place; the same dict is returned for ergonomic chaining.
    """
    now = now or datetime.now(timezone.utc)
    key = str(pr_number)
    if key not in state.get("prs", {}):
        return state, "unknown-pr"

    pr = state["prs"][key]
    current_phase = pr.get("phase")

    if current_phase in state_io.TERMINAL_PHASES:
        # Special case: close-as-not-now from Blocked is a legal transition.
        if not (current_phase == "Blocked" and decision_class == "close-as-not-now"):
            return state, "already-decided"

    if decision_class == "request-changes":
        pr["next_action"] = None
        pr["last_action_at"] = now.isoformat()
        return state, "cleared-next-action"

    target_phase = DECISION_TO_PHASE.get(decision_class)
    if not target_phase:
        return state, "unknown-decision"

    pr["phase"] = target_phase
    pr["phase_entered_at"] = now.isoformat()
    pr["last_action_at"] = now.isoformat()
    pr["next_action"] = None
    return state, "transitioned"


# ---------- CLI ----------

def _cmd_apply_label(args: argparse.Namespace) -> int:
    out = apply_label(args.repo, args.pr, args.decision_class)
    print(out)
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    template_text = Path(args.template).read_text(encoding="utf-8")
    context = json.loads(args.context)
    print(render_comment(template_text, context))
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    cache = Path(args.cache_dir)
    entry = json.loads(args.entry)
    path = append_decisions_log(cache, entry)
    print(json.dumps({"status": "ok", "path": str(path)}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_label = sub.add_parser("apply-label", help="Apply prj/decision:{class} label")
    p_label.add_argument("--repo", required=True)
    p_label.add_argument("--pr", type=int, required=True)
    p_label.add_argument("--decision-class", required=True,
                         choices=("ready-to-merge", "request-changes", "close-as-not-now"))
    p_label.set_defaults(func=_cmd_apply_label)

    p_render = sub.add_parser("render", help="Render the decision comment template")
    p_render.add_argument("--template", required=True)
    p_render.add_argument("--context", required=True, help="JSON-encoded dict")
    p_render.set_defaults(func=_cmd_render)

    p_log = sub.add_parser("log", help="Append a JSONL line to decisions.log")
    p_log.add_argument("--cache-dir", required=True)
    p_log.add_argument("--entry", required=True, help="JSON-encoded dict")
    p_log.set_defaults(func=_cmd_log)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
