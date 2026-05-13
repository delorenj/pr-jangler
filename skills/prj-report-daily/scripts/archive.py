#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Persist the rendered daily report to a markdown archive.

The archive doubles as audit trail: every send (and every dry-run render)
writes a frozen copy of subject + HTML body + plain-text body. Filename is
`{YYYY-MM-DD}.md` to align with the run-log convention.

Importable: archive_path_for, write_archive.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def archive_path_for(project_root: Path, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    date_str = now.astimezone().strftime("%Y-%m-%d")
    return project_root / "_bmad-output" / "pr-workflow" / "reports" / f"{date_str}.md"


def write_archive(
    project_root: Path,
    *,
    subject: str,
    html_body: str,
    plain_body: str,
    now: datetime | None = None,
) -> Path:
    """Write the archive file and return its absolute path."""
    path = archive_path_for(project_root, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    contents = (
        f"# {subject}\n\n"
        f"_Generated {(now or datetime.now(timezone.utc)).astimezone().isoformat()}_\n\n"
        f"## Plain-text body\n\n"
        f"```\n{plain_body}\n```\n\n"
        f"## HTML body\n\n"
        f"<!-- The HTML below is the exact body sent over SMTP. -->\n\n"
        f"{html_body}\n"
    )
    path.write_text(contents, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--html", required=True, help="Path to HTML body file")
    parser.add_argument("--plain", required=True, help="Path to plain-text body file")
    parser.add_argument("--now", help="ISO timestamp", default=None)
    args = parser.parse_args()

    root = Path(args.project_root)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    html_body = Path(args.html).read_text(encoding="utf-8")
    plain_body = Path(args.plain).read_text(encoding="utf-8")
    path = write_archive(
        root,
        subject=args.subject,
        html_body=html_body,
        plain_body=plain_body,
        now=now,
    )
    print(json.dumps({"status": "ok", "archive_path": str(path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
