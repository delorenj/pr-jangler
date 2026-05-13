---
name: prj-decision
description: PR Jangler final-call workflow. Use when the orchestrator dispatches the decision phase for a reviewed PR or when the user runs 'prj-decision' to label, comment, and log a ready-to-merge / request-changes / close-as-not-now verdict.
---

# prj-decision

## Overview

This skill closes the loop on a reviewed PR. One invocation aggregates the per-PR cache (review, verification, fix-plan, adversarial, implementation, overlap, decisions log), applies a deterministic rubric over the structural gates, optionally consults LLM judgement for ambiguous cases, then applies the `prj/decision:{class}` GitHub label, posts a structured summary comment from the template at `assets/template-decision-comment.md`, appends a JSONL entry to `prs/{n}/decisions.log`, and transitions the PR's phase in `state.json`.

Act as a senior reviewer making a defensible call. The cache is the evidence. The rubric is the law. Never decide on "vibes." Every output must trace back to a cache artifact.

## Conventions

- Bare paths (e.g. `scripts/run.py`) resolve from the skill root.
- `{project-root}/...` resolves from the project working directory.
- Configuration variables live under `[modules.prj]` in `{project-root}/_bmad/config.toml`.
- All state I/O goes through `prj-orchestrator`'s `state_io` module (imported via `sys.path`); this skill never touches `state.json` directly.

## On Activation

Load configuration from `{project-root}/_bmad/config.toml`. The required key is `prj_repo` (owner/name). The PR number is supplied via `--pr <number>` (the orchestrator passes the highest-priority PR; CLI users supply it explicitly). If `prj_repo` is missing, exit early with status `misconfigured`; if `--pr` is missing, exit with status `bad-args`.

Execute the decision:

```bash
python3 scripts/run.py --pr <number>
```

That single script handles aggregation, rubric application, label, comment, decisions.log append, state transition, and run-log entry. Flags:

- `--pr <N>` (required) the PR number to decide on
- `--dry-run` compute the decision and log the intent, but do not apply label, post comment, or transition state
- `--verbose` emit diagnostics to stderr
- `--project-root <path>` override autodetect

See `python3 scripts/run.py --help` for full detail.

## Decision Rubric

Three classes, evaluated in order. The first class whose gates all pass wins.

### close-as-not-now (CONSERVATIVE triple gate)

All three must hold:

1. **No maintainer activity in >30 days.** Measured from `last_maintainer_activity_at` field on the PR state entry (set by `prj-discover` when comments are reconciled). Absent or older than 30 days → gate (a) passes.
2. **One of:**
    - `triage.md` classification is `definite-no`, OR
    - `overlap.md` verdict is `redundant`, OR
    - `decisions.log` shows 2 or more entries with `kind: "adversarial-escalation"`.
3. **Phase is non-terminal but Reviewed-or-later.** PR has been substantively analysed; we are not closing un-reviewed PRs.

If all three hold, decision is `close-as-not-now`. The label is applied. The comment makes explicit that the PR is NOT auto-closed; the maintainer closes manually.

### ready-to-merge

All four must hold:

1. **Review findings resolved.** Either `review.md` has no `severity: "blocker"` findings, OR all blocker findings have a corresponding entry in `implementation.md` with `addresses_finding: <id>`.
2. **Tests pass.** `implementation.md` shows `tests: "pass"` AND `regressions: "none"`. If no fix was needed (no `implementation.md`), the review's `tests_status` must be `pass` or absent (PR is doc-only / chore).
3. **No overlap conflicts.** `overlap.md` is absent OR verdict is `independent` or `complementary`. `conflicting` or `redundant` block this class.
4. **Adversarial gate clean.** If `adversarial.md` exists, its verdict is `pass`. `reject` or `escalate` block this class.

### request-changes

Default fallback when neither `close-as-not-now` nor `ready-to-merge` apply. Requires at least one actionable signal:

- Outstanding `blocker` or `major` finding in `review.md`, OR
- `verification.md` exists with verdict `verified` AND no `fix-plan.md` follow-through, OR
- `adversarial.md` verdict is `reject` (single rejection — escalation is the close gate).

If none of those hold either, the rubric returns `request-changes` with `confidence: "low"` and the LLM activation context is responsible for either confirming or escalating to `please-advise` (not a decision class, but a phase). In practice the orchestrator should not have dispatched decision in this case; this branch is a safety net.

## State Transition Matrix

| Decision | From phase | To phase | Notes |
| --- | --- | --- | --- |
| `ready-to-merge` | Reviewed, FixImpl | ReadyToMerge | Terminal. Maintainer merges manually. |
| `request-changes` | Reviewed | Reviewed | No transition; PR stays for next round. `next_action` clears. |
| `close-as-not-now` | Reviewed, Blocked | Rejected | Terminal. PR is NOT auto-closed on GitHub. |

A PR already in a terminal phase (`ReadyToMerge`, `Rejected`, `Archived`, `Blocked` — except moving from `Blocked` to `Rejected`) is treated as no-op: `status: "already-decided"`.

## LLM Judgement Hook

The deterministic rubric handles the structural gates. Where the cache contains qualitative signals the rubric cannot reduce (e.g. "review found a `major` issue but the fix-plan claims it is out-of-scope") the rubric returns `decision: "request-changes"` with `confidence: "low"` and a list of `ambiguity_signals`. In normal cron-driven operation that result is taken at face value (conservative default). When the orchestrator routes the activation to a model context, the model may override using the comment-template's "Notes" section to record the override rationale. Overrides are written to the decisions.log with `kind: "llm-override"` so the audit trail is preserved.

## Artifacts Touched

This skill reads:

- `{project-root}/_bmad/config.toml`
- `{project-root}/_bmad-output/pr-workflow/state.json`
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/review.md`
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/verification.md`
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/fix-plan.md`
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/adversarial.md`
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/implementation.md`
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/overlap.md`
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/triage.md`
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/decisions.log`
- `assets/template-decision-comment.md`

This skill writes:

- `{project-root}/_bmad-output/pr-workflow/state.json` (via `state_io.save_state`, atomic)
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/decisions.log` (append-only JSONL)
- `{project-root}/_bmad-output/pr-workflow/logs/{YYYY-MM-DD}.jsonl` (append-only run-log)
- GitHub PR labels via `gh pr edit --add-label prj/decision:{class}`
- GitHub PR comments via `gh pr comment <n> --body-file <tmp>`

## Comment Template

The comment posted to GitHub uses `assets/template-decision-comment.md`. The placeholders are simple `{name}` tokens substituted by `decision_io.render_comment`:

- `{decision_class}` one of `ready-to-merge`, `request-changes`, `close-as-not-now`
- `{summary}` one-line summary of the call
- `{findings_block}` bulleted list of outstanding findings (or "None.")
- `{outstanding_items}` bulleted list of action items for the maintainer (or "None.")
- `{recommendation}` next-step recommendation in plain prose
- `{cache_paths}` newline-joined list of cache artifacts the decision drew from
- `{run_id}` correlation id with the run-log entry

For `close-as-not-now`, the comment explicitly states: "This PR has not been auto-closed. If you agree with this assessment, close it manually."

## Non-Negotiables

- **Defensible from cache alone.** Every decision class MUST be backed by an artifact in the per-PR cache. The rubric module enforces this structurally.
- **Conservative close gate.** Triple gate for `close-as-not-now` is non-negotiable. Two-of-three is `request-changes`, not close.
- **Never auto-close on GitHub.** `close-as-not-now` labels and comments; user closes manually.
- **Atomic state writes.** Always via `state_io.save_state`.
- **Append-only decisions log.** Never overwrite, never compact. The log IS the audit trail.

## Verification

Smoke-test with a populated per-PR cache (no `gh` required if `--dry-run`):

```bash
python3 scripts/run.py --pr 42 --dry-run --verbose
```

Run the unit tests (all subprocess invocations mocked):

```bash
python3 -m unittest discover scripts/tests
```
