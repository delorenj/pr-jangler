"""Daemon-local SQLite store.

Holds RUNTIME metadata only — never business state. PR Jangler's `state.json`
remains the canonical source of truth for PR phase. This store maps:

    repo            -> root_thread_id
    (repo, pr)      -> pr_thread_id, latest_phase, latest_state_sha
    run_id          -> trace items / lifecycle metadata
    approval_id     -> decision, decided_at, decided_by, reason

WAL mode for concurrent readers (e.g. a dashboard) while the daemon writes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS repo_threads (
    repo TEXT PRIMARY KEY,
    root_thread_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pr_threads (
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    pr_thread_id TEXT NOT NULL,
    latest_phase TEXT,
    latest_state_sha TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo, pr_number)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    pr_number INTEGER,
    skill TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    result_json TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    decision TEXT,
    reason TEXT,
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_repo_pr ON runs(repo, pr_number);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_approvals_decision ON approvals(decision);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RepoThread:
    repo: str
    root_thread_id: str


@dataclass(frozen=True)
class PrThread:
    repo: str
    pr_number: int
    pr_thread_id: str
    latest_phase: str | None
    latest_state_sha: str | None


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    repo: str
    pr_number: int | None
    skill: str
    status: str
    started_at: str
    completed_at: str | None
    selection: dict
    result: dict | None


class AgentdStore:
    """Thread-safe SQLite store for prj-agentd runtime metadata."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA)
            finally:
                conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

    # ---- repo threads ----

    def upsert_repo_thread(self, repo: str, thread_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO repo_threads (repo, root_thread_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(repo) DO UPDATE SET
                    root_thread_id=excluded.root_thread_id,
                    updated_at=excluded.updated_at
                """,
                (repo, thread_id, _now(), _now()),
            )

    def forget_repo_thread(self, repo: str) -> None:
        """Drop the cached repo thread for `repo`. Used when codex rejects the
        thread id (e.g. stale offline-mode cache after switching to live)."""
        with self._tx() as conn:
            conn.execute("DELETE FROM repo_threads WHERE repo = ?", (repo,))

    def get_repo_thread(self, repo: str) -> RepoThread | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT repo, root_thread_id FROM repo_threads WHERE repo = ?",
                    (repo,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return RepoThread(repo=row["repo"], root_thread_id=row["root_thread_id"])

    # ---- pr threads ----

    def upsert_pr_thread(
        self,
        repo: str,
        pr_number: int,
        thread_id: str,
        *,
        latest_phase: str | None = None,
        latest_state_sha: str | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO pr_threads
                    (repo, pr_number, pr_thread_id, latest_phase, latest_state_sha,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, pr_number) DO UPDATE SET
                    pr_thread_id=excluded.pr_thread_id,
                    latest_phase=COALESCE(excluded.latest_phase, pr_threads.latest_phase),
                    latest_state_sha=COALESCE(excluded.latest_state_sha, pr_threads.latest_state_sha),
                    updated_at=excluded.updated_at
                """,
                (repo, pr_number, thread_id, latest_phase, latest_state_sha,
                 _now(), _now()),
            )

    def forget_pr_thread(self, repo: str, pr_number: int) -> None:
        """Drop the cached PR thread for `(repo, pr_number)`. Same use case
        as `forget_repo_thread`: stale cache after server mode switch or
        codex thread cleanup."""
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM pr_threads WHERE repo = ? AND pr_number = ?",
                (repo, pr_number),
            )

    def get_pr_thread(self, repo: str, pr_number: int) -> PrThread | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """SELECT repo, pr_number, pr_thread_id, latest_phase, latest_state_sha
                       FROM pr_threads WHERE repo=? AND pr_number=?""",
                    (repo, pr_number),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return PrThread(
            repo=row["repo"],
            pr_number=row["pr_number"],
            pr_thread_id=row["pr_thread_id"],
            latest_phase=row["latest_phase"],
            latest_state_sha=row["latest_state_sha"],
        )

    # ---- runs ----

    def record_run_started(
        self,
        run_id: str,
        repo: str,
        pr_number: int | None,
        skill: str,
        selection: dict,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO runs
                    (run_id, repo, pr_number, skill, started_at, status, selection_json)
                VALUES (?, ?, ?, ?, ?, 'started', ?)
                """,
                (run_id, repo, pr_number, skill, _now(), json.dumps(selection, sort_keys=True)),
            )

    def record_run_completed(
        self,
        run_id: str,
        status: str,
        result: dict | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status=?, completed_at=?, result_json=?
                WHERE run_id=?
                """,
                (status, _now(), json.dumps(result, sort_keys=True) if result else None, run_id),
            )

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return RunRecord(
            run_id=row["run_id"],
            repo=row["repo"],
            pr_number=row["pr_number"],
            skill=row["skill"],
            status=row["status"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            selection=json.loads(row["selection_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
        )

    # ---- approvals ----

    def record_approval_request(
        self,
        approval_id: str,
        run_id: str,
        kind: str,
        payload: dict,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO approvals
                    (approval_id, run_id, kind, payload_json, requested_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (approval_id, run_id, kind, json.dumps(payload, sort_keys=True), _now()),
            )

    def record_approval_decision(
        self,
        approval_id: str,
        decision: str,
        reason: str,
        decided_by: str,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE approvals
                SET decision=?, reason=?, decided_at=?, decided_by=?
                WHERE approval_id=?
                """,
                (decision, reason, _now(), decided_by, approval_id),
            )

    def count_repo_threads(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                (n,) = conn.execute("SELECT COUNT(*) FROM repo_threads").fetchone()
            finally:
                conn.close()
        return int(n)

    def count_pr_threads(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                (n,) = conn.execute("SELECT COUNT(*) FROM pr_threads").fetchone()
            finally:
                conn.close()
        return int(n)

    def count_runs_since(self, iso_ts: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                (n,) = conn.execute(
                    "SELECT COUNT(*) FROM runs WHERE started_at >= ?", (iso_ts,)
                ).fetchone()
            finally:
                conn.close()
        return int(n)

    def recent_runs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT run_id, repo, pr_number, skill, status, started_at, completed_at
                       FROM runs ORDER BY started_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
        return [
            {
                "run_id": r["run_id"],
                "repo": r["repo"],
                "pr_number": r["pr_number"],
                "skill": r["skill"],
                "status": r["status"],
                "started_at": r["started_at"],
                "completed_at": r["completed_at"],
            }
            for r in rows
        ]

    def pending_approvals(self) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT approval_id, run_id, kind, payload_json, requested_at "
                    "FROM approvals WHERE decision IS NULL ORDER BY requested_at"
                ).fetchall()
            finally:
                conn.close()
        return [
            {
                "approval_id": r["approval_id"],
                "run_id": r["run_id"],
                "kind": r["kind"],
                "payload": json.loads(r["payload_json"]),
                "requested_at": r["requested_at"],
            }
            for r in rows
        ]

    def find_session_approval(
        self,
        *,
        skill: str,
        pr: int | None,
        state_sha: str,
    ) -> dict | None:
        """Find an approved skill-approval matching this selection.

        Match criteria (all required):
          - kind = 'skill'
          - decision = 'approve'
          - payload.skill == skill
          - payload.pr == pr (None compares equal to None)
          - payload.state_sha == state_sha (state_sha drift -> no match)

        Returns the most-recently-decided match, or None.
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT approval_id, payload_json, decided_at, decided_by, reason
                       FROM approvals
                       WHERE kind = 'skill' AND decision = 'approve'
                       ORDER BY decided_at DESC"""
                ).fetchall()
            finally:
                conn.close()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if (
                payload.get("skill") == skill
                and payload.get("pr") == pr
                and payload.get("state_sha") == state_sha
            ):
                return {
                    "approval_id": row["approval_id"],
                    "decided_at": row["decided_at"],
                    "decided_by": row["decided_by"],
                    "reason": row["reason"],
                    "payload": payload,
                }
        return None

    def find_drifted_approval(
        self,
        *,
        skill: str,
        pr: int | None,
        current_state_sha: str,
    ) -> dict | None:
        """Find an approved match for (skill, pr) where state_sha has DRIFTED.

        Used to surface 'you approved this, but state changed underneath' so
        the daemon can re-park rather than silently executing on stale intent.
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT approval_id, payload_json, decided_at
                       FROM approvals
                       WHERE kind = 'skill' AND decision = 'approve'
                       ORDER BY decided_at DESC"""
                ).fetchall()
            finally:
                conn.close()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if (
                payload.get("skill") == skill
                and payload.get("pr") == pr
                and payload.get("state_sha") != current_state_sha
                and payload.get("state_sha") is not None
            ):
                return {
                    "approval_id": row["approval_id"],
                    "decided_at": row["decided_at"],
                    "old_state_sha": payload.get("state_sha"),
                    "new_state_sha": current_state_sha,
                }
        return None
