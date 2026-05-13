"""Mission Control daemon loop.

The invariant:

    PR Jangler chooses/records the state transition.
    app-server executes/observes/explains the agent work.

Per tick:
    1. select via `--select-only`
    2. if idle/misconfigured: log + sleep
    3. if action-selected: pre-flight policy check
    4. ensure repo+PR threads exist
    5. inject mirrored context (state summary, recent log tail, prior artifacts)
    6. run the phase via app-server (or offline synthesizer)
    7. record run completion, mirror events to timeline
    8. yield control (loop mode) or exit (once mode)
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .appserver import AppServerInterface, OfflineAppServer, ThreadHandle
from .config import AgentdConfig
from .policy import ApprovalRequest, Decision, PolicyEngine
from .rpc import JsonRpcError
from .selector import Selection, run_selector
from .store import AgentdStore
from .timeline import Timeline


# Notification methods that carry per-turn item lifecycle. The daemon mirrors
# each one into the timeline as `item.<lifecycle>` for the audit trail.
ITEM_LIFECYCLE_METHODS = frozenset({"item/started", "item/updated", "item/completed"})


# Codex thread IDs are UUIDs (with or without `urn:uuid:` prefix). When we
# cached a thread under offline mode the ID looks like `thr_xxxxxxxxxx` — that
# value is meaningless to a live codex server and will be rejected with
# `invalid thread id`. Use this regex to detect cached values that are NOT
# valid for live-mode dispatch and preemptively drop them.
_LIVE_THREAD_ID_PATTERN = re.compile(
    r"^(?:urn:uuid:)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _is_live_thread_id(thread_id: str) -> bool:
    """True iff `thread_id` looks like a codex UUID."""
    return bool(_LIVE_THREAD_ID_PATTERN.match(thread_id))


# Substrings that indicate codex rejected our thread id (or it has been
# purged). When we see one of these, the right move is to forget the cached
# thread and let the next operation start fresh.
_STALE_THREAD_ERROR_FRAGMENTS = (
    "invalid thread id",
    "thread not found",
    "unknown thread",
)


def _is_stale_thread_error(exc: BaseException) -> bool:
    if not isinstance(exc, JsonRpcError):
        return False
    msg = str(exc).lower()
    return any(frag in msg for frag in _STALE_THREAD_ERROR_FRAGMENTS)


@dataclass
class TickResult:
    """One pass of the daemon loop. Returned for tests + status output."""

    selection: Selection
    run_id: str | None
    skill: str | None
    decision: Decision | None
    decision_reason: str | None
    thread_id: str | None
    pr_thread_id: str | None
    status: str  # "executed" | "skipped" | "human-approval-pending" | "idle" | "misconfigured"

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection": self.selection.to_dict(),
            "run_id": self.run_id,
            "skill": self.skill,
            "decision": self.decision.value if self.decision else None,
            "decision_reason": self.decision_reason,
            "thread_id": self.thread_id,
            "pr_thread_id": self.pr_thread_id,
            "status": self.status,
        }


class Daemon:
    """Mission Control runtime for one project root.

    Holds long-lived references to: the local store, the timeline mirror,
    the policy engine, and one AppServerInterface (real or offline).
    """

    SERVICE_NAME = "prj-agentd"

    def __init__(
        self,
        config: AgentdConfig,
        *,
        appserver: AppServerInterface,
        store: AgentdStore | None = None,
        timeline: Timeline | None = None,
        policy: PolicyEngine | None = None,
    ):
        self.config = config
        self.appserver = appserver
        self.store = store or AgentdStore(config.store_path)
        self.timeline = timeline or Timeline(config.agentd_state_dir / "timeline")
        self.policy = policy or PolicyEngine(approval_mode=config.approval_mode)
        # Install server-initiated handlers so the app-server can ask the
        # daemon to gate commands, and so item/* notifications get mirrored.
        self.appserver.set_request_handler(self._on_server_request)
        self.appserver.set_notification_handler(self._on_notification)

    # ----------------------- server-initiated handlers ---------------------

    async def _on_server_request(self, method: str, params: Any) -> dict[str, Any]:
        """Handle a server-initiated JSON-RPC request.

        The app-server sends these when it needs the client to gate something:
        approval/request for command execution, approval/request for file
        changes, etc. We evaluate the policy and respond approve|decline.
        """
        if method != "approval/request":
            return {"decision": "decline", "reason": f"unsupported server method {method!r}"}

        params = params or {}
        kind = params.get("kind", "command")
        command = params.get("command", "")
        if kind == "command":
            appr = ApprovalRequest.for_command(
                run_id=params.get("turnId", ""),
                command=command,
            )
        else:
            appr = ApprovalRequest(
                approval_id=f"appr_{uuid.uuid4().hex[:12]}",
                kind=kind,
                payload=dict(params),
                run_id=params.get("turnId", ""),
            )
        result = self.policy.evaluate(appr)
        self.timeline.event(
            "appserver.approval-requested",
            run_id=appr.run_id or None,
            approval_id=appr.approval_id,
            kind=kind,
            command=command if kind == "command" else None,
            decision=result.decision.value,
            reason=result.reason,
        )
        if result.decision is Decision.AUTO_APPROVE:
            return {"decision": "approve", "reason": result.reason}
        if result.decision is Decision.AUTO_DENY:
            return {"decision": "decline", "reason": result.reason}
        # REQUIRE_HUMAN within a live turn: record and decline for now. The
        # daemon's loop-level approval path lets the human grant a session
        # approval; the next turn can re-issue and clear.
        self.store.record_approval_request(
            approval_id=appr.approval_id,
            run_id=appr.run_id or "",
            kind=kind,
            payload=dict(params),
        )
        return {"decision": "decline", "reason": "human approval required; logged for offline decision"}

    async def _on_notification(self, method: str, params: Any) -> None:
        """Mirror app-server notifications into the timeline audit trail."""
        if method in ITEM_LIFECYCLE_METHODS:
            params = params or {}
            self.timeline.event(
                f"item.{method.split('/', 1)[1]}",
                run_id=params.get("turnId"),
                item_type=params.get("itemType"),
                thread_id=params.get("threadId"),
                data=params.get("data"),
            )
            return
        # Catch-all: still mirror so the timeline never drops an event.
        self.timeline.event(
            f"appserver.notification",
            method=method,
            params=params,
        )

    # ------------------------------------------------------------------ ticks

    async def tick(self) -> TickResult:
        """Run exactly one daemon iteration. Returns a TickResult.

        Idempotent at the "did anything change in the world" level: a tick
        that decides not to dispatch performs no GitHub mutations and no
        PR Jangler state mutations.
        """
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        self.timeline.event("tick.started", run_id=run_id, project_root=str(self.config.project_root))

        try:
            selection = run_selector(
                self.config.project_root,
                orchestrator_run_py=self.config.orchestrator_run_py,
            )
        except Exception as exc:
            self.timeline.event(
                "tick.selector-failed",
                run_id=run_id,
                error=repr(exc),
            )
            return TickResult(
                selection=Selection(  # synthetic failure record
                    status="misconfigured", repo=None, pr=None, phase=None,
                    skill=None, mode=None, priority=0,
                    reason=f"selector error: {exc!r}", state_sha="no-state",
                ),
                run_id=run_id, skill=None, decision=None, decision_reason=None,
                thread_id=None, pr_thread_id=None, status="misconfigured",
            )

        self.timeline.event(
            "tick.selection",
            run_id=run_id,
            **selection.to_dict(),
        )

        if selection.status == "misconfigured":
            return TickResult(
                selection=selection, run_id=run_id, skill=None,
                decision=None, decision_reason=None,
                thread_id=None, pr_thread_id=None, status="misconfigured",
            )
        if selection.status == "idle" or not selection.is_actionable:
            return TickResult(
                selection=selection, run_id=run_id, skill=None,
                decision=None, decision_reason=None,
                thread_id=None, pr_thread_id=None, status="idle",
            )

        # Skill allowlist (if configured) takes precedence over policy.
        if self.config.skill_allowlist and selection.skill not in self.config.skill_allowlist:
            self.timeline.event(
                "tick.skill-not-allowlisted",
                run_id=run_id, skill=selection.skill,
            )
            return TickResult(
                selection=selection, run_id=run_id, skill=selection.skill,
                decision=Decision.AUTO_DENY,
                decision_reason=f"skill {selection.skill!r} not in allowlist",
                thread_id=None, pr_thread_id=None, status="skipped",
            )

        appr = ApprovalRequest.for_skill(run_id, selection.skill, pr=selection.pr)
        result = self.policy.evaluate(appr)
        self.timeline.event(
            "tick.policy-decision",
            run_id=run_id,
            skill=selection.skill,
            decision=result.decision.value,
            reason=result.reason,
        )

        if result.decision is Decision.AUTO_DENY:
            return TickResult(
                selection=selection, run_id=run_id, skill=selection.skill,
                decision=result.decision, decision_reason=result.reason,
                thread_id=None, pr_thread_id=None, status="skipped",
            )

        if result.decision is Decision.REQUIRE_HUMAN:
            # Session approval bypass: a previously-recorded human decision
            # matching (skill, pr, state_sha) lets this tick execute through
            # the auto-approve path. State drift invalidates the approval.
            drift = self.store.find_drifted_approval(
                skill=selection.skill,
                pr=selection.pr,
                current_state_sha=selection.state_sha,
            )
            if drift is not None:
                self.timeline.event(
                    "tick.approval-drifted",
                    run_id=run_id,
                    approval_id=drift["approval_id"],
                    old_state_sha=drift["old_state_sha"],
                    new_state_sha=drift["new_state_sha"],
                    skill=selection.skill,
                    pr=selection.pr,
                )
            session = self.store.find_session_approval(
                skill=selection.skill,
                pr=selection.pr,
                state_sha=selection.state_sha,
            )
            if session is not None:
                self.timeline.event(
                    "tick.session-approval-found",
                    run_id=run_id,
                    approval_id=session["approval_id"],
                    decided_by=session["decided_by"],
                    decided_at=session["decided_at"],
                )
                # Fall through to execution with a synthesized auto-approve result.
                result = type(result)(  # PolicyResult constructor
                    decision=Decision.AUTO_APPROVE,
                    reason=f"session approval {session['approval_id']}",
                    request=appr,
                )
            else:
                self.store.record_approval_request(
                    approval_id=appr.approval_id,
                    run_id=run_id,
                    kind="skill",
                    payload={"skill": selection.skill, "pr": selection.pr,
                             "state_sha": selection.state_sha},
                )
                self.timeline.event(
                    "tick.approval-requested",
                    run_id=run_id,
                    approval_id=appr.approval_id,
                    skill=selection.skill,
                    pr=selection.pr,
                )
                return TickResult(
                    selection=selection, run_id=run_id, skill=selection.skill,
                    decision=Decision.REQUIRE_HUMAN,
                    decision_reason=result.reason,
                    thread_id=None, pr_thread_id=None, status="human-approval-pending",
                )

        # AUTO_APPROVE -> execute through app-server (real or offline).
        # The full "ensure threads + inject context + run turn" sequence is
        # retried once if codex rejects the cached thread id mid-flight
        # (stale entry after offline→live mode switch, codex thread purged,
        # etc.). The retry forgets caches and starts fresh threads.
        repo = selection.repo or self.config.prj_repo
        self.store.record_run_started(
            run_id=run_id,
            repo=repo,
            pr_number=selection.pr,
            skill=selection.skill,
            selection=selection.to_dict(),
        )

        skill_path = self.config.project_root / "skills" / selection.skill / "SKILL.md"
        user_text = self._compose_user_text(selection)

        async def _ensure_and_inject() -> tuple[ThreadHandle, ThreadHandle | None]:
            repo_thread = await self._ensure_repo_thread(repo)
            pr_thread = None
            if selection.pr is not None:
                pr_thread = await self._ensure_pr_thread(
                    repo, selection.pr,
                    latest_phase=selection.phase,
                    latest_state_sha=selection.state_sha,
                    parent_thread_id=repo_thread.thread_id,
                )
            active = pr_thread or repo_thread
            await self._inject_runtime_context(active.thread_id, selection, run_id)
            return repo_thread, pr_thread

        try:
            repo_thread, pr_thread = await _ensure_and_inject()
        except JsonRpcError as exc:
            if not _is_stale_thread_error(exc):
                raise
            # Codex rejected the cached thread id. Invalidate, log, retry once.
            self.timeline.event(
                "tick.thread-cache-invalidated",
                run_id=run_id,
                repo=repo,
                pr=selection.pr,
                reason="codex rejected cached thread id",
                error=str(exc),
            )
            self.store.forget_repo_thread(repo)
            if selection.pr is not None:
                self.store.forget_pr_thread(repo, selection.pr)
            repo_thread, pr_thread = await _ensure_and_inject()

        active_thread = pr_thread or repo_thread
        turn = await self.appserver.run_turn(
            thread_id=active_thread.thread_id,
            cwd=self.config.project_root,
            approval_policy=self.config.approval_mode,
            skill_name=selection.skill,
            skill_path=skill_path,
            user_text=user_text,
        )
        turn_error = (
            turn.raw.get("error")
            if isinstance(turn.raw, dict) and turn.status == "failed"
            else None
        )
        self.timeline.event(
            "tick.turn-completed",
            run_id=run_id,
            thread_id=active_thread.thread_id,
            turn_id=turn.turn_id,
            turn_status=turn.status,
            item_count=len(turn.items),
            error=turn_error,
        )
        self.store.record_run_completed(
            run_id=run_id,
            status=turn.status,
            result={
                "turn_id": turn.turn_id,
                "items": turn.items,
                "error": turn_error,
                "raw": turn.raw if turn.status == "failed" else None,
            },
        )

        return TickResult(
            selection=selection, run_id=run_id, skill=selection.skill,
            decision=result.decision, decision_reason=result.reason,
            thread_id=repo_thread.thread_id,
            pr_thread_id=pr_thread.thread_id if pr_thread else None,
            status="executed",
        )

    # ------------------------------------------------------- loop entrypoints

    async def run_once(self) -> TickResult:
        return await self.tick()

    async def run_forever(self) -> None:
        """Loop ticks at `tick_seconds`, surviving non-fatal errors.

        Stops cleanly on KeyboardInterrupt / CancelledError.
        """
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.timeline.event("tick.crashed", error=repr(exc))
            await asyncio.sleep(self.config.tick_seconds)

    # -------------------------------------------------- thread plumbing

    def _cached_thread_id_is_valid(self, thread_id: str) -> bool:
        """Validate a cached thread id against the current server mode.

        In live mode (real codex app-server), thread ids MUST be UUIDs;
        synthetic offline/harness ids like `thr_xxxxx` are silently invalid
        until we send them and codex rejects with `invalid thread id`.

        In offline/harness mode, anything non-empty is fine — the
        OfflineAppServer doesn't validate format.
        """
        if not thread_id:
            return False
        if self.config.offline:
            return True
        return _is_live_thread_id(thread_id)

    async def _ensure_repo_thread(self, repo: str) -> ThreadHandle:
        cached = self.store.get_repo_thread(repo)
        if cached is not None:
            if self._cached_thread_id_is_valid(cached.root_thread_id):
                return ThreadHandle(
                    thread_id=cached.root_thread_id,
                    cwd=self.config.project_root,
                    approval_policy=self.config.approval_mode,
                    sandbox=self.config.sandbox,
                )
            # Stale cache (typically: offline-mode synthetic ID `thr_xxx`
            # left behind from a prior offline run, now hitting a live
            # codex server that only accepts UUIDs). Drop and recreate.
            self.timeline.event(
                "tick.thread-cache-invalidated",
                repo=repo,
                kind="repo",
                stale_thread_id=cached.root_thread_id,
                reason="format mismatch for current server mode",
            )
            self.store.forget_repo_thread(repo)
        seed = (
            f"You are the PR Jangler control thread for {repo}. Maintain the "
            f"backlog pipeline, but never bypass PR Jangler's state machine. "
            f"All state mutations flow through scripts/state_io.py."
        )
        handle = await self.appserver.start_thread(
            cwd=self.config.project_root,
            approval_policy=self.config.approval_mode,
            sandbox=self.config.sandbox,
            service_name=self.SERVICE_NAME,
            seed_text=seed,
        )
        # Goals are an experimental codex feature and may be disabled by the
        # server's local config. Treat it as best-effort: if the call fails,
        # log and keep going — the thread is fully functional without a goal.
        try:
            await self.appserver.set_goal(
                handle.thread_id,
                objective=(
                    f"Continuously drain PR backlog for {repo} via PR Jangler. "
                    "Preserve idempotency, auditability, and human gates."
                ),
            )
        except Exception as exc:
            self.timeline.event(
                "appserver.goal-set-failed",
                thread_id=handle.thread_id,
                error=repr(exc),
            )
        self.store.upsert_repo_thread(repo, handle.thread_id)
        self.timeline.event("repo-thread.created", repo=repo, thread_id=handle.thread_id)
        return handle

    async def _ensure_pr_thread(
        self,
        repo: str,
        pr: int,
        *,
        latest_phase: str | None,
        latest_state_sha: str | None,
        parent_thread_id: str,
    ) -> ThreadHandle:
        cached = self.store.get_pr_thread(repo, pr)
        if cached is not None:
            if self._cached_thread_id_is_valid(cached.pr_thread_id):
                # Refresh phase / sha cache for downstream context
                self.store.upsert_pr_thread(
                    repo, pr, cached.pr_thread_id,
                    latest_phase=latest_phase,
                    latest_state_sha=latest_state_sha,
                )
                return ThreadHandle(
                    thread_id=cached.pr_thread_id,
                    cwd=self.config.project_root,
                    approval_policy=self.config.approval_mode,
                    sandbox=self.config.sandbox,
                    parent_thread_id=parent_thread_id,
                )
            # Stale cache; drop and recreate. See `_ensure_repo_thread`.
            self.timeline.event(
                "tick.thread-cache-invalidated",
                repo=repo, pr=pr,
                kind="pr",
                stale_thread_id=cached.pr_thread_id,
                reason="format mismatch for current server mode",
            )
            self.store.forget_pr_thread(repo, pr)
        seed = (
            f"You are the PR Jangler thread for PR #{pr} in {repo}. "
            "Cache-first: read per-PR artifacts before touching GitHub. "
            "Default: no GitHub mutations until explicitly approved."
        )
        handle = await self.appserver.start_thread(
            cwd=self.config.project_root,
            approval_policy=self.config.approval_mode,
            sandbox=self.config.sandbox,
            service_name=self.SERVICE_NAME,
            seed_text=seed,
            parent_thread_id=parent_thread_id,
        )
        self.store.upsert_pr_thread(
            repo, pr, handle.thread_id,
            latest_phase=latest_phase,
            latest_state_sha=latest_state_sha,
        )
        self.timeline.event(
            "pr-thread.created",
            repo=repo, pr=pr, thread_id=handle.thread_id,
            parent_thread_id=parent_thread_id,
        )
        return handle

    async def _inject_runtime_context(
        self,
        thread_id: str,
        selection: Selection,
        run_id: str,
    ) -> None:
        """Mirror PR Jangler state into the active thread before the turn.

        Per WHERE_WE_WANT_TO_BE.md Step 2: feed state summary, recent JSONL
        log tail, per-PR cache paths, and metadata so the next turn starts
        warm instead of wandering.
        """
        items: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "run_id": run_id,
                        "selection": selection.to_dict(),
                    },
                    indent=2,
                ),
            },
        ]
        pr_cache_dir = (
            self.config.project_root
            / "_bmad-output" / "pr-workflow" / "prs"
            / (str(selection.pr) if selection.pr else "")
        )
        if selection.pr is not None and pr_cache_dir.is_dir():
            for artifact in ("meta.json", "triage.md", "verification.md",
                             "review.md", "fix-plan.md", "adversarial.md"):
                p = pr_cache_dir / artifact
                if p.is_file():
                    items.append({
                        "type": "text",
                        "text": f"--- {artifact} ---\n{p.read_text(encoding='utf-8')}",
                    })
        await self.appserver.inject_items(thread_id, items)
        self.timeline.event(
            "context.injected",
            run_id=run_id,
            thread_id=thread_id,
            item_count=len(items),
        )

    def _compose_user_text(self, selection: Selection) -> str:
        if selection.pr is None:
            return (
                f"$ {selection.skill}\nRun {selection.skill}. "
                f"Reason: {selection.reason}. "
                "Default to cache-only and never mutate GitHub without explicit approval."
            )
        return (
            f"$ {selection.skill}\nRun {selection.skill} for PR #{selection.pr} "
            f"(phase: {selection.phase}). Reason: {selection.reason}. "
            "Default to cache-only and never mutate GitHub without explicit approval."
        )


# --------------------------------------------------- top-level factory


async def build_daemon(config: AgentdConfig) -> Daemon:
    """Build a Daemon, perform the handshake, and emit lifecycle events.

    Live mode: connect to `codex app-server` over Unix socket, run the
    initialize+skills/list handshake, emit `appserver.handshake` with the
    serverInfo for AC A.2.

    Offline mode: stand up an OfflineAppServer that satisfies the same
    interface; emit a marker handshake so the audit trail stays uniform.
    """
    timeline = Timeline(config.agentd_state_dir / "timeline")

    async def _on_event(name: str, payload: dict[str, Any]) -> None:
        timeline.event(name, **payload)

    if config.offline:
        appserver: AppServerInterface = OfflineAppServer(on_synthetic_event=_on_event)
        init_result = await appserver.initialize()
        skills = await appserver.list_skills()
        timeline.event(
            "appserver.handshake",
            mode="offline",
            server_info=init_result.get("serverInfo", {}),
            experimental_api=bool(init_result.get("experimentalApi", False)),
            skill_count=len(skills),
            skills=[s.get("name") for s in skills],
        )
    else:
        from .appserver import AppServerClient
        appserver = await AppServerClient.connect(config.socket_path)
        init_result = await appserver.initialize()
        # codex's InitializeResponse (protocol v1) returns top-level fields
        # like `userAgent`, `codexHome`, `platformOs`; harness wraps under
        # `serverInfo`. Be tolerant of both.
        server_info = (
            init_result.get("serverInfo")
            if isinstance(init_result, dict) and isinstance(init_result.get("serverInfo"), dict)
            else (init_result if isinstance(init_result, dict) else {})
        )
        try:
            skills = await appserver.list_skills()
        except Exception as exc:
            skills = []
            timeline.event("appserver.skills-list-failed", error=repr(exc))
        timeline.event(
            "appserver.handshake",
            mode="live",
            socket_path=str(config.socket_path),
            server_info=server_info,
            experimental_api=bool(init_result.get("experimentalApi", False))
            if isinstance(init_result, dict) else False,
            skill_count=len(skills),
            skills=[s.get("name") if isinstance(s, dict) else s for s in skills],
        )
    store = AgentdStore(config.store_path)
    return Daemon(config, appserver=appserver, store=store, timeline=timeline)
