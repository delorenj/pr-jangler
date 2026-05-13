---
name: pr-jangler
description: PR Jangler hub. Cron-driven autonomous workflow for triaging, verifying, reviewing, fix-planning, adversarially validating, fix-implementing, deciding, and daily-reporting on a GitHub PR backlog. Use when the user mentions "PR jangler", "pr-jangler", "PR backlog workflow", "PR triage", "autonomous PR review", "fix-plan", "fix-PR", "adversarial validator", "PleaseAdvise", or wants to install, configure, or extend the PR Jangler BMad module. Routes to 12 phase skills, owns the cross-cutting state-file conventions, idempotency rules, GitHub-label vocabulary, and 1Password/SMTP/`gh`/`op` dependency map shared by every phase. Triggers on prj-setup, prj-orchestrator, prj-discover, prj-triage, prj-verify-claim, prj-review, prj-detect-overlap, prj-plan-fix, prj-validate-adversarial, prj-implement-fix, prj-decision, prj-report-daily.
---

# PR Jangler

Autonomous PR backlog handler. Cron fires `prj-orchestrator` every N minutes; the orchestrator picks one queued action and dispatches to one phase skill. Per-PR state is durable; every transition is logged; nothing acts on a PR until a claim has been independently reproduced.

```
   DISCOVER       TRIAGE         VERIFY         REVIEW         PLAN            VALIDATE         IMPLEMENT      DECIDE         REPORT
  ┌────────┐    ┌────────┐    ┌────────┐    ┌────────┐    ┌──────────┐    ┌──────────────┐   ┌──────────┐   ┌────────┐    ┌────────┐
  │ GH → Q │───▶│Classify│───▶│Reproduce│──▶│ Read   │───▶│Fix +     │───▶│Adversarial   │──▶│ New PR   │──▶│ Label  │───▶│ Email  │
  │ sync   │    │ PR/cmt │    │ claim   │   │ diff   │    │test plan │    │6-item check  │   │ to branch│   │ verdict│    │ digest │
  └────────┘    └────────┘    └────────┘    └────────┘    └──────────┘    └──────────────┘   └──────────┘   └────────┘    └────────┘
  prj-discover  prj-triage    prj-verify    prj-review    prj-plan-fix    prj-validate-      prj-implement   prj-decision   prj-report-
                              -claim                                       adversarial        -fix                          daily

  Also dispatched on cadence: prj-detect-overlap (duplicate scoring), prj-setup (one-shot install).
                              Orchestrated by: prj-orchestrator (cron heartbeat).
```

## Operating Principles

These are non-negotiable across every phase skill. Hoisted here so each child does not redefine them.

1. **The queue is authoritative.** `{project-root}/_bmad-output/pr-workflow/state.json` is the single source of truth. Phase skills NEVER read GitHub for queue facts — they read `state.json`. Only `prj-discover` writes from GitHub into the queue.
2. **All state I/O goes through `state_io.py`.** Atomic via tmp + fsync + rename. Direct file writes to `state.json` are forbidden — a corrupted queue blocks the whole pipeline.
3. **Idempotent per heartbeat.** Every action survives mid-run interruption: the next heartbeat picks up cleanly without double-acting or losing the transition.
4. **Never silently fail.** Every outcome — dispatch, stub, abort, error, no-op — emits a structured JSONL entry to `{project-root}/_bmad-output/pr-workflow/logs/{YYYY-MM-DD}.jsonl`. Errors also surface on stderr.
5. **`PleaseAdvise` is the escape hatch.** Any phase may transition a PR to `PleaseAdvise` (priority 1000, outranks everything). Stays there until the user-acknowledged override label is applied.
6. **Never push to the contributor's branch.** `prj-implement-fix` opens a new PR targeting the contributor's branch. Force-pushes are forbidden. Every fix is a new commit; every fix is a new PR.
7. **Verify before acting on any claim.** A comment asserting a bug is a hypothesis, not a fact. `prj-verify-claim` reproduces in an isolated worktree before `prj-plan-fix` is allowed to run.
8. **System actions outrank per-PR actions.** Daily report (priority 700) and discovery sweep (priority 500) preempt the per-PR backlog (base 100).

## Routing Table

When the orchestrator's next action — or a user invocation — names one of these signals, dispatch to the matching child skill.

| Signal | Skill | What it does |
|---|---|---|
| First-time module install in a project | `prj-setup` | Writes `_bmad/config.{yaml,user.yaml}`, creates GH labels, validates `gh`/`op`/SMTP, initializes the workflow tree |
| Cron heartbeat (every N minutes) | `prj-orchestrator` | Picks one action, dispatches one phase, persists state, logs, exits |
| Queue empty, or cadence tick `heartbeat_count % prj_discover_every_n == 0` | `prj-discover` | Syncs GH open-PR set into the queue; adds Discovered, archives closed, bumps `new_comments_since_triage` |
| PR newly Discovered (`--mode pr`), or new comment on a triaged PR (`--mode comment`) | `prj-triage` | Classifies the PR/comment into one of the rubric buckets; sets `next_action` |
| Triage produced an actionable claim that asserts a bug | `prj-verify-claim` | Reproduces in an isolated worktree; verdict drives transition |
| PR triaged as needs-review (no claim, full-PR examination) | `prj-review` | Reads diff + changed files; emits `review.md` with findings |
| Triage flagged possible duplicate | `prj-detect-overlap` | Scores file-footprint overlap vs every open PR; LLM-compares high-overlap pairs |
| Claim verified, ready to design a fix | `prj-plan-fix` | Produces `fix-plan.md`: failing test + smallest diff + rationale + risks |
| Fix-plan written, needs adversarial gate | `prj-validate-adversarial` | Runs full regression suite + 6-item adversarial checklist; default verdict `reject` |
| Adversarial gate passed | `prj-implement-fix` | Opens fix-PR targeting the contributor's branch |
| Review/fix complete, ready for verdict | `prj-decision` | Aggregates per-PR cache, applies rubric, labels `prj/decision:{class}`, posts summary comment |
| Local hour ≥ `prj_report_hour_local` and no report sent today | `prj-report-daily` | Renders HTML+text digest, sends via SMTP, archives to reports/ |

## Common Combinations

The canonical per-PR pipeline, in order:

```
prj-discover → prj-triage → prj-verify-claim → prj-review → prj-plan-fix
            → prj-validate-adversarial → prj-implement-fix → prj-decision
```

Not every PR walks the full sequence. Triage may shortcut: a duplicate goes straight to `prj-detect-overlap → prj-decision (close-as-not-now)`; a clean review with no claim goes `prj-review → prj-decision (ready-to-merge)`; a verification failure goes `prj-verify-claim → prj-decision (request-changes-with-pushback)`.

## Cross-Cutting Conventions

### State tree layout

Owned by `prj-orchestrator`. Every phase reads from and writes to it (via `state_io.py`).

```
{project-root}/_bmad-output/pr-workflow/
├── state.json                       # the queue
├── prs/{pr_number}/                 # per-PR cache
│   ├── meta.json
│   ├── triage.md
│   ├── verification.md
│   ├── review.md
│   ├── overlap.md
│   ├── fix-plan.md
│   ├── adversarial.md
│   └── decisions.log                # JSONL audit
├── logs/{YYYY-MM-DD}.jsonl          # run-log
├── reports/{YYYY-MM-DD}.md          # daily-report archive
└── worktrees/                       # ephemeral, owned by prj-verify-claim
```

### Configuration

Module config lives under `[modules.prj]` in `{project-root}/_bmad/config.toml` (or `config.yaml` per the active BMad version). Required: `prj_repo`. Common: `prj_discover_every_n`, `prj_report_hour_local`, `prj_smtp_host`, `prj_smtp_port`, `prj_email_to`, `prj_bot_user`. User-only keys (`user_name`, `communication_language`) live in `config.user.yaml`.

### Path tokens

`{project-root}/...` is a **literal token** in config values. Do not substitute it. Filesystem operations resolve it at the point of use; the config itself keeps it symbolic.

### Dependency map

| Tool | Used by | Failure mode |
|---|---|---|
| `gh` (authenticated as `prj_bot_user`) | discover, triage, detect-overlap, review, implement-fix, decision | Rate-limit aware: defer cleanly when core quota < 100 |
| `op` (1Password CLI, signed in) | report-daily, setup | SMTP creds resolution; setup warns but does not block |
| `git` + worktrees | verify-claim, plan-fix, validate-adversarial, implement-fix | Worktrees are ephemeral; clean up after each verification |
| Project test runner (auto-detected) | verify-claim, validate-adversarial | Required for the adversarial gate to pass |
| SMTP (configured host/port + STARTTLS) | report-daily, setup test | Daily digest delivery |

### Label vocabulary

Pre-created on `prj_repo` by `prj-setup` (from `skills/prj-setup/assets/labels.txt`). Every phase that writes to GitHub uses these labels — never invents new ones.

- `prj/phase:{Discovered,Triaged,VerifyClaim,Reviewing,FixPlan,AdversarialCheck,FixImpl,Decision,ReadyToMerge,Blocked,Rejected,Archived,PleaseAdvise}`
- `prj/decision:{ready-to-merge,request-changes,close-as-not-now}`
- `prj/duplicate-of:{n}`, `prj/please-advise`, `prj/needs-retriage`, `prj/user-ack`

## Install Modes

This skill is the hub. There are three ways to use PR Jangler:

1. **Full pack** — install the whole plugin. The hub plus all 12 phase skills land in `.claude/skills/`. Recommended for autonomous operation.
2. **Hub only** — install just `pr-jangler/`. You get the routing knowledge, conventions, label vocab, and operating principles, but no executable scripts. Useful for orienting an agent that will hand off to a separately-installed phase skill, or for understanding the workflow before committing all 12.
3. **À la carte** — copy individual `prj-*` skills into your project. Skip the hub; each child SKILL.md is self-contained for its phase.

When this hub is loaded and a user task names a phase action that requires an uninstalled child, point them at the missing skill rather than improvising it: "this needs `prj-verify-claim`, which is not installed at `.claude/skills/prj-verify-claim/`. Install via `<plugin install line>` or copy from the pack repo."

## Out of Scope

This hub routes the PR Jangler module. It does NOT:

- **Review PRs in repos other than `prj_repo`.** The configured repo is the workspace; multi-repo orchestration is not built.
- **Write or modify skills.** Skill authoring belongs to `skill-creator`, not here.
- **Handle non-PR GitHub events** (Issues, Discussions, Actions failures). Use a dedicated issue-triage skill if you need that.
- **Replace BMad core or other modules.** It runs alongside them under `_bmad/`.
- **Generate code changes from scratch.** It can plan and implement fixes for *verified bugs* on existing PRs — not greenfield feature work. Use `bmad-bmm-dev-story` or `incremental-implementation` for that.
- **Bypass the verification gate.** A claim that cannot be reproduced gets a polite pushback comment, not a speculative fix.
