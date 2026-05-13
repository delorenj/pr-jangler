#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""File-overlap scan for prj-detect-overlap.

Pure-ish module:
  - `compute_pairs(target_files, others, threshold=2)` is a pure function over
    file lists. No subprocess, no clocks, fully unit-testable.
  - `fetch_changed_files(repo, pr_number)` wraps `gh pr diff --name-only`. The
    single seam tests patch (`_run_gh`) keeps subprocess use isolated.

Importable: OverlapPair, OverlapScanError, compute_pairs, fetch_changed_files,
collect_other_pr_numbers, scan.

The CLI surface is for debugging only; run.py calls the functions directly.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OVERLAP_THRESHOLD_DEFAULT = 2


class OverlapScanError(RuntimeError):
    """Raised when `gh` returns a non-zero exit code or output cannot be parsed."""


@dataclass(frozen=True)
class OverlapPair:
    """One scored overlap pair, target vs other."""

    target_pr: int
    other_pr: int
    shared_files: tuple[str, ...]
    overlap_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_pr": self.target_pr,
            "other_pr": self.other_pr,
            "shared_files": list(self.shared_files),
            "overlap_count": self.overlap_count,
        }


@dataclass
class ScanReport:
    """Full scan outcome: pairs that hit threshold, plus skipped sub-threshold pairs."""

    target_pr: int
    target_files: tuple[str, ...]
    pairs: list[OverlapPair] = field(default_factory=list)
    skipped: list[OverlapPair] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_pr": self.target_pr,
            "target_files": list(self.target_files),
            "pairs": [p.to_dict() for p in self.pairs],
            "skipped": [p.to_dict() for p in self.skipped],
            "totals": {
                "pairs_over_threshold": len(self.pairs),
                "pairs_skipped": len(self.skipped),
            },
        }


def _run_gh(args: list[str], timeout: int = 60) -> str:
    """Single subprocess seam. Tests patch this to inject fake gh output."""
    binary = shutil.which("gh") or "gh"
    cmd = [binary, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise OverlapScanError(f"gh CLI not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise OverlapScanError(
            f"gh CLI timed out after {timeout}s: {' '.join(cmd)}"
        ) from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise OverlapScanError(f"gh exited {proc.returncode}: {tail}")
    return proc.stdout


def fetch_changed_files(repo: str, pr_number: int) -> list[str]:
    """Return the list of files changed by `pr_number` via `gh pr diff --name-only`.

    Strips blank lines. The order returned by `gh` is preserved.
    """
    if not repo:
        raise OverlapScanError("repo must be non-empty (owner/name)")
    raw = _run_gh([
        "pr", "diff", str(pr_number),
        "--repo", repo,
        "--name-only",
    ])
    files: list[str] = []
    for line in raw.splitlines():
        cleaned = line.strip()
        if cleaned:
            files.append(cleaned)
    return files


def collect_other_pr_numbers(state: dict[str, Any], target_pr: int) -> list[int]:
    """Return every non-terminal open-PR number in state other than the target.

    Skips PRs in terminal phases (ReadyToMerge, Blocked, Rejected, Archived).
    Sorted ascending for determinism.
    """
    others: list[int] = []
    terminal = {"ReadyToMerge", "Blocked", "Rejected", "Archived"}
    for key, pr in state.get("prs", {}).items():
        try:
            number = int(pr.get("pr_number") or key)
        except (TypeError, ValueError):
            continue
        if number == target_pr:
            continue
        if pr.get("phase") in terminal:
            continue
        others.append(number)
    return sorted(others)


def compute_pairs(
    target_pr: int,
    target_files: list[str],
    other_files_by_pr: dict[int, list[str]],
    threshold: int = OVERLAP_THRESHOLD_DEFAULT,
) -> ScanReport:
    """Compute overlap pairs between the target PR and each other PR.

    Pairs are sorted by overlap_count descending, then by other_pr ascending
    for stable tie-breaking. Sub-threshold pairs (overlap < threshold) are
    captured under `skipped` so the runlog can show them.
    """
    target_set = set(target_files)
    report = ScanReport(target_pr=target_pr, target_files=tuple(target_files))
    for other_pr in sorted(other_files_by_pr):
        other_set = set(other_files_by_pr[other_pr])
        shared = sorted(target_set & other_set)
        if not shared:
            continue
        pair = OverlapPair(
            target_pr=target_pr,
            other_pr=other_pr,
            shared_files=tuple(shared),
            overlap_count=len(shared),
        )
        if pair.overlap_count >= threshold:
            report.pairs.append(pair)
        else:
            report.skipped.append(pair)
    report.pairs.sort(key=lambda p: (-p.overlap_count, p.other_pr))
    return report


def scan(
    repo: str,
    target_pr: int,
    other_pr_numbers: list[int],
    threshold: int = OVERLAP_THRESHOLD_DEFAULT,
) -> ScanReport:
    """End-to-end scan: fetch every diff via `gh`, compute pairs."""
    target_files = fetch_changed_files(repo, target_pr)
    other_files_by_pr: dict[int, list[str]] = {}
    for number in other_pr_numbers:
        other_files_by_pr[number] = fetch_changed_files(repo, number)
    return compute_pairs(target_pr, target_files, other_files_by_pr, threshold)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Repo in owner/name format")
    parser.add_argument("--pr-number", type=int, required=True, help="Target PR number")
    parser.add_argument(
        "--others",
        required=True,
        help="Comma-separated list of other open PR numbers to compare against",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=OVERLAP_THRESHOLD_DEFAULT,
        help="Minimum shared file count to surface (default: 2)",
    )
    args = parser.parse_args()

    other_numbers: list[int] = []
    for token in args.others.split(","):
        token = token.strip()
        if token:
            other_numbers.append(int(token))
    try:
        report = scan(args.repo, args.pr_number, other_numbers, args.threshold)
    except OverlapScanError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
