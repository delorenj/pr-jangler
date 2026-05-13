#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Initialize the PR Jangler workflow directory tree and empty state.json.

Creates (idempotently):
  {project-root}/_bmad-output/pr-workflow/
                            state.json    (empty schema, repo populated)
                            prs/
                            logs/
                            reports/
                            worktrees/

Skips files and dirs that already exist; never overwrites.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _empty_state(repo: str) -> dict[str, Any]:
    return {
        "version": "1.0",
        "repo": repo,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "heartbeat_count": 0,
        "last_report_sent": None,
        "prs": {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, help="Absolute path to the project root")
    parser.add_argument("--repo", required=True, help="Repo in owner/name format (written into state.json)")
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    base = root / "_bmad-output" / "pr-workflow"
    dirs = [base, base / "prs", base / "logs", base / "reports", base / "worktrees"]
    created_dirs: list[str] = []
    for d in dirs:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created_dirs.append(str(d))

    state_path = base / "state.json"
    state_status: str
    if state_path.exists():
        state_status = "skipped-existing"
    else:
        with state_path.open("w", encoding="utf-8") as fh:
            json.dump(_empty_state(args.repo), fh, indent=2, sort_keys=True)
        state_status = "created"

    print(json.dumps({
        "status": "ok",
        "base": str(base),
        "directories_created": created_dirs,
        "state_json": {"path": str(state_path), "status": state_status},
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
