import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prj_agentd.store import AgentdStore  # noqa: E402


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "agentd.sqlite"
        self.store = AgentdStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_thread_roundtrip(self):
        self.store.upsert_repo_thread("owner/repo", "thr_abc")
        got = self.store.get_repo_thread("owner/repo")
        self.assertIsNotNone(got)
        self.assertEqual(got.root_thread_id, "thr_abc")

    def test_repo_thread_upsert_overwrites_thread_id(self):
        self.store.upsert_repo_thread("owner/repo", "thr_one")
        self.store.upsert_repo_thread("owner/repo", "thr_two")
        got = self.store.get_repo_thread("owner/repo")
        self.assertEqual(got.root_thread_id, "thr_two")

    def test_pr_thread_roundtrip(self):
        self.store.upsert_pr_thread(
            "owner/repo", 42, "thr_pr42",
            latest_phase="ReviewPending", latest_state_sha="sha1",
        )
        got = self.store.get_pr_thread("owner/repo", 42)
        self.assertEqual(got.pr_thread_id, "thr_pr42")
        self.assertEqual(got.latest_phase, "ReviewPending")

    def test_pr_thread_partial_update_preserves_phase(self):
        self.store.upsert_pr_thread("owner/repo", 42, "thr_pr42",
                                    latest_phase="ReviewPending", latest_state_sha="sha1")
        self.store.upsert_pr_thread("owner/repo", 42, "thr_pr42",
                                    latest_phase=None, latest_state_sha="sha2")
        got = self.store.get_pr_thread("owner/repo", 42)
        self.assertEqual(got.latest_phase, "ReviewPending")
        self.assertEqual(got.latest_state_sha, "sha2")

    def test_run_lifecycle(self):
        sel = {"status": "action-selected", "skill": "prj-discover"}
        self.store.record_run_started("run_1", "owner/repo", None, "prj-discover", sel)
        rec = self.store.get_run("run_1")
        self.assertEqual(rec.status, "started")
        self.assertEqual(rec.selection["skill"], "prj-discover")

        self.store.record_run_completed("run_1", "completed", {"turn_id": "turn_a"})
        rec2 = self.store.get_run("run_1")
        self.assertEqual(rec2.status, "completed")
        self.assertEqual(rec2.result["turn_id"], "turn_a")

    def test_approval_lifecycle(self):
        self.store.record_approval_request("appr_1", "run_1", "skill",
                                           {"skill": "prj-decision", "pr": 42})
        pend = self.store.pending_approvals()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["approval_id"], "appr_1")

        self.store.record_approval_decision("appr_1", "approve", "ok", "human")
        pend2 = self.store.pending_approvals()
        self.assertEqual(pend2, [])


if __name__ == "__main__":
    unittest.main()
