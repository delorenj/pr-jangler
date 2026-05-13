#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Aggregate state + per-PR cache + runlog into report-ready buckets.

Pure functions: no I/O for the core grouping logic (`group_prs`,
`build_shortlist`, `is_all_quiet`). The I/O surface (`load_runlog_window`,
`gather_pr_context`) is small and deterministic so callers can substitute
fakes in tests.

Importable: SHORTLIST_CAP, NEEDS_ATTENTION_PHASES, IN_PROGRESS_PHASES,
TERMINAL_PHASES, group_prs, build_shortlist, is_all_quiet,
gather_pr_context, load_runlog_window, build_aggregate.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Sibling import: state_io is owned by prj-orchestrator.
_SIBLING = Path(__file__).resolve().parent.parent.parent / "prj-orchestrator" / "scripts"
if _SIBLING.exists() and str(_SIBLING) not in sys.path:
    sys.path.insert(0, str(_SIBLING))

import state_io  # noqa: E402

SHORTLIST_CAP = 5

# A PR in PleaseAdvise needs eyes. A PR in FixImpl has an auto-fix opened.
NEEDS_ATTENTION_PHASES = frozenset({"PleaseAdvise", "FixImpl"})
IN_PROGRESS_PHASES = frozenset({
    "Discovered", "Triaged", "OverlapCheck", "ReviewPending", "Reviewed",
    "CommentsTriage", "ClaimVerify", "FixPlan", "AdversarialCheck",
})
TERMINAL_PHASES = state_io.TERMINAL_PHASES  # ReadyToMerge, Blocked, Rejected, Archived


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _within_last_24h(ts: str | None, now: datetime) -> bool:
    parsed = _parse_iso(ts)
    if not parsed:
        return False
    return (now - parsed) <= timedelta(hours=24)


def group_prs(state: dict[str, Any], now: datetime) -> dict[str, list[dict[str, Any]]]:
    """Group PRs by report bucket. Returns a dict with four lists.

    Each list element is the raw PR dict from state["prs"]. Sorting is
    stable and human-meaningful: needs-attention by phase_entered_at asc,
    resolved-today by last_action_at desc, in-progress by phase_entered_at asc.
    no-change is summary-only (just the list, never deeply rendered).
    """
    needs_attention: list[dict[str, Any]] = []
    in_progress: list[dict[str, Any]] = []
    resolved_today: list[dict[str, Any]] = []
    no_change: list[dict[str, Any]] = []

    for pr_key, pr in state.get("prs", {}).items():
        # Synthetic system entry (pr_number == 0): surfaces as needs-attention only.
        if pr.get("pr_number") == 0 or pr_key in ("0", "__system__"):
            if pr.get("phase") == "PleaseAdvise":
                needs_attention.append(pr)
            continue

        phase = pr.get("phase")
        if phase in NEEDS_ATTENTION_PHASES:
            needs_attention.append(pr)
            continue
        if phase in TERMINAL_PHASES:
            if _within_last_24h(pr.get("last_action_at"), now):
                resolved_today.append(pr)
            continue
        if phase in IN_PROGRESS_PHASES:
            if _within_last_24h(pr.get("last_action_at"), now):
                in_progress.append(pr)
            else:
                no_change.append(pr)
            continue
        # Unknown phase falls through to no_change (defensive).
        no_change.append(pr)

    needs_attention.sort(key=lambda p: (
        0 if p.get("phase") == "PleaseAdvise" else 1,
        p.get("phase_entered_at", ""),
    ))
    in_progress.sort(key=lambda p: p.get("phase_entered_at", ""))
    resolved_today.sort(key=lambda p: p.get("last_action_at", ""), reverse=True)
    no_change.sort(key=lambda p: p.get("pr_number", 0))

    return {
        "needs_attention": needs_attention,
        "in_progress": in_progress,
        "resolved_today": resolved_today,
        "no_change": no_change,
    }


def build_shortlist(needs_attention: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the top SHORTLIST_CAP entries; report overflow."""
    top = needs_attention[:SHORTLIST_CAP]
    overflow = max(0, len(needs_attention) - SHORTLIST_CAP)
    return {"items": top, "overflow": overflow}


def is_all_quiet(
    groups: dict[str, list[dict[str, Any]]],
    runlog_entries: list[dict[str, Any]],
) -> bool:
    """All-quiet means no needs-attention, no resolved-today, AND no
    in-progress dispatch activity in the last 24h.
    """
    if groups["needs_attention"]:
        return False
    if groups["resolved_today"]:
        return False
    # Look for per-PR dispatch activity in the runlog. System actions (discover,
    # report-daily) do not count toward "something happened".
    per_pr_skills = {
        "prj-triage", "prj-verify-claim", "prj-review", "prj-detect-overlap",
        "prj-plan-fix", "prj-implement-fix", "prj-validate-adversarial",
        "prj-decision",
    }
    for entry in runlog_entries:
        if entry.get("action") != "dispatch":
            continue
        if entry.get("skill") in per_pr_skills and entry.get("status") == "ok":
            return False
    return True


def gather_pr_context(project_root: Path, pr_number: int) -> dict[str, Any]:
    """Read per-PR cache for inline rendering. Tolerant of missing files."""
    cache = project_root / "_bmad-output" / "pr-workflow" / "prs" / str(pr_number)
    context: dict[str, Any] = {"pr_number": pr_number}
    meta = cache / "meta.json"
    if meta.exists():
        try:
            context["meta"] = json.loads(meta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            context["meta"] = {}
    for name in ("review.md", "verification.md", "fix-plan.md",
                 "adversarial.md", "implementation.md", "triage.md"):
        path = cache / name
        if path.exists():
            try:
                context[name] = path.read_text(encoding="utf-8")
            except OSError:
                pass
    decisions = cache / "decisions.log"
    if decisions.exists():
        try:
            context["decisions"] = decisions.read_text(encoding="utf-8")
        except OSError:
            pass
    return context


def load_runlog_window(project_root: Path, now: datetime) -> list[dict[str, Any]]:
    """Read runlog lines from today and yesterday; filter to last 24h."""
    log_dir = project_root / "_bmad-output" / "pr-workflow" / "logs"
    if not log_dir.is_dir():
        return []
    cutoff = now - timedelta(hours=24)
    entries: list[dict[str, Any]] = []
    # Today + yesterday cover the full 24-hour window regardless of TZ.
    candidates = []
    for delta in (0, 1):
        day = (now - timedelta(days=delta)).strftime("%Y-%m-%d")
        candidates.append(log_dir / f"{day}.jsonl")
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_iso(entry.get("ts"))
            if ts and ts >= cutoff:
                entries.append(entry)
    return entries


def build_aggregate(
    state: dict[str, Any],
    runlog_entries: list[dict[str, Any]],
    project_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Top-level aggregation. Wraps grouping + shortlist + per-PR enrichment."""
    now = now or datetime.now(timezone.utc)
    groups = group_prs(state, now)
    shortlist = build_shortlist(groups["needs_attention"])
    all_quiet = is_all_quiet(groups, runlog_entries)

    # Optional per-PR enrichment (only when project_root provided).
    enrichment: dict[int, dict[str, Any]] = {}
    if project_root is not None:
        for bucket in ("needs_attention", "in_progress", "resolved_today"):
            for pr in groups[bucket]:
                num = pr.get("pr_number")
                if isinstance(num, int) and num not in enrichment:
                    enrichment[num] = gather_pr_context(project_root, num)

    return {
        "groups": groups,
        "shortlist": shortlist,
        "all_quiet": all_quiet,
        "enrichment": enrichment,
        "runlog_entry_count": len(runlog_entries),
        "generated_at": now.isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="Override project root", default=None)
    parser.add_argument("--now", help="ISO timestamp for testing", default=None)
    args = parser.parse_args()

    project_root = (
        Path(args.project_root) if args.project_root else state_io.find_project_root()
    )
    state = state_io.load_state(project_root)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    runlog = load_runlog_window(project_root, now)
    agg = build_aggregate(state, runlog, project_root, now)
    summary = {
        "needs_attention": len(agg["groups"]["needs_attention"]),
        "in_progress": len(agg["groups"]["in_progress"]),
        "resolved_today": len(agg["groups"]["resolved_today"]),
        "no_change": len(agg["groups"]["no_change"]),
        "shortlist_count": len(agg["shortlist"]["items"]),
        "shortlist_overflow": agg["shortlist"]["overflow"],
        "all_quiet": agg["all_quiet"],
        "runlog_entry_count": agg["runlog_entry_count"],
        "generated_at": agg["generated_at"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
