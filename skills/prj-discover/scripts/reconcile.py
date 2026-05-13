#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Reconcile fresh GitHub state against the persisted PR Jangler queue.

Pure functions: no I/O, no subprocess, no clocks unless injected. This makes
the diff fully unit-testable.

Inputs:
  - `state` dict matching state-schema.json (read via state_io.load_state)
  - `open_prs`: list returned by gh_client.list_open_prs
  - `details_by_number`: {pr_number: gh pr view payload} so reconcile can
    compute new-comment deltas
  - `now`: datetime injected by the caller for deterministic tests
  - `stale_days`: idle threshold (default 14)

Returns:
  - mutated state (operates in place, callers pass a copy if they need
    rollback semantics)
  - a `ReconcileReport` with per-change line items for the run-log

Importable: reconcile, ReconcileReport, IDLE_DAYS_DEFAULT.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sibling import: state_io is owned by prj-orchestrator. Add its scripts dir
# to sys.path so we can `import state_io` cleanly without copy-paste. The path
# walks from this file: scripts/ -> prj-discover/ -> skills/ -> prj-orchestrator/scripts.
_SIBLING = Path(__file__).resolve().parent.parent.parent / "prj-orchestrator" / "scripts"
if _SIBLING.exists() and str(_SIBLING) not in sys.path:
    sys.path.insert(0, str(_SIBLING))

import state_io  # noqa: E402

IDLE_DAYS_DEFAULT = 14


@dataclass
class ReconcileReport:
    """Summary of one discovery sweep, suitable for runlog embedding."""

    new_prs: list[int] = field(default_factory=list)
    archived_prs: list[int] = field(default_factory=list)
    prs_with_new_comments: list[int] = field(default_factory=list)
    retriaged_for_idle: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "new_prs": sorted(self.new_prs),
            "archived_prs": sorted(self.archived_prs),
            "prs_with_new_comments": sorted(self.prs_with_new_comments),
            "retriaged_for_idle": sorted(self.retriaged_for_idle),
            "totals": {
                "new": len(self.new_prs),
                "archived": len(self.archived_prs),
                "new_comments": len(self.prs_with_new_comments),
                "idle_flagged": len(self.retriaged_for_idle),
            },
        }


def _now_iso(now: datetime) -> str:
    return now.isoformat()


def _author_login(pr: dict[str, Any]) -> str:
    """Pull a stable login string out of the various gh response shapes."""
    author = pr.get("author")
    if isinstance(author, dict):
        return str(author.get("login") or author.get("name") or "unknown")
    if isinstance(author, str):
        return author
    return "unknown"


def _comment_id_set(details: dict[str, Any]) -> set[str]:
    """Same logic as gh_client.collect_comment_ids but as a set, local to keep
    reconcile.py decoupled from gh_client. Tests can build details payloads
    directly without importing gh_client.
    """
    ids: set[str] = set()
    for key in ("comments", "reviews"):
        for item in details.get(key) or []:
            ident = item.get("id") or item.get("databaseId")
            if ident is not None:
                ids.add(str(ident))
    for thread in details.get("reviewThreads") or []:
        for item in thread.get("comments") or []:
            ident = item.get("id") or item.get("databaseId")
            if ident is not None:
                ids.add(str(ident))
    return ids


def _last_maintainer_activity(details: dict[str, Any], maintainer_logins: list[str] | None) -> str | None:
    """Return the most recent ISO timestamp of comment/review activity authored
    by a configured maintainer.

    Returns None if no maintainers are configured or if no maintainer activity
    is present. prj-decision's close-as-not-now gate (a) is conservative when
    this is None: it never trips.
    """
    if not maintainer_logins:
        return None
    maintainers = {m.lower() for m in maintainer_logins}
    latest: str | None = None
    for key in ("comments", "reviews"):
        for item in details.get(key) or []:
            author = item.get("author") or {}
            login = (author.get("login") if isinstance(author, dict) else author) or ""
            if login.lower() not in maintainers:
                continue
            ts = item.get("createdAt") or item.get("submittedAt") or item.get("updatedAt")
            if not ts:
                continue
            if latest is None or ts > latest:
                latest = ts
    return latest


def _days_between(iso_ts: str, now: datetime) -> int:
    if not iso_ts:
        return 0
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0, (now - ts).days)


def _add_new_pr(
    state: dict[str, Any],
    pr: dict[str, Any],
    details: dict[str, Any],
    now: datetime,
    maintainer_logins: list[str] | None = None,
) -> None:
    number = int(pr["number"])
    entry: dict[str, Any] = {
        "pr_number": number,
        "phase": "Discovered",
        "phase_entered_at": _now_iso(now),
        "last_action_at": _now_iso(now),
        "contributor_login": _author_login(pr),
        "needs_retriage": False,
        "new_comments_since_triage": 0,
        "next_action": {"skill": "prj-triage", "mode": "pr"},
        # Internal bookkeeping: track which comment IDs we have seen so the
        # next sweep can detect new ones. Schema permits additionalProperties=false
        # at the top level, so we stash this on the PR object which schema-wise
        # is only required to have a known set of fields (other props allowed).
        "seen_comment_ids": sorted(_comment_id_set(details)),
        # Comments classified by prj-triage. Difference with seen_comment_ids
        # gives the orchestrator a deterministic "next unclassified" pick.
        "triaged_comment_ids": [],
        # Latest maintainer activity, used by prj-decision's close-as-not-now
        # gate. None when no maintainers configured.
        "last_maintainer_activity_at": _last_maintainer_activity(details, maintainer_logins),
    }
    state["prs"][str(number)] = entry


def _update_existing_pr(
    pr_entry: dict[str, Any],
    details: dict[str, Any],
    now: datetime,
    maintainer_logins: list[str] | None = None,
) -> bool:
    """Return True if new comments appeared since last sync."""
    # Refresh maintainer-activity timestamp on every sweep so close-as-not-now
    # always sees fresh data. Cheap to recompute.
    pr_entry["last_maintainer_activity_at"] = _last_maintainer_activity(details, maintainer_logins)

    fresh_ids = _comment_id_set(details)
    seen = set(pr_entry.get("seen_comment_ids") or [])
    new_ids = fresh_ids - seen
    if not new_ids:
        return False
    delta = len(new_ids)
    prior = int(pr_entry.get("new_comments_since_triage") or 0)
    pr_entry["new_comments_since_triage"] = prior + delta
    pr_entry["seen_comment_ids"] = sorted(fresh_ids)
    pr_entry["last_action_at"] = _now_iso(now)
    # Only flip to comment-triage when the PR is past initial triage. If it's
    # still Discovered or Triaged, the existing next_action (PR-triage) stays
    # the right call.
    if pr_entry.get("phase") not in {"Discovered", "Triaged"}:
        pr_entry["next_action"] = {"skill": "prj-triage", "mode": "comment"}
    return True


def reconcile(
    state: dict[str, Any],
    open_prs: list[dict[str, Any]],
    details_by_number: dict[int, dict[str, Any]],
    now: datetime | None = None,
    stale_days: int = IDLE_DAYS_DEFAULT,
    maintainer_logins: list[str] | None = None,
) -> ReconcileReport:
    """Mutate `state` to reflect the fresh GitHub snapshot.

    Returns a ReconcileReport describing every change made.
    """
    now = now or datetime.now(timezone.utc)
    report = ReconcileReport()
    if "prs" not in state:
        state["prs"] = {}

    open_numbers = {int(pr["number"]) for pr in open_prs}

    # 1. New + updated PRs
    for pr in open_prs:
        number = int(pr["number"])
        key = str(number)
        details = details_by_number.get(number, {})
        if key not in state["prs"]:
            _add_new_pr(state, pr, details, now, maintainer_logins=maintainer_logins)
            report.new_prs.append(number)
            continue
        if _update_existing_pr(state["prs"][key], details, now, maintainer_logins=maintainer_logins):
            report.prs_with_new_comments.append(number)

    # 2. Closed PRs (still in state but not in fresh open list)
    for key, pr_entry in state["prs"].items():
        number = int(pr_entry["pr_number"])
        if number not in open_numbers and pr_entry["phase"] != "Archived":
            pr_entry["phase"] = "Archived"
            pr_entry["phase_entered_at"] = _now_iso(now)
            pr_entry["last_action_at"] = _now_iso(now)
            pr_entry["next_action"] = None
            report.archived_prs.append(number)

    # 3. Idle PRs (open + non-terminal + idle past threshold)
    for pr_entry in state["prs"].values():
        number = int(pr_entry["pr_number"])
        if pr_entry["phase"] in state_io.TERMINAL_PHASES:
            continue
        if number not in open_numbers:
            continue
        if pr_entry.get("needs_retriage"):
            continue
        idle = _days_between(pr_entry.get("last_action_at", ""), now)
        if idle >= stale_days:
            pr_entry["needs_retriage"] = True
            report.retriaged_for_idle.append(number)

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, help="Path to state.json snapshot")
    parser.add_argument("--open-prs", required=True, help="Path to JSON file with `gh pr list` output")
    parser.add_argument(
        "--details",
        required=True,
        help="Path to JSON file mapping {pr_number: pr_view_payload}",
    )
    parser.add_argument("--stale-days", type=int, default=IDLE_DAYS_DEFAULT)
    args = parser.parse_args()

    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    open_prs = json.loads(Path(args.open_prs).read_text(encoding="utf-8"))
    details_raw = json.loads(Path(args.details).read_text(encoding="utf-8"))
    details = {int(k): v for k, v in details_raw.items()}

    report = reconcile(state, open_prs, details, stale_days=args.stale_days)
    print(json.dumps({"state": state, "report": report.to_dict()}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
