---
name: prj-detect-overlap
description: PR Jangler duplicate and overlap detector. Use when the orchestrator dispatches the overlap check after triage marked a PR as possible-duplicate, or when the user runs 'prj-detect-overlap' for manual debug.
---

# prj-detect-overlap

## Overview

This skill is the overlap-and-duplication detector for the PR Jangler module. One invocation scores one target PR's file footprint against every other open PR in the queue, runs a structured semantic comparison on the high-overlap pairs, and writes a verdict that drives the next phase transition. The deterministic plumbing (file overlap scan via `gh pr diff --name-only`, label application via `gh pr edit`, overlap report rendering, atomic state I/O) lives in Python scripts. The semantic comparison itself is LLM-driven from `assets/prompt-semantic-compare.md`; the script accepts the per-pair verdicts on the CLI and persists them.

Act as a careful cross-referencer. Treat any 2-shared-files pair as worth a semantic look, but apply verdicts conservatively: `independent` is the default when comparison is unclear, `redundant` requires near-identical intent on near-identical files, and `conflicting` requires the two diffs to disagree on the same lines or contracts.

## Conventions

- Bare paths (e.g. `scripts/run.py`) resolve from the skill root.
- `{project-root}/...` resolves from the project working directory.
- Configuration variables live under `[modules.prj]` in `{project-root}/_bmad/config.toml`.
- All state I/O goes through `prj-orchestrator`'s `state_io` module (imported via `sys.path`); this skill never touches `state.json` directly.

## On Activation

The orchestrator passes `--pr-number N` (the target). Load configuration from `{project-root}/_bmad/config.toml` (`[modules.prj]`). Required key: `prj_repo`. If absent, exit early with status `misconfigured` and a clear run-log entry.

Two-step workflow:

### Step 1: scan for overlap pairs

```bash
python3 scripts/run.py --pr-number N --scan
```

This invokes `gh pr diff --name-only` for the target PR and every other open PR, computes intersections, and emits a JSON pairs report on stdout. Each pair carries the two PR numbers, the shared file list, and the overlap count. Pairs with fewer than 2 shared files are filtered out and listed under `skipped`.

If `pairs` is empty, the script writes the overlap report (empty table), transitions the PR to `ReviewPending`, applies no labels, and exits. No semantic comparison is needed.

### Step 2: invoke semantic comparison for each pair

For every pair returned in step 1, read `assets/prompt-semantic-compare.md`, fill in the variables (target PR diff, other PR diff, shared files), and run the comparison. Pick exactly one verdict per pair:

- `independent` — the two diffs touch the same files but address unrelated concerns
- `conflicting` — the two diffs disagree on the same lines, the same contracts, or mutually exclusive approaches
- `complementary` — the two diffs work together; the older one logically lands first
- `redundant` — the two diffs do the same thing on the same surface; one of them should be closed

Then persist all verdicts in one call:

```bash
python3 scripts/run.py --pr-number N --verdicts-json '{"82": "complementary", "75": "independent"}'
```

The script applies labels, writes the overlap report at `prs/{n}/overlap.md`, executes the phase transition, and appends a run-log entry. Pass `--rationale-json '{"82": "...", "75": "..."}'` to surface one-paragraph rationales in the report (optional but strongly recommended; the prompt asks for them).

Flags on `scripts/run.py`:

- `--pr-number N` (required) — target PR number.
- `--scan` — run step 1 only (emit pairs as JSON, do not persist).
- `--verdicts-json STR` — JSON object mapping `other_pr_number` (string) -> verdict.
- `--rationale-json STR` — JSON object mapping `other_pr_number` (string) -> one-paragraph rationale.
- `--dry-run` — compute everything, log intent, do not call `gh`, do not write state, do not write the report.
- `--skip-label` — perform state and cache writes but skip the `gh` label call.
- `--verbose` — emit progress diagnostics to stderr.
- `--project-root PATH` — override autodetect.

See `python3 scripts/run.py --help` for full detail.

## Verdict Aggregation and Phase Transitions

The target PR receives one final phase based on the strongest verdict across all pairs (most-severe wins):

| Strongest pair verdict | Target PR phase | Target PR next_action | Labels applied |
| --- | --- | --- | --- |
| `redundant` | `Rejected` | `null` | `prj/overlap:duplicate`, `prj/triage:definite-no` |
| `conflicting` | `Blocked` (target is newer) | `null` | `prj/overlap:conflicts-with-{other-pr}` (per pair) |
| `complementary` | `ReviewPending` | `{skill: "prj-review", mode: null}` | `prj/overlap:depends-on-{older-pr}` (per pair) |
| `independent` only | `ReviewPending` | `{skill: "prj-review", mode: null}` | none |

Severity order: `redundant > conflicting > complementary > independent`.

If verdict is `conflicting` and the target PR is older than its counterpart (by `phase_entered_at` ascending, fallback to PR number ascending), the OTHER PR moves to `Blocked` instead, with `blocking_on` set to the target. Either way, the script also sets `blocking_on` on the blocked PR.

For `complementary` pairs, `older_pr` is the lower PR number tiebroken by earlier `phase_entered_at`. The label encodes the dependency direction: the newer PR carries `prj/overlap:depends-on-{older-pr-number}`.

`complementary` verdicts also append a one-line note to the target PR's cache at `prs/{n}/overlap-notes.md` so the daily report can surface the merge-ordering recommendation.

## Architecture

| Concern | Lives in |
| --- | --- |
| Shared state I/O (atomic load, save, runlog) | `state_io` (imported from `prj-orchestrator/scripts`) |
| File overlap computation + `gh pr diff --name-only` calls | `scripts/overlap_scan.py` |
| Label application + report rendering + phase transitions | `scripts/verdict_io.py` |
| Orchestration: config, scan, verdict persist, runlog | `scripts/run.py` |
| Semantic-compare prompt (LLM input) | `assets/prompt-semantic-compare.md` |

The script split keeps each module narrow and independently testable. `run.py` itself is mostly argument routing. The semantic comparison is the LLM's job, not Python's: the prompt asset is the contract.

## State and Cache Touched

This skill writes to:

- `{project-root}/_bmad-output/pr-workflow/state.json` (via `state_io.save_state`, atomic)
- `{project-root}/_bmad-output/pr-workflow/prs/{pr_number}/overlap.md` (overwritten each run)
- `{project-root}/_bmad-output/pr-workflow/prs/{pr_number}/overlap-notes.md` (append-only, complementary pairs only)
- `{project-root}/_bmad-output/pr-workflow/logs/{YYYY-MM-DD}.jsonl` (append-only run-log)
- GitHub: `prj/overlap:*` labels per the verdict aggregation table

This skill reads from:

- `{project-root}/_bmad/config.toml` (config)
- `{project-root}/_bmad-output/pr-workflow/state.json` (prior queue state; finds all other open PRs)
- GitHub via `gh pr diff --name-only` (changed-file lists for target and every other open PR)

## Non-Negotiables

- **Compare against ALL other open PRs.** Not just recent ones. Stale PR overlaps must be detected.
- **2+ shared files triggers semantic compare.** No skipping the LLM step when the threshold is hit.
- **Conservative verdicts.** When the comparison is ambiguous, pick `independent`. `redundant` requires near-identical intent on near-identical files.
- **Atomic state writes.** Always via `state_io.save_state`. Direct file writes to `state.json` are forbidden.
- **Idempotent persistence.** Rerunning with the same inputs produces the same state. `overlap.md` is overwritten; `overlap-notes.md` appends.

## Verification

Smoke-test the scan step (requires `gh` auth, reads target PR's changed files):

```bash
python3 scripts/run.py --pr-number 82 --scan --verbose
```

Expected: exit 0, JSON on stdout with `pairs` array, no state changes.

Then exercise verdict persistence in dry-run mode (no `gh`, no state writes):

```bash
python3 scripts/run.py --pr-number 82 --verdicts-json '{"81": "complementary"}' --dry-run --verbose
```

Expected: exit 0, run-log entry with `status: "dry-run"`, no state changes, no label applied, no overlap.md written.

Run the unit tests (no `gh` calls; all subprocess invocations mocked):

```bash
python3 -m unittest discover scripts/tests
```
