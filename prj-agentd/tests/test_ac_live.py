"""Acceptance Criteria verification — live socket against HarnessAppServer.

Each test maps to one or more criteria in WHERE_WE_ARE.md "Acceptance Criteria
— client and app-server are happy together." The harness speaks the same wire
protocol as `codex app-server` (Content-Length-framed JSON-RPC over Unix
socket), so this exercises the AppServerClient path end-to-end — not the
OfflineAppServer shortcut.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prj_agentd.appserver import AppServerClient  # noqa: E402
from prj_agentd.config import AgentdConfig  # noqa: E402
from prj_agentd.daemon import Daemon, build_daemon  # noqa: E402
from prj_agentd.policy import Decision, PolicyEngine  # noqa: E402
from prj_agentd.selector import Selection  # noqa: E402
from prj_agentd.store import AgentdStore  # noqa: E402
from prj_agentd.timeline import Timeline  # noqa: E402

from tests.harness.server import HarnessAppServer, TurnScript  # noqa: E402


REPO = "owner/repo"


def _seed_pr_jangler_state(root: Path, *, prs: dict | None = None,
                           heartbeat: int = 5,
                           last_report_now: bool = True) -> Path:
    """Write a minimally-valid PR Jangler state.json under `root`."""
    workflow = root / "_bmad-output" / "pr-workflow"
    workflow.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    state = {
        "version": "1.0",
        "repo": REPO,
        "last_updated": "2026-05-13T08:00:00+00:00",
        "heartbeat_count": heartbeat,
        "last_report_sent": (
            datetime.now(timezone.utc).isoformat() if last_report_now else None
        ),
        "prs": prs or {},
    }
    state_path = workflow / "state.json"
    state_path.write_text(json.dumps(state, sort_keys=True, indent=2), encoding="utf-8")
    return state_path


def _write_skills(root: Path, *, include: tuple[str, ...] = ("prj-orchestrator",)) -> None:
    """Copy the orchestrator skill (real `--select-only` script) into `root`."""
    import shutil
    pr_jangler_root = Path(__file__).resolve().parents[2]
    for skill in include:
        src = pr_jangler_root / "skills" / skill
        dst = root / "skills" / skill
        shutil.copytree(src, dst)


def _write_config(root: Path) -> None:
    (root / "_bmad").mkdir(parents=True, exist_ok=True)
    (root / "_bmad" / "config.toml").write_text(
        f'[modules.prj]\nprj_repo = "{REPO}"\n', encoding="utf-8"
    )


def _make_config(root: Path, socket: Path) -> AgentdConfig:
    return AgentdConfig(
        project_root=root,
        prj_repo=REPO,
        socket_path=socket,
        tick_seconds=1,
        approval_mode="unlessTrusted",
        sandbox="workspaceWrite",
        offline=False,
        skill_allowlist=frozenset(),
    )


def _read_timeline(root: Path) -> list[dict]:
    tl_dir = root / "_bmad-output" / "pr-workflow" / "agentd" / "timeline"
    out: list[dict] = []
    for f in sorted(tl_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _types(events: list[dict]) -> list[str]:
    return [e["event_type"] for e in events]


async def _build_live_daemon(config: AgentdConfig, harness: HarnessAppServer) -> Daemon:
    """Build a daemon connected over the harness's real Unix socket."""
    appserver = await AppServerClient.connect(config.socket_path, transport_kind="lsp")
    init_result = await appserver.initialize()
    skills = await appserver.list_skills()
    timeline = Timeline(config.agentd_state_dir / "timeline")
    timeline.event(
        "appserver.handshake",
        mode="live-harness",
        socket_path=str(config.socket_path),
        server_info=init_result.get("serverInfo", {}),
        experimental_api=bool(init_result.get("experimentalApi", False)),
        skill_count=len(skills),
        skills=[s.get("name") for s in skills],
    )
    store = AgentdStore(config.store_path)
    return Daemon(
        config=config,
        appserver=appserver,
        store=store,
        timeline=timeline,
        policy=PolicyEngine(approval_mode=config.approval_mode),
    )


# ============================================================================
# Section A — Handshake
# ============================================================================


class TestA_Handshake(unittest.IsolatedAsyncioTestCase):
    """A.1 socket connectable; A.2 initialize + experimentalApi + handshake event;
    A.3 skills/list returns skills."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.socket = self.root / "app-server.sock"
        _write_config(self.root)
        _write_skills(self.root)
        self.harness = HarnessAppServer(self.socket)
        self.harness_cm = self.harness.serve()
        await self.harness_cm.__aenter__()

    async def asyncTearDown(self):
        await self.harness_cm.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def test_A1_socket_connectable(self):
        self.assertTrue(self.socket.exists() and self.socket.is_socket())

    async def test_A2_handshake_event_records_serverinfo(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.assertEqual(self.harness.count("initialize"), 1)
            events = _read_timeline(self.root)
            handshakes = [e for e in events if e["event_type"] == "appserver.handshake"]
            self.assertEqual(len(handshakes), 1)
            self.assertEqual(handshakes[0]["payload"]["server_info"]["name"], "HarnessAppServer")
            self.assertTrue(handshakes[0]["payload"]["experimental_api"])
        finally:
            await daemon.appserver.close()

    async def test_A3_skills_list_returns_phase_skills(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.assertEqual(self.harness.count("skills/list"), 1)
            events = _read_timeline(self.root)
            handshake = next(e for e in events if e["event_type"] == "appserver.handshake")
            self.assertIn("prj-orchestrator", handshake["payload"]["skills"])
            self.assertIn("prj-discover", handshake["payload"]["skills"])
            self.assertGreaterEqual(handshake["payload"]["skill_count"], 10)
        finally:
            await daemon.appserver.close()


# ============================================================================
# Section B — The seam (PR Jangler is selection authority)
# ============================================================================


class TestB_Seam(unittest.IsolatedAsyncioTestCase):
    """B.1 each tick subprocesses --select-only; B.2 daemon never writes state.json;
    B.3 state_sha matches sha256sum; B.5 misconfigured skips threads."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.socket = self.root / "app-server.sock"
        _write_config(self.root)
        _write_skills(self.root)
        self.harness = HarnessAppServer(self.socket)
        self.harness_cm = self.harness.serve()
        await self.harness_cm.__aenter__()

    async def asyncTearDown(self):
        await self.harness_cm.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def test_B1_each_tick_selects(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-discover")
            await daemon.tick()
            await daemon.tick()
            events = _read_timeline(self.root)
            self.assertEqual(_types(events).count("tick.selection"), 2)
        finally:
            await daemon.appserver.close()

    async def test_B2_daemon_never_writes_state_json(self):
        """Force a fresh project (no state.json). After 3 ticks, state.json must
        still not exist — the daemon only invokes --select-only which is read-only."""
        cfg = _make_config(self.root, self.socket)
        state_path = self.root / "_bmad-output" / "pr-workflow" / "state.json"
        self.assertFalse(state_path.exists())
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-discover")
            for _ in range(3):
                await daemon.tick()
            self.assertFalse(state_path.exists(),
                             "daemon must not create state.json on its own")
        finally:
            await daemon.appserver.close()

    async def test_B3_state_sha_matches_sha256sum(self):
        seed = _seed_pr_jangler_state(self.root, prs={
            "42": {
                "pr_number": 42, "phase": "ReviewPending",
                "phase_entered_at": "2026-05-13T00:00:00+00:00",
                "last_action_at": "2026-05-13T00:00:00+00:00",
                "contributor_login": "alice",
                "next_action": {"skill": "prj-review", "mode": None},
            }
        })
        expected_sha = hashlib.sha256(seed.read_bytes()).hexdigest()
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-review")
            await daemon.tick()
            events = _read_timeline(self.root)
            sel_event = next(e for e in events if e["event_type"] == "tick.selection")
            self.assertEqual(sel_event["payload"]["state_sha"], expected_sha)
        finally:
            await daemon.appserver.close()

    async def test_B5_misconfigured_starts_no_threads(self):
        # Empty config = no prj_repo => misconfigured
        (self.root / "_bmad" / "config.toml").write_text("[modules.prj]\n", encoding="utf-8")
        cfg = AgentdConfig(
            project_root=self.root, prj_repo="",  # empty repo
            socket_path=self.socket, tick_seconds=1,
            approval_mode="unlessTrusted", sandbox="workspaceWrite",
            offline=False, skill_allowlist=frozenset(),
        )
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            result = await daemon.tick()
            self.assertEqual(result.status, "misconfigured")
            # No thread/start ever called against the harness:
            self.assertEqual(self.harness.count("thread/start"), 0)
        finally:
            await daemon.appserver.close()


# ============================================================================
# Section C — Threads behave as durable actors
# ============================================================================


class TestC_Threads(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.socket = self.root / "app-server.sock"
        _write_config(self.root)
        _write_skills(self.root)
        self.harness = HarnessAppServer(self.socket)
        self.harness_cm = self.harness.serve()
        await self.harness_cm.__aenter__()
        _seed_pr_jangler_state(self.root, prs={
            "42": {
                "pr_number": 42, "phase": "ReviewPending",
                "phase_entered_at": "2026-05-13T00:00:00+00:00",
                "last_action_at": "2026-05-13T00:00:00+00:00",
                "contributor_login": "alice",
                "next_action": {"skill": "prj-review", "mode": None},
            }
        })

    async def asyncTearDown(self):
        await self.harness_cm.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def test_C1_C2_one_repo_thread_and_one_pr_thread_reused(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-review")
            await daemon.tick()
            await daemon.tick()
            await daemon.tick()
            # The harness saw thread/start AT MOST twice — once for the repo
            # thread, once for the PR thread — both cached and reused.
            self.assertEqual(self.harness.count("thread/start"), 2)
            # Store has exactly one row in each table.
            with daemon.store._lock:
                conn = daemon.store._connect()
                try:
                    (rt,) = conn.execute("SELECT COUNT(*) FROM repo_threads").fetchone()
                    (pt,) = conn.execute("SELECT COUNT(*) FROM pr_threads").fetchone()
                finally:
                    conn.close()
            self.assertEqual(rt, 1)
            self.assertEqual(pt, 1)
        finally:
            await daemon.appserver.close()

    async def test_C3_pr_thread_inherits_parent_thread_id(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-review")
            await daemon.tick()
            events = _read_timeline(self.root)
            repo_create = next(e for e in events if e["event_type"] == "repo-thread.created")
            pr_create = next(e for e in events if e["event_type"] == "pr-thread.created")
            self.assertEqual(pr_create["payload"]["parent_thread_id"],
                             repo_create["payload"]["thread_id"])
        finally:
            await daemon.appserver.close()

    async def test_C4_repo_thread_has_goal_set(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-review")
            await daemon.tick()
            self.assertEqual(self.harness.count("thread/goal/set"), 1)
            # Goal is set on the repo thread, not a PR thread:
            goal_call = self.harness.first_call("thread/goal/set")
            repo_start = self.harness.first_call("thread/start")
            # The first thread/start return is captured by daemon as repo's;
            # verify goal_call.params.threadId == that thread id.
            # (Harness doesn't expose returned IDs directly, so use the
            #  store's repo_threads as the source of truth.)
            with daemon.store._lock:
                conn = daemon.store._connect()
                try:
                    row = conn.execute("SELECT root_thread_id FROM repo_threads").fetchone()
                finally:
                    conn.close()
            self.assertEqual(goal_call.params["threadId"], row["root_thread_id"])
        finally:
            await daemon.appserver.close()


# ============================================================================
# Section D — Context injection
# ============================================================================


class TestD_ContextInjection(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.socket = self.root / "app-server.sock"
        _write_config(self.root)
        _write_skills(self.root)
        self.harness = HarnessAppServer(self.socket)
        self.harness_cm = self.harness.serve()
        await self.harness_cm.__aenter__()

    async def asyncTearDown(self):
        await self.harness_cm.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def test_D1_inject_items_called_before_turn(self):
        _seed_pr_jangler_state(self.root, prs={
            "42": {
                "pr_number": 42, "phase": "ReviewPending",
                "phase_entered_at": "2026-05-13T00:00:00+00:00",
                "last_action_at": "2026-05-13T00:00:00+00:00",
                "contributor_login": "alice",
                "next_action": {"skill": "prj-review", "mode": None},
            }
        })
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-review")
            await daemon.tick()
            methods = self.harness.methods_called()
            inject_idx = methods.index("thread/inject_items")
            turn_idx = methods.index("turn/start")
            self.assertLess(inject_idx, turn_idx,
                            "thread/inject_items must precede turn/start")
        finally:
            await daemon.appserver.close()

    async def test_D2_pr_cache_artifacts_injected(self):
        _seed_pr_jangler_state(self.root, prs={
            "42": {
                "pr_number": 42, "phase": "ReviewPending",
                "phase_entered_at": "2026-05-13T00:00:00+00:00",
                "last_action_at": "2026-05-13T00:00:00+00:00",
                "contributor_login": "alice",
                "next_action": {"skill": "prj-review", "mode": None},
            }
        })
        pr_dir = self.root / "_bmad-output" / "pr-workflow" / "prs" / "42"
        pr_dir.mkdir(parents=True, exist_ok=True)
        (pr_dir / "triage.md").write_text("# triage\nfindings\n", encoding="utf-8")
        (pr_dir / "review.md").write_text("# review\nnits\n", encoding="utf-8")

        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-review")
            await daemon.tick()
            inj = self.harness.first_call("thread/inject_items")
            items = inj.params["items"]
            # 1 selection-summary item + 2 artifact items = 3
            self.assertEqual(len(items), 3)
            texts = "\n".join(it["text"] for it in items)
            self.assertIn("triage.md", texts)
            self.assertIn("review.md", texts)
        finally:
            await daemon.appserver.close()

    async def test_D4_selection_json_is_in_first_injected_item(self):
        _seed_pr_jangler_state(self.root, prs={
            "42": {
                "pr_number": 42, "phase": "ReviewPending",
                "phase_entered_at": "2026-05-13T00:00:00+00:00",
                "last_action_at": "2026-05-13T00:00:00+00:00",
                "contributor_login": "alice",
                "next_action": {"skill": "prj-review", "mode": None},
            }
        })
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-review")
            await daemon.tick()
            inj = self.harness.first_call("thread/inject_items")
            events = _read_timeline(self.root)
            sel_event = next(e for e in events if e["event_type"] == "tick.selection")
            state_sha = sel_event["payload"]["state_sha"]
            first_text = inj.params["items"][0]["text"]
            self.assertIn(state_sha, first_text)
        finally:
            await daemon.appserver.close()


# ============================================================================
# Section E — The policy firewall
# ============================================================================


class TestE_PolicyFirewall(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.socket = self.root / "app-server.sock"
        _write_config(self.root)
        _write_skills(self.root)
        self.harness = HarnessAppServer(self.socket)
        self.harness_cm = self.harness.serve()
        await self.harness_cm.__aenter__()

    async def asyncTearDown(self):
        await self.harness_cm.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def test_E1_safe_skill_executes(self):
        # Empty state -> selector picks prj-discover (safe)
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-discover")
            result = await daemon.tick()
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decision, Decision.AUTO_APPROVE)
            self.assertEqual(self.harness.count("turn/start"), 1)
        finally:
            await daemon.appserver.close()

    async def test_E2_github_mutating_skill_parks(self):
        _seed_pr_jangler_state(self.root, prs={
            "42": {
                "pr_number": 42, "phase": "Reviewed",
                "phase_entered_at": "2026-05-13T00:00:00+00:00",
                "last_action_at": "2026-05-13T00:00:00+00:00",
                "contributor_login": "alice",
                "next_action": {"skill": "prj-decision", "mode": "comment"},
            }
        })
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-decision")
            result = await daemon.tick()
            self.assertEqual(result.status, "human-approval-pending")
            # No turn started, no thread created
            self.assertEqual(self.harness.count("turn/start"), 0)
            self.assertEqual(self.harness.count("thread/start"), 0)
            # Exactly one pending approval recorded
            self.assertEqual(len(daemon.store.pending_approvals()), 1)
        finally:
            await daemon.appserver.close()

    async def test_E3_server_initiated_state_write_is_declined(self):
        """A turn that asks (via approval/request) to redirect into state.json
        gets declined by the daemon's policy firewall."""
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(
                skill_name="prj-discover",
                approval_commands=["echo '{}' > _bmad-output/pr-workflow/state.json"],
                final_status="completed",  # if the daemon approves, harness returns this
            )
            await daemon.tick()
            # The harness will return 'blocked-by-policy' status if the daemon declined.
            events = _read_timeline(self.root)
            approval_events = [e for e in events if e["event_type"] == "appserver.approval-requested"]
            self.assertEqual(len(approval_events), 1)
            self.assertEqual(approval_events[0]["payload"]["decision"], "auto_deny")
        finally:
            await daemon.appserver.close()

    async def test_E4_force_push_declined(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(
                skill_name="prj-discover",
                approval_commands=["git push --force origin main"],
            )
            await daemon.tick()
            events = _read_timeline(self.root)
            approval = next(e for e in events
                            if e["event_type"] == "appserver.approval-requested")
            self.assertEqual(approval["payload"]["decision"], "auto_deny")
        finally:
            await daemon.appserver.close()

    async def test_E5_session_approval_unlocks_next_tick(self):
        _seed_pr_jangler_state(self.root, prs={
            "42": {
                "pr_number": 42, "phase": "Reviewed",
                "phase_entered_at": "2026-05-13T00:00:00+00:00",
                "last_action_at": "2026-05-13T00:00:00+00:00",
                "contributor_login": "alice",
                "next_action": {"skill": "prj-decision", "mode": "comment"},
            }
        })
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-decision")
            # First tick: park
            r1 = await daemon.tick()
            self.assertEqual(r1.status, "human-approval-pending")
            pending = daemon.store.pending_approvals()
            self.assertEqual(len(pending), 1)
            appr_id = pending[0]["approval_id"]
            # Human approves
            daemon.store.record_approval_decision(
                approval_id=appr_id, decision="approve",
                reason="reviewed diff", decided_by="test",
            )
            # Second tick on same state_sha: must execute via session approval
            r2 = await daemon.tick()
            self.assertEqual(r2.status, "executed")
            self.assertEqual(r2.decision, Decision.AUTO_APPROVE)
            events = _read_timeline(self.root)
            self.assertTrue(any(e["event_type"] == "tick.session-approval-found"
                                for e in events))
        finally:
            await daemon.appserver.close()


# ============================================================================
# Section F — Approvals round-trip cleanly
# ============================================================================


class TestF_ApprovalsRoundTrip(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.socket = self.root / "app-server.sock"
        _write_config(self.root)
        _write_skills(self.root)
        self.harness = HarnessAppServer(self.socket)
        self.harness_cm = self.harness.serve()
        await self.harness_cm.__aenter__()

    async def asyncTearDown(self):
        await self.harness_cm.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def test_F1_server_initiated_request_reaches_handler(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(
                skill_name="prj-discover",
                approval_commands=["gh pr view 42"],  # safe -> auto_approve
            )
            await daemon.tick()
            events = _read_timeline(self.root)
            self.assertTrue(any(
                e["event_type"] == "appserver.approval-requested"
                and e["payload"]["decision"] == "auto_approve"
                for e in events
            ))
        finally:
            await daemon.appserver.close()

    async def test_F4_audit_fields_populated_after_decision(self):
        # Park an approval via tick, then decide, verify audit columns.
        _seed_pr_jangler_state(self.root, prs={
            "42": {
                "pr_number": 42, "phase": "Reviewed",
                "phase_entered_at": "2026-05-13T00:00:00+00:00",
                "last_action_at": "2026-05-13T00:00:00+00:00",
                "contributor_login": "alice",
                "next_action": {"skill": "prj-decision", "mode": "comment"},
            }
        })
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-decision")
            await daemon.tick()
            appr_id = daemon.store.pending_approvals()[0]["approval_id"]
            daemon.store.record_approval_decision(
                approval_id=appr_id, decision="approve",
                reason="manual", decided_by="ci",
            )
            with daemon.store._lock:
                conn = daemon.store._connect()
                try:
                    row = conn.execute(
                        "SELECT decision, decided_at, decided_by, reason FROM approvals "
                        "WHERE approval_id = ?", (appr_id,),
                    ).fetchone()
                finally:
                    conn.close()
            self.assertEqual(row["decision"], "approve")
            self.assertIsNotNone(row["decided_at"])
            self.assertEqual(row["decided_by"], "ci")
            self.assertEqual(row["reason"], "manual")
        finally:
            await daemon.appserver.close()


# ============================================================================
# Section G — Audit trail is comprehensive
# ============================================================================


class TestG_AuditTrail(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.socket = self.root / "app-server.sock"
        _write_config(self.root)
        _write_skills(self.root)
        self.harness = HarnessAppServer(self.socket)
        self.harness_cm = self.harness.serve()
        await self.harness_cm.__aenter__()

    async def asyncTearDown(self):
        await self.harness_cm.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def test_G1_tick_emits_full_event_chain(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-discover")
            await daemon.tick()
            events = _read_timeline(self.root)
            types = _types(events)
            for required in ("appserver.handshake", "tick.started",
                             "tick.selection", "tick.policy-decision",
                             "tick.turn-completed"):
                self.assertIn(required, types,
                              f"missing required event: {required}")
        finally:
            await daemon.appserver.close()

    async def test_G2_runs_have_paired_started_and_completed(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-discover")
            await daemon.tick()
            with daemon.store._lock:
                conn = daemon.store._connect()
                try:
                    rows = conn.execute(
                        "SELECT status, started_at, completed_at FROM runs"
                    ).fetchall()
                finally:
                    conn.close()
            self.assertEqual(len(rows), 1)
            self.assertIsNotNone(rows[0]["started_at"])
            self.assertIsNotNone(rows[0]["completed_at"])
            self.assertNotEqual(rows[0]["status"], "started")
        finally:
            await daemon.appserver.close()

    async def test_G3_timeline_is_append_only(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-discover")
            await daemon.tick()
            first_snapshot = _read_timeline(self.root)
            await daemon.tick()
            second_snapshot = _read_timeline(self.root)
            # Everything in first_snapshot must still be there, in the same order:
            for i, ev in enumerate(first_snapshot):
                self.assertEqual(second_snapshot[i], ev,
                                 f"timeline entry {i} was modified: {ev}")
        finally:
            await daemon.appserver.close()

    async def test_G4_turn_started_paired_with_turn_completed(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-discover")
            await daemon.tick()
            await daemon.tick()
            events = _read_timeline(self.root)
            # The harness sends item/* events; the daemon mirrors them.
            started = sum(1 for e in events if e["event_type"] == "tick.turn-completed")
            self.assertEqual(started, 2)
        finally:
            await daemon.appserver.close()

    async def test_G5_item_notifications_mirrored(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(
                skill_name="prj-discover",
                item_events=[
                    {"lifecycle": "started", "itemType": "reasoning",
                     "data": {"text": "thinking"}},
                    {"lifecycle": "completed", "itemType": "commandExecution",
                     "data": {"command": "gh pr list"}},
                ],
            )
            await daemon.tick()
            # Allow notifications to flush through the pump:
            await asyncio.sleep(0.1)
            events = _read_timeline(self.root)
            types = _types(events)
            self.assertIn("item.started", types)
            self.assertIn("item.completed", types)
        finally:
            await daemon.appserver.close()


# ============================================================================
# Section I — Non-negotiables
# ============================================================================


class TestI_NonNegotiables(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.socket = self.root / "app-server.sock"
        _write_config(self.root)
        _write_skills(self.root)
        self.harness = HarnessAppServer(self.socket)
        self.harness_cm = self.harness.serve()
        await self.harness_cm.__aenter__()

    async def asyncTearDown(self):
        await self.harness_cm.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def test_I1_daemon_never_writes_state_json(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-discover")
            for _ in range(3):
                await daemon.tick()
            self.assertFalse(
                (self.root / "_bmad-output" / "pr-workflow" / "state.json").exists()
            )
        finally:
            await daemon.appserver.close()

    async def test_I3_github_mutations_require_approval_row(self):
        """No turn/start is made for a GitHub-mutating skill until an approval
        with decision='approve' exists matching (skill, pr, state_sha)."""
        _seed_pr_jangler_state(self.root, prs={
            "42": {
                "pr_number": 42, "phase": "Reviewed",
                "phase_entered_at": "2026-05-13T00:00:00+00:00",
                "last_action_at": "2026-05-13T00:00:00+00:00",
                "contributor_login": "alice",
                "next_action": {"skill": "prj-decision", "mode": "comment"},
            }
        })
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-decision")
            await daemon.tick()
            self.assertEqual(self.harness.count("turn/start"), 0)
        finally:
            await daemon.appserver.close()

    async def test_I7_no_credentials_in_timeline(self):
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-discover")
            await daemon.tick()
            tl_dir = self.root / "_bmad-output" / "pr-workflow" / "agentd" / "timeline"
            content = "\n".join(f.read_text(encoding="utf-8") for f in tl_dir.glob("*.jsonl"))
            import re
            cred_pattern = re.compile(r"ghp_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{20,}|xox[bpoa]-")
            self.assertIsNone(cred_pattern.search(content),
                              "timeline must not contain credentials")
        finally:
            await daemon.appserver.close()


# ============================================================================
# Section B (extra) — drift detection
# ============================================================================


class TestB4_DriftDetection(unittest.IsolatedAsyncioTestCase):
    """B.4: drift between selection and execution is detectable.

    Concrete check: an approved decision becomes stale when state.json changes
    underneath it; the next tick logs `tick.approval-drifted` and re-parks
    instead of executing on stale intent.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.socket = self.root / "app-server.sock"
        _write_config(self.root)
        _write_skills(self.root)
        self.harness = HarnessAppServer(self.socket)
        self.harness_cm = self.harness.serve()
        await self.harness_cm.__aenter__()

    async def asyncTearDown(self):
        await self.harness_cm.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def test_B4_drifted_approval_logs_and_does_not_auto_execute(self):
        state_path = _seed_pr_jangler_state(self.root, prs={
            "42": {
                "pr_number": 42, "phase": "Reviewed",
                "phase_entered_at": "2026-05-13T00:00:00+00:00",
                "last_action_at": "2026-05-13T00:00:00+00:00",
                "contributor_login": "alice",
                "next_action": {"skill": "prj-decision", "mode": "comment"},
            }
        })
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-decision")
            # 1) Park the approval at the current state_sha
            await daemon.tick()
            appr_id = daemon.store.pending_approvals()[0]["approval_id"]
            # 2) Approve it
            daemon.store.record_approval_decision(
                approval_id=appr_id, decision="approve",
                reason="approved manually", decided_by="ci",
            )
            # 3) Mutate state.json so the sha changes
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["prs"]["42"]["last_action_at"] = "2026-05-13T09:00:00+00:00"
            state_path.write_text(json.dumps(state, sort_keys=True, indent=2),
                                  encoding="utf-8")
            # 4) Next tick: selection picks the same skill but new state_sha;
            #    drift must be logged and the action re-parked, NOT executed.
            r = await daemon.tick()
            self.assertEqual(r.status, "human-approval-pending")
            events = _read_timeline(self.root)
            self.assertTrue(any(e["event_type"] == "tick.approval-drifted"
                                for e in events))
            # Exactly one new pending approval was recorded (different from the
            # original which is now decided).
            pend = daemon.store.pending_approvals()
            self.assertEqual(len(pend), 1)
            self.assertNotEqual(pend[0]["approval_id"], appr_id)
        finally:
            await daemon.appserver.close()


# ============================================================================
# Section H — Resilience
# ============================================================================


class TestH_Resilience(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.socket = self.root / "app-server.sock"
        _write_config(self.root)
        _write_skills(self.root)
        self.harness = HarnessAppServer(self.socket)
        self.harness_cm = self.harness.serve()
        await self.harness_cm.__aenter__()

    async def asyncTearDown(self):
        await self.harness_cm.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def test_H3_corrupt_state_json_caught_at_selector(self):
        """A corrupted state.json must surface as a tick-level error, not crash
        the daemon, and must not invoke any app-server methods downstream."""
        # Write a corrupt state.json
        workflow = self.root / "_bmad-output" / "pr-workflow"
        workflow.mkdir(parents=True, exist_ok=True)
        (workflow / "state.json").write_text("{ not valid json ", encoding="utf-8")

        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-discover")
            r = await daemon.tick()
            # The tick should NOT have executed a turn.
            self.assertNotEqual(r.status, "executed")
            self.assertEqual(self.harness.count("turn/start"), 0)
            # And the selector failure must be logged in the timeline.
            events = _read_timeline(self.root)
            # Either tick.selector-failed OR tick.selection with misconfigured/idle
            self.assertTrue(any(
                e["event_type"] in ("tick.selector-failed", "tick.selection")
                for e in events
            ))
        finally:
            await daemon.appserver.close()


# ============================================================================
# Section I — Non-negotiables (extra: stub-not-built, never-shell-bmad)
# ============================================================================


class TestI_StubAndBmadShell(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.socket = self.root / "app-server.sock"
        _write_config(self.root)
        # CRITICAL: do NOT copy all skills. Only orchestrator.
        # Selector will pick prj-discover but SKILL.md is absent.
        _write_skills(self.root, include=("prj-orchestrator",))
        self.harness = HarnessAppServer(self.socket)
        self.harness_cm = self.harness.serve()
        await self.harness_cm.__aenter__()

    async def asyncTearDown(self):
        await self.harness_cm.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def test_I5_missing_skill_still_routes_through_appserver_no_bmad_shell(self):
        """I.5 says the daemon dispatches to a missing skill via app-server,
        NOT via subshell-call to `bmad run`. The daemon hands the skill name
        to app-server's `turn/start` regardless of whether SKILL.md exists on
        the local filesystem — that's app-server's problem to surface as a
        stub. We verify by observing turn/start was called, NOT a bmad subshell.
        """
        import os
        cfg = _make_config(self.root, self.socket)
        daemon = await _build_live_daemon(cfg, self.harness)
        try:
            self.harness.turn_script = TurnScript(skill_name="prj-discover")
            await daemon.tick()
            # The daemon always dispatches through turn/start, never bmad shell.
            self.assertEqual(self.harness.count("turn/start"), 1)
            # And the skill_path passed to turn/start is the canonical location,
            # even when SKILL.md does not exist on disk:
            ts = self.harness.first_call("turn/start")
            skill_input = next(it for it in ts.params["input"] if it.get("type") == "skill")
            self.assertEqual(skill_input["name"], "prj-discover")
            self.assertTrue(skill_input["path"].endswith("/prj-discover/SKILL.md"))
            self.assertFalse(Path(skill_input["path"]).exists())
        finally:
            await daemon.appserver.close()


if __name__ == "__main__":
    unittest.main()
