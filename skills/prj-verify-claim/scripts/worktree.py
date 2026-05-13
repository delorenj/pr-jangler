#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Worktree provisioning for prj-verify-claim.

A verification needs an isolated checkout of the PR branch so reproduction work
does not touch the maintainer's working tree. This module wraps the `gh pr
checkout` subprocess and the cleanup half of the lifecycle.

Importable:
  - WORKTREE_ROOT_REL: relative path under {project-root} where worktrees live
  - WorktreeError
  - WorktreeResult (dataclass)
  - worktree_path
  - provision_worktree
  - cleanup_worktree

CLI subcommands: provision, cleanup, path.

The module exposes a single subprocess seam (`_run`) so tests can patch it
without touching the network or the host filesystem outside a tmpdir.
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


WORKTREE_ROOT_REL = ("_bmad-output", "pr-workflow", "worktrees")


class WorktreeError(RuntimeError):
    """Raised when worktree provisioning or cleanup fails."""


@dataclass(frozen=True)
class WorktreeResult:
    """Outcome of a worktree provisioning attempt."""

    pr_number: int
    path: Path
    created: bool
    reused: bool
    refreshed: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr_number": self.pr_number,
            "path": str(self.path),
            "created": self.created,
            "reused": self.reused,
            "refreshed": self.refreshed,
            "notes": list(self.notes),
        }


def worktree_path(project_root: Path, pr_number: int) -> Path:
    """Return the deterministic worktree path for a PR."""
    if pr_number <= 0:
        raise WorktreeError(f"pr_number must be a positive int, got {pr_number!r}")
    return project_root.joinpath(*WORKTREE_ROOT_REL, str(pr_number))


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Single subprocess seam. Tests patch this.

    Returns (returncode, stdout, stderr). Raises WorktreeError only on
    FileNotFoundError or TimeoutExpired (i.e. the process never produced an
    exit code). Non-zero exit codes are returned verbatim for the caller to
    inspect.
    """
    if not cmd:
        raise WorktreeError("empty command")
    binary = shutil.which(cmd[0]) or cmd[0]
    try:
        proc = subprocess.run(
            [binary, *cmd[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorktreeError(f"{cmd[0]} not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(
            f"{cmd[0]} timed out after {timeout}s: {' '.join(cmd)}"
        ) from exc
    return proc.returncode, proc.stdout, proc.stderr


def _ensure_root(project_root: Path) -> Path:
    """Ensure the worktrees root directory exists. Returns its path."""
    root = project_root.joinpath(*WORKTREE_ROOT_REL)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _looks_like_existing_worktree(path: Path) -> bool:
    """Heuristic: a populated worktree has a .git file or .git dir at its root.

    `gh pr checkout` produces a .git FILE (worktree-style) when run inside an
    existing repo with `git worktree add`, and a .git DIR when it does a full
    clone. Either is fine for our purposes.
    """
    return path.exists() and (path / ".git").exists()


def provision_worktree(
    project_root: Path,
    pr_number: int,
    repo: str,
    refresh: bool = False,
) -> WorktreeResult:
    """Provision an isolated worktree for the PR at `_bmad-output/.../{n}/`.

    Behavior:
      - If no worktree directory exists: run `gh pr checkout` into it.
      - If a worktree directory exists AND looks valid: reuse it (no-op),
        unless `refresh=True` in which case run `git fetch` + `git checkout`
        to bring the branch up to date.
      - If a worktree directory exists but does NOT look valid: raise
        WorktreeError (caller must clean up by hand; we do not silently rm).

    `repo` is the `owner/name` slug passed to `gh pr checkout --repo`.
    """
    if not repo:
        raise WorktreeError("repo must be non-empty (owner/name)")
    _ensure_root(project_root)
    target = worktree_path(project_root, pr_number)
    notes: list[str] = []

    if target.exists():
        if not _looks_like_existing_worktree(target):
            raise WorktreeError(
                f"worktree path {target} exists but is not a valid checkout; "
                "refusing to overwrite. Remove it by hand and retry."
            )
        if not refresh:
            notes.append("reused existing worktree without refresh")
            return WorktreeResult(
                pr_number=pr_number,
                path=target,
                created=False,
                reused=True,
                refreshed=False,
                notes=notes,
            )
        rc, stdout, stderr = _run(["git", "fetch", "origin"], cwd=target)
        if rc != 0:
            raise WorktreeError(
                f"git fetch failed in {target}: {(stderr or stdout).strip()[-500:]}"
            )
        notes.append("git fetch origin succeeded")
        return WorktreeResult(
            pr_number=pr_number,
            path=target,
            created=False,
            reused=True,
            refreshed=True,
            notes=notes,
        )

    # Fresh provision via gh pr checkout. Pass --force so a stale partial
    # checkout does not block us; we already verified `target` does not
    # exist, so --force only affects branch-state inside the new clone.
    cmd = [
        "gh", "pr", "checkout", str(pr_number),
        "--repo", repo,
        "--force",
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    rc, stdout, stderr = _run(cmd, cwd=target.parent)
    if rc != 0:
        tail = (stderr or stdout).strip()[-500:]
        raise WorktreeError(
            f"gh pr checkout #{pr_number} exited {rc}: {tail}"
        )
    notes.append(f"gh pr checkout #{pr_number} succeeded")
    # gh pr checkout creates a subdirectory whose name is the PR's
    # head ref slug, not the PR number. We need a stable name, so we
    # rename the most-recently-created child of target.parent.
    candidates = sorted(
        (
            p for p in target.parent.iterdir()
            if p.is_dir() and p.name != target.name and not p.name.isdigit()
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        # `gh pr checkout` may have checked out into the parent directly when
        # the parent looks like a git working tree. We treat that as an error
        # because we need a per-PR isolated path.
        raise WorktreeError(
            f"gh pr checkout produced no directory under {target.parent}; "
            "cannot establish isolated worktree."
        )
    chosen = candidates[0]
    chosen.rename(target)
    notes.append(f"renamed {chosen.name} -> {target.name}")

    return WorktreeResult(
        pr_number=pr_number,
        path=target,
        created=True,
        reused=False,
        refreshed=False,
        notes=notes,
    )


def cleanup_worktree(
    project_root: Path,
    pr_number: int,
    force: bool = False,
) -> dict[str, Any]:
    """Remove the worktree directory for a PR.

    With `force=False` (default), only proceeds if the target looks like a
    valid worktree (.git present). With `force=True`, removes whatever is
    at the target path unconditionally.

    Returns a dict describing the outcome. Never raises on a missing
    directory; that is a successful no-op.
    """
    target = worktree_path(project_root, pr_number)
    if not target.exists():
        return {"pr_number": pr_number, "path": str(target), "status": "absent"}
    if not force and not _looks_like_existing_worktree(target):
        return {
            "pr_number": pr_number,
            "path": str(target),
            "status": "skipped-not-a-worktree",
        }
    shutil.rmtree(target)
    return {"pr_number": pr_number, "path": str(target), "status": "removed"}


# ---------- CLI surface ----------

def _resolve_project_root(arg_value: str | None) -> Path:
    """Resolve --project-root, falling back to state_io.find_project_root."""
    if arg_value:
        return Path(arg_value).resolve()
    # Lazy import to keep the CLI usable without state_io on path for tests.
    sibling = Path(__file__).resolve().parent.parent.parent / "prj-orchestrator" / "scripts"
    if sibling.exists() and str(sibling) not in sys.path:
        sys.path.insert(0, str(sibling))
    import state_io  # noqa: WPS433 (intentional local import)
    return state_io.find_project_root()


def _cmd_path(args: argparse.Namespace) -> int:
    root = _resolve_project_root(args.project_root)
    print(str(worktree_path(root, args.pr_number)))
    return 0


def _cmd_provision(args: argparse.Namespace) -> int:
    root = _resolve_project_root(args.project_root)
    try:
        result = provision_worktree(root, args.pr_number, args.repo, refresh=args.refresh)
    except WorktreeError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


def _cmd_cleanup(args: argparse.Namespace) -> int:
    root = _resolve_project_root(args.project_root)
    result = cleanup_worktree(root, args.pr_number, force=args.force)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="Override project root", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_path = sub.add_parser("path", help="Print the deterministic worktree path for a PR")
    p_path.add_argument("--pr-number", required=True, type=int)
    p_path.set_defaults(func=_cmd_path)

    p_prov = sub.add_parser("provision", help="Provision an isolated worktree via gh pr checkout")
    p_prov.add_argument("--pr-number", required=True, type=int)
    p_prov.add_argument("--repo", required=True, help="Repo slug owner/name")
    p_prov.add_argument(
        "--refresh", action="store_true",
        help="If worktree exists, run git fetch origin to bring branch current",
    )
    p_prov.set_defaults(func=_cmd_provision)

    p_clean = sub.add_parser("cleanup", help="Remove the worktree directory")
    p_clean.add_argument("--pr-number", required=True, type=int)
    p_clean.add_argument(
        "--force", action="store_true",
        help="Remove even if the target does not look like a valid worktree",
    )
    p_clean.set_defaults(func=_cmd_cleanup)

    args = parser.parse_args()
    rc = args.func(args)
    sys.exit(rc)


if __name__ == "__main__":
    main()
