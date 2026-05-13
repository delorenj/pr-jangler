import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prj_agentd.timeline import Timeline, TimelineEvent  # noqa: E402


class TestTimeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tl = Timeline(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_emit_writes_jsonl(self):
        self.tl.event("tick.started", run_id="run_1", project_root="/x")
        files = list(self.root.glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        line = files[0].read_text(encoding="utf-8").strip()
        record = json.loads(line)
        self.assertEqual(record["event_type"], "tick.started")
        self.assertEqual(record["run_id"], "run_1")
        self.assertEqual(record["payload"]["project_root"], "/x")

    def test_multiple_events_append(self):
        for i in range(3):
            self.tl.event("step", run_id=f"r{i}")
        files = list(self.root.glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 3)

    def test_event_dataclass_has_iso_ts(self):
        ev = TimelineEvent(event_type="x")
        self.assertTrue("T" in ev.ts)
        self.assertTrue(ev.ts.endswith("+00:00") or "Z" in ev.ts)


if __name__ == "__main__":
    unittest.main()
