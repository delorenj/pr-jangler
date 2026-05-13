---
name: prj-discover
description: PR Jangler GitHub-to-queue sync. Use when the orchestrator dispatches the discovery sweep (on cadence or empty queue) or when the user runs 'prj-discover' for manual debug.
---

# prj-discover

## Overview

This skill is the GitHub-state synchronizer for the PR Jangler module. One invocation reads the configured `prj_repo` via `gh`, diffs its open-PR set against the persisted queue at `{project-root}/_bmad-output/pr-workflow/state.json`, and writes back additive changes: new PRs land at phase `Discovered`, closed PRs flip to `Archived`, fresh comments bump `new_comments_since_triage` and queue a `prj-triage --mode comment` follow-up, and PRs idle longer than 14 days get `needs_retriage: true`. Per-PR cache directories are created on first discovery with a `meta.json` snapshot. Rate-limit aware: defers cleanly if GitHub's core quota dips below 100 remaining.

Act as a careful diffing librarian. Never silently drop a PR or a comment. Treat additive writes as the contract: existing phases are only flipped to `Archived` (closed upstream) or annotated with `needs_retriage` (idle); the rest of the state machine is owned by the phase skills.

## Conventions

- Bare paths (e.g. `scripts/run.py`) resolve from the skill root.
- `{project-root}/...` resolves from the project working directory.
- Configuration variables live under `[modules.prj]` in `{project-root}/_bmad/config.toml`.
- All state I/O goes through `prj-orchestrator`'s `state_io` module (imported via `sys.path`); this skill never touches `state.json` directly.

## On Activation

Load configuration from `{project-root}/_bmad/config.toml` (the `[modules.prj]` block). The required key is `prj_repo` (owner/name). If absent, exit early with status `misconfigured` and a clear run-log entry; do not call `gh`.

Execute the sweep:

```bash
python3 scripts/run.py
```

That single script handles the full sweep: rate-limit check, `gh` calls, reconciliation, atomic state save, per-PR cache creation, run-log append. Flags:

- `--dry-run` compute the diff and log the would-be changes, do not write state or cache
- `--verbose` emit progress diagnostics to stderr
- `--project-root <path>` override autodetect

See `python3 scripts/run.py --help` for full detail.

## Architecture

Three Python modules, each with a single concern. Determinism lives in the scripts; the LLM activation context exists so the orchestrator's Skill-tool dispatch resolves cleanly when the cadence fires.

| Concern | Lives in |
| --- | --- |
| Shared state I/O (atomic load, validate, save, runlog) | `state_io` (imported from `prj-orchestrator/scripts`) |
| `gh` CLI wrapping: list, view, diff-stats, rate-limit | `scripts/gh_client.py` |
| Pure-function reconciliation (state-vs-GitHub diff) | `scripts/reconcile.py` |
| Orchestration: config, sweep, cache writes, runlog | `scripts/run.py` |

The sibling import is intentional: state ownership belongs to `prj-orchestrator`, and rewriting it here would fork the contract.

## State and Cache Touched

This skill writes to:

- `{project-root}/_bmad-output/pr-workflow/state.json` (via `state_io.save_state`, atomic)
- `{project-root}/_bmad-output/pr-workflow/prs/{pr_number}/meta.json` (created on first discovery; never overwritten silently after that)
- `{project-root}/_bmad-output/pr-workflow/logs/{YYYY-MM-DD}.jsonl` (append-only run-log)

This skill reads from:

- `{project-root}/_bmad/config.toml` (config)
- `{project-root}/_bmad-output/pr-workflow/state.json` (prior queue state)
- GitHub via `gh` (open PRs, comment payloads, diff stats, rate-limit)

## Reconciliation Rules

Codified in `scripts/reconcile.py`; summarized here for activation review.

1. **New PR** (in GitHub, not in state) — added with `phase: "Discovered"`, `contributor_login` from `author.login`, `next_action: {skill: "prj-triage", mode: "pr"}`, `seen_comment_ids` set to whatever ids the PR already carries.
2. **Existing PR with new comment ids** — `new_comments_since_triage` is bumped by the delta count; `last_action_at` advances. If the PR's phase is past initial triage (anything other than `Discovered` or `Triaged`), `next_action` flips to `{skill: "prj-triage", mode: "comment"}`. Otherwise the existing `prj-triage --mode pr` action wins.
3. **PR no longer in the open-PR list** — phase → `Archived`, `next_action` → `null`. Skill skips PRs already at `Archived`.
4. **Idle PR** (in open list, non-terminal, `last_action_at` older than 14 days) — `needs_retriage: true` set. The orchestrator picks up the flag on the next heartbeat via its existing priority bump.

## Rate-Limit Awareness

Before any other `gh` call, the script invokes `gh api rate_limit` and reads `resources.core.remaining`. If remaining < 100, the sweep is deferred: a run-log entry with `status: "deferred-rate-limit"` is appended, state is left untouched, and the script exits 0. The orchestrator's next cadence fire will retry.

## Non-Negotiables

- **Additive writes only.** This skill creates, archives, and annotates; it never reclassifies a PR's mid-pipeline phase. Phase-skill territory is off-limits.
- **Atomic state writes.** Always via `state_io.save_state`. Direct file writes to `state.json` are forbidden.
- **No silent drops.** Every PR change emits a structured run-log line summarizing new/archived/new-comment/idle counts.

## Verification

Smoke-test against a configured project (requires `gh` auth):

```bash
python3 scripts/run.py --dry-run --verbose
```

Expected on a fresh project with PRs open: exit 0, run-log entry with `status: "dry-run"`, `report.new_prs` listing every open PR number. No state changes on disk.

Run the unit tests (no `gh` calls; all subprocess invocations mocked):

```bash
python3 -m unittest discover scripts/tests
```
