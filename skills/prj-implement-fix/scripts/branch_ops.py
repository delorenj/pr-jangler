#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Git operations for prj-implement-fix.

Pure-ish helpers around `git` and `git apply`. All subprocess calls flow
through `_run_git`, the single seam tests patch. Importable surface:

  - slugify(title, max_len=30)
  - build_branch_name(pr_number, title)
  - build_commit_subject(summary, pr_number)
  - build_commit_body(rationale, risks, claim_source, pr_number)
  - create_branch(repo_dir, branch, base_ref)
  - apply_diff(repo_dir, diff_text)
  - run_tests(repo_dir, test_command)
  - commit_change(repo_dir, subject, body, bot_user, bot_email, dry_run=False)
  - push_branch(repo_dir, branch, token=None, dry_run=False)
  - extract_diff_from_fix_plan(text)

NEVER force-pushes. NEVER amends. NEVER pushes to anything but
`prj/auto-fix/...`. The push helper asserts these invariants before
constructing argv.

CLI is a thin wrapper for ad-hoc debug.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# A subject line clamped to 72 chars is conventional-commit safe.
SUBJECT_MAX = 72

# Refuse to push to anything outside this prefix. Defense in depth.
SAFE_BRANCH_PREFIX = "prj/auto-fix/"

# Patterns inside `gh pr create` stderr that signal we should fall back to a
# comment instead of retrying the PR creation. Pulled here (not pr_create.py)
# so other phase skills could reuse if needed.
PR_FALLBACK_PATTERNS = (
    "branch is protected",
    "head and base ref are the same",
    "not authorized",
    "pull request creation is disabled",
    "forbidden",
    "no commits between",
)


class GitOpError(RuntimeError):
    """Raised on any git subprocess failure or invariant violation."""


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def _run_git(
    args: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
    timeout: int = 120,
) -> GitResult:
    """Single seam for git subprocess calls. Tests patch this."""
    binary = shutil.which("git") or "git"
    cmd = [binary, *args]
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=full_env,
        )
    except FileNotFoundError as exc:
        raise GitOpError(f"git not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitOpError(f"git timed out after {timeout}s: {' '.join(cmd)}") from exc
    return GitResult(proc.returncode, proc.stdout, proc.stderr)


# ---------- pure helpers ----------

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(title: str, max_len: int = 30) -> str:
    """Kebab-case ASCII slug, trimmed to max_len. Empty input becomes 'untitled'."""
    if not title:
        return "untitled"
    normalized = unicodedata.normalize("NFKD", title)
    ascii_bytes = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_bytes.lower()
    slug = _SLUG_STRIP.sub("-", lowered).strip("-")
    if not slug:
        return "untitled"
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-") or "untitled"
    return slug


def build_branch_name(pr_number: int, title: str) -> str:
    """Compose `prj/auto-fix/{n}-{slug}`. Always starts with SAFE_BRANCH_PREFIX."""
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise GitOpError(f"pr_number must be a positive int, got {pr_number!r}")
    slug = slugify(title)
    return f"{SAFE_BRANCH_PREFIX}{pr_number}-{slug}"


def build_commit_subject(summary: str, pr_number: int) -> str:
    """Conventional-commit fix subject, clamped to SUBJECT_MAX chars."""
    summary = (summary or "apply verified fix").strip().splitlines()[0]
    base = f"fix: {summary} (re #{pr_number})"
    if len(base) <= SUBJECT_MAX:
        return base
    # Truncate the summary portion only, keep the (re #n) tail intact.
    tail = f" (re #{pr_number})"
    prefix = "fix: "
    budget = SUBJECT_MAX - len(prefix) - len(tail) - 1  # 1 for ellipsis
    if budget < 5:
        # Pathological case: still return something valid even if a bit overlong.
        return base[:SUBJECT_MAX]
    return f"{prefix}{summary[:budget].rstrip()}…{tail}"


def _wrap_paragraph(text: str, width: int = 72) -> str:
    """Greedy word wrap. Stdlib's textwrap would also work but is slower for the
    very short bodies we generate and we want zero hidden behaviour around
    long-token handling (URLs etc)."""
    out_lines: list[str] = []
    for paragraph in text.split("\n\n"):
        current = ""
        for word in paragraph.split():
            if not current:
                current = word
                continue
            if len(current) + 1 + len(word) <= width:
                current = f"{current} {word}"
            else:
                out_lines.append(current)
                current = word
        if current:
            out_lines.append(current)
        out_lines.append("")
    return "\n".join(out_lines).rstrip() + "\n"


def build_commit_body(
    rationale: str,
    risks: str,
    claim_source: str,
    pr_number: int,
) -> str:
    """Compose the commit body with Co-Authored-By + Refs trailers.

    Raises GitOpError if claim_source is empty -- we never ship a fix without
    crediting the reporter.
    """
    if not claim_source or not claim_source.strip():
        raise GitOpError("claim_source is required for Co-Authored-By trailer")

    rationale = (rationale or "Applies the adversarially-validated fix-plan.").strip()
    risks = (risks or "See fix-plan.md for risk enumeration.").strip()

    body_paragraphs = _wrap_paragraph(rationale) + "\n" + _wrap_paragraph(risks)
    trailer = f"Co-Authored-By: {claim_source.strip()}\nRefs: #{pr_number}\n"
    return f"{body_paragraphs.rstrip()}\n\n{trailer}"


_DIFF_FENCE = re.compile(
    r"^```(?:diff|patch)\s*\n(?P<body>.*?)\n```",
    re.MULTILINE | re.DOTALL,
)


def extract_diff_from_fix_plan(text: str) -> str:
    """Return the first ```diff or ```patch fenced block body from a fix-plan.

    Raises GitOpError if no such block is found or if the body is empty.
    """
    if not text:
        raise GitOpError("fix-plan text is empty; nothing to extract")
    match = _DIFF_FENCE.search(text)
    if not match:
        raise GitOpError("no fenced ```diff (or ```patch) block found in fix-plan")
    body = match.group("body").strip("\n")
    if not body.strip():
        raise GitOpError("fenced diff block is empty")
    return body + "\n"


# ---------- git side-effects ----------

def create_branch(repo_dir: Path, branch: str, base_ref: str) -> None:
    """Create a new branch on this repo. Refuses unsafe branch names."""
    if not branch.startswith(SAFE_BRANCH_PREFIX):
        raise GitOpError(
            f"refuse to create branch outside {SAFE_BRANCH_PREFIX!r}: {branch!r}"
        )
    res = _run_git(["checkout", "-b", branch, base_ref], cwd=repo_dir)
    if res.returncode != 0:
        raise GitOpError(
            f"git checkout -b {branch} {base_ref} failed: {res.stderr.strip()}"
        )


def apply_diff(repo_dir: Path, diff_text: str) -> None:
    """Apply a unified diff via `git apply --index`. Raises on failure."""
    if not diff_text.strip():
        raise GitOpError("diff_text is empty")
    res = _run_git(
        ["apply", "--index", "--whitespace=nowarn", "-"],
        cwd=repo_dir,
        stdin_text=diff_text,
    )
    if res.returncode != 0:
        raise GitOpError(f"git apply failed: {res.stderr.strip() or res.stdout.strip()}")


def commit_change(
    repo_dir: Path,
    subject: str,
    body: str,
    bot_user: str,
    bot_email: str,
    dry_run: bool = False,
) -> str:
    """Create a single commit with the bot identity and Co-Authored-By trailer.

    Returns the new commit SHA. In dry-run mode returns an empty string and
    runs nothing. Never amends. Never bypasses hooks.
    """
    if not subject.strip():
        raise GitOpError("commit subject must not be empty")
    if "Co-Authored-By:" not in body:
        raise GitOpError("commit body is missing required Co-Authored-By trailer")
    message = f"{subject}\n\n{body}".rstrip() + "\n"

    if dry_run:
        return ""

    res = _run_git(
        [
            "-c", f"user.name={bot_user}",
            "-c", f"user.email={bot_email}",
            "commit",
            "--allow-empty-message",
            "-F", "-",
        ],
        cwd=repo_dir,
        stdin_text=message,
    )
    if res.returncode != 0:
        raise GitOpError(f"git commit failed: {res.stderr.strip() or res.stdout.strip()}")

    sha_res = _run_git(["rev-parse", "HEAD"], cwd=repo_dir)
    if sha_res.returncode != 0:
        raise GitOpError(f"git rev-parse HEAD failed: {sha_res.stderr.strip()}")
    return sha_res.stdout.strip()


def push_branch(
    repo_dir: Path,
    branch: str,
    token: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Push the branch with --set-upstream origin. Returns the argv used.

    Refuses to push:
      - any branch that does not start with SAFE_BRANCH_PREFIX
      - with --force or --force-with-lease (this helper never accepts them)
    """
    if not branch.startswith(SAFE_BRANCH_PREFIX):
        raise GitOpError(
            f"refuse to push branch outside {SAFE_BRANCH_PREFIX!r}: {branch!r}"
        )
    argv = ["push", "--set-upstream", "origin", branch]

    if dry_run:
        return argv

    env: dict[str, str] | None = None
    if token:
        env = {"GITHUB_TOKEN": token}

    res = _run_git(argv, cwd=repo_dir, env=env)
    if res.returncode != 0:
        raise GitOpError(f"git push failed: {res.stderr.strip() or res.stdout.strip()}")
    return argv


def run_tests(repo_dir: Path, test_command: str | None) -> dict[str, object]:
    """Run the project test command and return a {status, returncode, tail} dict.

    test_command is split via shlex; a None / empty command yields a synthetic
    {'status': 'skipped'} payload so dry-runs and unit tests don't depend on a
    real runner being installed.
    """
    import shlex
    import time

    if not test_command:
        return {"status": "skipped", "reason": "no test_command configured"}

    cmd = shlex.split(test_command)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    except FileNotFoundError as exc:
        return {
            "status": "failed",
            "returncode": -1,
            "tail": f"test command not found: {exc}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "failed",
            "returncode": -2,
            "tail": f"test command timed out: {test_command}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-30:]
    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "tail": "\n".join(tail),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


# ---------- CLI surface (debug) ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_slug = sub.add_parser("slugify", help="Kebab-case slugify a string")
    p_slug.add_argument("title")
    p_slug.add_argument("--max-len", type=int, default=30)

    p_subj = sub.add_parser("subject", help="Build a conventional-commit subject")
    p_subj.add_argument("--summary", required=True)
    p_subj.add_argument("--pr", type=int, required=True)

    p_body = sub.add_parser("body", help="Build commit body with Co-Authored-By trailer")
    p_body.add_argument("--rationale", default="")
    p_body.add_argument("--risks", default="")
    p_body.add_argument("--claim-source", required=True)
    p_body.add_argument("--pr", type=int, required=True)

    args = parser.parse_args()
    try:
        if args.cmd == "slugify":
            print(json.dumps({"slug": slugify(args.title, args.max_len)}))
        elif args.cmd == "subject":
            print(json.dumps({"subject": build_commit_subject(args.summary, args.pr)}))
        elif args.cmd == "body":
            body = build_commit_body(args.rationale, args.risks, args.claim_source, args.pr)
            print(json.dumps({"body": body}))
    except GitOpError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
