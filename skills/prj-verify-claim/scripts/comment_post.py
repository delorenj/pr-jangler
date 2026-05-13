#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Pushback comment posting for prj-verify-claim.

Thin wrapper around `gh pr comment` plus template rendering. The template
lives at `assets/pushback-comment-template.md` and uses string.Template
`$variable` substitution (single-pass, no embedded Python logic).

Importable:
  - TEMPLATE_REL_PATH
  - CommentError
  - render_pushback (pure: no subprocess)
  - build_opening (pure: chooses greeting by role)
  - post_pr_comment (subprocess; single seam `_run_gh`)

CLI: `python3 scripts/comment_post.py --pr-number N --repo owner/name --body-file PATH`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import string
import subprocess
import sys
from pathlib import Path
from typing import Any


TEMPLATE_REL_PATH = "assets/pushback-comment-template.md"


class CommentError(RuntimeError):
    """Raised when gh comment fails or inputs are invalid."""


def _skill_root() -> Path:
    """Return the prj-verify-claim skill root (parent of scripts/)."""
    return Path(__file__).resolve().parent.parent


def _load_template(skill_root: Path | None = None) -> str:
    """Read the pushback template from assets/."""
    root = skill_root or _skill_root()
    path = root / TEMPLATE_REL_PATH
    if not path.exists():
        raise CommentError(f"pushback template missing: {path}")
    return path.read_text(encoding="utf-8")


def build_opening(commenter: str, role: str = "contributor") -> str:
    """Return the greeting line, tone-calibrated by commenter role.

    Roles:
      - "maintainer": brief and direct
      - "first-timer": extra warmth
      - "contributor" (default): standard
      - "bot": still polite; bot account shouldn't normally reach this code
        path, but if it does we keep the tone professional.
    """
    handle = (commenter or "there").strip().lstrip("@")
    if role == "maintainer":
        return f"Hi @{handle}, thanks for flagging this."
    if role == "first-timer":
        return (
            f"Hi @{handle}, thanks so much for the report — and welcome! "
            "I wanted to share what I found before this moves on."
        )
    if role == "bot":
        return f"Hi @{handle}, automated note in reply to your comment."
    return f"Hi @{handle}, thanks for the report."


def _strip_html_comments(text: str) -> str:
    """Remove HTML-style comment blocks from the rendered output.

    The template carries an HTML comment block at the top documenting the
    expected variables; that block is for skill authors, not GitHub users.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        start = text.find("<!--", i)
        if start == -1:
            out.append(text[i:])
            break
        out.append(text[i:start])
        end = text.find("-->", start)
        if end == -1:
            # Unterminated comment; drop the rest defensively.
            break
        i = end + 3
        # Swallow the immediately-following newline so we don't leave a blank
        # line where the comment block used to be.
        if i < len(text) and text[i] == "\n":
            i += 1
    return "".join(out)


def _truncate_claim(claim: str, limit: int = 280) -> str:
    cleaned = (claim or "").strip().replace("\r\n", "\n")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def render_pushback(
    claim: str,
    commenter: str,
    strategy: str,
    worktree: str,
    command: str,
    observation: str,
    next_step: str | None = None,
    role: str = "contributor",
    bot_signature: str | None = None,
    skill_root: Path | None = None,
) -> str:
    """Render the pushback comment from the template. Pure function."""
    template_text = _load_template(skill_root)
    tmpl = string.Template(_strip_html_comments(template_text).strip())
    next_step = next_step or (
        "Could you share the exact command, environment, or repro steps you used? "
        "Even a one-liner gives me a concrete handle to retry against."
    )
    bot_signature = bot_signature or (
        "_This is an automated reproduction attempt by the PR Jangler bot. "
        "A human maintainer will read your reply and decide what happens next._"
    )
    try:
        return tmpl.substitute(
            opening=build_opening(commenter, role=role),
            commenter=(commenter or "there").strip().lstrip("@"),
            claim_excerpt=_truncate_claim(claim),
            strategy=strategy or "unspecified",
            worktree=worktree or "<unknown>",
            command=command or "<unspecified>",
            observation=(observation or "").strip(),
            next_step=next_step,
            bot_signature=bot_signature,
        )
    except KeyError as exc:
        raise CommentError(f"template references unknown variable: {exc}") from exc


def _run_gh(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Single subprocess seam. Tests patch this."""
    binary = shutil.which("gh") or "gh"
    cmd = [binary, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise CommentError(f"gh CLI not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommentError(
            f"gh CLI timed out after {timeout}s: {' '.join(cmd)}"
        ) from exc
    return proc.returncode, proc.stdout, proc.stderr


def post_pr_comment(
    repo: str,
    pr_number: int,
    body: str,
) -> dict[str, Any]:
    """Post `body` as a comment on the PR. Returns a runlog-friendly dict.

    Body is passed to gh via stdin to avoid argv length limits and quoting
    pain. We use `gh pr comment N --repo R --body-file -` which reads from
    stdin.
    """
    if not repo:
        raise CommentError("repo must be non-empty (owner/name)")
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise CommentError(f"pr_number must be a positive int, got {pr_number!r}")
    if not body.strip():
        raise CommentError("comment body must be non-empty")
    args = [
        "pr", "comment", str(pr_number),
        "--repo", repo,
        "--body-file", "-",
    ]
    rc, stdout, stderr = _run_gh_with_stdin(args, body)
    if rc != 0:
        tail = (stderr or stdout).strip()[-500:]
        raise CommentError(
            f"gh exited {rc} posting comment on #{pr_number}: {tail}"
        )
    return {
        "repo": repo,
        "pr_number": pr_number,
        "status": "posted",
        "bytes": len(body),
    }


def _run_gh_with_stdin(args: list[str], stdin_text: str, timeout: int = 30) -> tuple[int, str, str]:
    """Variant of _run_gh that pipes a body in via stdin.

    Kept as a separate function so tests can patch either seam independently
    while sharing the same fallback logic.
    """
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
        raise CommentError(f"gh CLI not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommentError(
            f"gh CLI timed out after {timeout}s: {' '.join(cmd)}"
        ) from exc
    return proc.returncode, proc.stdout, proc.stderr


# ---------- CLI surface ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Repo slug owner/name")
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument(
        "--body-file", required=True,
        help="Path to a file containing the comment body, or '-' to read stdin",
    )
    args = parser.parse_args()
    if args.body_file == "-":
        body = sys.stdin.read()
    else:
        body = Path(args.body_file).read_text(encoding="utf-8")
    try:
        result = post_pr_comment(args.repo, args.pr_number, body)
    except CommentError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
