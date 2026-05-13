# prj-agentd

**Mission Control daemon for PR Jangler.**

> Looking to test this on a real repo? Read
> [`docs/getting-started.md`](docs/getting-started.md) — a friendly walkthrough
> with copy-pasteable commands for every flow (cold start, safe action, gated
> action with approval, drift detection, and live `codex app-server`).

PR Jangler owns the deterministic state machine (queue, phases, run-log).
`prj-agentd` owns the runtime control plane: persistent agent threads via
`codex app-server`, approval policy, event mirroring, and the heartbeat loop.

```
GitHub reality
  v
PR Jangler   deterministic queue/state/log layer        (skills/, _bmad-output/pr-workflow/state.json)
  v
app-server   thread/turn/item runtime layer             (codex app-server, Unix socket JSON-RPC)
  v
prj-agentd   policy + audit + scheduling layer          (this package)
  v
dashboard    mission-control surface                    (tail timeline JSONL or build a UI)
```

## The invariant

> PR Jangler chooses/records the state transition.
> app-server executes/observes/explains the agent work.

`prj-agentd` is the wedge between them. Selection is via PR Jangler's
`--select-only` pure selector (no state mutation, no runlog, no dispatch).
Execution is via app-server's `turn/start` with the chosen skill attached.
No double books.

## Install

Stdlib only. Python >= 3.11.

```bash
cd prj-agentd
pip install -e .
```

## Configure

Add a `[modules.prj.agentd]` block to `_bmad/config.toml`:

```toml
[modules.prj]
prj_repo = "owner/repo"

[modules.prj.agentd]
socket_path = "~/.codex/app-server.sock"
tick_seconds = 30
approval_mode = "unlessTrusted"   # neverInteractive | unlessTrusted | always
sandbox = "workspaceWrite"
# skill_allowlist = ["prj-discover", "prj-triage", "prj-review"]  # optional, locks scope
```

## Run

```bash
# Smoke-test with no app-server: in-process offline mode
prj-agentd once --offline

# Production: connect to live codex app-server
prj-agentd run

# Inspect pending human approvals + recent runs
prj-agentd status

# Approve a pending request
prj-agentd approve appr_abc123 --decision approve --reason "reviewed PR #42 diff"
```

## Milestones

The destination doc defines four milestones; this package implements all of A
and the policy/plumbing for B-D.

- **Milestone A — cache-only review loop.** Selector picks an action, daemon
  ensures repo+PR threads, injects context, runs `$prj-review` (or whatever
  was selected) through app-server, persists review.md, does **not** post to
  GitHub. ✅ Implemented; smoke-testable via `--offline`.
- **Milestone B — human-approved GitHub comment.** `prj-decision` is gated
  REQUIRE_HUMAN by policy; daemon stores an approval request; CLI
  `prj-agentd approve …` records the decision. ✅ Wiring in place.
- **Milestone C — adversarial validator in detached thread.**
  `prj-validate-adversarial` runs as a child thread; policy auto-approves
  (read-only against cached artifacts). ✅ Plumbed via `parent_thread_id`.
- **Milestone D — approved fix PR generation.** `prj-implement-fix` is gated
  REQUIRE_HUMAN; same approval flow as B. ✅ Wiring in place.

## How offline mode is honest

`--offline` swaps the live app-server connection for `OfflineAppServer`, an
in-process implementation of the same interface. Every method resolves
deterministically:

- `start_thread` mints a synthetic `thr_…` ID
- `set_goal`, `inject_items` emit timeline events but do nothing else
- `run_turn` mints a `turn_…` ID and returns `status: "completed-offline"`

Crucially, the selector still runs for real (subprocess to
`skills/prj-orchestrator/scripts/run.py --select-only`), the SQLite store
still writes for real, the policy still evaluates for real, and the timeline
JSONL still appends for real. So `--offline` is faithful to every layer
**except** the LLM turn itself.

## What this package will never do

- Write `state.json` directly. Always goes through `state_io.py`.
- Bypass `prj_repo` config — refuses to dispatch on misconfigured repos.
- Push to a contributor's branch. Fix PRs land on a daemon-owned branch.
- Skip the approval gate for GitHub mutations.
- Tail-call `bmad` CLI — every dispatch goes through app-server, never a
  subshell. The orchestrator's own `bmad run` fallback is for cron, not
  for the daemon.

## State locations

```
_bmad-output/pr-workflow/agentd/
├── agentd.sqlite                 ← runtime metadata (threads, runs, approvals)
└── timeline/
    └── 2026-05-13.jsonl          ← structured event timeline (rotates daily)
```
