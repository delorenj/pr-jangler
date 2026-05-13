import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prj_agentd.policy import ApprovalRequest, Decision, PolicyEngine  # noqa: E402


class TestSkillPolicy(unittest.TestCase):
    def setUp(self):
        self.engine = PolicyEngine(approval_mode="unlessTrusted")

    def test_safe_skill_auto_approved(self):
        appr = ApprovalRequest.for_skill("run_x", "prj-discover", pr=None)
        result = self.engine.evaluate(appr)
        self.assertEqual(result.decision, Decision.AUTO_APPROVE)

    def test_github_mutating_skill_requires_human(self):
        appr = ApprovalRequest.for_skill("run_x", "prj-decision", pr=42)
        result = self.engine.evaluate(appr)
        self.assertEqual(result.decision, Decision.REQUIRE_HUMAN)

    def test_unknown_skill_requires_human(self):
        appr = ApprovalRequest.for_skill("run_x", "prj-mystery", pr=None)
        result = self.engine.evaluate(appr)
        self.assertEqual(result.decision, Decision.REQUIRE_HUMAN)

    def test_always_mode_forces_human_for_everything(self):
        engine = PolicyEngine(approval_mode="always")
        appr = ApprovalRequest.for_skill("run_x", "prj-discover", pr=None)
        self.assertEqual(engine.evaluate(appr).decision, Decision.REQUIRE_HUMAN)


class TestCommandPolicy(unittest.TestCase):
    def setUp(self):
        self.engine = PolicyEngine(approval_mode="unlessTrusted")

    def test_gh_pr_view_auto_approved(self):
        appr = ApprovalRequest.for_command("r", "gh pr view 42")
        self.assertEqual(self.engine.evaluate(appr).decision, Decision.AUTO_APPROVE)

    def test_select_only_auto_approved(self):
        appr = ApprovalRequest.for_command(
            "r", "python3 skills/prj-orchestrator/scripts/run.py --select-only"
        )
        self.assertEqual(self.engine.evaluate(appr).decision, Decision.AUTO_APPROVE)

    def test_gh_pr_comment_requires_human(self):
        appr = ApprovalRequest.for_command("r", "gh pr comment 42 --body 'hi'")
        self.assertEqual(self.engine.evaluate(appr).decision, Decision.REQUIRE_HUMAN)

    def test_state_redirect_is_denied(self):
        appr = ApprovalRequest.for_command("r", "echo '{}' > state.json")
        self.assertEqual(self.engine.evaluate(appr).decision, Decision.AUTO_DENY)

    def test_rm_bmad_output_is_denied(self):
        appr = ApprovalRequest.for_command("r", "rm -rf /tmp/_bmad-output")
        self.assertEqual(self.engine.evaluate(appr).decision, Decision.AUTO_DENY)

    def test_force_push_is_denied(self):
        appr = ApprovalRequest.for_command("r", "git push --force origin main")
        self.assertEqual(self.engine.evaluate(appr).decision, Decision.AUTO_DENY)

    def test_unlisted_command_requires_human(self):
        appr = ApprovalRequest.for_command("r", "curl https://example.com")
        self.assertEqual(self.engine.evaluate(appr).decision, Decision.REQUIRE_HUMAN)

    def test_never_interactive_denies_unlisted(self):
        engine = PolicyEngine(approval_mode="neverInteractive")
        appr = ApprovalRequest.for_command("r", "curl https://example.com")
        self.assertEqual(engine.evaluate(appr).decision, Decision.AUTO_DENY)


class TestSessionApprovals(unittest.TestCase):
    def test_session_approves_github_mutations(self):
        engine = PolicyEngine(approval_mode="unlessTrusted").with_session_approvals(["github_mutation"])
        appr = ApprovalRequest.for_command("r", "gh pr comment 42 --body 'hi'")
        self.assertEqual(engine.evaluate(appr).decision, Decision.AUTO_APPROVE)


class TestStateKind(unittest.TestCase):
    def test_direct_state_write_always_denied(self):
        engine = PolicyEngine(approval_mode="neverInteractive")
        from prj_agentd.policy import ApprovalRequest as AR
        appr = AR(approval_id="x", kind="state", payload={"target": "state.json"})
        self.assertEqual(engine.evaluate(appr).decision, Decision.AUTO_DENY)


if __name__ == "__main__":
    unittest.main()
