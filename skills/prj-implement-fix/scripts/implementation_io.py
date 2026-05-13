#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Per-PR implementation.md writer + state transition helpers.

Importable: write_implementation_record, transition_to_ready_to_merge,
parse_adversarial_verdict, parse_claim_source, parse_fix_plan_summary,
load_adversarial, load_fix_plan, load_verification.

CLI lets an operator dry-write a record or print a parsed verdict.

State I/O goes through prj-orchestrator/scripts/state_io.py via the same
sibling-`sys.path`-insert pattern used elsewhere.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SIBLING = (
    Path(__file__).resolve().parent.parent.parent / "prj-orchestrator" / "scripts"
)
if _SIBLING.exists() and str(_SIBLING) not in sys.path:
    sys.path.insert(0, str(_SIBLING))

import state_io  # noqa: E402


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_VERDICT_RE = re.compile(r"^verdict\s*:\s*([A-Za-z_-]+)\s*$", re.MULTILINE)
_CLAIM_SOURCE_RE = re.compile(r"^claim[_-]?source\s*:\s*(.+?)\s*$", re.MULTILINE)
_SUMMARY_RE = re.compile(r"^summary\s*:\s*(.+?)\s*$", re.MULTILINE)


def _pr_cache_dir(project_root: Path, pr_number: int) -> Path:
    return project_root / "_bmad-output" / "pr-workflow" / "prs" / str(pr_number)


def load_adversarial(project_root: Path, pr_number: int) -> str:
    path = _pr_cache_dir(project_root, pr_number) / "adversarial.md"
    if not path.exists():
        raise FileNotFoundError(f"adversarial.md not found at {path}")
    return path.read_text(encoding="utf-8")


def load_fix_plan(project_root: Path, pr_number: int) -> str:
    path = _pr_cache_dir(project_root, pr_number) / "fix-plan.md"
    if not path.exists():
        raise FileNotFoundError(f"fix-plan.md not found at {path}")
    return path.read_text(encoding="utf-8")


def load_verification(project_root: Path, pr_number: int) -> str:
    path = _pr_cache_dir(project_root, pr_number) / "verification.md"
    if not path.exists():
        raise FileNotFoundError(f"verification.md not found at {path}")
    return path.read_text(encoding="utf-8")


def _extract_frontmatter(text: str) -> str:
    """Return the frontmatter block of a doc (or '' if absent)."""
    match = _FRONTMATTER_RE.match(text)
    return match.group(1) if match else ""


def parse_adversarial_verdict(text: str) -> str:
    """Pull `verdict: <value>` from adversarial.md frontmatter (or body fallback).

    Returns lowercased verdict. Raises ValueError if absent.
    """
    frontmatter = _extract_frontmatter(text)
    target = frontmatter or text
    match = _VERDICT_RE.search(target)
    if not match:
        raise ValueError("adversarial.md has no `verdict:` field")
    return match.group(1).strip().lower()


def parse_claim_source(text: str) -> str:
    """Pull `claim_source:` (or `claim-source:`) from verification.md.

    Returns the raw string. Raises ValueError if absent or empty.
    """
    frontmatter = _extract_frontmatter(text)
    target = frontmatter or text
    match = _CLAIM_SOURCE_RE.search(target)
    if not match:
        raise ValueError("verification.md has no `claim_source:` field")
    value = match.group(1).strip()
    if not value:
        raise ValueError("`claim_source:` is empty in verification.md")
    return value


def parse_fix_plan_summary(text: str) -> str:
    """Pull `summary:` from fix-plan.md frontmatter; fall back to first H1."""
    frontmatter = _extract_frontmatter(text)
    if frontmatter:
        match = _SUMMARY_RE.search(frontmatter)
        if match and match.group(1).strip():
            return match.group(1).strip()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return "apply verified fix"


def write_implementation_record(
    project_root: Path,
    pr_number: int,
    *,
    path: str,
    url: str,
    branch: str,
    base_branch: str,
    head_branch: str,
    commit_sha: str,
    test_summary: dict[str, Any],
    title: str,
    fallback_reason: str | None,
    fix_plan_excerpt: str,
) -> Path:
    """Write `implementation.md` to the per-PR cache and return its path.

    `path` must be one of: 'tier-2-pr' | 'tier-1-comment-fallback' | 'dry-run'.
    """
    cache = _pr_cache_dir(project_root, pr_number)
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / "implementation.md"
    generated_at = datetime.now(timezone.utc).isoformat()

    frontmatter_lines = [
        "---",
        f"pr_number: {pr_number}",
        f"path: {path}",
        f"url: {url}",
        f"branch: {branch}",
        f"base_branch: {base_branch}",
        f"head_branch: {head_branch}",
        f"commit_sha: {commit_sha or '(dry-run)'}",
        f"title: {title}",
    ]
    if fallback_reason:
        frontmatter_lines.append(f"fallback_reason: {fallback_reason}")
    frontmatter_lines.append(f"generated_at: {generated_at}")
    frontmatter_lines.append("---")

    test_block = json.dumps(test_summary, indent=2, sort_keys=True)
    body = "\n".join(frontmatter_lines) + "\n\n"
    body += f"# Implementation record for PR #{pr_number}\n\n"
    body += f"- Path: **{path}**\n"
    body += f"- URL: {url}\n"
    body += f"- Branch: `{branch}` (head) -> `{base_branch}` (base)\n"
    body += f"- Commit SHA: `{commit_sha or '(dry-run)'}`\n"
    body += f"- Title: {title}\n"
    if fallback_reason:
        body += f"- Fallback reason: `{fallback_reason}`\n"
    body += "\n## Test summary\n\n"
    body += f"```json\n{test_block}\n```\n\n"
    body += "## Fix-plan excerpt\n\n"
    body += f"```diff\n{fix_plan_excerpt.rstrip()}\n```\n"

    out.write_text(body, encoding="utf-8")
    return out


def transition_to_ready_to_merge(
    project_root: Path,
    pr_number: int,
    *,
    implementation_url: str,
    commit_sha: str,
    path: str,
) -> None:
    """Atomically move the PR from FixImpl to ReadyToMerge.

    No-op if the PR is not currently in FixImpl (the orchestrator's responsibility
    to keep phases sane). Raises if the PR is unknown to the queue.
    """
    state = state_io.load_state(project_root)
    key = str(pr_number)
    if key not in state["prs"]:
        raise KeyError(f"PR {pr_number} not in state.json; cannot transition")
    pr = state["prs"][key]
    now = datetime.now(timezone.utc).isoformat()
    pr["phase"] = "ReadyToMerge"
    pr["phase_entered_at"] = now
    pr["last_action_at"] = now
    pr["next_action"] = None
    pr.setdefault("history", []).append({
        "ts": now,
        "from": "FixImpl",
        "to": "ReadyToMerge",
        "by": "prj-implement-fix",
        "implementation_url": implementation_url,
        "commit_sha": commit_sha,
        "path": path,
    })
    state["prs"][key] = pr
    state_io.save_state(project_root, state)


# ---------- CLI surface ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_verdict = sub.add_parser("verdict", help="Print adversarial verdict for a PR")
    p_verdict.add_argument("--pr", type=int, required=True)

    p_source = sub.add_parser("claim-source", help="Print the parsed claim source")
    p_source.add_argument("--pr", type=int, required=True)

    p_summary = sub.add_parser("summary", help="Print the parsed fix-plan summary")
    p_summary.add_argument("--pr", type=int, required=True)

    args = parser.parse_args()
    project_root = (
        Path(args.project_root) if args.project_root else state_io.find_project_root()
    )

    if args.cmd == "verdict":
        try:
            verdict = parse_adversarial_verdict(load_adversarial(project_root, args.pr))
        except (FileNotFoundError, ValueError) as exc:
            print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
            return 1
        print(json.dumps({"pr": args.pr, "verdict": verdict}))
        return 0

    if args.cmd == "claim-source":
        try:
            source = parse_claim_source(load_verification(project_root, args.pr))
        except (FileNotFoundError, ValueError) as exc:
            print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
            return 1
        print(json.dumps({"pr": args.pr, "claim_source": source}))
        return 0

    if args.cmd == "summary":
        try:
            summary = parse_fix_plan_summary(load_fix_plan(project_root, args.pr))
        except FileNotFoundError as exc:
            print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
            return 1
        print(json.dumps({"pr": args.pr, "summary": summary}))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
