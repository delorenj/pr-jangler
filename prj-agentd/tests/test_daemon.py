"""End-to-end daemon tests against OfflineAppServer + a stubbed selector.

These tests do not touch the real `prj-orchestrator` script; they patch
`run_selector` to return canned Selection objects. The orchestrator-level
integration is covered separately by the smoke test.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prj_agentd.appserver import OfflineAppServer  # noqa: E402
from prj_agentd.config import AgentdConfig  # noqa: E402
from prj_agentd.daemon import Daemon  # noqa: E402
from prj_agentd.policy import Decision, PolicyEngine  # noqa: E402
from prj_agentd.selector import Selection  # noqa: E402
from prj_agentd.store import AgentdStore  # noqa: E402
from prj_agentd.timeline import Timeline  # noqa: E402


def _make_config(root: Path) -> AgentdConfig:
    return AgentdConfig(
        project_root=root,
        prj_repo="owner/repo",
        socket_path=Path("/tmp/never-used.sock"),
        tick_seconds=1,
        approval_mode="unlessTrusted",
        sandbox="workspaceWrite",
        offline=True,
        skill_allowlist=frozenset(),
    )


def _make_daemon(root: Path) -> Daemon:
    timeline = Timeline(root / "_bmad-output" / "pr-workflow" / "agentd" / "timeline")
    return Daemon(
        config=_make_config(root),
        appserver=OfflineAppServer(),
        store=AgentdStore(root / "_bmad-output" / "pr-workflow" / "agentd" / "agentd.sqlite"),
        timeline=timeline,
        policy=PolicyEngine(approval_mode="unlessTrusted"),
    )


class TestDaemonTicks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_idle_selection_yields_idle_tick(self):
        daemon = _make_daemon(self.root)
        idle = Selection(status="idle", repo="owner/repo", pr=None, phase=None,
                         skill=None, mode=None, priority=0, reason="quiet",
                         state_sha="sha")
        with patch("prj_agentd.daemon.run_selector", return_value=idle):
            result = await daemon.tick()
        self.assertEqual(result.status, "idle")
        self.assertIsNone(result.decision)

    async def test_misconfigured_selection_yields_misconfigured_tick(self):
        daemon = _make_daemon(self.root)
        bad = Selection(status="misconfigured", repo=None, pr=None, phase=None,
                        skill=None, mode=None, priority=0, reason="no repo",
                        state_sha="no-state")
        with patch("prj_agentd.daemon.run_selector", return_value=bad):
            result = await daemon.tick()
        self.assertEqual(result.status, "misconfigured")

    async def test_safe_skill_executes_and_records_run(self):
        daemon = _make_daemon(self.root)
        sel = Selection(status="action-selected", repo="owner/repo", pr=None,
                        phase=None, skill="prj-discover", mode=None,
                        priority=500, reason="queue empty", state_sha="no-state")
        with patch("prj_agentd.daemon.run_selector", return_value=sel):
            result = await daemon.tick()
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decision, Decision.AUTO_APPROVE)
        self.assertEqual(result.skill, "prj-discover")
        self.assertIsNotNone(result.thread_id)
        # Repo thread cached in the store
        repo_thread = daemon.store.get_repo_thread("owner/repo")
        self.assertIsNotNone(repo_thread)
        # Run completed
        run = daemon.store.get_run(result.run_id)
        self.assertEqual(run.status, "completed-offline")

    async def test_per_pr_action_creates_pr_thread(self):
        daemon = _make_daemon(self.root)
        sel = Selection(status="action-selected", repo="owner/repo", pr=42,
                        phase="ReviewPending", skill="prj-review", mode=None,
                        priority=130, reason="next_action", state_sha="abc")
        with patch("prj_agentd.daemon.run_selector", return_value=sel):
            result = await daemon.tick()
        self.assertEqual(result.status, "executed")
        self.assertIsNotNone(result.pr_thread_id)
        pr = daemon.store.get_pr_thread("owner/repo", 42)
        self.assertEqual(pr.latest_phase, "ReviewPending")
        self.assertEqual(pr.latest_state_sha, "abc")

    async def test_github_mutating_skill_routes_to_human_approval(self):
        daemon = _make_daemon(self.root)
        sel = Selection(status="action-selected", repo="owner/repo", pr=42,
                        phase="Reviewed", skill="prj-decision", mode=None,
                        priority=150, reason="ready to post", state_sha="abc")
        with patch("prj_agentd.daemon.run_selector", return_value=sel):
            result = await daemon.tick()
        self.assertEqual(result.status, "human-approval-pending")
        self.assertEqual(result.decision, Decision.REQUIRE_HUMAN)
        pend = daemon.store.pending_approvals()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["kind"], "skill")
        self.assertEqual(pend[0]["payload"]["skill"], "prj-decision")
        self.assertEqual(pend[0]["payload"]["pr"], 42)

    async def test_skill_not_in_allowlist_is_skipped(self):
        config = AgentdConfig(
            project_root=self.root, prj_repo="owner/repo",
            socket_path=Path("/tmp/x.sock"), tick_seconds=1,
            approval_mode="unlessTrusted", sandbox="workspaceWrite",
            offline=True, skill_allowlist=frozenset({"prj-review"}),
        )
        daemon = Daemon(
            config=config,
            appserver=OfflineAppServer(),
            store=AgentdStore(self.root / "agentd.sqlite"),
            timeline=Timeline(self.root / "tl"),
            policy=PolicyEngine(approval_mode="unlessTrusted"),
        )
        sel = Selection(status="action-selected", repo="owner/repo", pr=None,
                        phase=None, skill="prj-discover", mode=None,
                        priority=500, reason="queue empty", state_sha="x")
        with patch("prj_agentd.daemon.run_selector", return_value=sel):
            result = await daemon.tick()
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.decision, Decision.AUTO_DENY)

    async def test_repeated_ticks_reuse_repo_thread(self):
        daemon = _make_daemon(self.root)
        sel = Selection(status="action-selected", repo="owner/repo", pr=None,
                        phase=None, skill="prj-discover", mode=None,
                        priority=500, reason="queue empty", state_sha="x")
        with patch("prj_agentd.daemon.run_selector", return_value=sel):
            r1 = await daemon.tick()
            r2 = await daemon.tick()
        self.assertEqual(r1.thread_id, r2.thread_id)

    async def test_timeline_records_every_tick(self):
        daemon = _make_daemon(self.root)
        sel = Selection(status="idle", repo="owner/repo", pr=None, phase=None,
                        skill=None, mode=None, priority=0, reason="quiet",
                        state_sha="x")
        with patch("prj_agentd.daemon.run_selector", return_value=sel):
            await daemon.tick()
        files = list(daemon.timeline.root_dir.glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        events = [json.loads(line)["event_type"] for line in lines]
        self.assertIn("tick.started", events)
        self.assertIn("tick.selection", events)


if __name__ == "__main__":
    unittest.main()
