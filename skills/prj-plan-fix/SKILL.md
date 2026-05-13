---
name: prj-plan-fix
description: PR Jangler fix-planning workflow. Use when a Trello-like comment claim on a PR has been verified by prj-verify-claim and a fix plus failing-test plan must be designed before any implementation. Activated by prj-orchestrator when a PR's phase is FixPlan.
---

# prj-plan-fix

## Overview

For one PR whose latest actionable claim has been verified, design (a) a failing test that demonstrates the bug, (b) the smallest diff that makes the test pass, (c) the rationale for why the fix is correct, and (d) the risks of applying it. Output is a structured `fix-plan.md` in the per-PR cache. This skill never writes to GitHub; the implementation skill (`prj-implement-fix`) does that after the adversarial gate.

Act as a disciplined fix-planner. Treat "no failing test, no fix" as an inviolable contract.

## Conventions

- Bare paths (e.g. `scripts/run.py`) resolve from the skill root.
- `{project-root}/...` resolves from the project working directory.
- Configuration variables live under `[modules.prj]` in `{project-root}/_bmad/config.toml`.

## On Activation

Run the entry script:

```bash
python3 scripts/run.py --pr-number <n>
```

Flags:

- `--pr-number <n>` (required) PR being planned
- `--dry-run` design + run tests but do NOT write fix-plan.md or transition state
- `--verbose` emit diagnostics to stderr
- `--project-root <path>` override autodetect

The script orchestrates the deterministic work: load verification, reuse-or-provision worktree, run the failing-test gate, write fix-plan.md, persist state, append runlog entry. The LLM-driven steps (designing the failing test, designing the fix, writing rationale + risk) happen via the prompts in this SKILL.md before each deterministic check.

## Phase Contract

| Pre-state | Post-state (success) | Post-state (test-not-failing) | Post-state (rejected x2) |
| --- | --- | --- | --- |
| `FixPlan` | `AdversarialCheck` | unchanged + log `test-does-not-demonstrate-bug` | `PleaseAdvise` |

Refuses to run unless `prs/{n}/verification.md` exists with `verdict: verified`. On refusal, emits exit code 2 and a runlog entry tagged `not-verified`.

## Inputs

- `{project-root}/_bmad-output/pr-workflow/prs/{n}/verification.md` (verified claim and observation)
- `{project-root}/_bmad-output/pr-workflow/prs/{n}/meta.json` (PR title, head ref, files-changed)
- Per-PR worktree at `{project-root}/_bmad-output/pr-workflow/worktrees/{n}/` (reused if present, else provisioned via `gh pr checkout`)
- Project test runner (autodetected; `prj_test_runner` override in `[modules.prj]`)

## Outputs

- `{project-root}/_bmad-output/pr-workflow/prs/{n}/fix-plan.md` (the plan document)
- State transition `FixPlan -> AdversarialCheck` with `next_action.skill = prj-validate-adversarial`
- Runlog entry tagged `plan-fix` with status `ok | test-does-not-demonstrate-bug | regression-detected | escalated-please-advise`

## Workflow

1. **Refuse-or-proceed.** Read `verification.md`. If verdict is not `verified`, abort with exit 2 and runlog entry `not-verified`. No worktree work.

2. **Worktree.** Reuse `worktrees/{n}/` if it exists AND is the right branch; else provision fresh via `gh pr checkout` into that path. Owned by `scripts/worktree.py`.

3. **Design the failing test (prompt step).** Read the claim and observation from `verification.md`. Inspect existing test patterns in the repo (look at `tests/`, `test/`, `*.test.ts`, `*_test.py`). Write a single new test that exercises the alleged bug. The test MUST capture the bug behavior, not merely the proposed fix; ask yourself: "if I remove the fix, does this test still fail for the bug-shaped reason?"

   Heuristics:
   - One test per claim. Keep scope tight.
   - Prefer the smallest possible reproduction. Mock external services if the repo's existing tests do so.
   - Name the test after the bug, not after the fix (e.g., `test_attach_data_handles_empty_buffer`, not `test_buffer_length_check`).
   - If a test cannot reasonably be written (e.g., the bug is purely visual or environmental), DO NOT advance. Record in the runlog as `test-not-writable` and exit non-zero so escalation logic kicks in.

4. **Failing-test gate.** Run the new test via `scripts/test_runner.py`. The script verifies the new test EXITS NON-ZERO (i.e., actually fails). If it passes, refuse to advance: runlog `test-does-not-demonstrate-bug`, no state transition, exit non-zero. This is non-negotiable.

5. **Design the fix (prompt step).** With the failing test in hand, design the smallest diff that makes the test pass. Justify each file touched. If the fix grows beyond one file, explicitly defend the breadth.

   Heuristics:
   - Smallest diff that makes the new test pass without regressing the old suite.
   - Touch one file by default; touch more only with explicit rationale per file.
   - Prefer reusing existing helpers over introducing new abstractions.
   - Do not refactor opportunistically. Refactor is a separate PR.

6. **Apply fix + verify.** `scripts/test_runner.py` applies the diff (or asks the LLM to apply it via Edit/Write), runs the new test (must pass), then runs the full test suite (must pass with no regressions). On regression, refuse to advance with runlog `regression-detected`. Revert the diff in the worktree before exiting.

7. **Articulate rationale and risk (prompt step).** Write two paragraphs:
   - **Rationale.** Why this fix is correct. Cite project conventions if Hindsight's `prj` bank provides them (look at `CLAUDE.md` and any conventions docs the team has accumulated). Reference the failing test as the contract.
   - **Risk.** What could go wrong. Side effects on shared utilities. Public API surface touched. Configuration coupling. Anything a maintainer would want flagged.

8. **Write fix-plan.md.** `scripts/plan_io.py` writes the structured plan with the required schema. The schema is validated before write; malformed plans are refused.

9. **State transition + runlog.** `scripts/plan_io.py` advances `FixPlan -> AdversarialCheck`, updates `next_action`, appends a runlog entry. The retry counter on the PR is incremented if this is a redo after adversarial rejection.

## fix-plan.md Schema

A valid `fix-plan.md` has the following sections, in this order, with these exact headings:

```markdown
# Fix Plan: PR #{n}

## Claim

(One paragraph quoting and summarizing the verified claim from verification.md.)

## Failing Test

```{lang}
(The test code that demonstrates the bug. Must be a runnable test.)
```

## Proposed Diff

```diff
(Unified diff of the proposed fix. Smallest viable change.)
```

## Rationale

(Why this fix is correct. Cite conventions if known.)

## Risk

(What could go wrong. Side effects, scope concerns.)

## Attempts

- attempt: 1
- previous_rejections: []
```

`Attempts` increments by one on each redo after `prj-validate-adversarial` rejects. `previous_rejections` is a list of one-line summaries from prior adversarial verdicts, appended in order.

## Retry Semantics

- First plan: `attempts: 1`, no `previous_rejections`.
- Adversarial rejects: orchestrator re-queues `FixPlan` for this PR. `scripts/plan_io.py` increments `attempts` and appends the rejection summary on the next plan run.
- After **two consecutive rejections** (attempts would become 3), this skill REFUSES to plan again. It transitions the PR to `PleaseAdvise`, appends a `escalated-please-advise` runlog entry, and exits. The orchestrator surfaces PleaseAdvise items at top priority on the next heartbeat and in the daily report.

## Non-Negotiables

- **Failing test first.** If the new test does not actually fail before the fix, the fix does not advance. No exceptions.
- **No GitHub writes.** Comments and PRs are the implementation skill's job. This skill only touches local cache and state.
- **No silent regressions.** A diff that breaks any pre-existing test is rejected. Revert the worktree before exiting.
- **Two-strike escalation.** Two consecutive adversarial rejections promote the PR to `PleaseAdvise`. Do not loop forever.
- **Stateless invocation.** Read state at start, write at end, exit. No daemons, no in-memory caches across runs.

## Verification

After install, smoke-test with:

```bash
python3 scripts/run.py --help
```

Run unit tests:

```bash
python3 -m unittest discover scripts/tests -v
```
