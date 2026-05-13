---
pipeline-status:
  - new
modified: 2026-05-13T03:45:50-04:00
---
Absolutely. I’d build this as **PR Jangler Mission Control**: not “Codex runs cron,” but a **daemonized app-server control plane** that wraps PR Jangler’s deterministic state machine with persistent agent threads, approval policy, resumable context, live traces, and per-PR memory.

The key insight: **PR Jangler already has the right core abstraction**. Its README says the heartbeat chooses one queued action, dispatches one phase skill, repeats forever, and logs every outcome; `state.json` is the queue/source of truth, while deterministic plumbing lives in scripts and LLM work lives in skills. ([GitHub](https://raw.githubusercontent.com/delorenj/pr-jangler/main/README.md "raw.githubusercontent.com")) App-server then supplies the missing “agent runtime OS” layer: thread persistence, turn streaming, goals, approvals, skill invocation, dynamic tools, command/process execution, and review threads. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub")) Your pasted Copilot brainstorm is basically pointing at the same monster: treat app-server as a stateful local runtime / mission-control substrate rather than a chat endpoint.

## The architecture: three layers, no nonsense

### 1. PR Jangler remains the deterministic source of truth

Do **not** let the app-server agent freelance-write `_bmad-output/pr-workflow/state.json`. That way lies gremlin soup.

PR Jangler’s hub skill already establishes the rules: the queue is authoritative, all state I/O goes through `state_io.py`, actions are idempotent per heartbeat, every outcome is logged, and `PleaseAdvise` is the escape hatch. ([GitHub](https://raw.githubusercontent.com/delorenj/pr-jangler/main/skills/pr-jangler/SKILL.md "raw.githubusercontent.com")) The orchestrator skill also says each invocation executes exactly one state transition, picks the highest-priority next action, persists state atomically, appends a structured run-log entry, and exits. ([GitHub](https://raw.githubusercontent.com/delorenj/pr-jangler/main/skills/prj-orchestrator/SKILL.md "raw.githubusercontent.com"))

So the app-server daemon should treat PR Jangler like this:

```text
PR Jangler = state machine + durable queue + phase contracts
app-server = runtime control plane + agent execution + approvals + trace UI
```

That separation is beautiful. Layers! Delicious layers. Software lasagna, but the good kind.

### 2. App-server becomes the daemonized agent runtime

Run a single local `codex app-server` as a daemon, preferably over a Unix socket. The README lists `stdio`, websocket, Unix socket, and off transports; websocket is explicitly experimental/unsupported, while the Unix socket transport is intended for local app-server control-plane clients. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub"))

Then connect multiple small clients to it:

```text
codex app-server daemon
        ▲
        │ JSON-RPC over unix socket
        │
┌───────┴──────────────────────────────┐
│ prj-agentd                            │  scheduler + state mirror + dispatcher
│ prj-console                           │  dashboard / timeline / approvals
│ prj-approvald                         │  policy engine / human gate
│ prj-metrics                           │  event consumer / traces / cost
└───────────────────────────────────────┘
```

This lines up nicely with app-server’s per-connection notification opt-out: a dashboard can subscribe to rich `item/*` events, a metrics client can ignore token deltas, and an approval UI can mostly care about approval requests. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub"))

### 3. The daemon owns scheduling, threads, approvals, and observability

The daemon does **not** own PR state. It owns the agent runtime state:

```text
repo_id
repo_root
prj_repo
root_thread_id
pr_number -> pr_thread_id
pr_number -> latest_phase
pr_number -> latest_appserver_turn
run_id -> appserver trace items
approval_id -> decision/audit metadata
```

Important caveat: app-server’s `thread/metadata/update` currently supports persisted `gitInfo`; I would not depend on it for arbitrary PR metadata. Keep your own tiny SQLite mapping for PR number → thread id, and use `thread/name/set` for human-readable labels if desired. The app-server docs describe `thread/metadata/update` in terms of `gitInfo`, not custom app payloads. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub"))

## The first experiment I’d build

Build a thin daemon called something like `prj-agentd`.

Its job:

```text
1. Start / connect to daemonized app-server.
2. Initialize JSON-RPC with experimentalApi enabled.
3. Discover PR Jangler skills with skills/list.
4. Start or resume one root thread per repo.
5. Set a repo-level goal.
6. Run exactly one PR Jangler heartbeat.
7. Mirror the resulting state/log event.
8. If an LLM phase is selected, execute that phase through app-server.
9. Stream all app-server events into an audit timeline.
10. Stop before any irreversible GitHub mutation unless approval policy allows it.
```

App-server supports `skills/list`, skill invocation using `$<skill-name>` plus a structured `skill` input item, and emits `skills/changed` when local skill files change. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub")) That is perfect for your PR Jangler skills because the daemon can discover `prj-orchestrator`, `prj-review`, `prj-plan-fix`, etc., then explicitly attach the relevant skill item to a turn instead of relying on fuzzy name resolution.

## Recommended MVP flow

### Step 0: Add one tiny PR Jangler adapter mode

I would add this to `prj-orchestrator`:

```bash
python3 scripts/run.py --select-only --json
```

Output:

```json
{
  "status": "action-selected",
  "repo": "owner/repo",
  "pr": 123,
  "phase": "ReviewPending",
  "skill": "prj-review",
  "mode": null,
  "priority": 130,
  "reason": "next_action",
  "state_sha": "..."
}
```

Why? Your current orchestrator owns action selection and dispatch. That is good for cron. But for app-server, you want the **deterministic selector** to choose the action, and the **app-server daemon** to execute the selected phase so it can stream, approve, inject context, and persist thread memory.

You already have `--dry-run`; this new mode would be the more machine-readable cousin. Less “pretend to act,” more “give me the next actor message.” Tiny change, huge leverage.

### Step 1: Start the repo control thread

For each configured repo:

```json
{
  "method": "thread/start",
  "params": {
    "cwd": "/path/to/project",
    "approvalPolicy": "unlessTrusted",
    "sandbox": "workspaceWrite",
    "serviceName": "prj-agentd",
    "input": [
      {
        "type": "text",
        "text": "You are the PR Jangler control thread for owner/repo. Maintain the backlog pipeline, but never bypass PR Jangler's state machine."
      }
    ]
  }
}
```

Then immediately call `thread/goal/set`:

```json
{
  "method": "thread/goal/set",
  "params": {
    "threadId": "thr_repo",
    "objective": "Continuously drain PR backlog for owner/repo via PR Jangler. Preserve idempotency, auditability, and human gates.",
    "tokenBudget": 200000
  }
}
```

App-server supports persisted thread goals with objective, status, token budget, token usage, and update notifications. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub")) Use that as your daemon-visible mission contract, not as business state.

### Step 2: Mirror PR Jangler state into the thread

Before a phase turn, inject the relevant structured context:

```text
- current state.json summary
- selected action
- recent JSONL log tail
- per-PR cache paths
- GitHub PR metadata snapshot
- previous phase artifacts: triage.md, verification.md, review.md, fix-plan.md, adversarial.md
```

Use `thread/inject_items` for this. The app-server API explicitly supports appending raw Responses-style items into a loaded thread without starting a user turn. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub")) This is the “synthetic memory” trick from the Copilot note: preload logs, ticket summaries, repo metadata, and prior remediation context so the next turn starts warm rather than wandering around like a Roomba with imposter syndrome.

### Step 3: Execute the selected phase through the right lane

Use two execution lanes.

|Phase type|Example skills|Best primitive|Why|
|---|---|--:|---|
|Deterministic plumbing|`prj-discover`, state validation, log inspection|`command/exec`|Runs under sandbox and does not require an LLM turn.|
|LLM-heavy phase|`prj-triage`, `prj-review`, `prj-plan-fix`, adversarial reasoning|`turn/start` with skill item|Gives skill instructions, event stream, approvals, and persisted thread context.|
|Specialist review|`prj-review`, final sanity check, policy review|`review/start` with `delivery: "detached"`|Separate review context without polluting the main thread.|
|Long-lived helpers|log tailer, preview server, benchmark watcher|`process/spawn`, cautiously|Useful for daemons; explicitly unsandboxed, so don’t let it become goblin root.|

App-server exposes `command/exec` for sandboxed commands and `process/spawn` for standalone unsandboxed host processes. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub")) It also supports detached review threads via `review/start`, where `delivery: "detached"` forks a new review thread and streams review items separately. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub"))

For a skill turn:

```json
{
  "method": "turn/start",
  "params": {
    "threadId": "thr_pr_123",
    "cwd": "/path/to/project",
    "approvalPolicy": "unlessTrusted",
    "approvalsReviewer": "auto_review",
    "input": [
      {
        "type": "text",
        "text": "$prj-review Run cache-first review for PR #123. Do not post to GitHub unless explicitly approved."
      },
      {
        "type": "skill",
        "name": "prj-review",
        "path": "/path/to/pr-jangler/skills/prj-review/SKILL.md"
      }
    ]
  }
}
```

This maps perfectly onto `prj-review`: the skill fetches PR diff/context, requires findings anchored to file and line, persists `review.md`, defaults `prj_post_review_comment` to false, and never approves or requests changes. ([GitHub](https://raw.githubusercontent.com/delorenj/pr-jangler/main/skills/prj-review/SKILL.md "raw.githubusercontent.com"))

### Step 4: One PR = one actor thread

This is the clever bit.

Model each PR as an actor:

```text
Repo control thread
  ├── PR #101 thread
  ├── PR #102 thread
  ├── PR #103 thread
  │      ├── detached review thread
  │      └── detached adversarial validation thread
  └── daily report thread
```

The repo thread owns scheduling and summary. The PR thread owns local memory for that PR. Detached threads own adversarial/specialist perspectives.

That maps almost comically well to PR Jangler’s own phase split. The adversarial validator explicitly wants structural separation from the planner: it reads `fix-plan.md`, `verification.md`, and `review.md`, runs the regression suite, then defaults to reject unless the six-item checklist passes. ([GitHub](https://raw.githubusercontent.com/delorenj/pr-jangler/main/skills/prj-validate-adversarial/SKILL.md "raw.githubusercontent.com")) App-server’s `thread/fork` and detached `review/start` give you that separation at the runtime level too. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub"))

## The daemon loop

Something like:

```ts
while (true) {
  const repo = await loadRepoConfig();

  const rootThread = await ensureRepoThread(repo);
  await setOrRefreshGoal(rootThread, repo);

  const selection = await commandExecJson({
    cwd: repo.root,
    command: "python3 skills/prj-orchestrator/scripts/run.py --select-only --json"
  });

  if (selection.status === "idle") {
    await sleepUntilNextHeartbeat();
    continue;
  }

  const prThread = selection.pr
    ? await ensurePrThread(repo, selection.pr)
    : rootThread;

  await injectPrjContext(prThread, {
    stateSummary: await readStateSummary(repo),
    selectedAction: selection,
    recentLogs: await tailRunLog(repo),
    prCache: await readPrCache(repo, selection.pr)
  });

  const result = await runPhaseThroughAppServer({
    thread: prThread,
    skill: selection.skill,
    pr: selection.pr,
    mode: selection.mode,
    approvalPolicy: policyFor(selection)
  });

  await mirrorEventsToTimeline(result.events);
  await reconcileStateAndLogs(repo);
}
```

The important invariant:

```text
PR Jangler chooses/records the state transition.
app-server executes/observes/explains the agent work.
```

No double books. No “LLM rewrote the queue because it felt spiritually aligned.” Jail for that robot.

## Approval policy: make it a firewall, not a prompt

App-server approval handling is first-class: command and file-change approvals arrive as server-initiated JSON-RPC requests; clients respond with decisions like accept, accept-for-session, decline, or cancel. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub")) It also supports `approvalsReviewer: "auto_review"` to route approval requests through a prompted subagent. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub"))

I’d use a policy matrix:

|Action|Default|
|---|--:|
|Read `state.json`, per-PR cache, logs|Auto-approve|
|`gh pr view`, `gh pr diff`, `gh api rate_limit`|Auto-approve|
|`python3 scripts/run.py --dry-run`|Auto-approve|
|Write PR Jangler cache files through approved scripts|Auto-approve in workspace|
|Modify `state.json` directly|Deny|
|Post GitHub PR comment/review|Human approval|
|Apply labels|Human approval at first; maybe session approval later|
|Open fix PR|Human approval|
|Send email digest|Human approval until trusted|
|`thread/shellCommand`|Avoid for daemon automation; it runs unsandboxed/full access.|

That last point matters: `thread/shellCommand` is documented as unsandboxed and not inheriting the thread sandbox policy. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub")) Use `command/exec` for daemon-controlled deterministic work instead.

## Dynamic tools I’d expose to the agent

Use app-server dynamic tools as the agent’s read-only “sensory organs,” not as state mutators. Dynamic tools are experimental, require `experimentalApi`, and can be registered with `deferLoading` so they remain callable without bloating ordinary model context. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub"))

Suggested tools:

```text
prj_read_state()
prj_get_next_action()
prj_read_pr_cache(pr_number)
prj_tail_runlog(date, limit)
github_pr_snapshot(pr_number)
github_pr_diff_stats(pr_number)
prj_policy_check(action)
prj_emit_dashboard_event(event)
```

I would **not** expose:

```text
prj_write_state(...)
github_post_comment(...)
github_apply_label(...)
github_open_fix_pr(...)
```

Those should go through PR Jangler scripts plus app-server approval flow.

## Event timeline / dashboard

This is where app-server becomes wildly useful. Each turn streams `turn/started`, `turn/completed`, `turn/diff/updated`, plan updates, and item lifecycle events; item types include `commandExecution`, `fileChange`, `mcpToolCall`, `webSearch`, `reasoning`, and more. ([GitHub](https://github.com/openai/codex/tree/main/codex-rs/app-server "codex/codex-rs/app-server at main · openai/codex · GitHub"))

Build a UI that renders:

```text
Repo backlog
  PR #123
    phase: ReviewPending
    next action: prj-review
    app-server thread: active
    last turn: completed
    artifacts:
      meta.json
      triage.md
      review.md
    approvals:
      pending GitHub comment approval
    trace:
      plan -> gh diff fetch -> review findings -> cache persist -> state transition
```

This is the “Temporal for local AI work” shape from the Copilot note: threads as jobs, goals as objectives, approvals as queue items, turns as executions, items as trace spans.

## Suggested first milestone

Make the first milestone intentionally boring:

### Milestone A: cache-only review loop

Target one repo and one open PR.

1. Start `codex app-server` over Unix socket.
    
2. Start `prj-agentd`.
    
3. Run `prj-setup` / config validation.
    
4. Run `prj-discover --dry-run`.
    
5. Create repo thread.
    
6. Create PR thread.
    
7. Inject PR metadata and selected action.
    
8. Invoke `$prj-review` through `turn/start`.
    
9. Persist `review.md`.
    
10. Do **not** post to GitHub.
    
11. Show a dashboard timeline with:
    
    - selected action,
        
    - app-server streamed events,
        
    - command executions,
        
    - final `review.md`,
        
    - PR Jangler run-log entry.
        

That proves the stack without irreversible side effects.

Then Milestone B is “human-approved GitHub comment.” Milestone C is “adversarial validator in detached thread.” Milestone D is “approved fix PR generation.”

## One strong product shape

I’d name the experiment internally:

```text
prj-agentd: actorized PR backlog daemon
```

Its killer feature is:

```text
Every PR becomes a durable agent thread.
Every heartbeat becomes an auditable turn.
Every mutation goes through PR Jangler state_io + app-server approvals.
Every risky judgment gets a detached adversarial sibling.
```

That is not just “cron with an LLM.” That is a proper layered runtime:

```text
GitHub reality
  ↓
PR Jangler deterministic queue/state/log layer
  ↓
app-server thread/turn/item runtime layer
  ↓
approval + policy layer
  ↓
dashboard / mission-control layer
```

For the very first code change, I’d add `--select-only --json` to `prj-orchestrator`, then write the tiny Unix-socket JSON-RPC daemon that can turn that selected action into an app-server skill turn. That one seam is where the whole agentic backlog pipeline becomes composable instead of becoming cron wearing a fake mustache.