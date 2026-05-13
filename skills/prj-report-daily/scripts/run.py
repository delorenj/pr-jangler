#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""prj-report-daily entry point.

One report cycle:
  1. Load config + state.
  2. Validate required keys (prj_repo, prj_email_to, prj_email_from,
     prj_smtp_host, prj_smtp_port, prj_smtp_creds_ref). Missing -> exit 2.
  3. Aggregate state + per-PR caches + last-24h runlog.
  4. Render subject + HTML + plain-text payloads.
  5. Write archive markdown file (always, even on dry-run).
  6. Resolve SMTP creds via `op read` (skipped in dry-run).
  7. Send via smtplib SMTP+STARTTLS (skipped in dry-run).
  8. On success, update state.last_report_sent and reset send-failure counter.
  9. On SMTP failure, bump report_send_failures; if it hits 3, inject a
     synthetic __system__ PleaseAdvise entry.
 10. Append a structured run-log entry.

Exit codes:
  0  success (including dry-run)
  2  misconfigured (required config key missing)
  3  unexpected error (state load / aggregation / rendering)
  4  credential resolution failure
  5  SMTP send failure
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sibling import path setup: aggregate.py already inserts state_io's parent
# onto sys.path, so importing it first means subsequent siblings resolve.
import aggregate
import archive
from creds import CredsError, resolve_smtp_creds
from render import RenderError, render_report
from smtp_send import SmtpSendError, send_report

import state_io  # noqa: E402

REQUIRED_CONFIG_KEYS = (
    "prj_repo",
    "prj_email_to",
    "prj_email_from",
    "prj_smtp_host",
    "prj_smtp_port",
    "prj_smtp_creds_ref",
)

SYNTHETIC_SYSTEM_KEY = "0"
SEND_FAILURE_ESCALATION_THRESHOLD = 3


def load_prj_config(project_root: Path) -> dict[str, Any]:
    """Read [modules.prj] from _bmad/config.toml. Defaults applied for non-required keys."""
    import tomllib

    path = project_root / "_bmad" / "config.toml"
    defaults: dict[str, Any] = {
        "prj_smtp_port": 587,
    }
    cfg = dict(defaults)
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        cfg.update(data.get("modules", {}).get("prj", {}))
    return cfg


def _validate_required(config: dict[str, Any]) -> str | None:
    """Return the name of the first missing required key, or None."""
    for key in REQUIRED_CONFIG_KEYS:
        value = config.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            return key
    return None


def _bump_send_failures(state: dict[str, Any]) -> int:
    count = int(state.get("report_send_failures", 0)) + 1
    state["report_send_failures"] = count
    return count


def _inject_system_please_advise(state: dict[str, Any], reason: str, now: datetime) -> None:
    """Add or update the synthetic __system__ PR entry to surface a failure
    in the next successful report.
    """
    ts = now.isoformat()
    state["prs"][SYNTHETIC_SYSTEM_KEY] = {
        "pr_number": 0,
        "phase": "PleaseAdvise",
        "phase_entered_at": ts,
        "last_action_at": ts,
        "contributor_login": "prj-report-daily",
        "title": f"SMTP send failed {SEND_FAILURE_ESCALATION_THRESHOLD}x: {reason}",
    }


def _runlog_skeleton(run_id: str, repo: str) -> dict[str, Any]:
    return {
        "action": "report-daily",
        "run_id": run_id,
        "skill": "prj-report-daily",
        "repo": repo,
    }


def _emit_runlog(project_root: Path, entry: dict[str, Any]) -> None:
    state_io.append_runlog(project_root, entry)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="Override project root", default=None)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Render and archive without sending or updating last_report_sent",
    )
    parser.add_argument("--verbose", action="store_true", help="Emit diagnostics to stderr")
    parser.add_argument("--now", help="ISO timestamp for deterministic testing", default=None)
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:12]
    started = time.monotonic()

    try:
        project_root = (
            Path(args.project_root) if args.project_root else state_io.find_project_root()
        )
    except RuntimeError as exc:
        print(json.dumps({"status": "no-project-root", "error": str(exc)}), file=sys.stderr)
        return 3

    if args.verbose:
        print(f"[run {run_id}] project_root={project_root}", file=sys.stderr)

    config = load_prj_config(project_root)
    missing = _validate_required(config)
    if missing:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": "report-daily",
            "skill": "prj-report-daily",
            "run_id": run_id,
            "status": "misconfigured",
            "reason": f"required config key missing: {missing}",
        }
        _emit_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 2

    repo = str(config["prj_repo"]).strip()
    log = _runlog_skeleton(run_id, repo)

    # State load (or init if absent).
    if not state_io.state_path(project_root).exists():
        state_io.init_state(project_root, repo)
    try:
        state = state_io.load_state(project_root)
    except Exception as exc:  # pragma: no cover -- defensive
        entry = {**log, "status": "state-error", "error": str(exc)}
        _emit_runlog(project_root, entry)
        return 3

    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    try:
        runlog_window = aggregate.load_runlog_window(project_root, now)
        agg = aggregate.build_aggregate(state, runlog_window, project_root, now)
    except Exception as exc:
        entry = {**log, "status": "aggregate-error", "error": str(exc)}
        _emit_runlog(project_root, entry)
        return 3

    archive_path = archive.archive_path_for(project_root, now)
    try:
        rendered = render_report(agg, repo, now, str(archive_path))
    except RenderError as exc:
        entry = {**log, "status": "render-error", "error": str(exc)}
        _emit_runlog(project_root, entry)
        return 3

    # Archive first so a forensic copy exists even if SMTP fails.
    try:
        archive.write_archive(
            project_root,
            subject=rendered["subject"],
            html_body=rendered["html"],
            plain_body=rendered["plain"],
            now=now,
        )
    except OSError as exc:
        entry = {**log, "status": "archive-error", "error": str(exc)}
        _emit_runlog(project_root, entry)
        return 3

    if args.dry_run:
        entry = {
            **log,
            "status": "dry-run",
            "subject": rendered["subject"],
            "all_quiet": agg["all_quiet"],
            "needs_attention": len(agg["groups"]["needs_attention"]),
            "in_progress": len(agg["groups"]["in_progress"]),
            "resolved_today": len(agg["groups"]["resolved_today"]),
            "archive_path": str(archive_path),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        _emit_runlog(project_root, entry)
        if args.verbose:
            print(f"[run {run_id}] dry-run archived to {archive_path}", file=sys.stderr)
        print(json.dumps(entry, sort_keys=True))
        return 0

    # Resolve credentials.
    try:
        creds = resolve_smtp_creds(str(config["prj_smtp_creds_ref"]))
    except CredsError as exc:
        entry = {**log, "status": "creds-error", "error": str(exc)}
        _emit_runlog(project_root, entry)
        return 4

    # Send.
    try:
        send_result = send_report(
            host=str(config["prj_smtp_host"]),
            port=int(config["prj_smtp_port"]),
            username=creds.username,
            password=creds.password,
            sender=str(config["prj_email_from"]),
            recipient=str(config["prj_email_to"]),
            subject=rendered["subject"],
            plain_body=rendered["plain"],
            html_body=rendered["html"],
        )
    except SmtpSendError as exc:
        failures = _bump_send_failures(state)
        escalated = False
        if failures >= SEND_FAILURE_ESCALATION_THRESHOLD:
            _inject_system_please_advise(state, str(exc), now)
            escalated = True
            state["report_send_failures"] = 0
        try:
            state_io.save_state(project_root, state)
        except Exception:
            pass
        entry = {
            **log,
            "status": "smtp-error",
            "error": str(exc),
            "report_send_failures": failures,
            "escalated_to_please_advise": escalated,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        _emit_runlog(project_root, entry)
        return 5

    # Success: update last_report_sent and clear failure counter.
    state["last_report_sent"] = now.isoformat()
    state["report_send_failures"] = 0
    try:
        state_io.save_state(project_root, state)
    except Exception as exc:
        entry = {**log, "status": "state-save-error", "error": str(exc)}
        _emit_runlog(project_root, entry)
        return 3

    entry = {
        **log,
        "status": "ok",
        "subject": rendered["subject"],
        "all_quiet": agg["all_quiet"],
        "needs_attention": len(agg["groups"]["needs_attention"]),
        "in_progress": len(agg["groups"]["in_progress"]),
        "resolved_today": len(agg["groups"]["resolved_today"]),
        "archive_path": str(archive_path),
        "send": send_result,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    _emit_runlog(project_root, entry)
    if args.verbose:
        print(f"[run {run_id}] sent: {rendered['subject']}", file=sys.stderr)
    print(json.dumps(entry, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
