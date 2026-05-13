"""OfflineAppServer is the in-process AppServerInterface implementation.

These tests pin its deterministic surface so the daemon can rely on it for
offline smoke tests.
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prj_agentd.appserver import OfflineAppServer  # noqa: E402


class TestOfflineAppServer(unittest.IsolatedAsyncioTestCase):
    async def test_start_thread_returns_synthetic_id(self):
        srv = OfflineAppServer()
        handle = await srv.start_thread(
            cwd=Path("/tmp"), approval_policy="unlessTrusted",
            sandbox="workspaceWrite", service_name="prj-agentd",
            seed_text="hello",
        )
        self.assertTrue(handle.thread_id.startswith("thr_"))
        self.assertEqual(handle.parent_thread_id, None)
        await srv.close()

    async def test_run_turn_emits_synthetic_completion(self):
        events: list[tuple[str, dict]] = []

        async def on_event(name, payload):
            events.append((name, payload))

        srv = OfflineAppServer(on_synthetic_event=on_event)
        thr = await srv.start_thread(
            cwd=Path("/tmp"), approval_policy="unlessTrusted",
            sandbox="workspaceWrite", service_name="prj-agentd",
            seed_text="x",
        )
        turn = await srv.run_turn(
            thread_id=thr.thread_id,
            cwd=Path("/tmp"),
            approval_policy="unlessTrusted",
            skill_name="prj-discover",
            skill_path=Path("/tmp/SKILL.md"),
            user_text="$prj-discover",
        )
        self.assertEqual(turn.status, "completed-offline")
        self.assertTrue(turn.turn_id.startswith("turn_"))
        event_types = [e[0] for e in events]
        self.assertIn("turn.started", event_types)
        self.assertIn("turn.completed", event_types)
        await srv.close()

    async def test_initialize_advertises_offline(self):
        srv = OfflineAppServer()
        result = await srv.initialize()
        self.assertEqual(result["serverInfo"]["name"], "OfflineAppServer")
        await srv.close()


if __name__ == "__main__":
    unittest.main()
