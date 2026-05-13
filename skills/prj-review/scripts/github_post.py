#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Post a review.md as a GitHub Comment-type PR review.

Gated by the `prj_post_review_comment` config flag (default False at v1).
When the flag is False, this module never invokes `gh`. When True, it calls
`gh pr review --comment --body-file <path>` and NEVER passes `--approve` or
`--request-changes`. Those flags are out of scope for autonomous review.

Importable: post_review_comment, GhPostError.

Exit codes for CLI:
  0  success or skipped (flag off)
  1  gh failure
  2  bad arguments / missing body file
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


class GhPostError(RuntimeError):
    """Raised when `gh pr review` fails or arguments are invalid."""


FORBIDDEN_FLAGS = frozenset({"--approve", "--request-changes", "-a", "-r"})


def _run_gh(args: list[str], timeout: int = 60) -> str:
    """Single seam for the gh subprocess. Tests patch this."""
    # Defensive guard: refuse to ever construct a review with approve / request-changes.
    for flag in args:
        if flag in FORBIDDEN_FLAGS:
            raise GhPostError(
                f"forbidden flag {flag!r} in gh pr review args; "
                "this skill only posts Comment-type reviews"
            )
    binary = shutil.which("gh") or "gh"
    cmd = [binary, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise GhPostError(f"gh CLI not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhPostError(f"gh timed out after {timeout}s: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise GhPostError(f"gh exited {proc.returncode}: {tail}")
    return proc.stdout


def post_review_comment(
    repo: str,
    pr_number: int,
    body_file: Path,
    enabled: bool,
) -> dict[str, object]:
    """Post (or skip) a Comment-type review on the PR.

    Returns a small JSON-serializable dict describing what happened:
      {"status": "posted" | "skipped" | "error", ...}

    Behaviour:
    - `enabled=False`: do nothing. Return `{"status": "skipped", "reason": "..."}`.
    - `enabled=True` but body_file missing: raise GhPostError.
    - `enabled=True` and file exists: call gh pr review --comment --body-file <path>.
    """
    if not enabled:
        return {
            "status": "skipped",
            "reason": "prj_post_review_comment=False; review.md cached only",
            "pr_number": pr_number,
        }
    if not repo:
        raise GhPostError("repo is required when prj_post_review_comment is True")
    if not body_file.exists():
        raise GhPostError(f"body file does not exist: {body_file}")

    args = [
        "pr", "review", str(pr_number),
        "--repo", repo,
        "--comment",
        "--body-file", str(body_file),
    ]
    stdout = _run_gh(args)
    return {
        "status": "posted",
        "pr_number": pr_number,
        "repo": repo,
        "stdout": stdout.strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="owner/name (required when enabled)", default="")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--body-file", required=True, type=Path)
    parser.add_argument(
        "--enabled",
        action="store_true",
        help="Actually post. Without this flag, the run is a no-op skip.",
    )
    args = parser.parse_args()
    try:
        result = post_review_comment(
            repo=args.repo,
            pr_number=args.pr,
            body_file=args.body_file,
            enabled=args.enabled,
        )
    except GhPostError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
