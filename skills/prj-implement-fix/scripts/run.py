#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""prj-implement-fix entry point.

Happy path:
  1. Load config + state + per-PR cache (fix-plan, adversarial, verification).
  2. Refuse if adversarial verdict != pass.
  3. Build branch name, commit subject + body (Co-Authored-By), apply diff,
     run failing test then full suite, commit, push.
  4. `gh pr create` against the contributor's branch; on contributor-veto
     stderr, fall back to a comment on the original PR.
  5. Label the original PR `prj/fix-proposed`.
  6. Write implementation.md, transition state FixImpl -> ReadyToMerge.
  7. Append a structured run-log entry.

Exit codes:
  0  success (including dry-run)
  2  misconfigured (missing required config keys)
  3  gh / git transport error
  4  adversarial verdict missing or != pass (refuse)
  5  bad-fix-plan (no diff block or no claim_source)
  6  fallback also failed (PR + comment both failed)
  7  pre-commit hook failed (git commit non-zero)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Sibling imports. implementation_io imports state_io for us; importing it first
# patches sys.path so subsequent state_io references work cleanly.
import branch_ops
import implementation_io
import pr_create

# state_io comes via implementation_io's sys.path patch; import after it.
import state_io  # noqa: E402

# Single-sourced config loader from prj-orchestrator.
from select_next_action import load_prj_config  # noqa: E402


REQUIRED_CONFIG_KEYS = ("prj_repo", "prj_bot_user", "prj_bot_token_ref")


def _emit_runlog(project_root: Path, entry: dict[str, Any]) -> None:
    """Wrapper so tests can patch a single seam."""
    state_io.append_runlog(project_root, entry)


def _pr_cache_dir(project_root: Path, pr_number: int) -> Path:
    return project_root / "_bmad-output" / "pr-workflow" / "prs" / str(pr_number)


def _contributor_base_ref(state: dict[str, Any], pr_number: int) -> str:
    """Look up the contributor's head ref (which we target as `--base`) from state."""
    key = str(pr_number)
    if key not in state.get("prs", {}):
        raise RuntimeError(f"PR {pr_number} not in state.json; cannot resolve base ref")
    pr = state["prs"][key]
    base_ref = pr.get("contributor_head_ref") or pr.get("head_ref")
    if not base_ref:
        # meta.json fallback (written by prj-discover).
        meta_path = _pr_cache_dir(state_io.find_project_root(), pr_number) / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            base_ref = meta.get("head_ref")
    if not base_ref:
        raise RuntimeError(
            f"could not resolve contributor head ref for PR {pr_number}; "
            "state needs a `contributor_head_ref` or meta.json with `head_ref`"
        )
    return base_ref


def _pr_title_from_state(state: dict[str, Any], pr_number: int) -> str:
    pr = state.get("prs", {}).get(str(pr_number), {}) or {}
    return pr.get("title") or pr.get("pr_title") or ""


def _resolve_token(config: dict[str, Any]) -> str | None:
    """Resolve the bot token via the local creds module. Returns None on failure."""
    from creds import CredentialError, resolve_reference

    ref = config.get("prj_bot_token_ref")
    if not ref:
        return None
    try:
        return resolve_reference(ref)
    except CredentialError as exc:
        print(
            json.dumps({"status": "creds-warning", "error": str(exc)}),
            file=sys.stderr,
        )
        return None


def _render_pr_body(
    template_path: Path,
    *,
    pr_number: int,
    summary: str,
    rationale: str,
    risks: str,
    claim_source: str,
    test_summary: dict[str, Any],
    bot_user: str,
    slug: str,
    generated_at: str,
) -> str:
    template = template_path.read_text(encoding="utf-8")
    return template.format(
        pr_number=pr_number,
        summary=summary,
        rationale=rationale or "Applies the adversarially-validated fix-plan.",
        risks=risks or "See fix-plan.md for the enumerated risks.",
        claim_source=claim_source,
        test_summary=json.dumps(test_summary, indent=2, sort_keys=True),
        bot_user=bot_user,
        slug=slug,
        generated_at=generated_at,
    )


def _parse_rationale_and_risks(fix_plan_text: str) -> tuple[str, str]:
    """Best-effort extract Rationale and Risks sections from fix-plan.md.

    Returns ("", "") on absence -- the templates have safe fallbacks.
    """
    import re as _re

    def _grab(label: str) -> str:
        pattern = _re.compile(
            rf"^##+\s*{label}\s*\n(.*?)(?=^##+\s|\Z)",
            _re.MULTILINE | _re.DOTALL | _re.IGNORECASE,
        )
        match = pattern.search(fix_plan_text)
        return match.group(1).strip() if match else ""

    rationale = _grab("Rationale") or _grab("Why this is the right fix")
    risks = _grab("Risks") or _grab("What could go wrong")
    return rationale, risks


def _runlog_base(run_id: str, pr_number: int) -> dict[str, Any]:
    return {
        "action": "implement-fix",
        "skill": "prj-implement-fix",
        "run_id": run_id,
        "pr_number": pr_number,
    }


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-number", type=int, required=True, help="PR to implement a fix for")
    parser.add_argument("--project-root", default=None, help="Override project root")
    parser.add_argument("--dry-run", action="store_true", help="Plan + log without git push or gh writes")
    parser.add_argument("--verbose", action="store_true", help="Diagnostics to stderr")
    args = parser.parse_args(argv)

    run_id = uuid.uuid4().hex[:12]
    project_root = (
        Path(args.project_root) if args.project_root else state_io.find_project_root()
    )
    started = time.monotonic()

    config = load_prj_config(project_root)
    missing = [k for k in REQUIRED_CONFIG_KEYS if not str(config.get(k) or "").strip()]
    if missing:
        entry = _runlog_base(run_id, args.pr_number) | {
            "status": "misconfigured",
            "missing_config": missing,
        }
        _emit_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 2

    repo = str(config["prj_repo"]).strip()
    bot_user = str(config["prj_bot_user"]).strip()
    bot_email = str(config.get("prj_bot_email") or f"{bot_user}@users.noreply.github.com")

    # Load per-PR cache contents.
    try:
        adversarial_text = implementation_io.load_adversarial(project_root, args.pr_number)
        fix_plan_text = implementation_io.load_fix_plan(project_root, args.pr_number)
        verification_text = implementation_io.load_verification(project_root, args.pr_number)
    except FileNotFoundError as exc:
        entry = _runlog_base(run_id, args.pr_number) | {
            "status": "not-validated",
            "reason": str(exc),
        }
        _emit_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 4

    # Adversarial gate.
    try:
        verdict = implementation_io.parse_adversarial_verdict(adversarial_text)
    except ValueError as exc:
        entry = _runlog_base(run_id, args.pr_number) | {
            "status": "not-validated",
            "reason": str(exc),
        }
        _emit_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 4
    if verdict != "pass":
        entry = _runlog_base(run_id, args.pr_number) | {
            "status": "not-validated",
            "reason": f"adversarial verdict is {verdict!r}, refusing to implement",
        }
        _emit_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 4

    # Claim source + diff extraction.
    try:
        claim_source = implementation_io.parse_claim_source(verification_text)
        diff_text = branch_ops.extract_diff_from_fix_plan(fix_plan_text)
    except (ValueError, branch_ops.GitOpError) as exc:
        entry = _runlog_base(run_id, args.pr_number) | {
            "status": "bad-fix-plan",
            "reason": str(exc),
        }
        _emit_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 5

    # Resolve state-driven inputs.
    state = state_io.load_state(project_root)
    try:
        base_branch = _contributor_base_ref(state, args.pr_number)
    except RuntimeError as exc:
        entry = _runlog_base(run_id, args.pr_number) | {
            "status": "bad-fix-plan",
            "reason": str(exc),
        }
        _emit_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 5

    pr_title = _pr_title_from_state(state, args.pr_number)
    summary = implementation_io.parse_fix_plan_summary(fix_plan_text)
    head_branch = branch_ops.build_branch_name(args.pr_number, pr_title or summary)
    slug = head_branch.rsplit("-", 0)[0]  # no-op placeholder
    slug = head_branch.split("/")[-1]  # `{n}-{slug}` portion
    commit_subject = branch_ops.build_commit_subject(summary, args.pr_number)

    rationale_text, risks_text = _parse_rationale_and_risks(fix_plan_text)
    commit_body = branch_ops.build_commit_body(
        rationale_text, risks_text, claim_source, args.pr_number
    )

    if args.verbose:
        print(
            f"[run {run_id}] pr={args.pr_number} branch={head_branch} "
            f"base={base_branch} verdict={verdict}",
            file=sys.stderr,
        )

    # Token resolution (optional in dry-run; warned otherwise).
    token = _resolve_token(config) if not args.dry_run else None

    # Git operations.
    repo_dir = project_root
    test_summary: dict[str, Any] = {"status": "skipped", "reason": "dry-run"}
    commit_sha = ""
    try:
        if not args.dry_run:
            branch_ops.create_branch(repo_dir, head_branch, base_ref="HEAD")
            branch_ops.apply_diff(repo_dir, diff_text)
            test_summary = branch_ops.run_tests(repo_dir, config.get("prj_test_runner"))
            if test_summary.get("status") == "failed":
                entry = _runlog_base(run_id, args.pr_number) | {
                    "status": "tests-failed",
                    "test_summary": test_summary,
                }
                _emit_runlog(project_root, entry)
                print(json.dumps(entry, sort_keys=True), file=sys.stderr)
                return 3
            commit_sha = branch_ops.commit_change(
                repo_dir,
                subject=commit_subject,
                body=commit_body,
                bot_user=bot_user,
                bot_email=bot_email,
                dry_run=False,
            )
            branch_ops.push_branch(repo_dir, head_branch, token=token, dry_run=False)
    except branch_ops.GitOpError as exc:
        msg = str(exc).lower()
        if "commit failed" in msg or "hook" in msg:
            code = 7
            status = "hook-failed"
        else:
            code = 3
            status = "git-error"
        entry = _runlog_base(run_id, args.pr_number) | {
            "status": status,
            "reason": str(exc),
        }
        _emit_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return code

    # PR title + body for the fix-PR (or fallback comment).
    pr_title_out = f"[prj] {summary} (re #{args.pr_number})"
    if len(pr_title_out) > 100:
        pr_title_out = pr_title_out[:99] + "…"

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    template_path = Path(__file__).resolve().parent.parent / "assets" / "template-fix-pr.md"
    body_text = _render_pr_body(
        template_path,
        pr_number=args.pr_number,
        summary=summary,
        rationale=rationale_text,
        risks=risks_text,
        claim_source=claim_source,
        test_summary=test_summary,
        bot_user=bot_user,
        slug=slug,
        generated_at=generated_at,
    )

    # Write body to a tmp file under the per-PR cache so gh can read it.
    cache_dir = _pr_cache_dir(project_root, args.pr_number)
    cache_dir.mkdir(parents=True, exist_ok=True)
    body_path = cache_dir / "fix-pr-body.md"
    body_path.write_text(body_text, encoding="utf-8")

    # PR creation with fallback.
    try:
        outcome = pr_create.create_fix_pr(
            repo=repo,
            base_branch=base_branch,
            head_branch=head_branch,
            title=pr_title_out,
            body_path=body_path,
            pr_number=args.pr_number,
            fix_plan_diff=diff_text,
            token=token,
            dry_run=args.dry_run,
        )
    except pr_create.GhCreateError as exc:
        entry = _runlog_base(run_id, args.pr_number) | {
            "status": "fallback-failed",
            "reason": str(exc),
        }
        _emit_runlog(project_root, entry)
        print(json.dumps(entry, sort_keys=True), file=sys.stderr)
        return 6

    # Write implementation.md + transition state (skip transition in dry-run).
    implementation_io.write_implementation_record(
        project_root,
        args.pr_number,
        path=outcome.path if not args.dry_run else "dry-run",
        url=outcome.url,
        branch=head_branch,
        base_branch=base_branch,
        head_branch=head_branch,
        commit_sha=commit_sha,
        test_summary=test_summary,
        title=pr_title_out,
        fallback_reason=outcome.fallback_reason,
        fix_plan_excerpt=diff_text,
    )

    if not args.dry_run:
        try:
            implementation_io.transition_to_ready_to_merge(
                project_root,
                args.pr_number,
                implementation_url=outcome.url,
                commit_sha=commit_sha,
                path=outcome.path,
            )
        except (KeyError, ValueError) as exc:
            entry = _runlog_base(run_id, args.pr_number) | {
                "status": "state-error",
                "reason": str(exc),
            }
            _emit_runlog(project_root, entry)
            print(json.dumps(entry, sort_keys=True), file=sys.stderr)
            return 3

    duration_ms = int((time.monotonic() - started) * 1000)
    entry = _runlog_base(run_id, args.pr_number) | {
        "status": "dry-run" if args.dry_run else "ok",
        "path": outcome.path if not args.dry_run else "dry-run",
        "url": outcome.url,
        "branch": head_branch,
        "base_branch": base_branch,
        "commit_sha": commit_sha,
        "title": pr_title_out,
        "fallback_reason": outcome.fallback_reason,
        "duration_ms": duration_ms,
    }
    _emit_runlog(project_root, entry)
    print(json.dumps(entry, sort_keys=True))
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
