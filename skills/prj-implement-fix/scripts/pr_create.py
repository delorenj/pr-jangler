#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""GitHub PR creation with fallback to comment for prj-implement-fix.

`gh pr create` is attempted first. On failure that matches one of the
contributor-veto signatures, fall back to posting the fix-plan diff as a
comment on the ORIGINAL PR. Either path then applies the `prj/fix-proposed`
label.

Single subprocess seam: `_run_gh`. Tests patch this.

Importable: create_fix_pr, post_fallback_comment, ensure_label, apply_label,
GhCreateError, PrCreateOutcome.

CLI subcommands let an operator dry-run each step.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from branch_ops import PR_FALLBACK_PATTERNS

FALLBACK_LABEL = "prj/fix-proposed"
FALLBACK_LABEL_COLOR = "5319e7"
FALLBACK_LABEL_DESC = "PR Jangler proposed a fix"


class GhCreateError(RuntimeError):
    """Raised when both the PR-create path AND the fallback path fail."""


@dataclass
class PrCreateOutcome:
    """Result of the create-or-fallback dance."""

    path: str  # "tier-2-pr" or "tier-1-comment-fallback"
    url: str
    branch: str
    base: str
    head: str
    title: str
    fallback_reason: str | None = None
    raw: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "url": self.url,
            "branch": self.branch,
            "base": self.base,
            "head": self.head,
            "title": self.title,
            "fallback_reason": self.fallback_reason,
        }


def _run_gh(
    args: list[str],
    timeout: int = 60,
    stdin_text: str | None = None,
) -> tuple[int, str, str]:
    """Single seam for `gh` subprocess calls. Tests patch this."""
    binary = shutil.which("gh") or "gh"
    cmd = [binary, *args]
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return (-1, "", f"gh not found on PATH: {exc}")
    except subprocess.TimeoutExpired:
        return (-2, "", f"gh timed out after {timeout}s: {' '.join(cmd)}")
    return (proc.returncode, proc.stdout, proc.stderr)


def _stderr_signals_fallback(stderr: str) -> str | None:
    """Return the matched pattern (cause) if stderr signals a contributor veto."""
    lowered = stderr.lower()
    for pattern in PR_FALLBACK_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


def ensure_label(repo: str, label: str = FALLBACK_LABEL) -> None:
    """Create the label on the repo if it doesn't already exist. Idempotent."""
    rc, _, _ = _run_gh([
        "label", "create", label,
        "--repo", repo,
        "--color", FALLBACK_LABEL_COLOR,
        "--description", FALLBACK_LABEL_DESC,
        "--force",  # idempotent: --force makes existing labels reuse the spec
    ])
    # We deliberately swallow non-zero here: if the label already exists with
    # different metadata, gh exits non-zero but the label is still usable.
    # The downstream `apply_label` will fail loudly if the label is genuinely
    # unusable.
    _ = rc


def apply_label(repo: str, pr_number: int, label: str = FALLBACK_LABEL) -> None:
    """Apply the label to the original PR. Best-effort: logs but does not raise.

    We deliberately do not raise here because a label-add failure is a
    cosmetic concern, not a correctness one. The caller logs the failure.
    """
    rc, _, stderr = _run_gh([
        "pr", "edit", str(pr_number),
        "--repo", repo,
        "--add-label", label,
    ])
    if rc != 0:
        # Best-effort: surface on stderr but do not raise.
        print(
            json.dumps({
                "status": "label-warning",
                "pr": pr_number,
                "label": label,
                "stderr": stderr.strip()[-400:],
            }),
            file=sys.stderr,
        )


def create_fix_pr(
    *,
    repo: str,
    base_branch: str,
    head_branch: str,
    title: str,
    body_path: Path,
    pr_number: int,
    fix_plan_diff: str,
    token: str | None = None,
    dry_run: bool = False,
) -> PrCreateOutcome:
    """Attempt `gh pr create`; on contributor veto, post a comment instead.

    `base_branch` is the contributor's branch (their PR's head ref). `head_branch`
    is `prj/auto-fix/{n}-{slug}` on this repo. We never invert this.

    On any other failure we raise GhCreateError so the caller can record a
    `fallback-failed` run-log entry.
    """
    title = title.strip()
    if dry_run:
        return PrCreateOutcome(
            path="tier-2-pr",
            url="(dry-run)",
            branch=head_branch,
            base=base_branch,
            head=head_branch,
            title=title,
            raw={"dry_run": "true"},
        )

    # Attempt PR creation. We pass --body-file to avoid escaping issues.
    rc, stdout, stderr = _run_gh([
        "pr", "create",
        "--repo", repo,
        "--base", base_branch,
        "--head", head_branch,
        "--title", title,
        "--body-file", str(body_path),
    ])
    if rc == 0:
        url = stdout.strip().splitlines()[-1] if stdout.strip() else ""
        ensure_label(repo)
        apply_label(repo, pr_number)
        return PrCreateOutcome(
            path="tier-2-pr",
            url=url,
            branch=head_branch,
            base=base_branch,
            head=head_branch,
            title=title,
            raw={"stdout": stdout.strip()[-400:]},
        )

    fallback_cause = _stderr_signals_fallback(stderr)
    if fallback_cause is None:
        raise GhCreateError(
            f"gh pr create failed with non-fallback signature (rc={rc}): "
            f"{stderr.strip()[-500:] or stdout.strip()[-500:]}"
        )

    # Fall back: post fix-plan diff as a comment on the ORIGINAL PR.
    outcome = post_fallback_comment(
        repo=repo,
        pr_number=pr_number,
        head_branch=head_branch,
        base_branch=base_branch,
        fix_plan_diff=fix_plan_diff,
        title=title,
        fallback_reason=fallback_cause,
    )
    ensure_label(repo)
    apply_label(repo, pr_number)
    return outcome


def _format_fallback_comment(
    *,
    pr_number: int,
    head_branch: str,
    base_branch: str,
    fix_plan_diff: str,
    title: str,
    fallback_reason: str,
) -> str:
    """Build the comment body for the Tier-1 fallback path."""
    return (
        f"**PR Jangler: fix proposed inline**\n\n"
        f"I could not open a fix-PR targeting `{base_branch}` "
        f"(reason: `{fallback_reason}`), so the proposed change is posted here "
        f"for review. The fix is also available locally on branch "
        f"`{head_branch}`.\n\n"
        f"**Title:** {title}\n\n"
        f"**Diff:**\n\n"
        f"```diff\n{fix_plan_diff.rstrip()}\n```\n\n"
        f"Full provenance lives at `prs/{pr_number}/implementation.md`."
    )


def post_fallback_comment(
    *,
    repo: str,
    pr_number: int,
    head_branch: str,
    base_branch: str,
    fix_plan_diff: str,
    title: str,
    fallback_reason: str,
) -> PrCreateOutcome:
    """Post the fix-plan diff as a comment on the original PR. Raises on failure."""
    body = _format_fallback_comment(
        pr_number=pr_number,
        head_branch=head_branch,
        base_branch=base_branch,
        fix_plan_diff=fix_plan_diff,
        title=title,
        fallback_reason=fallback_reason,
    )
    rc, stdout, stderr = _run_gh(
        [
            "pr", "comment", str(pr_number),
            "--repo", repo,
            "--body-file", "-",
        ],
        stdin_text=body,
    )
    if rc != 0:
        raise GhCreateError(
            f"fallback gh pr comment failed (rc={rc}): "
            f"{stderr.strip()[-500:] or stdout.strip()[-500:]}"
        )
    comment_url = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    return PrCreateOutcome(
        path="tier-1-comment-fallback",
        url=comment_url,
        branch=head_branch,
        base=base_branch,
        head=head_branch,
        title=title,
        fallback_reason=fallback_reason,
        raw={"stdout": stdout.strip()[-400:]},
    )


# ---------- CLI surface (debug) ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_label = sub.add_parser("ensure-label", help="Create the prj/fix-proposed label")
    p_label.add_argument("--repo", required=True)

    p_apply = sub.add_parser("apply-label", help="Add the label to a PR")
    p_apply.add_argument("--repo", required=True)
    p_apply.add_argument("--pr", type=int, required=True)

    args = parser.parse_args()
    if args.cmd == "ensure-label":
        ensure_label(args.repo)
        print(json.dumps({"status": "ok", "label": FALLBACK_LABEL}))
        return 0
    if args.cmd == "apply-label":
        apply_label(args.repo, args.pr)
        print(json.dumps({"status": "ok", "pr": args.pr, "label": FALLBACK_LABEL}))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
