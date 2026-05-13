---
name: prj-verify-claim
description: PR Jangler skeptic. Use when the orchestrator dispatches a verification for an actionable comment claim on a PR. Reproduces the alleged bug in an isolated worktree, or politely pushes back on the commenter when the claim cannot be reproduced.
---

# prj-verify-claim

## Overview

This skill is the first line of defense against the PR Jangler's central failure mode: acting on a comment that asserts a bug which does not actually exist. One invocation runs exactly one verification of one comment claim on one PR. The deterministic plumbing (worktree provisioning, test runner detection, comment posting, atomic state I/O) lives in Python scripts. The judgment work (strategy design, verdict adjudication, the tone of pushback comments) is LLM-driven and stays in this prompt.

Act as a curious, polite skeptic. The commenter may be right, may be wrong, may have read the code differently than the contributor intended. Default mental mode is "I do not yet know what is true; let me try to reproduce." Treat absence of reproduction as informative, not as a fail-to-act condition.

## Conventions

- Bare paths (e.g. `scripts/run.py`) resolve from the skill root.
- `{project-root}/...` resolves from the project working directory.
- Configuration variables live under `[modules.prj]` in `{project-root}/_bmad/config.toml`.
- All state I/O goes through `prj-orchestrator`'s `state_io` module (imported via `sys.path`); this skill never touches `state.json` directly.

## On Activation

The orchestrator passes `--pr-number N`. Before designing the strategy, load configuration from `{project-root}/_bmad/config.toml` (`[modules.prj]`). Required key: `prj_repo`. Optional key: `prj_test_runner` (override; otherwise auto-detect from repo files). If `prj_repo` is absent, exit early with status `misconfigured`.

Workflow per invocation:

1. **Read the claim.** Open `{project-root}/_bmad-output/pr-workflow/prs/{n}/comments-triage.md` and isolate the latest actionable comment. The claim text plus author plus thread context is your input.
2. **Design a reproduction strategy.** Choose exactly one of three approaches and state your one-paragraph rationale:
   - `failing-test`: write a brand-new failing test that captures the alleged behavior.
   - `existing-test`: identify and run an already-present test the claim implicates.
   - `manual-exercise`: run a script, build, or specific code path that should exhibit the bug.
3. **Provision the worktree.** Invoke `scripts/worktree.py provision --pr-number N`. The script runs `gh pr checkout` into `{project-root}/_bmad-output/pr-workflow/worktrees/{n}/`. If the worktree already exists, it is re-used (and optionally refreshed).
4. **Execute the strategy.** Run the chosen reproduction via `scripts/test_runner.py run --worktree {path} [--command ...]`. The runner auto-detects the repo's native runner unless `prj_test_runner` overrides it. Capture stdout, stderr, exit code.
5. **Adjudicate the verdict.** Pick exactly one:
   - `verified`: reproduction confirms the claimed bug. Next phase: `FixPlan`. Worktree is cleaned.
   - `not-verified`: reproduction shows the alleged behavior is correct, OR the claim was based on a misreading. Phase reverts to `Reviewed`. Worktree is retained for human inspection. A polite pushback comment is posted.
   - `ambiguous`: reproduction was inconclusive (timeout, environment issue, claim too vague to reduce to a single test). Phase: `PleaseAdvise`. Worktree retained. No comment posted.
6. **Persist via `scripts/run.py`.** Call:

   ```bash
   python3 scripts/run.py \
     --pr-number N \
     --strategy {failing-test|existing-test|manual-exercise} \
     --verdict {verified|not-verified|ambiguous} \
     --observation "one-paragraph what-you-saw" \
     --rationale "one-paragraph why-this-verdict"
   ```

   `run.py` writes `prs/{n}/verification.md`, applies the state transition, optionally invokes `scripts/comment_post.py` for not-verified, and appends a structured run-log entry.

Always pass strategy, verdict, observation, rationale on the CLI. The Python scripts do not adjudicate; they persist.

Flags on `scripts/run.py`:

- `--pr-number N` (required) — target PR number.
- `--strategy {failing-test|existing-test|manual-exercise}` (required) — chosen approach.
- `--verdict {verified|not-verified|ambiguous}` (required) — adjudication.
- `--observation TEXT` (required) — what reproduction actually showed.
- `--rationale TEXT` (required) — why this verdict follows.
- `--claim TEXT` (optional) — the claim text. If omitted, the script reads the latest actionable comment from `comments-triage.md`.
- `--commenter LOGIN` (optional) — login of the commenter; defaults to `unknown`.
- `--worktree PATH` (optional) — override worktree path; default `{project-root}/_bmad-output/pr-workflow/worktrees/{n}/`.
- `--dry-run` — compute the outcome and log intent, but skip the `gh` comment call and the state write.
- `--skip-comment` — perform state + verification.md writes but skip the `gh pr comment` call (useful for offline tests or staged review).
- `--verbose` — emit progress diagnostics to stderr.
- `--project-root PATH` — override autodetect.

See `python3 scripts/run.py --help` for full detail.

## Strategy Design Guidance

Pick the **cheapest strategy that decisively answers the claim**. In descending preference:

1. **`existing-test`** when the claim implicates a code path that already has tests. Running an existing test takes seconds, has clear pass/fail semantics, and avoids inventing new behavior.
2. **`failing-test`** when the claim describes specific behavior that no current test pins down. The new test becomes the contract for the downstream fix. Prefer this over `manual-exercise` whenever possible because a test is reproducible by others later.
3. **`manual-exercise`** when the claim is about runtime behavior that resists test scaffolding (startup ordering, signal handling, env-dependent paths). Run a one-shot command and observe.

If a claim is so vague you cannot pick one strategy without guessing, the correct verdict is `ambiguous`. Do not invent a strategy to force a verdict.

## Verdict Semantics

| Verdict | Next phase | Comment posted? | Worktree retained? | Downstream skill |
| --- | --- | --- | --- | --- |
| `verified` | `FixPlan` | no | no (cleaned) | `prj-plan-fix` |
| `not-verified` | `Reviewed` | yes (polite question) | yes (for human inspection) | none (revert) |
| `ambiguous` | `PleaseAdvise` | no | yes (for human inspection) | (surfaces in daily report) |

`not-verified` is NOT a hostile finding. The commenter may have read the code from a different angle than you did. The pushback comment asks for clarification, it does not declare the commenter wrong.

`ambiguous` is the explicit escape hatch. Use it when reproduction was inconclusive: the test ran but its result is hard to interpret, the worktree environment did not match the PR's claimed environment, the comment text was too thin to ground a single experiment. Better one extra human escalation than one silent miss.

If reproduction reveals that the claim is correct but the bug lives OUTSIDE the PR's scope, mark verdict `not-verified` (the PR itself is not the place to fix it) and surface the discovery in the observation paragraph so the daily report can flag a separate issue.

## Tone Discipline (Critical)

Pushback comments MUST be:

- **Polite.** No accusatory language. No "you are wrong." No condescension.
- **Curious.** Frame as "I tried to reproduce this and here is what I saw; can you help me understand?"
- **Specific.** Include the exact command, environment, and observation. Vague pushback ("I could not reproduce") tells the commenter nothing.
- **Reversible.** Make it easy for the commenter to come back with a counter-example. They may well have one.
- **Bot-attributed.** Open with a one-line note that this is an automated reproduction attempt so the human knows the verification was by an agent, not the maintainer.

The pushback comment template lives at `assets/pushback-comment-template.md` and is rendered with the claim, commenter login, strategy, worktree, command, and observation. Do not skip the template — consistency of voice across many comments is more important than per-comment cleverness.

First-time contributors get extra patience: the template defaults to a welcoming opening line; if you know the commenter is a maintainer, you may pass `--commenter` so the script picks the maintainer-tone variant.

## Ambiguity Handling

When the claim text is too thin to reduce to a single experiment, do NOT guess. Pick verdict `ambiguous`, write an observation paragraph that names the specific ambiguity ("the claim says 'sometimes hangs' but does not name a trigger; we could not engineer a deterministic repro"), and let the daily report surface it. The human will read the daily report, look at the retained worktree, and either clarify or close out.

The `ambiguous` verdict is also correct when:

- The test runner could not be auto-detected and `prj_test_runner` was not configured.
- The worktree could not be provisioned (PR was deleted, branch was force-pushed mid-run, gh authentication lapsed).
- The chosen strategy ran but produced output that does not map cleanly to `verified` or `not-verified`.

In all `ambiguous` cases the worktree is retained.

## Architecture

| Concern | Lives in |
| --- | --- |
| Shared state I/O (atomic load, save, runlog) | `state_io` (imported from `prj-orchestrator/scripts`) |
| Mode/CLI parsing, orchestration | `scripts/run.py` |
| `gh pr checkout` into isolated worktree, cleanup | `scripts/worktree.py` |
| Auto-detect repo's test runner; honor override | `scripts/test_runner.py` |
| `gh pr comment` with template rendering | `scripts/comment_post.py` |
| Write `verification.md`, apply phase transition | `scripts/verification_io.py` |
| Pushback comment template (polite, curious) | `assets/pushback-comment-template.md` |

The script split keeps each module narrow and independently testable. `run.py` is mostly argument routing and the dispatch glue.

## State and Cache Touched

This skill writes to:

- `{project-root}/_bmad-output/pr-workflow/state.json` (via `state_io.save_state`, atomic)
- `{project-root}/_bmad-output/pr-workflow/prs/{pr_number}/verification.md` (overwritten each run; one verification per claim)
- `{project-root}/_bmad-output/pr-workflow/logs/{YYYY-MM-DD}.jsonl` (append-only run-log)
- `{project-root}/_bmad-output/pr-workflow/worktrees/{pr_number}/` (worktree; cleaned on `verified`, retained otherwise)
- GitHub: PR comment on the original PR thread (verdict `not-verified` only)

This skill reads from:

- `{project-root}/_bmad/config.toml` (config: `prj_repo`, `prj_test_runner`)
- `{project-root}/_bmad-output/pr-workflow/state.json` (prior queue state)
- `{project-root}/_bmad-output/pr-workflow/prs/{pr_number}/comments-triage.md` (claim source)
- The PR's checked-out source tree under the worktree (read for strategy execution)

## Non-Negotiables

- **Pushback before action.** A `not-verified` claim NEVER advances to fix-planning. The agent posts a polite question and parks the claim at `Reviewed`.
- **Worktree isolation.** All reproduction work happens inside `_bmad-output/pr-workflow/worktrees/{n}/`. The agent does not run the contributor's code against the main repo working tree.
- **Atomic state writes.** Always via `state_io.save_state`. Direct file writes to `state.json` are forbidden.
- **Idempotent persistence.** The verification can be rerun safely; `verification.md` is overwritten, state transitions are deterministic. Posted comments are NOT idempotent (gh comments are append-only) — use `--dry-run` or `--skip-comment` when iterating.
- **No silent failures.** Every outcome (verified, not-verified, ambiguous, error) appends a structured run-log entry.
- **Polite tone.** Pushback comments always render through `assets/pushback-comment-template.md`. No hand-rolled comment bodies.

## Verification

Smoke-test in dry-run mode (no `gh` calls, no state writes):

```bash
python3 scripts/run.py \
  --pr-number 101 \
  --strategy existing-test \
  --verdict not-verified \
  --observation "tests pass on the PR head" \
  --rationale "no failure observed" \
  --dry-run --verbose
```

Expected: exit 0, run-log entry with `status: "dry-run"`, no state changes, no comment posted.

Run the unit tests (no `gh` or `git` calls; all subprocess invocations mocked):

```bash
python3 -m unittest discover scripts/tests
```
