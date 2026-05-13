#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Validate findings, render review.md, and transition state to Reviewed.

The findings schema is a hard contract: every finding must be a dict with
exactly the keys {file, line, severity, category, claim, suggested_fix}, with
severity in {blocker, major, minor, nit}. Validation rejects on the first
malformed entry and reports its index + reason.

Importable: validate_findings, render_review_markdown, write_review,
transition_to_reviewed, ReviewIoError, VALID_SEVERITIES, REQUIRED_KEYS.

CLI subcommands: validate, render, write, transition.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sibling import: state_io lives under prj-orchestrator/scripts.
_SIBLING = Path(__file__).resolve().parent.parent.parent / "prj-orchestrator" / "scripts"
if _SIBLING.exists() and str(_SIBLING) not in sys.path:
    sys.path.insert(0, str(_SIBLING))

import state_io  # noqa: E402

VALID_SEVERITIES = frozenset({"blocker", "major", "minor", "nit"})
REQUIRED_KEYS = frozenset({"file", "line", "severity", "category", "claim", "suggested_fix"})

# Severity sort order for stable rendering: blocker first, nit last.
_SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}


class ReviewIoError(ValueError):
    """Raised when a finding fails schema validation or persistence fails."""


def validate_findings(findings: Any) -> list[dict[str, Any]]:
    """Raise ReviewIoError if findings is malformed; otherwise return it unchanged.

    Rules:
    - Top-level must be a list.
    - Each item must be a dict with EXACTLY the REQUIRED_KEYS set.
    - `severity` must be in VALID_SEVERITIES.
    - `line` must be a positive integer.
    - All string fields must be non-empty strings.
    """
    if not isinstance(findings, list):
        raise ReviewIoError(
            f"findings must be a list, got {type(findings).__name__}"
        )
    for idx, item in enumerate(findings):
        if not isinstance(item, dict):
            raise ReviewIoError(
                f"finding[{idx}] must be an object, got {type(item).__name__}"
            )
        keys = set(item.keys())
        missing = REQUIRED_KEYS - keys
        extra = keys - REQUIRED_KEYS
        if missing:
            raise ReviewIoError(
                f"finding[{idx}] missing required keys: {sorted(missing)}"
            )
        if extra:
            raise ReviewIoError(
                f"finding[{idx}] has unexpected keys: {sorted(extra)}"
            )
        if item["severity"] not in VALID_SEVERITIES:
            raise ReviewIoError(
                f"finding[{idx}] invalid severity {item['severity']!r}; "
                f"must be one of {sorted(VALID_SEVERITIES)}"
            )
        line = item["line"]
        if not isinstance(line, int) or isinstance(line, bool) or line <= 0:
            raise ReviewIoError(
                f"finding[{idx}] line must be a positive int, got {line!r}"
            )
        for key in ("file", "category", "claim", "suggested_fix"):
            value = item[key]
            if not isinstance(value, str) or not value.strip():
                raise ReviewIoError(
                    f"finding[{idx}] {key} must be a non-empty string"
                )
    return findings


def _sort_key(finding: dict[str, Any]) -> tuple[int, str, int]:
    return (
        _SEVERITY_ORDER.get(finding["severity"], 99),
        finding["file"],
        finding["line"],
    )


def _count_by_severity(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {s: 0 for s in ("blocker", "major", "minor", "nit")}
    for finding in findings:
        counts[finding["severity"]] += 1
    return counts


def render_review_markdown(
    pr_number: int,
    findings: list[dict[str, Any]],
    summary: str = "",
    pr_title: str = "",
    timestamp: datetime | None = None,
) -> str:
    """Return the rendered review.md content. Pure function (no I/O)."""
    findings = validate_findings(findings)
    sorted_findings = sorted(findings, key=_sort_key)
    counts = _count_by_severity(sorted_findings)
    ts = (timestamp or datetime.now(timezone.utc)).isoformat()

    lines: list[str] = []
    title = pr_title.strip() if pr_title else f"PR #{pr_number}"
    lines.append(f"# Review: {title} (#{pr_number})")
    lines.append("")
    lines.append(f"_Generated: {ts}_")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(summary.strip() if summary.strip() else "_No summary provided._")
    lines.append("")
    lines.append("## Counts")
    lines.append("")
    lines.append(
        f"- blocker: {counts['blocker']}"
    )
    lines.append(f"- major: {counts['major']}")
    lines.append(f"- minor: {counts['minor']}")
    lines.append(f"- nit: {counts['nit']}")
    lines.append("")
    lines.append("## Findings")
    lines.append("")
    if not sorted_findings:
        lines.append("_No findings. The reviewer found nothing actionable._")
        lines.append("")
        return "\n".join(lines)

    for finding in sorted_findings:
        lines.append(
            f"### [{finding['severity']}] {finding['category']} — "
            f"`{finding['file']}:{finding['line']}`"
        )
        lines.append("")
        lines.append(f"**Claim:** {finding['claim'].strip()}")
        lines.append("")
        lines.append(f"**Suggested fix:** {finding['suggested_fix'].strip()}")
        lines.append("")
    return "\n".join(lines)


def review_path(project_root: Path, pr_number: int) -> Path:
    return (
        project_root
        / "_bmad-output"
        / "pr-workflow"
        / "prs"
        / str(pr_number)
        / "review.md"
    )


def write_review(
    project_root: Path,
    pr_number: int,
    findings: list[dict[str, Any]],
    summary: str = "",
    pr_title: str = "",
    timestamp: datetime | None = None,
) -> Path:
    """Validate, render, and write `review.md`. Returns the written path."""
    rendered = render_review_markdown(
        pr_number=pr_number,
        findings=findings,
        summary=summary,
        pr_title=pr_title,
        timestamp=timestamp,
    )
    path = review_path(project_root, pr_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    return path


def transition_to_reviewed(
    project_root: Path,
    pr_number: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Move PR `pr_number` to phase=Reviewed and queue prj-decision next.

    Returns the updated PR entry. Raises ReviewIoError if state or PR is missing.
    """
    now = now or datetime.now(timezone.utc)
    state = state_io.load_state(project_root)
    key = str(pr_number)
    if key not in state.get("prs", {}):
        raise ReviewIoError(
            f"state has no PR entry for #{pr_number}; cannot transition"
        )
    pr_entry = state["prs"][key]
    pr_entry["phase"] = "Reviewed"
    pr_entry["phase_entered_at"] = now.isoformat()
    pr_entry["last_action_at"] = now.isoformat()
    pr_entry["next_action"] = {"skill": "prj-decision", "mode": None}
    state_io.save_state(project_root, state)
    return pr_entry


def _cmd_validate(args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        print(json.dumps({"status": "error", "error": "no stdin"}), file=sys.stderr)
        return 1
    data = json.loads(raw)
    try:
        validate_findings(data)
    except ReviewIoError as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}))
        return 1
    print(json.dumps({"status": "ok", "count": len(data)}))
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    data = json.loads(raw)
    findings = data.get("findings", [])
    summary = data.get("summary", "")
    pr_title = data.get("pr_title", "")
    try:
        out = render_review_markdown(
            pr_number=args.pr,
            findings=findings,
            summary=summary,
            pr_title=pr_title,
        )
    except ReviewIoError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(out)
    return 0


def _cmd_write(args: argparse.Namespace) -> int:
    project_root = (
        Path(args.project_root) if args.project_root else state_io.find_project_root()
    )
    raw = sys.stdin.read()
    data = json.loads(raw)
    try:
        path = write_review(
            project_root=project_root,
            pr_number=args.pr,
            findings=data.get("findings", []),
            summary=data.get("summary", ""),
            pr_title=data.get("pr_title", ""),
        )
    except ReviewIoError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "path": str(path)}))
    return 0


def _cmd_transition(args: argparse.Namespace) -> int:
    project_root = (
        Path(args.project_root) if args.project_root else state_io.find_project_root()
    )
    try:
        pr_entry = transition_to_reviewed(project_root, args.pr)
    except ReviewIoError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "pr": pr_entry}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="Override project root", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="Validate findings JSON from stdin")
    p_val.set_defaults(func=_cmd_validate)

    p_ren = sub.add_parser("render", help="Render review.md to stdout")
    p_ren.add_argument("--pr", required=True, type=int)
    p_ren.set_defaults(func=_cmd_render)

    p_wri = sub.add_parser("write", help="Validate + render + write review.md")
    p_wri.add_argument("--pr", required=True, type=int)
    p_wri.set_defaults(func=_cmd_write)

    p_tra = sub.add_parser("transition", help="Set PR to phase=Reviewed")
    p_tra.add_argument("--pr", required=True, type=int)
    p_tra.set_defaults(func=_cmd_transition)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
