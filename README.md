# PR Jangler

**An autonomous workflow for your GitHub PR backlog.**

Cron fires a heartbeat. The heartbeat picks one queued action. That action dispatches to one phase skill. Repeat forever. The backlog clears itself — triaged, verified, reviewed, sometimes fixed (via a _new_ PR targeting the contributor's branch), always logged, never silently failing.

```
   DISCOVER     TRIAGE       VERIFY       REVIEW       PLAN          VALIDATE       IMPLEMENT    DECIDE       REPORT
  ┌────────┐  ┌────────┐  ┌─────────┐  ┌────────┐  ┌──────────┐  ┌────────────┐  ┌──────────┐  ┌────────┐  ┌────────┐
  │ GH→Q   │─▶│Classify│─▶│Reproduce│─▶│Read    │─▶│Fix +     │─▶│Adversarial │─▶│ Fix-PR   │─▶│ Verdict│─▶│ Email  │
  │ sync   │  │ PR/cmt │  │ claim   │  │diff    │  │test plan │  │6-item gate │  │ to branch│  │ + label│  │ digest │
  └────────┘  └────────┘  └─────────┘  └────────┘  └──────────┘  └────────────┘  └──────────┘  └────────┘  └────────┘
```

---

## Three install modes

PR Jangler ships as three artifacts so you can pick the dose that fits.

| Mode           | What lands                                                                   | Best for                                      |
| -------------- | ---------------------------------------------------------------------------- | --------------------------------------------- |
| **Full pack**  | Hub + 12 phase skills                                                        | Autonomous operation under cron               |
| **Hub only**   | `pr-jangler` skill (routing knowledge, conventions, principles) — no scripts | Orienting an agent, lightweight understanding |
| **À la carte** | Pick individual `prj-*` skills                                               | One-off use of a specific phase               |

### Full pack — Claude Code

```
/plugin marketplace add delorenj/pr-jangler
/plugin install pr-jangler@pr-jangler-marketplace
```

### Hub only — Claude Code

```
/plugin install pr-jangler-hub@pr-jangler-marketplace
```

The hub teaches an agent the workflow, the state-file layout, the label vocabulary, and which phase to call for which signal. It will tell you which child skill to install if a task needs executable scripts.

### Local development

```bash
git clone https://github.com/delorenj/pr-jangler.git
claude --plugin-dir /path/to/pr-jangler
```

### À la carte (any agent)

Every `skills/prj-*/SKILL.md` is self-contained for its phase. Copy the directory into your agent's skills path (`.claude/skills/`, `.cursor/rules/`, `.kiro/skills/`, etc.) and it works without the hub.

---

## The 12 phase skills

| Skill                      | Phase     | One-liner                                                                                          |
| -------------------------- | --------- | -------------------------------------------------------------------------------------------------- |
| `prj-setup`                | Install   | Writes config, creates GH labels, validates `gh`/`op`/SMTP, initializes the workflow tree          |
| `prj-orchestrator`         | Heartbeat | Cron entrypoint; picks one action, dispatches one phase, persists state                            |
| `prj-discover`             | Discover  | Syncs GitHub open-PR set into the queue; adds, archives, bumps comment counters                    |
| `prj-triage`               | Classify  | Dual-mode: classifies a PR (`--mode pr`) or the latest unclassified comment (`--mode comment`)     |
| `prj-verify-claim`         | Verify    | Reproduces a comment's alleged bug in an isolated worktree, or pushes back politely                |
| `prj-review`               | Review    | Reads diff + changed files; produces `review.md` with structured findings                          |
| `prj-detect-overlap`       | Overlap   | Scores file-footprint overlap vs every open PR; LLM-compares the high-overlap pairs                |
| `prj-plan-fix`             | Plan      | Designs a failing test + smallest diff + rationale + risks → `fix-plan.md`                         |
| `prj-validate-adversarial` | Validate  | Runs full regression suite + 6-item adversarial checklist; default verdict is `reject`             |
| `prj-implement-fix`        | Implement | Opens a _new_ PR targeting the contributor's branch. Never push to their branch. Never force-push. |
| `prj-decision`             | Decide    | Aggregates the per-PR cache, applies the rubric, labels and comments the verdict                   |
| `prj-report-daily`         | Report    | Renders an HTML+text email digest, sends via SMTP, archives to `reports/`                          |

The hub (`pr-jangler`) sits on top of these and owns the cross-cutting rules nobody wants to repeat 12 times: state-file layout, label vocabulary, idempotency rules, dependency map, the `PleaseAdvise` escape hatch.

---

## Architecture in one paragraph

`state.json` is the queue and the source of truth. Every transition is a single Python script invocation; every script goes through `state_io.py` for atomic reads and writes. LLM work (classification, claim verification, fix-plan design, adversarial review, review writing) lives in the SKILL.md prompts; deterministic plumbing (gh calls, label application, worktree provisioning, diff fetching, email rendering, SMTP) lives in scripts. Every outcome — dispatch, stub, abort, error, no-op — is one JSONL line in `logs/{YYYY-MM-DD}.jsonl`. If anything looks wrong, that log is the audit trail.

---

## Requirements

- `gh` (GitHub CLI), authenticated as `prj_bot_user`
- `op` (1Password CLI), signed in — for SMTP creds resolution
- `git` with worktree support
- Python 3.11+
- SMTP host reachable from where cron runs
- A target repo (`prj_repo`) that the bot user can read, comment on, and label

`prj-setup` validates all of these and warns (does not block) on missing pieces — fix them at your leisure.

---

## License

MIT — see [LICENSE](./LICENSE).
