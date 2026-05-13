---
name: prj-triage
description: PR Jangler classifier for a PR or for a single new comment on a PR. Use when the orchestrator dispatches '--mode pr' (initial PR classification) or '--mode comment' (latest unclassified comment classification) for the PR Jangler module.
---

# prj-triage

## Overview

This skill is the dual-mode classifier for the PR Jangler module. One invocation classifies exactly one thing: either an open PR (mode `pr`) or the most recent unclassified comment on a PR (mode `comment`). The classification is LLM-driven and applies the rubric below; the Python scripts handle the deterministic plumbing — `gh` label application, per-PR cache writes, state-machine phase transitions, atomic state I/O through `prj-orchestrator`'s `state_io`.

Act as a conservative, pattern-driven triage librarian. Default to `needs-review` (PR) or `advisory` (comment) when ambiguous. Never auto-reject a PR without a clear definite-no pattern. Treat maintainer comments with extra weight; treat first-time contributors with extra patience.

## Conventions

- Bare paths (e.g. `scripts/run.py`) resolve from the skill root.
- `{project-root}/...` resolves from the project working directory.
- Configuration variables live under `[modules.prj]` in `{project-root}/_bmad/config.toml`.
- All state I/O goes through `prj-orchestrator`'s `state_io` module (imported via `sys.path`); this skill never touches `state.json` directly.

## On Activation

The orchestrator passes `--mode pr` or `--mode comment` plus `--pr-number N`. Before classifying, load configuration from `{project-root}/_bmad/config.toml` (`[modules.prj]`). Required key: `prj_repo`. If absent, exit early with status `misconfigured` and a clear run-log entry.

Workflow per invocation:

1. **Gather context.** Read the per-PR cache (`prs/{n}/meta.json`, comments-triage history if relevant) and the PR state entry. For mode `comment`, read the latest unclassified comment from the per-PR cache snapshot prj-discover left behind (the `seen_comment_ids` delta).
2. **Apply the rubric below.** Pick exactly one category. State your one-sentence rationale.
3. **Persist via scripts.** Invoke `python3 scripts/run.py --mode {pr|comment} --pr-number {n} --classification {class} --rationale {text}`. The script applies the GitHub label, writes the per-PR cache document, atomically updates `state.json`, and appends a run-log entry.

Always pass classification + rationale on the CLI. The script does not classify; it persists.

Flags on `scripts/run.py`:

- `--mode pr|comment` (required) — which mode you are classifying.
- `--pr-number N` (required) — target PR number.
- `--classification CLASS` (required) — the chosen category.
- `--rationale "..."` (required) — one-sentence reasoning the script will write into the cache and runlog.
- `--comment-id ID` (mode=comment only, optional) — explicit ID of the comment being classified. If omitted, the script picks the latest unclassified comment from `seen_comment_ids` ordering.
- `--dry-run` — compute the outcome and log intent, but skip the `gh` label call and the state write.
- `--skip-label` — perform state and cache writes but skip the `gh` label call (useful in offline tests).
- `--verbose` — emit progress diagnostics to stderr.
- `--project-root PATH` — override autodetect.

See `python3 scripts/run.py --help` for full detail.

## PR-Mode Classification Rubric

The full pattern-based seed rubric lives in `assets/rubric.md`. The compressed rubric for in-line decisioning:

**Categories:** `actionable` | `definite-no` | `possible-duplicate` | `needs-review`

**Pick `definite-no` only when one of these patterns matches** (no patterns matched -> escalate, do not auto-reject):

- README typo or formatting changes without context, justification, or linked issue
- Pure formatting/whitespace changes without behavior implications
- Dependency bumps without changelog summary or stated reason
- Adds a new external dependency without architectural justification
- Removes existing tool or feature without deprecation path
- Edits files under `skill/` (generated artifacts; should edit `src/` per CLAUDE.md)
- Edits files under `build/` (gitignored build output)

**Pick `possible-duplicate` when any of:**

- Touches files already touched by another open PR
- Title or description shares strong keyword overlap with another open PR
- Adds a tool with name similar to an existing tool or another open PR's proposed tool

**Pick `actionable` when the PR is a substantive change matching:**

- Adds a new MCP tool with a clear use case
- Fixes a reproducible bug accompanied by a test
- Refactors with clear architectural justification
- Adds test coverage to existing functionality
- Documents existing behavior that lacked docs

**Pick `needs-review` for everything else** that is substantive but does not match a definite-no or possible-duplicate pattern.

**Phase transitions (encoded in scripts/persistence.py):**

| Classification | Next phase | Next action |
| --- | --- | --- |
| `actionable` | `ReviewPending` | `{skill: "prj-review", mode: null}` |
| `possible-duplicate` | `OverlapCheck` | `{skill: "prj-detect-overlap", mode: null}` |
| `definite-no` | `Rejected` | `null` (terminal) |
| `needs-review` | `ReviewPending` | `{skill: "prj-review", mode: null}` |

Also resets `new_comments_since_triage` to 0 and `needs_retriage` to false.

## Comment-Mode Classification Rubric

**Categories:** `actionable` | `advisory` | `noise`

**Pick `actionable` when:**

- Maintainer says "this should X" or "X is broken when Y"
- Contributor says "I tested this and it fails when Z"
- Code block in comment showing reproduction steps
- Linked issue referenced as a bug

**Pick `advisory` when:**

- Opinion or aesthetic preference without reproduction steps
- "Have you considered..." style suggestions
- Praise or general feedback

**Pick `noise` when:**

- Status check ("any update?")
- Bot-generated comments (CI status, dependabot)
- Off-topic discussion

**Author weighting:**

- Maintainer comments: highest weight; bias toward `actionable` if they assert a concrete behavior.
- First-time contributors: extra patience and welcome tone if you escalate.
- Returning contributors: standard weight.
- Bot accounts: `noise` unless explicitly whitelisted.

**Phase transitions:**

| Classification | Next phase | Next action |
| --- | --- | --- |
| `actionable` | `ClaimVerify` | `{skill: "prj-verify-claim", mode: null}` |
| `advisory` | `Reviewed` | `null` |
| `noise` | `Reviewed` | `null` |

Decrements `new_comments_since_triage` by 1 (floored at 0) so the next sweep can pick up further unclassified comments.

## Architecture

| Concern | Lives in |
| --- | --- |
| Shared state I/O (atomic load, save, runlog) | `state_io` (imported from `prj-orchestrator/scripts`) |
| Mode/CLI parsing, orchestration | `scripts/run.py` |
| GitHub label application via `gh pr edit` | `scripts/github_ops.py` |
| Per-PR cache writes + phase transitions | `scripts/persistence.py` |
| Seed rubric (also embedded above) | `assets/rubric.md` |

The script split keeps each module narrow and independently testable. `run.py` itself is mostly argument routing; the classification decision is the LLM's job, not Python's.

## State and Cache Touched

This skill writes to:

- `{project-root}/_bmad-output/pr-workflow/state.json` (via `state_io.save_state`, atomic)
- `{project-root}/_bmad-output/pr-workflow/prs/{pr_number}/triage.md` (mode `pr`; overwritten each run)
- `{project-root}/_bmad-output/pr-workflow/prs/{pr_number}/comments-triage.md` (mode `comment`; append-only)
- `{project-root}/_bmad-output/pr-workflow/logs/{YYYY-MM-DD}.jsonl` (append-only run-log)
- GitHub: `prj/triage:{class}` label on the PR (mode `pr` only)

This skill reads from:

- `{project-root}/_bmad/config.toml` (config)
- `{project-root}/_bmad-output/pr-workflow/state.json` (prior queue state)
- `{project-root}/_bmad-output/pr-workflow/prs/{pr_number}/meta.json` (per-PR snapshot)
- `{project-root}/_bmad-output/pr-workflow/prs/{pr_number}/comments-triage.md` (mode `comment`, for prior history)

## Non-Negotiables

- **Conservative classification.** Never auto-reject without an explicit definite-no pattern. Default to `needs-review` for PRs, `advisory` for comments.
- **Atomic state writes.** Always via `state_io.save_state`. Direct file writes to `state.json` are forbidden.
- **Idempotent persistence.** Triage scripts can be rerun safely; same inputs produce same state. `triage.md` is overwritten on rerun; `comments-triage.md` appends.
- **Single responsibility.** This skill does NOT verify claims, review code, detect overlaps, or post review comments. It classifies and hands off.

## Verification

Smoke-test in dry-run mode (no `gh` calls, no state writes):

```bash
python3 scripts/run.py --mode pr --pr-number 101 --classification actionable --rationale "test" --dry-run --verbose
```

Expected: exit 0, run-log entry with `status: "dry-run"`, no state changes, no label applied.

Run the unit tests (no `gh` calls; all subprocess invocations mocked):

```bash
python3 -m unittest discover scripts/tests
```
