#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Fetch the PR context needed for a code review.

Wraps `gh pr view` and `gh pr diff`. All subprocess calls go through one seam
(`_run_gh`) so tests can patch it cleanly. The module-level functions are the
contract; `GhFetcher` is a convenience facade that pre-binds the repo.

Importable: fetch_pr_view, fetch_pr_diff, fetch_changed_files, extract_acceptance_criteria,
GhFetcher, GhFetchError.

Exit codes for the CLI:
  0  success
  1  gh failure
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


class GhFetchError(RuntimeError):
    """Raised when gh exits non-zero or returns unparseable output."""


def _run_gh(args: list[str], timeout: int = 60) -> str:
    """Single seam for `gh` invocation. Tests patch this."""
    binary = shutil.which("gh") or "gh"
    cmd = [binary, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise GhFetchError(f"gh CLI not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhFetchError(f"gh timed out after {timeout}s: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise GhFetchError(f"gh exited {proc.returncode}: {tail}")
    return proc.stdout


def _parse_json(raw: str, context: str) -> Any:
    raw = raw.strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GhFetchError(f"gh returned non-JSON for {context}: {exc}") from exc


# `## Acceptance Criteria` block, captures all lines until the next heading or EOF.
_AC_HEADING_RE = re.compile(
    r"^##+\s*(?:acceptance criteria|acceptance-criteria|ac)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def extract_acceptance_criteria(body: str) -> list[str]:
    """Pull bullet items out of a `## Acceptance Criteria` section.

    Returns a list of cleaned strings. Empty list if no AC section is present.
    Accepts `## AC`, `## Acceptance Criteria`, `## Acceptance-Criteria` (case-insensitive).
    """
    if not body:
        return []
    match = _AC_HEADING_RE.search(body)
    if not match:
        return []
    rest = body[match.end():]
    # Stop at the next top-level or higher-level heading (`#` or `##`).
    end_match = re.search(r"^##?\s+\S", rest, re.MULTILINE)
    section = rest[: end_match.start()] if end_match else rest
    items: list[str] = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Bullet items: -, *, +, or numbered.
        bullet = re.match(r"^(?:[-*+]|\d+[.)])\s+(.+)$", line)
        if bullet:
            items.append(bullet.group(1).strip())
    return items


def fetch_pr_view(repo: str, pr_number: int) -> dict[str, Any]:
    """Return the structured PR view payload (title, body, refs, author, files)."""
    if not repo:
        raise GhFetchError("repo is required")
    raw = _run_gh([
        "pr", "view", str(pr_number),
        "--repo", repo,
        "--json", "number,title,body,baseRefName,headRefName,author,files,state,url",
    ])
    data = _parse_json(raw, f"pr view #{pr_number}")
    if not isinstance(data, dict):
        raise GhFetchError(f"pr view returned non-object: {type(data).__name__}")
    return data


def fetch_pr_diff(repo: str, pr_number: int) -> str:
    """Return the unified diff for the PR (entire diff, not per-file)."""
    if not repo:
        raise GhFetchError("repo is required")
    return _run_gh([
        "pr", "diff", str(pr_number),
        "--repo", repo,
    ])


# Heuristic binary-extension list. We do not stream file content for these.
_BINARY_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a",
    ".class", ".jar", ".wasm",
    ".mp3", ".mp4", ".mov", ".webm", ".wav",
})

# Cap how much of any single file we ship into the LLM context, to keep the
# prompt size sane on large files. The LLM still sees the full diff.
_FILE_BYTE_CAP = 256_000


def _is_binary(path: str) -> bool:
    lower = path.lower()
    for ext in _BINARY_EXTS:
        if lower.endswith(ext):
            return True
    return False


def fetch_changed_files(
    repo: str,
    pr_number: int,
    head_ref: str,
    file_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return [{path, status, content}] for each file in the PR.

    `file_entries` comes from the `files` array of `gh pr view`. We pull each
    file's content at the PR's head ref via `gh api /repos/.../contents`.
    """
    results: list[dict[str, Any]] = []
    for entry in file_entries:
        path = entry.get("path") or entry.get("filename") or ""
        status = entry.get("status") or "modified"
        if not path:
            continue
        if _is_binary(path) or status == "removed":
            results.append({"path": path, "status": status, "content": None})
            continue
        try:
            raw = _run_gh([
                "api", f"/repos/{repo}/contents/{path}",
                "-H", "Accept: application/vnd.github.raw",
                "-f", f"ref={head_ref}",
            ])
        except GhFetchError as exc:
            results.append({
                "path": path,
                "status": status,
                "content": None,
                "fetch_error": str(exc),
            })
            continue
        if len(raw) > _FILE_BYTE_CAP:
            raw = raw[:_FILE_BYTE_CAP] + "\n...[truncated]\n"
        results.append({"path": path, "status": status, "content": raw})
    return results


@dataclass
class PrContext:
    """Bundled review context produced by GhFetcher.fetch_all."""

    pr: dict[str, Any]
    diff: str
    files: list[dict[str, Any]]
    acceptance_criteria: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "diff": self.diff,
            "files": self.files,
            "acceptance_criteria": self.acceptance_criteria,
        }


class GhFetcher:
    """Repo-bound facade for the module-level functions."""

    def __init__(self, repo: str):
        if not repo:
            raise GhFetchError("GhFetcher requires a non-empty repo")
        self.repo = repo

    def fetch_all(self, pr_number: int) -> PrContext:
        view = fetch_pr_view(self.repo, pr_number)
        diff = fetch_pr_diff(self.repo, pr_number)
        head_ref = view.get("headRefName") or ""
        file_entries = view.get("files") or []
        files = fetch_changed_files(self.repo, pr_number, head_ref, file_entries)
        ac = extract_acceptance_criteria(view.get("body") or "")
        return PrContext(pr=view, diff=diff, files=files, acceptance_criteria=ac)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int, help="PR number")
    parser.add_argument(
        "--no-files",
        action="store_true",
        help="Skip per-file content fetch (faster, just metadata + diff)",
    )
    args = parser.parse_args()

    try:
        fetcher = GhFetcher(args.repo)
        if args.no_files:
            view = fetch_pr_view(args.repo, args.pr)
            diff = fetch_pr_diff(args.repo, args.pr)
            payload = {
                "pr": view,
                "diff": diff,
                "files": [],
                "acceptance_criteria": extract_acceptance_criteria(view.get("body") or ""),
            }
        else:
            payload = fetcher.fetch_all(args.pr).to_dict()
    except GhFetchError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
