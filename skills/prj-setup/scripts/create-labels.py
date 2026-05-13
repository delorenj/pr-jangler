#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Pre-create PR Jangler GitHub labels on the target repo.

Reads `assets/labels.txt` (or the path passed via --labels). Each non-empty,
non-comment line is `name|color|description`. Uses `gh label create`. Existing
labels with matching names are skipped (gh returns non-zero; we detect and
treat as success). Per-label failure does not abort the batch.

Outputs JSON summary. Exit code 0 if gh is reachable (regardless of per-label
results), 1 if gh is missing or repo flag is empty.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _parse_labels(path: Path) -> list[tuple[str, str, str]]:
    labels: list[tuple[str, str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        name = parts[0]
        color = parts[1]
        desc = parts[2] if len(parts) >= 3 else ""
        labels.append((name, color, desc))
    return labels


def _create_one(repo: str, name: str, color: str, desc: str) -> dict[str, Any]:
    cmd = ["gh", "label", "create", name, "--repo", repo, "--color", color]
    if desc:
        cmd.extend(["--description", desc])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return {"name": name, "status": "timeout"}
    if result.returncode == 0:
        return {"name": name, "status": "created"}
    err = (result.stderr or "").lower()
    if "already exists" in err:
        return {"name": name, "status": "skipped-existing"}
    return {"name": name, "status": "failed", "stderr": (result.stderr or "")[-300:]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name target repo")
    parser.add_argument("--labels", required=True, help="Path to labels.txt")
    args = parser.parse_args()

    if not args.repo or not args.repo.strip():
        print(json.dumps({"status": "config-error", "error": "empty --repo"}), file=sys.stderr)
        return 1
    if not shutil.which("gh"):
        print(json.dumps({"status": "gh-missing"}), file=sys.stderr)
        return 1

    labels_path = Path(args.labels)
    if not labels_path.exists():
        print(json.dumps({"status": "labels-file-missing", "path": str(labels_path)}), file=sys.stderr)
        return 1

    labels = _parse_labels(labels_path)
    results = [_create_one(args.repo, n, c, d) for n, c, d in labels]
    totals = {
        "created": sum(1 for r in results if r["status"] == "created"),
        "skipped_existing": sum(1 for r in results if r["status"] == "skipped-existing"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "timeout": sum(1 for r in results if r["status"] == "timeout"),
    }
    print(json.dumps({"repo": args.repo, "totals": totals, "results": results}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
