---
name: prj-validate-adversarial
description: PR Jangler adversarial fix-plan validator. Use when the orchestrator dispatches AdversarialCheck on a per-PR fix-plan, or when the user runs 'prj-validate-adversarial' for manual debug.
---

# prj-validate-adversarial

## Overview

This skill is the worst-case guard rail of the PR Jangler module. One invocation reviews a single per-PR fix-plan from a structurally separate context: it loads `prs/{n}/fix-plan.md`, `prs/{n}/verification.md`, and `prs/{n}/review.md`, runs the full project regression suite against the fix branch, then evaluates a six-item adversarial checklist with `reject` as the default verdict. A pass advances the PR to `FixImpl`; a reject sends it back to `FixPlan` with structured concerns appended; two consecutive rejects on the same plan, or any new regression in the full test suite, escalate to `PleaseAdvise`.

Act as a skeptical adversarial reviewer. You did NOT plan this fix. You do NOT carry the planner's framing. Treat suspicion as default mental mode and require evidence, not plausibility.

## Conventions

- Bare paths (e.g. `scripts/run.py`) resolve from the skill root.
- `{project-root}/...` resolves from the project working directory.
- Configuration variables live under `[modules.prj]` in `{project-root}/_bmad/config.toml`.
- All state I/O goes through `prj-orchestrator`'s `state_io` module (imported via `sys.path`); this skill never touches `state.json` directly.

## On Activation

Load configuration from `{project-root}/_bmad/config.toml` (the `[modules.prj]` block). Required key: `prj_repo`. Optional: `prj_test_runner` (auto-detected from the repo otherwise). If `prj_repo` is missing, exit early with status `misconfigured` and a clear run-log entry.

Execute the validation:

```bash
python3 scripts/run.py --pr <N>
```

Flags:

- `--pr <N>` (required) PR number whose fix-plan to validate
- `--dry-run` skip regression suite and state writes; emit the would-be verdict skeleton
- `--verbose` emit progress to stderr
- `--project-root <path>` override autodetect

See `python3 scripts/run.py --help` for full detail.

## How the LLM Step Fits

The actual six-item adversarial reasoning is an LLM task, not a script task. The flow is:

1. `scripts/run.py` gathers inputs: it reads the per-PR cache documents, runs the regression suite via `scripts/regression_run.py`, and assembles a context bundle.
2. If `regression_run.py` reports any regressions (a previously-passing test now failing on the fix branch), the script short-circuits to `verdict: reject` with `item: regressions, passes: false` and writes `adversarial.md` directly. This is the hard structural gate. The LLM is NOT consulted; structural gates always win.
3. Otherwise the orchestrating agent (you, when this skill is active) reads the prompt at `assets/prompt-adversarial.md` and the gathered context, and produces a JSON response covering all six checklist items.
4. The agent passes that JSON back to `scripts/run.py --finalize <response-path>` (or pipes via stdin), which `scripts/checklist_io.py` validates and `scripts/adversarial_io.py` persists.

## Architecture

Three Python modules, single concerns. The LLM activation context exists so the orchestrator's Skill-tool dispatch resolves cleanly when AdversarialCheck fires.

| Concern | Lives in |
| --- | --- |
| Shared state I/O (atomic load, validate, save, runlog) | `state_io` (imported from `prj-orchestrator/scripts`) |
| Regression-suite execution and gating | `scripts/regression_run.py` |
| Adversarial-response structural validation | `scripts/checklist_io.py` |
| Verdict persistence, retry counter, state transitions | `scripts/adversarial_io.py` |
| Orchestration: config, gather, dispatch, runlog | `scripts/run.py` |
| Adversarial reasoning prompt (LLM-facing) | `assets/prompt-adversarial.md` |

The adversarial prompt is intentionally stored as a separate asset and not inlined here. This enforces structural separation from `prj-plan-fix`: the planner's framing never leaks into the validator, and the prompt is auditable as a versioned artifact.

## Six-Item Checklist (Enforced)

`checklist_io.py` rejects any response missing or misnaming an item.

1. `test_validity` — does the failing test demonstrate the alleged bug, or merely the proposed fix's own logic?
2. `scope` — does the diff change anything beyond fixing the bug?
3. `side_effects` — does the fix touch shared utilities, public APIs, or configs?
4. `regressions` — full project test suite must show zero new failures on the fix branch. STRUCTURAL HARD GATE; auto-reject on any new failure.
5. `ac_alignment` — does the fix align with the PR's stated acceptance criteria, not just the requesting comment?
6. `worst_case_probe` — what breaks if we do NOT apply the fix? Is the bug actually load-bearing?

Each item must produce `{passes: bool, finding: str}`. A single `passes: false` is grounds for `reject`. See `assets/prompt-adversarial.md` for the full adversarial prompt and pass criteria.

## State and Cache Touched

Reads from:

- `{project-root}/_bmad/config.toml` (config: `prj_repo`, `prj_test_runner`)
- `{project-root}/_bmad-output/pr-workflow/state.json` (current phase + retry counter)
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/fix-plan.md`
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/verification.md`
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/review.md`

Writes to:

- `{project-root}/_bmad-output/pr-workflow/state.json` (via `state_io.save_state`, atomic) — phase transition + `adversarial_reject_count` bookkeeping
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/adversarial.md` — full verdict report with checklist findings and regression summary
- `{project-root}/_bmad-output/pr-workflow/logs/{YYYY-MM-DD}.jsonl` (append-only run-log)

## Phase Transitions

| Verdict | Next phase | Side effects |
| --- | --- | --- |
| `pass` | `FixImpl` | `next_action = {skill: "prj-implement-fix"}`; `adversarial_reject_count` reset to 0 |
| `reject` (first attempt on this plan) | `FixPlan` | `next_action = {skill: "prj-plan-fix"}`; `adversarial_reject_count` incremented; concerns appended to fix-plan context for the redo |
| `reject` (second consecutive) | `PleaseAdvise` | Same as `escalate` |
| `escalate` | `PleaseAdvise` | `next_action = null`; `adversarial_reject_count` reset to 0 |

The two-reject-then-escalate rule is enforced in `adversarial_io.py`, not left to the LLM. This prevents an infinite replan loop on a fundamentally flawed plan.

## Non-Negotiables

- **Default verdict is reject.** The plan must affirmatively clear all six checklist items.
- **Regression suite is the strict floor.** Any new failure on the fix branch = automatic reject. Implemented as a structural short-circuit in `regression_run.py`; the LLM cannot override it.
- **Structural separation from `prj-plan-fix`.** This skill runs in a separate invocation context. The adversarial prompt is written from a skeptical persona and lives in `assets/prompt-adversarial.md`. It does NOT inherit the planner's framing.
- **Atomic state writes.** Always via `state_io.save_state`. Direct writes to `state.json` are forbidden.
- **No silent drops.** Every outcome (pass, reject, escalate, regression-gate, error) emits a structured run-log line.

## Verification

After install, smoke-test by validating the response-structure path with no real PR (no fix-plan needed):

```bash
python3 scripts/checklist_io.py --check '{"verdict": "pass", "findings": []}'
```

Expected: exit code 2 with a `status: "invalid"` JSON entry describing the missing checklist items.

Run unit tests (no subprocess shell-outs to the real test runner; all external calls mocked):

```bash
python3 -m unittest discover scripts/tests
```
