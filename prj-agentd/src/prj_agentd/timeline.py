"""Timeline mirror.

Every interesting daemon event becomes a JSONL line in
`_bmad-output/pr-workflow/agentd/{YYYY-MM-DD}.jsonl`. A dashboard or `tail -f`
gives a live audit feed. PR Jangler's own per-action run-log is the canonical
state-machine audit; this timeline is the canonical RUNTIME audit.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class TimelineEvent:
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "event_type": self.event_type,
            "run_id": self.run_id,
            "payload": self.payload,
        }


class Timeline:
    """Append-only JSONL timeline with optional stdout tee.

    Thread-safe. Rotates by UTC date — one file per day.
    """

    def __init__(self, root_dir: Path, *, verbose: bool = False):
        self.root_dir = root_dir
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._verbose = verbose

    def _path_for(self, ts: str) -> Path:
        # ts is ISO-8601; the first 10 chars are YYYY-MM-DD
        day = ts[:10]
        return self.root_dir / f"{day}.jsonl"

    def emit(self, event: TimelineEvent) -> None:
        record = event.to_dict()
        line = json.dumps(record, sort_keys=True)
        with self._lock:
            path = self._path_for(event.ts)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if self._verbose:
                print(line, file=sys.stderr)

    def event(self, event_type: str, *, run_id: str | None = None, **payload: Any) -> TimelineEvent:
        """Convenience: build and emit in one call. Returns the event for chaining."""
        ev = TimelineEvent(event_type=event_type, run_id=run_id, payload=payload)
        self.emit(ev)
        return ev
