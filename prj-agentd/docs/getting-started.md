# Getting started with prj-agentd

This guide walks you through running `prj-agentd` against a real repository.
You'll start in offline mode (no `codex app-server` required), seed a few
realistic states, watch the daemon make decisions, and approve a gated action
end-to-end. By the end, you'll know what a healthy tick looks like and where
to look when something's off.

If you want the architecture instead of the walkthrough, read
[`../README.md`](../README.md). For the verification checklist, see
[`../../WHERE_WE_ARE.md`](../../WHERE_WE_ARE.md).

## What you'll do

1. Pick a test repository and configure it for `prj-agentd`.
2. Run a cold-start tick in offline mode and inspect the audit trail.
3. Seed a PR that triggers a safe phase action; watch the daemon dispatch it.
4. Seed a PR that triggers a GitHub-mutating action; approve it manually; see
   it execute on the next tick.
5. Mutate state underneath an approved action and watch drift detection
   re-park it.
6. (Optional) Switch to live mode against a running `codex app-server`.

## Prerequisites

You need:

- Python **3.11 or newer** on `PATH` (`python3 --version` confirms).
- `git` and a local clone of [`pr-jangler`](https://github.com/delorenj/pr-jangler)
  (this repo).
- A repository you're willing to point `prj-agentd` at. The pr-jangler repo
  itself works fine as the test target — that's what this guide uses.
- (Optional, for Step 6) A working `codex` install with `codex app-server`
  reachable over a Unix socket.

You don't need any external Python packages — `prj-agentd` is stdlib-only.

<!-- prettier-ignore -->
> [!NOTE]
> Every step in Steps 1–5 runs in offline mode. The daemon's selector,
> store, policy, and timeline are all real; only the LLM turn is synthesized
> by `OfflineAppServer`. That's enough to validate the whole control plane
> before bringing a live model into the loop.

## Step 1: Install and pick a test repo

You can either install `prj-agentd` as a package or run it directly from the
source tree. The source-tree path is the fastest for testing.

### Install (recommended for daily use)

From the pr-jangler repo root:

```bash
cd prj-agentd
pip install -e .
```

After install, the `prj-agentd` command is on your `PATH`.

### Run directly (recommended for testing)

If you don't want to install, set `PYTHONPATH` and invoke the package as a
module:

```bash
export PYTHONPATH=/path/to/pr-jangler/prj-agentd/src
python3 -m prj_agentd --help
```

The rest of this guide assumes `prj-agentd` is callable. Substitute
`python3 -m prj_agentd` if you're using the direct-run approach.

### Choose your test repo

You have two paths. Pick the one that matches what you're testing.

**Path A: Test against pr-jangler itself.** Use a scratch copy of the repo —
the orchestrator skill is already at `skills/prj-orchestrator/`, so the
selector finds it without configuration:

```bash
cp -r /path/to/pr-jangler /tmp/prj-test
cd /tmp/prj-test
```

**Path B: Test against any other repo (recommended for daily use).** Point
the daemon at the canonical pr-jangler install via the
`orchestrator_run_py` config key. You don't have to copy any skill files
into the target repo:

```bash
cd /path/to/your-repo   # e.g. /home/delorenj/code/intelliforia
```

You'll set `orchestrator_run_py` in the next step. The daemon's selector
subprocesses that script and passes `--project-root` so the orchestrator
operates on your repo's state while living elsewhere on disk.

## Step 2: Configure the repo

`prj-agentd` reads `_bmad/config.toml` for the repo it's bound to and the
runtime options. The exact config depends on whether you chose Path A or
Path B above.

**Path A (pr-jangler as target):**

```bash
mkdir -p _bmad
cat > _bmad/config.toml <<'EOF'
[modules.prj]
prj_repo = "delorenj/pr-jangler"

[modules.prj.agentd]
socket_path = "~/.codex/app-server.sock"
tick_seconds = 30
approval_mode = "unlessTrusted"
sandbox = "workspaceWrite"
EOF
```

**Path B (any other repo):** Add the `orchestrator_run_py` override so the
daemon knows where to find the selector script. Adjust the path to match
your local pr-jangler checkout:

```bash
mkdir -p _bmad
cat > _bmad/config.toml <<'EOF'
[modules.prj]
prj_repo = "delorenj/intelliforia"

[modules.prj.agentd]
orchestrator_run_py = "/home/delorenj/code/pr-jangler/skills/prj-orchestrator/scripts/run.py"
socket_path = "~/.codex/app-server.sock"
tick_seconds = 30
approval_mode = "unlessTrusted"
sandbox = "workspaceWrite"
EOF
```

The required keys are:

- `prj_repo`: the GitHub `owner/name` that this daemon is responsible for.
- `socket_path`: where the daemon will look for `codex app-server`. You can
  leave this default; offline mode ignores it.
- `orchestrator_run_py` (Path B only): absolute path to
  `skills/prj-orchestrator/scripts/run.py` in your pr-jangler checkout.
  When this key is absent, the daemon falls back to
  `<project_root>/skills/prj-orchestrator/scripts/run.py`.

`approval_mode` controls how aggressive the firewall is:

| Value                | Behavior                                                  |
| -------------------- | --------------------------------------------------------- |
| `unlessTrusted`      | Default. Auto-approves safe operations; gates the rest.   |
| `always`             | Routes every operation through human approval.            |
| `neverInteractive`   | Denies anything not on the auto-approve list. No prompts. |

## Step 3: Run your first tick (cold start)

With no `state.json` yet, the selector returns a system action: run discovery
to populate the PR backlog. In offline mode, that gets dispatched through the
`OfflineAppServer`, which synthesizes a "completed-offline" turn.

Run one tick:

```bash
prj-agentd once --offline
```

You should see JSON like this:

```json
{
  "decision": "auto_approve",
  "decision_reason": "skill 'prj-discover' default policy",
  "pr_thread_id": null,
  "run_id": "run_abcdef012345",
  "selection": {
    "mode": null,
    "phase": null,
    "pr": null,
    "priority": 500,
    "reason": "queue empty",
    "repo": "delorenj/pr-jangler",
    "skill": "prj-discover",
    "state_sha": "no-state",
    "status": "action-selected"
  },
  "skill": "prj-discover",
  "status": "executed",
  "thread_id": "thr_9336f2239b"
}
```

The two key fields:

- `"status": "executed"`: the daemon dispatched the action through the
  app-server (offline synthesizer in this case).
- `"decision": "auto_approve"`: the policy firewall allowed it without
  human gating, because `prj-discover` is a safe, read-only phase.

### Look at the audit trail

The daemon writes two artifacts on every tick:

1. A **timeline** of structured events at
   `_bmad-output/pr-workflow/agentd/timeline/{YYYY-MM-DD}.jsonl`.
2. A **runtime store** with thread, run, and approval rows at
   `_bmad-output/pr-workflow/agentd/agentd.sqlite`.

Tail the timeline:

```bash
cat _bmad-output/pr-workflow/agentd/timeline/*.jsonl | jq -r .event_type
```

You should see this chain, in order:

```
appserver.handshake
tick.started
tick.selection
tick.policy-decision
thread.started
thread.goal.set
repo-thread.created
thread.items.injected
context.injected
turn.started
turn.completed
tick.turn-completed
```

Inspect the store:

```bash
sqlite3 _bmad-output/pr-workflow/agentd/agentd.sqlite \
  "SELECT repo, root_thread_id FROM repo_threads;
   SELECT run_id, skill, status FROM runs;"
```

You'll see one repo thread and one completed run.

<!-- prettier-ignore -->
> [!IMPORTANT]
> The daemon did NOT create `state.json`. Verify with
> `ls _bmad-output/pr-workflow/state.json` — it doesn't exist yet. The
> selector ran read-only against an ephemeral empty state. PR Jangler's
> phase skills are what mutate `state.json` (through `state_io.py`), never
> `prj-agentd` directly.

## Step 4: Trigger a safe per-PR action

Now seed a `state.json` with a PR that needs a review. The selector will
pick `prj-review` (a cache-only, auto-approved skill), and the daemon will
dispatch it.

Create a realistic state:

```bash
mkdir -p _bmad-output/pr-workflow
cat > _bmad-output/pr-workflow/state.json <<'EOF'
{
  "version": "1.0",
  "repo": "delorenj/pr-jangler",
  "last_updated": "2026-05-13T08:00:00+00:00",
  "heartbeat_count": 5,
  "last_report_sent": "2026-05-13T08:30:00+00:00",
  "prs": {
    "42": {
      "pr_number": 42,
      "phase": "ReviewPending",
      "phase_entered_at": "2026-05-13T07:00:00+00:00",
      "last_action_at": "2026-05-13T07:00:00+00:00",
      "contributor_login": "alice",
      "next_action": {"skill": "prj-review", "mode": null}
    }
  }
}
EOF
```

Optionally, drop a triage artifact for PR #42 so the daemon has context to
inject:

```bash
mkdir -p _bmad-output/pr-workflow/prs/42
cat > _bmad-output/pr-workflow/prs/42/triage.md <<'EOF'
# PR #42 triage

- Author: alice
- Risk: low
- Touches: `src/auth.py`, `tests/test_auth.py`
EOF
```

Run another tick:

```bash
prj-agentd once --offline
```

The output now references PR #42:

```json
{
  "selection": {
    "pr": 42,
    "phase": "ReviewPending",
    "skill": "prj-review",
    "state_sha": "f1a2b3c4...",
    ...
  },
  "status": "executed",
  "thread_id": "thr_...",
  "pr_thread_id": "thr_..."
}
```

Three things worth confirming:

1. `state_sha` is a real 64-character SHA-256 of `state.json`. Cross-check
   with `sha256sum _bmad-output/pr-workflow/state.json`.
2. A second thread now exists in `pr_threads`:

   ```bash
   sqlite3 _bmad-output/pr-workflow/agentd/agentd.sqlite \
     "SELECT pr_number, pr_thread_id, latest_phase FROM pr_threads;"
   ```

3. The injected context included your `triage.md`. Search the timeline:

   ```bash
   grep '"context.injected"' \
     _bmad-output/pr-workflow/agentd/timeline/*.jsonl | jq .
   ```

   You'll see an `item_count` of `2` — one summary item plus one artifact.

## Step 5: Trigger a gated action and approve it

GitHub-mutating skills (`prj-decision`, `prj-implement-fix`,
`prj-report-daily`) park as **pending approval** instead of executing. This
step walks through the full approve-and-execute flow.

### 5a. Trigger the gate

Edit `state.json` so PR #42's `next_action` is `prj-decision`:

```bash
python3 -c "
import json, pathlib
p = pathlib.Path('_bmad-output/pr-workflow/state.json')
s = json.loads(p.read_text())
s['prs']['42']['phase'] = 'Reviewed'
s['prs']['42']['next_action'] = {'skill': 'prj-decision', 'mode': 'comment'}
p.write_text(json.dumps(s, indent=2, sort_keys=True))
"
```

Tick once:

```bash
prj-agentd once --offline
```

Output:

```json
{
  "decision": "require_human",
  "decision_reason": "skill 'prj-decision' default policy",
  "status": "human-approval-pending",
  "thread_id": null,
  "pr_thread_id": null
}
```

Notice what didn't happen: no thread was started, no `turn/start` was
issued, no run was recorded. The firewall held the line.

### 5b. Inspect the pending approval

```bash
prj-agentd status
```

```json
{
  "pending_approvals": [
    {
      "approval_id": "appr_a1b2c3d4e5f6",
      "kind": "skill",
      "payload": {
        "pr": 42,
        "skill": "prj-decision",
        "state_sha": "f1a2b3c4..."
      },
      "requested_at": "2026-05-13T09:15:22+00:00"
    }
  ],
  "approval_count": 1
}
```

Copy the `approval_id` from your output (it's deterministic per run; yours
will differ from the example).

### 5c. Approve

```bash
prj-agentd approve appr_a1b2c3d4e5f6 \
  --decision approve \
  --reason "reviewed the diff, looks safe"
```

```json
{"approval_id": "appr_a1b2c3d4e5f6", "decision": "approve"}
```

### 5d. Tick again — the approved action executes

```bash
prj-agentd once --offline
```

```json
{
  "decision": "auto_approve",
  "decision_reason": "session approval appr_a1b2c3d4e5f6",
  "status": "executed",
  ...
}
```

The daemon found your approval, matched it on `(skill, pr, state_sha)`, and
executed the action via the session-approval path. The timeline includes a
`tick.session-approval-found` event linking the run to the approval row.

## Step 6: Watch drift detection re-park

This step proves the daemon doesn't blindly execute on stale intent. If
`state.json` changes between approval and execution, the approval is
considered drifted and a new gate is requested.

Set up a fresh gated approval as in Step 5a–5b, then:

1. Record the decision (Step 5c).
2. Mutate `state.json` so the SHA changes:

   ```bash
   python3 -c "
   import json, pathlib
   p = pathlib.Path('_bmad-output/pr-workflow/state.json')
   s = json.loads(p.read_text())
   s['prs']['42']['last_action_at'] = '2026-05-13T10:00:00+00:00'
   p.write_text(json.dumps(s, indent=2, sort_keys=True))
   "
   ```

3. Tick:

   ```bash
   prj-agentd once --offline
   ```

The result is now `human-approval-pending` again — not `executed`. Check
the timeline:

```bash
grep '"tick.approval-drifted"' \
  _bmad-output/pr-workflow/agentd/timeline/*.jsonl | jq .
```

You'll see `old_state_sha` and `new_state_sha` recorded for the drift event,
and a fresh pending-approval row in the store.

## Step 7 (optional): Connect to a live `codex app-server`

When you're ready to swap `OfflineAppServer` for the real thing, the only
difference is the absence of `--offline`.

1. Start `codex app-server` over a Unix socket. Confirm the socket path
   matches `socket_path` in your `_bmad/config.toml`:

   ```bash
   test -S ~/.codex/app-server.sock && echo "socket is live"
   ```

2. Run a single live tick:

   ```bash
   prj-agentd once
   ```

3. Or run the loop until you interrupt it:

   ```bash
   prj-agentd run
   ```

Everything else (timeline, store, approvals CLI) works identically. The
only behavior that changes is the LLM turn itself: it actually runs through
`codex app-server`, executes commands under your sandbox policy, and may
issue server-initiated `approval/request` callbacks that the daemon will
route through the same policy firewall.

<!-- prettier-ignore -->
> [!TIP]
> Run live mode against a non-production repo first. The
> `approval_mode = "unlessTrusted"` default makes the daemon ask before
> doing anything write-y, but it's still a good idea to confirm the wire
> contract against a real `codex app-server` before turning the daemon
> loose on a backlog you care about.

## What "healthy" looks like

A clean tick produces this event chain in the timeline, in this order:

1. `appserver.handshake` — once at startup (or on reconnect).
2. `tick.started` — beginning of each tick.
3. `tick.selection` — selector returned a Selection (with `state_sha`).
4. `tick.policy-decision` — firewall returned `auto_approve`,
   `require_human`, or `auto_deny`.
5. One of these terminal events:
   - `tick.turn-completed` (executed)
   - `tick.approval-requested` (parked)
   - `tick.approval-drifted` followed by `tick.approval-requested` (stale
     approval invalidated)
   - `tick.skill-not-allowlisted` (skipped by allowlist)
   - `tick.selector-failed` (selector error)

Pair that with a clean store: every `runs` row has matching `started_at`
and `completed_at` (no `status='started'` lingering), every `approvals`
row either has `decision IS NULL` (pending) or has all of `decision`,
`decided_at`, `decided_by`, `reason` populated.

If both look right, the daemon is doing its job.

## Troubleshooting

### `prj_repo not configured`

You haven't set `prj_repo` in `_bmad/config.toml`. Check:

```bash
grep prj_repo _bmad/config.toml
```

### `orchestrator run.py not found at <path>`

The daemon's selector couldn't find `prj-orchestrator/scripts/run.py`.
You'll see this when running against a target repo that doesn't have the
pr-jangler skills copied in. The error looks like:

```json
{
  "status": "misconfigured",
  "selection": {
    "reason": "selector error: SelectorError('orchestrator run.py not found at /home/you/code/yourrepo/skills/prj-orchestrator/scripts/run.py')"
  }
}
```

You have two fixes. The override is recommended for testing against
arbitrary repos:

**Fix A (recommended): add the config override.** Edit
`_bmad/config.toml` and add the absolute path to your pr-jangler checkout:

```toml
[modules.prj.agentd]
orchestrator_run_py = "/home/you/code/pr-jangler/skills/prj-orchestrator/scripts/run.py"
```

Confirm the path exists:

```bash
ls /home/you/code/pr-jangler/skills/prj-orchestrator/scripts/run.py
```

**Fix B: copy the skill in.** If you want the orchestrator to live with
the target repo:

```bash
cp -r /path/to/pr-jangler/skills/prj-orchestrator skills/
```

### `state.json` keeps not being created

That's correct behavior. `prj-agentd` never writes `state.json` directly.
The PR Jangler phase skills (`prj-discover`, `prj-triage`, etc.) do that
through `state_io.py` when they actually run. In offline mode the LLM turn
is synthesized, so the real phase skill subprocess never runs, so
`state.json` stays uncreated. Seed it manually as shown in Step 4 to
exercise per-PR flows.

### Socket connection refused in live mode

The `codex app-server` socket isn't where you said it would be. Confirm:

```bash
ls -la ~/.codex/app-server.sock
```

Then either start `codex app-server` or update `socket_path` in
`_bmad/config.toml`.

### A pending approval won't clear

Two things to check:

1. `state.json` may have drifted since you approved — see Step 6. Look for
   `tick.approval-drifted` in the timeline.
2. The approval's `(skill, pr, state_sha)` must match the current
   selection's. Run:

   ```bash
   sqlite3 _bmad-output/pr-workflow/agentd/agentd.sqlite \
     "SELECT approval_id, decision, payload_json FROM approvals;"
   ```

   And compare the `state_sha` inside `payload_json` to the current
   `sha256sum _bmad-output/pr-workflow/state.json`.

## Next steps

- Read [`../../WHERE_WE_ARE.md`](../../WHERE_WE_ARE.md) for the full
  acceptance criteria — each row tells you what to look at to verify the
  system is doing its job.
- Read [`../README.md`](../README.md) for the architecture (selector seam,
  approval firewall, the policy matrix, the four milestones).
- Run the test suite from `prj-agentd/`:

  ```bash
  python3 -m unittest discover tests
  ```

  All 82 tests pass against the in-process and live-socket harnesses.
- When you're ready to drive a real backlog, install a real
  `codex app-server`, point the daemon at it (Step 7), and start with
  `approval_mode = "always"` so every action prompts for your decision
  the first few times.
