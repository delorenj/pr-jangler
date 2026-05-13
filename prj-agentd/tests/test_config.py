import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prj_agentd.config import (  # noqa: E402
    DEFAULT_APPROVAL_MODE,
    DEFAULT_TICK_SECONDS,
    load_config,
)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "_bmad").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_with_minimal_config(self):
        (self.root / "_bmad" / "config.toml").write_text(
            '[modules.prj]\nprj_repo = "owner/repo"\n', encoding="utf-8"
        )
        cfg = load_config(project_root=self.root)
        self.assertEqual(cfg.prj_repo, "owner/repo")
        self.assertEqual(cfg.tick_seconds, DEFAULT_TICK_SECONDS)
        self.assertEqual(cfg.approval_mode, DEFAULT_APPROVAL_MODE)
        self.assertFalse(cfg.offline)
        self.assertEqual(cfg.skill_allowlist, frozenset())

    def test_agentd_overrides_take_precedence(self):
        (self.root / "_bmad" / "config.toml").write_text(
            """[modules.prj]
prj_repo = "owner/repo"

[modules.prj.agentd]
tick_seconds = 5
approval_mode = "always"
skill_allowlist = ["prj-review", "prj-triage"]
""",
            encoding="utf-8",
        )
        cfg = load_config(project_root=self.root)
        self.assertEqual(cfg.tick_seconds, 5)
        self.assertEqual(cfg.approval_mode, "always")
        self.assertEqual(cfg.skill_allowlist, frozenset({"prj-review", "prj-triage"}))

    def test_offline_flag_propagates(self):
        (self.root / "_bmad" / "config.toml").write_text(
            '[modules.prj]\nprj_repo = "owner/repo"\n', encoding="utf-8"
        )
        cfg = load_config(project_root=self.root, offline=True)
        self.assertTrue(cfg.offline)

    def test_paths_resolve_from_project_root(self):
        (self.root / "_bmad" / "config.toml").write_text(
            '[modules.prj]\nprj_repo = "owner/repo"\n', encoding="utf-8"
        )
        cfg = load_config(project_root=self.root)
        self.assertEqual(cfg.orchestrator_run_py.parts[-3:],
                         ("prj-orchestrator", "scripts", "run.py"))
        self.assertTrue(str(cfg.store_path).endswith("agentd.sqlite"))


if __name__ == "__main__":
    unittest.main()
