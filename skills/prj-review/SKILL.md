---
name: prj-review
description: PR Jangler full-PR code reviewer. Use when the orchestrator dispatches the review phase for a PR or when the user runs 'prj-review' manually to produce a structured review.md from the PR's diff and changed files.
---

# prj-review

## Overview

This skill performs a full code review of one open PR on the `prj_repo`. Deterministic plumbing (fetch the PR diff, fetch changed-file contents, look up project conventions in Hindsight, validate finding schemas, persist `review.md`, optionally post a GitHub Comment-type review) lives in Python scripts. The review itself is LLM-driven: you read the diff and changed files, produce findings, run a self-check pass, then hand the validated findings to the scripts.

Act as a careful senior reviewer. Anchor every finding to a specific file and line. Prefer signal over volume.

## Conventions

- Bare paths (e.g. `scripts/run.py`) resolve from the skill root.
- `{project-root}/...` resolves from the project working directory.
- Configuration variables live under `[modules.prj]` in `{project-root}/_bmad/config.toml`.

## On Activation

The orchestrator calls this skill with a target PR number. The full review proceeds in three phases.

### Phase 1: Fetch context (script)

```bash
python3 scripts/run.py --pr <pr_number> --fetch-only
```

This emits a JSON document to stdout containing:

- `pr`: `{number, title, body, base_ref, head_ref, author, acceptance_criteria}`
- `diff`: full unified diff (string)
- `files`: list of `{path, status, content}` for each non-binary changed file
- `conventions`: Hindsight bank `prj` excerpts relevant to this repo (best-effort; empty list if Hindsight is unreachable)

Read everything before forming opinions. If the PR description contains a fenced `## Acceptance Criteria` block, treat it as the contract.

### Phase 2: Produce findings (you)

Apply the review heuristics below. Anchor every claim to a specific `file` and `line`. Produce findings as a JSON array conforming to the STRICT schema:

```json
{
  "file": "src/foo.ts",
  "line": 42,
  "severity": "blocker|major|minor|nit",
  "category": "correctness|edge-case|error-handling|test-coverage|style|security|performance|docs",
  "claim": "Concrete observation tied to the cited line.",
  "suggested_fix": "Smallest change that resolves the claim."
}
```

All five keys are required. `severity` must be one of `blocker | major | minor | nit`. Findings missing a key or with an invalid severity are rejected by `scripts/review_io.py`.

#### Review hierarchy (apply in this order)

1. **PR description and acceptance criteria** — the contract. Anything the PR explicitly promises is non-negotiable.
2. **Project conventions** — from Hindsight `prj` bank and the repo's `CLAUDE.md`. Includes anti-patterns ("NO `any` types", "DO NOT edit `skill/`").
3. **Tests** — does the PR add or update tests for new behavior? Are existing tests still meaningful?
4. **Style** — readability, naming, structure. Lowest priority. Do not gate the PR on taste.

A finding in a higher tier outranks any number of lower-tier findings. If the diff violates a stated AC, that's a `blocker` regardless of style polish.

#### Review heuristics

- **Correctness.** Off-by-ones, wrong operator, swapped arguments, mutated input, race conditions, incorrect type coercion. Reproduce mentally line by line.
- **Edge cases.** Empty input, null/undefined, max int, very long string, unicode, leading/trailing whitespace, simultaneous writes, partial failures. What inputs make this break?
- **Error handling.** Swallowed exceptions, unchecked returns, missing rollback, broken cleanup paths, error messages that leak internals or hide the cause.
- **Test coverage.** New behavior without a test is at least `major`. Tests that assert nothing meaningful (e.g., `expect(x).toBeDefined()` only) are at least `minor`.
- **Style alignment.** Match repo conventions, not personal preference. Cite the convention if you flag style.

#### Severity rubric

- **blocker** — merging would break the build, regress an AC, or introduce a security/data-loss bug.
- **major** — incorrect behavior on plausible input, missing coverage of new logic, or a convention violation that the project enforces.
- **minor** — likely-but-not-certain bug, weak test, style that hurts readability.
- **nit** — tiny polish item. Reviewer chooses whether to mention.

### Phase 3: Self-check (you)

Re-read every finding from the perspective of a skeptical maintainer. Ask:

1. **Is the claim anchored?** If the cited line does not actually contain what you claim, drop it.
2. **Is the claim load-bearing?** If the file does not depend on the alleged failure mode, drop it.
3. **Does the suggested fix help?** If the fix would not actually resolve the claim, rewrite it or drop the finding.
4. **Is the severity defensible?** If you cannot justify the severity in one sentence, downgrade.

Findings that survive the self-check go into the final array. Aim for fewer, sharper findings rather than a long list.

### Phase 4: Persist + optional post (script)

Pipe the validated finding array (and a free-form summary string) into:

```bash
python3 scripts/run.py --pr <pr_number> --persist --summary-file <path>
```

`scripts/run.py` will:

1. Validate every finding against the strict schema (rejects on first violation, logs which finding).
2. Render `{project-root}/_bmad-output/pr-workflow/prs/{pr_number}/review.md`.
3. Transition the PR's state from `ReviewPending` to `Reviewed`.
4. If `prj_post_review_comment = true`, post a Comment-type review on GitHub via `gh pr review --comment --body-file`. **Default is `false` at v1**, so most invocations only write the cache.
5. Append a structured run-log entry.

`scripts/run.py` will NEVER pass `--approve` or `--request-changes` to `gh pr review`. The skill posts an advisory comment or it stays silent; the human owns the gate.

## Architecture

| Concern | Lives in |
| --- | --- |
| Orchestration (fetch / validate / persist / optional post) | `scripts/run.py` |
| `gh pr view` + `gh pr diff` fetching, AC extraction | `scripts/github_fetch.py` |
| Findings schema validation + `review.md` writer + state transition | `scripts/review_io.py` |
| Optional GitHub PR comment post (gated by `prj_post_review_comment`) | `scripts/github_post.py` |
| Hindsight bank `prj` best-effort lookup | `scripts/hindsight_lookup.py` |

All state I/O routes through `state_io` (owned by `prj-orchestrator`). Sibling-imported via `sys.path` patch.

## State Locations

- Per-PR review: `{project-root}/_bmad-output/pr-workflow/prs/{pr_number}/review.md`
- State queue (read/write): `{project-root}/_bmad-output/pr-workflow/state.json`
- Run-log (append-only): `{project-root}/_bmad-output/pr-workflow/logs/{YYYY-MM-DD}.jsonl`

## Configuration

| Variable | Default | Effect on this skill |
| --- | --- | --- |
| `prj_repo` | (required) | Repo passed to every `gh` call |
| `prj_post_review_comment` | `false` | When true, post the review as a GitHub PR Comment-type review |

## Non-Negotiables

- **Every finding cites file + line.** No structural rants without anchors.
- **Strict schema or reject.** `scripts/review_io.py` will fail the run if a finding is malformed; this is the contract.
- **Never approve or request-changes.** The optional post is Comment-type only. Approval and change-requests are human decisions.
- **Cache-first.** Default behavior is cache-only. Flipping `prj_post_review_comment` to true is an explicit project choice.
- **Hindsight best-effort.** A Hindsight outage must not block a review. Log and continue.

## Verification

Smoke test (no PR posting, no state writes):

```bash
python3 scripts/run.py --pr 1 --fetch-only --project-root <path-to-project>
```

Unit tests:

```bash
python3 -m unittest discover scripts/tests
```
