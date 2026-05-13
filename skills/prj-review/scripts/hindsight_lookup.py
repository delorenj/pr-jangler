#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Best-effort lookup of project conventions from the Hindsight `prj` bank.

If the `hindsight` CLI is missing, the bank is empty, or the recall call exits
non-zero for any reason, this module returns an empty list and writes a
warning. A Hindsight outage never blocks a review.

Importable: lookup_conventions, HindsightLookupResult.

CLI: lookup_conventions emits the result as JSON on stdout.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

DEFAULT_BANK = "prj"
DEFAULT_BUDGET = "mid"


@dataclass
class HindsightLookupResult:
    """Result of one best-effort recall."""

    bank: str
    query: str
    excerpts: list[str]
    available: bool
    warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "bank": self.bank,
            "query": self.query,
            "excerpts": list(self.excerpts),
            "available": self.available,
            "warning": self.warning,
        }


def _run_hindsight(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Single seam for the hindsight subprocess. Tests patch this."""
    binary = shutil.which("hindsight")
    if binary is None:
        return -1, "", "hindsight CLI not found on PATH"
    cmd = [binary, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return -2, "", f"hindsight timed out after {timeout}s"
    return proc.returncode, proc.stdout, proc.stderr


def _parse_excerpts(raw: str) -> list[str]:
    """Pull a flat list of strings out of varied hindsight output shapes.

    Hindsight may return:
      - JSON list of strings
      - JSON list of {content: ...} objects
      - JSON object with "results" or "memories" or "items" key
      - Plain text, one excerpt per line
    """
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [line.strip() for line in raw.splitlines() if line.strip()]
    return _flatten_excerpts(data)


def _flatten_excerpts(data: Any) -> list[str]:
    out: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                text = item.get("content") or item.get("text") or item.get("memory") or ""
                if isinstance(text, str) and text.strip():
                    out.append(text.strip())
        return out
    if isinstance(data, dict):
        for key in ("results", "memories", "items", "excerpts"):
            if key in data:
                return _flatten_excerpts(data[key])
        return []
    if isinstance(data, str):
        return [data]
    return out


def lookup_conventions(
    query: str,
    bank: str = DEFAULT_BANK,
    budget: str = DEFAULT_BUDGET,
) -> HindsightLookupResult:
    """Recall `query` from the named hindsight bank. Never raises."""
    if not query.strip():
        return HindsightLookupResult(
            bank=bank, query=query, excerpts=[],
            available=False, warning="empty query, skipped",
        )
    rc, stdout, stderr = _run_hindsight([
        "memory", "recall", bank, query, "--budget", budget,
    ])
    if rc == -1:
        return HindsightLookupResult(
            bank=bank, query=query, excerpts=[],
            available=False, warning="hindsight CLI not found on PATH",
        )
    if rc == -2:
        return HindsightLookupResult(
            bank=bank, query=query, excerpts=[],
            available=False, warning="hindsight recall timed out",
        )
    if rc != 0:
        tail = (stderr or stdout or "").strip()[-300:]
        return HindsightLookupResult(
            bank=bank, query=query, excerpts=[],
            available=False,
            warning=f"hindsight recall exited {rc}: {tail}",
        )
    excerpts = _parse_excerpts(stdout)
    return HindsightLookupResult(
        bank=bank, query=query, excerpts=excerpts, available=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True, help="Recall query string")
    parser.add_argument("--bank", default=DEFAULT_BANK)
    parser.add_argument("--budget", default=DEFAULT_BUDGET)
    args = parser.parse_args()
    result = lookup_conventions(args.query, bank=args.bank, budget=args.budget)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
