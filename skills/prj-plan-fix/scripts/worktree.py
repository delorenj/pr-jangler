#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Worktree management for prj-plan-fix.

Reuses an existing per-PR worktree at
`{project-root}/_bmad-output/pr-workflow/worktrees/{n}/` when it is present
and on the right branch. Provisions a fresh worktree via `gh pr checkout`
when absent.

NOTE: a near-identical worktree module lives (or will live) in prj-verify-claim.
Duplicated here intentionally for v1 to avoid brittle cross-skill imports
during parallel build. Consolidate once both skills land.

CLI: `python3 worktree.py ensure --pr-number 42 [--project-root <path>]`
Importable: `ensure_worktree`, `worktree_path`, `WorktreeError`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    """Raised when a worktree cannot be reused or provisioned."""


@dataclass
class WorktreeResult:
    path: Path
    reused: bool
    branch: str | None

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "reused": self.reused,
            "branch": self.branch,
        }


def worktree_path(project_root: Path, pr_number: int) -> Path:
    """Return the canonical worktree path for a PR."""
    return (
        project_root
        / "_bmad-output"
        / "pr-workflow"
        / "worktrees"
        / str(pr_number)
    )


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess command and capture output."""
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )


def _current_branch(path: Path) -> str | None:
    """Return the current branch name of a checkout, or None if undetectable."""
    if not (path / ".git").exists() and not (path / ".git").is_file():
        return None
    result = _run(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"])
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def _is_usable_worktree(path: Path) -> bool:
    """A worktree is usable if it exists and has a .git linkfile/dir."""
    if not path.is_dir():
        return False
    git_marker = path / ".git"
    return git_marker.exists()


def ensure_worktree(
    project_root: Path,
    pr_number: int,
    *,
    expected_branch: str | None = None,
    gh_checkout: bool = True,
    _runner=_run,
) -> WorktreeResult:
    """Reuse existing worktree if present and (optionally) on expected_branch.

    Otherwise provision a fresh worktree via `gh pr checkout {n}`.

    The `_runner` kwarg exists for test injection. Production callers should
    leave it at its default.
    """
    wt = worktree_path(project_root, pr_number)

    if _is_usable_worktree(wt):
        # Reuse path. Branch check is best-effort: if expected_branch given,
        # require match; otherwise accept whatever is checked out.
        current = _current_branch(wt)
        if expected_branch and current and current != expected_branch:
            # Wrong branch checked out at the reuse path. Refuse rather than
            # silently wipe — caller decides.
            raise WorktreeError(
                f"worktree at {wt} is on branch {current!r}, expected {expected_branch!r}"
            )
        return WorktreeResult(path=wt, reused=True, branch=current)

    # Provision fresh.
    wt.parent.mkdir(parents=True, exist_ok=True)
    if not gh_checkout:
        # Test path: caller has already pre-staged the worktree dir.
        raise WorktreeError(
            f"no worktree at {wt} and gh_checkout disabled"
        )
    # `gh pr checkout` requires a git repo cwd. Use the project_root.
    if not (project_root / ".git").exists():
        raise WorktreeError(
            f"project root {project_root} is not a git repo; cannot gh pr checkout"
        )
    # Use `git worktree add` semantics under the hood by leveraging gh's
    # ability to clone into a new directory via `--detach` is not supported;
    # the canonical move is `gh pr checkout {n} --branch <local>` inside a
    # fresh `git worktree add`. We do it in two steps for clarity.
    add = _runner(
        ["git", "-C", str(project_root), "worktree", "add", str(wt), "HEAD"],
    )
    if add.returncode != 0:
        raise WorktreeError(
            f"git worktree add failed: {add.stderr.strip() or add.stdout.strip()}"
        )
    co = _runner(
        ["gh", "pr", "checkout", str(pr_number)],
        cwd=wt,
    )
    if co.returncode != 0:
        # Clean up the half-provisioned worktree to keep the cache tidy.
        _runner(
            ["git", "-C", str(project_root), "worktree", "remove", "--force", str(wt)],
        )
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
        raise WorktreeError(
            f"gh pr checkout {pr_number} failed: {co.stderr.strip() or co.stdout.strip()}"
        )

    branch = _current_branch(wt)
    return WorktreeResult(path=wt, reused=False, branch=branch)


# ---------- CLI surface ----------

def _cmd_ensure(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve() if args.project_root else Path.cwd()
    try:
        result = ensure_worktree(
            project_root,
            args.pr_number,
            expected_branch=args.expected_branch,
            gh_checkout=not args.no_gh,
        )
    except WorktreeError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", **result.to_dict()}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ensure = sub.add_parser(
        "ensure", help="Reuse-or-provision the per-PR worktree"
    )
    p_ensure.add_argument("--pr-number", type=int, required=True)
    p_ensure.add_argument("--project-root", help="Override project root", default=None)
    p_ensure.add_argument(
        "--expected-branch",
        help="If set, require the reused worktree to be on this branch",
        default=None,
    )
    p_ensure.add_argument(
        "--no-gh",
        action="store_true",
        help="Do not run `gh pr checkout` (debug only)",
    )
    p_ensure.set_defaults(func=_cmd_ensure)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
