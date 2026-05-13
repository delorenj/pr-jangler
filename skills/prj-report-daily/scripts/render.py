#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Render the daily report into subject + HTML + plain-text payloads.

Pure functions. No I/O except reading the template file. The template uses
`string.Template` with `$var` placeholders; Jinja2 is deliberately avoided
to stay stdlib-only.

Importable: TEMPLATE_PATH, render_subject, render_html, render_plain,
render_report, RenderError.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "assets" / "email-template.html"

# Detect $var or ${var} placeholders. Allow $$ as literal $.
_PLACEHOLDER_RE = re.compile(r"(?<!\$)\$(?:\w+|\{\w+\})")


class RenderError(RuntimeError):
    """Raised when template substitution leaves unresolved placeholders or
    when an aggregate is structurally incomplete."""


def _safe(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def render_subject(
    aggregate: dict[str, Any],
    repo: str,
    now: datetime,
) -> str:
    date_str = now.astimezone().strftime("%Y-%m-%d")
    if aggregate["all_quiet"]:
        return f"[PR Jangler] {date_str}: all quiet"
    n = len(aggregate["groups"]["needs_attention"])
    m = len(aggregate["groups"]["in_progress"])
    return f"[PR Jangler] {date_str}: {n} need attention, {m} in progress"


def _pr_url(repo: str, pr_number: int) -> str:
    return f"https://github.com/{repo}/pull/{pr_number}"


def _format_pr_row_html(pr: dict[str, Any], repo: str) -> str:
    num = pr.get("pr_number", "?")
    contributor = pr.get("contributor_login", "unknown")
    phase = pr.get("phase", "?")
    title = pr.get("title", "") or _meta_title(pr)
    title_html = _safe(title) if title else _safe(f"PR #{num}")
    url = _pr_url(repo, num) if isinstance(num, int) else "#"
    return (
        f"<li style='margin:0 0 10px 0;font-size:14px;line-height:1.5;'>"
        f"<a href='{_safe(url)}' style='color:#0b3d91;text-decoration:none;font-weight:600;'>"
        f"#{_safe(num)}</a> &middot; <span style='color:#52606d;'>{_safe(phase)}</span> "
        f"&middot; <span style='color:#52606d;'>@{_safe(contributor)}</span><br>"
        f"<span style='color:#1f2933;'>{title_html}</span>"
        f"</li>"
    )


def _meta_title(pr: dict[str, Any]) -> str:
    """Pull title from per-PR cache enrichment if state lacks it."""
    return ""


def _format_pr_row_plain(pr: dict[str, Any], repo: str) -> str:
    num = pr.get("pr_number", "?")
    contributor = pr.get("contributor_login", "unknown")
    phase = pr.get("phase", "?")
    title = pr.get("title", "") or ""
    url = _pr_url(repo, num) if isinstance(num, int) else ""
    return f"- #{num} [{phase}] @{contributor} {title}\n  {url}".rstrip()


def _format_bucket_html(
    title: str,
    prs: list[dict[str, Any]],
    repo: str,
    color: str,
) -> str:
    if not prs:
        return ""
    items = "\n".join(_format_pr_row_html(pr, repo) for pr in prs)
    return (
        f"<h2 style='margin:24px 0 8px 0;font-size:16px;color:{color};"
        f"border-bottom:1px solid #e4e7eb;padding-bottom:4px;'>{html.escape(title)} "
        f"({len(prs)})</h2>"
        f"<ul style='margin:0;padding-left:20px;'>{items}</ul>"
    )


def _format_bucket_plain(title: str, prs: list[dict[str, Any]], repo: str) -> str:
    if not prs:
        return ""
    rows = "\n".join(_format_pr_row_plain(pr, repo) for pr in prs)
    return f"\n== {title} ({len(prs)}) ==\n{rows}\n"


def _format_shortlist_html(shortlist: dict[str, Any], repo: str) -> str:
    items = shortlist["items"]
    if not items:
        return ""
    rows = "\n".join(_format_pr_row_html(pr, repo) for pr in items)
    overflow_html = ""
    if shortlist["overflow"]:
        overflow_html = (
            f"<p style='margin:8px 0 0 0;font-size:13px;color:#52606d;font-style:italic;'>"
            f"+ {shortlist['overflow']} more, see archive."
            f"</p>"
        )
    return (
        f"<div style='background:#fff7e6;border-left:4px solid #f0b429;"
        f"padding:12px 16px;margin:0 0 16px 0;border-radius:4px;'>"
        f"<h2 style='margin:0 0 8px 0;font-size:16px;color:#8d5b00;'>"
        f"Needs your eyes</h2>"
        f"<ul style='margin:0;padding-left:20px;'>{rows}</ul>"
        f"{overflow_html}"
        f"</div>"
    )


def _format_shortlist_plain(shortlist: dict[str, Any], repo: str) -> str:
    items = shortlist["items"]
    if not items:
        return ""
    rows = "\n".join(_format_pr_row_plain(pr, repo) for pr in items)
    out = f"\n** NEEDS YOUR EYES **\n{rows}\n"
    if shortlist["overflow"]:
        out += f"+ {shortlist['overflow']} more, see archive.\n"
    return out


def _intro_text(aggregate: dict[str, Any]) -> str:
    if aggregate["all_quiet"]:
        return (
            "All quiet over the last 24 hours. No PRs need your attention, "
            "no fix-PRs landed, no triage events. This message is the bot's "
            "daily liveness confirmation."
        )
    n = len(aggregate["groups"]["needs_attention"])
    m = len(aggregate["groups"]["in_progress"])
    r = len(aggregate["groups"]["resolved_today"])
    return (
        f"{n} PR(s) need your attention, {m} in progress, "
        f"{r} resolved in the last 24 hours."
    )


def _read_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def render_html(
    aggregate: dict[str, Any],
    repo: str,
    now: datetime,
    archive_path: str,
    template_text: str | None = None,
) -> str:
    template_text = template_text if template_text is not None else _read_template()
    template = Template(template_text)

    groups = aggregate["groups"]
    shortlist_block = _format_shortlist_html(aggregate["shortlist"], repo)
    needs_block = _format_bucket_html("Needs attention", groups["needs_attention"], repo, "#c92a2a")
    in_prog_block = _format_bucket_html("In progress", groups["in_progress"], repo, "#0b3d91")
    resolved_block = _format_bucket_html("Resolved today", groups["resolved_today"], repo, "#2b8a3e")
    no_change_count = len(groups["no_change"])
    no_change_block = ""
    if no_change_count:
        no_change_block = (
            f"<p style='margin:24px 0 0 0;font-size:13px;color:#7b8794;'>"
            f"{no_change_count} PR(s) unchanged in the last 24 hours. See archive for the roster."
            f"</p>"
        )

    subject = render_subject(aggregate, repo, now)
    mapping = {
        "subject": _safe(subject),
        "report_date": _safe(now.astimezone().strftime("%Y-%m-%d")),
        "repo": _safe(repo),
        "intro": _safe(_intro_text(aggregate)),
        "shortlist_block": shortlist_block,
        "needs_attention_block": needs_block,
        "in_progress_block": in_prog_block,
        "resolved_today_block": resolved_block,
        "no_change_block": no_change_block,
        "generated_at": _safe(now.astimezone().strftime("%Y-%m-%d %H:%M %Z")),
        "archive_path": _safe(archive_path),
    }
    output = template.safe_substitute(mapping)
    unresolved = _PLACEHOLDER_RE.findall(output)
    if unresolved:
        raise RenderError(
            f"Template substitution left unresolved placeholders: {sorted(set(unresolved))}"
        )
    return output


def render_plain(
    aggregate: dict[str, Any],
    repo: str,
    now: datetime,
    archive_path: str,
) -> str:
    groups = aggregate["groups"]
    lines: list[str] = []
    lines.append(f"PR Jangler Daily Report -- {now.astimezone().strftime('%Y-%m-%d')}")
    lines.append(f"repo: {repo}")
    lines.append("")
    lines.append(_intro_text(aggregate))

    lines.append(_format_shortlist_plain(aggregate["shortlist"], repo))
    lines.append(_format_bucket_plain("Needs attention", groups["needs_attention"], repo))
    lines.append(_format_bucket_plain("In progress", groups["in_progress"], repo))
    lines.append(_format_bucket_plain("Resolved today", groups["resolved_today"], repo))
    if groups["no_change"]:
        lines.append(
            f"\n{len(groups['no_change'])} PR(s) unchanged in the last 24 hours. "
            f"See archive for the roster."
        )
    lines.append("")
    lines.append(f"Generated {now.astimezone().strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append(f"Archive: {archive_path}")
    return "\n".join(line for line in lines if line is not None)


def render_report(
    aggregate: dict[str, Any],
    repo: str,
    now: datetime,
    archive_path: str,
    template_text: str | None = None,
) -> dict[str, str]:
    """Return a dict with subject, html, plain. Raises RenderError on bad templates."""
    subject = render_subject(aggregate, repo, now)
    html_body = render_html(aggregate, repo, now, archive_path, template_text=template_text)
    plain_body = render_plain(aggregate, repo, now, archive_path)
    return {"subject": subject, "html": html_body, "plain": plain_body}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aggregate", required=True, help="Path to JSON file with aggregate dict")
    parser.add_argument("--repo", required=True, help="GitHub repo slug for links")
    parser.add_argument("--archive-path", default="(none)", help="Archive path string to embed")
    parser.add_argument("--now", help="ISO timestamp", default=None)
    args = parser.parse_args()

    aggregate = json.loads(Path(args.aggregate).read_text(encoding="utf-8"))
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    rendered = render_report(aggregate, args.repo, now, args.archive_path)
    print(json.dumps({
        "subject": rendered["subject"],
        "html_bytes": len(rendered["html"]),
        "plain_bytes": len(rendered["plain"]),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
