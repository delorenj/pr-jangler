---
name: prj-orchestrator
description: PR Jangler cron heartbeat dispatcher. Use when cron invokes one heartbeat of the PR backlog workflow or when the user runs 'prj-orchestrator' for manual debug.
---

# prj-orchestrator

## Overview

This skill is the cron heartbeat for the PR Jangler module. Each invocation executes exactly one state transition: load the persistent queue at `_bmad-output/pr-workflow/state.json`, pick the highest-priority next action, dispatch to the target phase skill, persist state, append a structured run-log entry, exit. Idempotent with atomic state writes. System actions (discovery on cadence, daily report at the configured hour) outrank per-PR actions.

Act as a reliability-minded ops engineer. Treat the queue as authoritative state, the run-log as the audit trail, and "never silently fail" as the prime directive.

## Conventions

- Bare paths (e.g. `scripts/run.py`) resolve from the skill root.
- `{project-root}/...` resolves from the project working directory.
- Configuration variables live under `[modules.prj]` in `{project-root}/_bmad/config.toml`.

## On Activation

Load configuration from `{project-root}/_bmad/config.toml` (the `[modules.prj]` block). Required key: `prj_repo`. If absent, exit early with a clear "configure prj_repo first" message and a run-log entry tagged `misconfigured`. Defaults for optional keys are applied by the scripts themselves.

Execute the heartbeat:

```bash
python3 scripts/run.py
```

That single script orchestrates everything: state initialization on first run, action selection, dispatch (with v1 stub fallback for not-yet-built phase skills), atomic state persistence, and run-log append. Flags:

- `--dry-run` compute next action, log the intent, do not dispatch
- `--select-only` pure selector mode (see below) for external daemons
- `--verbose` emit diagnostics to stderr
- `--once` no-op flag for cron clarity (always single-shot)
- `--project-root <path>` override autodetect

See `python3 scripts/run.py --help` for full detail.

## `--select-only` (daemon seam)

`--select-only` is the read-only selector mode. It computes the highest-priority next action and emits one JSON object on stdout — without mutating state, without appending a run-log entry, and without dispatching. It is the integration seam for an external control plane (e.g. an app-server-backed `prj-agentd`) that wants the deterministic selector to choose the action while the daemon owns execution, streaming, approvals, and thread persistence.

Output schema:

```json
{
  "status": "action-selected" | "idle" | "misconfigured",
  "repo": "owner/name" | null,
  "pr": 123 | null,
  "phase": "ReviewPending" | null,
  "skill": "prj-review" | null,
  "mode": "pr" | "comment" | null,
  "priority": 130,
  "reason": "highest-priority PR action (PR 123, phase ReviewPending)",
  "state_sha": "no-state" | "<sha256 hex of state.json>"
}
```

Contract:

- **No state mutation.** `state.json` is not initialized, the heartbeat counter is not incremented, no fields are written. A fresh project (no `state.json`) returns an `action-selected` for `prj-discover` against an ephemeral empty state.
- **No run-log entry.** Pure selector calls are silent in the audit trail; they are not actions.
- **No dispatch.** The caller is responsible for executing (or not executing) the returned action.
- **`state_sha` is the idempotency token.** Daemons that select an action and later execute it should re-check `state_sha` to detect drift between selection and execution.
- **Exit codes.** `0` on success (including `idle` and `action-selected`); `2` only for `misconfigured` (prj_repo missing). The misconfigured JSON is still emitted on stdout for machine consumption.

Example:

```bash
python3 scripts/run.py --select-only
# {"mode": null, "phase": null, "pr": null, "priority": 500, "reason": "queue empty",
#  "repo": "owner/repo", "skill": "prj-discover", "state_sha": "no-state",
#  "status": "action-selected"}
```

## Architecture

The orchestrator is intentionally thin. All deterministic work lives in Python scripts. The LLM activation context exists so the Skill tool can dispatch to phase skills once they are installed; in v1 stub mode, the dispatch path falls back to `bmad run` subprocess invocations or logs a `stub: not built` entry when the target skill is missing.

| Concern | Lives in |
| --- | --- |
| State I/O (atomic load, validate, save, init) | `scripts/state_io.py` |
| Priority computation and next-action selection | `scripts/select_next_action.py` |
| Dispatch and run-log append | `scripts/run.py` |
| State schema (reference) | `assets/state-schema.json` |
| Initial empty state template | `assets/state-empty.json` |

## State Locations

Owned by this skill. All other phase skills MUST go through `scripts/state_io.py` to read or write state to preserve atomicity.

- Queue: `{project-root}/_bmad-output/pr-workflow/state.json`
- Per-PR cache: `{project-root}/_bmad-output/pr-workflow/prs/{pr_number}/`
- Run-log (daily, append-only): `{project-root}/_bmad-output/pr-workflow/logs/{YYYY-MM-DD}.jsonl`
- Reports archive: `{project-root}/_bmad-output/pr-workflow/reports/{YYYY-MM-DD}.md`

## Priority Logic

System actions outrank per-PR actions.

1. **Daily report** (priority 700): local hour >= `prj_report_hour_local` AND no report sent today.
2. **Discovery on cadence** (priority 500): queue is empty, OR `heartbeat_count % prj_discover_every_n == 0`.

Per-PR actions, in descending priority:

3. **PleaseAdvise unacknowledged** (priority 1000, outranks everything): rises until the user-acknowledged override label is applied.
4. **Per-PR `next_action`** set by the previous phase skill. Base 100, with bumps:
   - `new_comments_since_triage > 0`: +50
   - `needs_retriage == true`: +30
   - Age tiebreak: +1 per day idle since `phase_entered_at`, capped at 30.

Terminal phases (`ReadyToMerge`, `Blocked`, `Rejected`, `Archived`) carry no `next_action` and are skipped. If no action qualifies, the heartbeat is a no-op: log idle, exit cleanly.

## v1 Stub Mode

The other 10 phase skills (`prj-discover`, `prj-triage`, `prj-verify-claim`, `prj-review`, `prj-detect-overlap`, `prj-plan-fix`, `prj-implement-fix`, `prj-validate-adversarial`, `prj-decision`, `prj-report-daily`) do not exist yet. When the orchestrator dispatches to a target skill whose `SKILL.md` is not present at `{project-root}/skills/{skill-name}/`, it:

- Records `status: "stub: not built"` in the run-log entry
- Does NOT transition state (the PR stays in its current phase)
- Exits cleanly with code 0

This lets the orchestrator be exercised end-to-end before any other skill lands.

## Non-Negotiables

- **Idempotent per action.** If a heartbeat is interrupted mid-run, the next heartbeat picks up cleanly. Atomic writes via tmp + fsync + rename.
- **Never silently fail.** Every outcome (dispatch, stub, abort, error) emits a structured run-log entry. Errors also surface on stderr.
- **No PR content inspection here.** The orchestrator reads queue metadata and dispatches. Code review, classification, and verification belong to the phase skills.

## Verification

After install, smoke-test with:

```bash
python3 scripts/run.py --dry-run --verbose
```

Expected on a fresh project (no state, `prj_repo` configured): exit 0, run-log entry showing `action: dispatch`, `skill: prj-discover`, `status: dry-run`. On a project missing `prj_repo`: exit 2, run-log entry `status: misconfigured`.

Run unit tests:

```bash
python3 -m unittest discover scripts/tests
```
