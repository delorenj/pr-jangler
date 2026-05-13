#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Thin wrapper around the `gh` CLI for prj-discover.

All calls return parsed JSON (or structured Python dicts/lists). Subprocess
failures raise GhClientError with the captured stderr tail so the caller can
distinguish transport errors from semantic ones.

Importable: list_open_prs, fetch_pr_details, fetch_pr_diff_stats,
get_rate_limit_remaining, GhClientError, GhClient.

The module is structured so unit tests can patch a single seam
(`_run_gh`) to inject fake output. No global state.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


class GhClientError(RuntimeError):
    """Raised when the gh CLI returns a non-zero exit code or unparseable output."""


def _run_gh(args: list[str], timeout: int = 60) -> str:
    """Execute `gh` with the given args. Returns stdout text.

    This is the single seam tests patch.
    """
    binary = shutil.which("gh") or "gh"
    cmd = [binary, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise GhClientError(f"gh CLI not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhClientError(f"gh CLI timed out after {timeout}s: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise GhClientError(f"gh exited {proc.returncode}: {tail}")
    return proc.stdout


def _parse_json(raw: str, context: str) -> Any:
    raw = raw.strip()
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GhClientError(f"gh returned non-JSON output for {context}: {exc}") from exc


@dataclass(frozen=True)
class RateLimitStatus:
    """Snapshot of GitHub REST core rate-limit remaining quota."""

    remaining: int
    limit: int
    deferred: bool

    def to_dict(self) -> dict[str, Any]:
        return {"remaining": self.remaining, "limit": self.limit, "deferred": self.deferred}


def get_rate_limit_remaining(min_required: int = 100) -> RateLimitStatus:
    """Query `gh api rate_limit` and return the core remaining count.

    `deferred=True` means the caller should skip the sweep this cycle.
    """
    raw = _run_gh(["api", "rate_limit"])
    data = _parse_json(raw, "rate_limit")
    if not isinstance(data, dict):
        raise GhClientError(f"rate_limit response was not an object: {type(data).__name__}")
    core = data.get("resources", {}).get("core", {}) or data.get("rate", {})
    remaining = int(core.get("remaining", 0))
    limit = int(core.get("limit", 0))
    return RateLimitStatus(
        remaining=remaining,
        limit=limit,
        deferred=remaining < min_required,
    )


def list_open_prs(repo: str) -> list[dict[str, Any]]:
    """Return the open PR list for the configured repo.

    Each item carries number, title, author.login, createdAt, headRefName, baseRefName.
    """
    if not repo:
        raise GhClientError("repo must be non-empty (owner/name)")
    raw = _run_gh([
        "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--limit", "200",
        "--json", "number,title,author,createdAt,headRefName,baseRefName",
    ])
    data = _parse_json(raw, "pr list")
    if not isinstance(data, list):
        raise GhClientError(f"pr list response was not a list: {type(data).__name__}")
    return data


def fetch_pr_details(repo: str, pr_number: int) -> dict[str, Any]:
    """Return comments, reviews, state for a single PR.

    Note: gh pr view does not expose review-thread granularity via --json.
    Discussion-level resolution lives in `reviews[].comments`. If thread-level
    state ever becomes required, switch to `gh api graphql` for that subset.
    """
    raw = _run_gh([
        "pr", "view", str(pr_number),
        "--repo", repo,
        "--json", "number,state,comments,reviews,updatedAt",
    ])
    data = _parse_json(raw, f"pr view #{pr_number}")
    if not isinstance(data, dict):
        raise GhClientError(f"pr view response was not an object: {type(data).__name__}")
    return data


# `gh pr diff --stat` writes a textual stats summary like:
#   src/foo.ts | 12 ++++++++++++
#   src/bar.ts |  3 ++-
#   2 files changed, 13 insertions(+), 2 deletions(-)
# We pluck the trailing files-changed integer. Falling back to 0 on any parse miss.
_FILES_CHANGED_RE = re.compile(r"(\d+)\s+files?\s+changed", re.IGNORECASE)


def fetch_pr_diff_stats(repo: str, pr_number: int) -> dict[str, int]:
    """Return {'files_changed': N} for the PR. Best-effort: never raises on parse."""
    try:
        raw = _run_gh([
            "pr", "diff", str(pr_number),
            "--repo", repo,
            "--stat",
        ])
    except GhClientError:
        return {"files_changed": 0}
    files_changed = 0
    for line in raw.splitlines():
        match = _FILES_CHANGED_RE.search(line)
        if match:
            files_changed = int(match.group(1))
            break
    return {"files_changed": files_changed}


def collect_comment_ids(details: dict[str, Any]) -> list[str]:
    """Extract a stable list of comment + review identifiers from a pr view payload.

    `gh pr view` returns issue-comments under `comments`, PR-level reviews under
    `reviews`, and inline review threads under `reviewThreads`. Each item has an
    `id` (string or int depending on the API surface). We coerce everything to
    string so set diffing works regardless of source.
    """
    ids: list[str] = []
    for key in ("comments", "reviews"):
        for item in details.get(key) or []:
            ident = item.get("id") or item.get("databaseId")
            if ident is not None:
                ids.append(str(ident))
    for thread in details.get("reviewThreads") or []:
        for item in thread.get("comments") or []:
            ident = item.get("id") or item.get("databaseId")
            if ident is not None:
                ids.append(str(ident))
    return ids


class GhClient:
    """Convenience facade that bundles repo context.

    The module-level functions are the underlying contract; this class exists
    so callers (run.py, tests) can hold a configured client without passing
    `repo` to every method.
    """

    def __init__(self, repo: str, min_required: int = 100):
        if not repo:
            raise GhClientError("GhClient requires a non-empty repo (owner/name)")
        self.repo = repo
        self.min_required = min_required

    def rate_limit(self) -> RateLimitStatus:
        return get_rate_limit_remaining(self.min_required)

    def list_open(self) -> list[dict[str, Any]]:
        return list_open_prs(self.repo)

    def view(self, pr_number: int) -> dict[str, Any]:
        return fetch_pr_details(self.repo, pr_number)

    def diff_stats(self, pr_number: int) -> dict[str, int]:
        return fetch_pr_diff_stats(self.repo, pr_number)


# ---------- CLI surface (debugging aid) ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Repo in owner/name format")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("rate-limit", help="Print rate_limit core remaining as JSON")
    sub.add_parser("list", help="List open PRs as JSON")

    p_view = sub.add_parser("view", help="View one PR's comment payload as JSON")
    p_view.add_argument("pr_number", type=int)

    p_stats = sub.add_parser("diff-stats", help="Print files-changed for a PR as JSON")
    p_stats.add_argument("pr_number", type=int)

    args = parser.parse_args()
    try:
        if args.cmd == "rate-limit":
            print(json.dumps(get_rate_limit_remaining().to_dict(), indent=2, sort_keys=True))
        elif args.cmd == "list":
            print(json.dumps(list_open_prs(args.repo), indent=2, sort_keys=True))
        elif args.cmd == "view":
            print(json.dumps(fetch_pr_details(args.repo, args.pr_number), indent=2, sort_keys=True))
        elif args.cmd == "diff-stats":
            print(json.dumps(fetch_pr_diff_stats(args.repo, args.pr_number), indent=2, sort_keys=True))
    except GhClientError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
