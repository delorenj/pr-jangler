---
name: prj-implement-fix
description: PR Jangler Tier-2 implementer. Use when prj-validate-adversarial passed for a PR and the orchestrator dispatches FixImpl. Pushes a fix branch and opens a fix-PR into the contributor's branch; falls back to a comment if the contributor's branch refuses PRs.
---

# prj-implement-fix

## Overview

Apply an adversarially-validated fix-plan and surface it back to the contributor as a NEW PR targeting their branch (Tier 2 of the PR Jangler autonomy ramp). Never push to the contributor's branch. Never force-push. Every fix is a new commit; every fix is a new PR.

If `gh pr create` rejects the fix-PR (contributor's branch is protected, or the contributor disabled PRs against their fork branch), fall back to Tier 1: post the fix-plan diff as a comment on the original PR with the diff in a fenced code block. The original PR also gets a `prj/fix-proposed` label whichever path runs.

Act as a release engineer: small commits, conventional-commit subjects, explicit attribution, every action recorded.

## Conventions

- Bare paths (e.g. `scripts/run.py`) resolve from the skill root.
- `{project-root}/...` resolves from the project working directory.
- Configuration variables live under `[modules.prj]` in `{project-root}/_bmad/config.toml`.

## On Activation

Load configuration. Required keys: `prj_repo`, `prj_bot_user`, `prj_bot_token_ref`. If any is missing, exit code 2 with a `misconfigured` run-log entry.

Refuse to run unless `prs/{pr_number}/adversarial.md` carries a frontmatter `verdict: pass`. Any other verdict (or missing adversarial.md) exits code 4 with a `not-validated` run-log entry; the state stays put for the orchestrator to retry the prior phase.

Execute the implementation:

```bash
python3 scripts/run.py --pr-number {n}
```

Flags:

- `--pr-number N` (required) the PR whose fix-plan to implement.
- `--project-root PATH` override autodetect.
- `--dry-run` perform every step except git push / gh pr create / `gh pr comment`; emit the planned commands.
- `--verbose` diagnostics to stderr.

See `python3 scripts/run.py --help` for the full surface.

## Architecture

| Concern | Lives in |
| --- | --- |
| Git branch ops, diff apply, conventional-commit author + Co-Authored-By, push | `scripts/branch_ops.py` |
| `gh pr create` with fallback to `gh pr comment`, label application | `scripts/pr_create.py` |
| `implementation.md` writer, state transition FixImpl to ReadyToMerge | `scripts/implementation_io.py` |
| Bot token resolution via `op read` (duplicated for v1; consolidate later) | `scripts/creds.py` |
| End-to-end orchestration | `scripts/run.py` |
| Fix-PR body template | `assets/template-fix-pr.md` |

State I/O is sourced from `prj-orchestrator/scripts/state_io.py` via the same sibling-`sys.path`-insert pattern other phase skills use; no copy-paste.

## Inputs Read

- `prs/{n}/fix-plan.md` (drives the commit subject, diff body, and the actual code change).
- `prs/{n}/adversarial.md` (must carry `verdict: pass`; otherwise refuse).
- `prs/{n}/verification.md` (parsed for `claim_source:` to credit the original reporter in the `Co-Authored-By` trailer).
- `{project-root}/_bmad-output/pr-workflow/state.json` (for the contributor's head ref / login, so the fix-PR targets the right branch).

The fix-plan is expected to expose a fenced diff block tagged `diff` (the unified diff that `git apply` accepts). If no such block is found, exit code 5 with `bad-fix-plan` and no git side-effects.

## Outputs

- New branch `prj/auto-fix/{n}-{slug}` on the SAME repo (never on the contributor's branch). Slug is the PR title kebab-cased, ASCII-folded, truncated to 30 chars.
- One commit on that branch, author = `{prj_bot_user}` (configurable), trailer `Co-Authored-By: <claim source>`. Conventional-commit style. Subject line clamped to 72 chars.
- One PR via `gh pr create`, base = contributor's head ref (the original PR's `headRefName`), head = `prj/auto-fix/{n}-{slug}`. Title: `[prj] {short summary} (re #{n})`. Body rendered from `assets/template-fix-pr.md`.
- Fallback path: on PR creation failure, post the fix-plan diff as a comment on the ORIGINAL PR. Diff is fenced as ```diff ... ```. Comment includes a banner identifying the fallback.
- Label `prj/fix-proposed` on the ORIGINAL PR (created on demand).
- `prs/{n}/implementation.md` records the PR URL or comment URL, the new branch name, the commit SHA, the test-run summary, and which path ran (`tier-2-pr` or `tier-1-comment-fallback`).
- Phase transition in `state.json`: `FixImpl` to `ReadyToMerge`, `next_action` cleared.
- Run-log entry tagged `action: implement-fix` summarising path, PR url, commit SHA, status.

## Commit Style

- Subject: `fix: {short summary} (re #{n})` (max 72 chars). Falls back to truncation with an ellipsis.
- Body: two paragraphs. First paragraph paraphrases the verified claim. Second paragraph cites the fix-plan + adversarial verdict. Wrapped at 72 cols.
- Trailers (last paragraph):
  - `Co-Authored-By: {claim source}` (parsed from `claim_source:` in `verification.md`; required).
  - `Refs: #{n}`.
- Author: `{prj_bot_user} <{prj_bot_user}@users.noreply.github.com>` unless `prj_bot_email` overrides.
- Plain `git -c user.name=... -c user.email=... commit`; never `--amend`, never `--no-verify`, never `-S`/`--gpg-sign` (we let the host's gpg config decide).

## Fallback Flow

`gh pr create` runs first. On non-zero exit OR if stderr matches one of the contributor-veto patterns (`branch is protected`, `head and base ref are the same`, `not authorized`, `Pull request creation is disabled`, `forbidden`), pivot:

1. Post fix-plan diff as a comment on the ORIGINAL PR (`gh pr comment {n} --repo {repo} --body-file ...`). The comment is a banner ("Fix proposed inline because the fix-PR could not be opened") + the unified diff in a `diff` fence + a footer linking the per-PR cache path.
2. Label the ORIGINAL PR `prj/fix-proposed`.
3. `implementation.md` records `path: tier-1-comment-fallback` and the comment URL.

If even the fallback comment fails, the skill exits code 6 with `fallback-failed`; state is NOT advanced; the orchestrator can retry.

## Attribution Rules

- `claim_source:` in `verification.md` is the source of truth for the `Co-Authored-By` trailer (usually a GitHub login with a `@users.noreply.github.com` mailbox, or a fully-qualified address if the maintainer specified one).
- If `claim_source` is missing or empty, the skill exits code 5 (`bad-fix-plan`). We never ship a fix without crediting the reporter.
- Bot identity for `author` and `committer` comes from `prj_bot_user` + optional `prj_bot_email`. Tokens never appear in commit content.

## Non-Negotiables

- NEVER push directly to the contributor's branch. The push target is ALWAYS `prj/auto-fix/{n}-{slug}` on this repo, with `--set-upstream origin`.
- NEVER force-push. All pushes are vanilla `git push`. No `--force`, no `--force-with-lease`.
- NEVER amend. Every change is a new commit.
- NEVER bypass hooks. No `--no-verify`. If a pre-commit hook fails, the run aborts (exit 7, `hook-failed`) and the orchestrator retries.
- NEVER write the bot token to a file. `creds.py` reads via `op read` and passes the value to git/gh via env vars for the duration of the subprocess only.
- Refuse the run if `adversarial.md` is missing or `verdict != pass`.

## Verification

Local smoke test (works against a fake `gh`/`git` if you mock them; see the unit tests for an example):

```bash
python3 scripts/run.py --pr-number 42 --dry-run --verbose
```

Expected on a passed adversarial: exit 0, run-log `action: implement-fix`, `status: dry-run`, planned branch + commit subject + PR title printed.

Run unit tests:

```bash
python3 -m unittest discover scripts/tests -v
```
