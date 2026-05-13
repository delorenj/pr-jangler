"""HarnessAppServer — a faithful stand-in for `codex app-server`.

Speaks the same wire protocol as the real app-server (Content-Length-framed
JSON-RPC 2.0 over a Unix domain socket). Implements the subset of methods
prj-agentd calls during a tick, and can be scripted to:

    - emit `item/*` lifecycle notifications during a turn,
    - issue server-initiated `approval/request`s in the middle of a turn,
    - simulate transient socket failure (disconnect mid-turn).

The harness is NOT a model. `turn/start` synthesizes a deterministic completion
+ a small set of item events. The point is to verify the WIRE and the
CLIENT BEHAVIOR — not LLM semantics.

Usage:

    async with HarnessAppServer(socket_path).serve():
        # daemon connects to socket_path; harness records every method call
        ...
        harness.record  # list[CapturedCall]
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator


HandlerFn = Callable[["HarnessAppServer", "Connection", dict[str, Any]], Awaitable[Any]]


@dataclass
class CapturedCall:
    """One JSON-RPC method call observed by the harness."""

    method: str
    params: dict[str, Any] | list[Any] | None
    ts: float
    direction: str = "incoming"  # "incoming" (client->server) or "outgoing"
    response: Any = None


@dataclass
class TurnScript:
    """Defines what `turn/start` will do.

    `item_events` are emitted in order as `item/*` notifications BEFORE the
    final response. `approval_commands` are server-initiated approval requests
    (kind: command) issued mid-turn; the harness waits for each decision
    before continuing. `final_status` is the eventual response status.
    """

    skill_name: str = ""
    item_events: list[dict[str, Any]] = field(default_factory=list)
    approval_commands: list[str] = field(default_factory=list)
    final_status: str = "completed"


class Connection:
    """One client connection's send/recv pair."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self._send_lock = asyncio.Lock()
        self._next_id = 100_000  # server-initiated IDs avoid colliding with client IDs

    async def send(self, msg: dict[str, Any]) -> None:
        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        async with self._send_lock:
            self.writer.write(header + body)
            await self.writer.drain()

    async def recv(self) -> dict[str, Any] | None:
        content_length: int | None = None
        while True:
            line = await self.reader.readline()
            if not line:
                return None
            stripped = line.strip()
            if not stripped:
                break
            if stripped.lower().startswith(b"content-length:"):
                content_length = int(stripped.split(b":", 1)[1].strip())
        if content_length is None:
            return None
        body = await self.reader.readexactly(content_length)
        return json.loads(body.decode("utf-8"))

    def next_server_id(self) -> int:
        self._next_id += 1
        return self._next_id


class HarnessAppServer:
    """Async Unix-socket JSON-RPC server matching the app-server contract."""

    SERVER_INFO = {"name": "HarnessAppServer", "version": "0.1.0"}

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path
        self.record: list[CapturedCall] = []
        self.turn_script: TurnScript | None = None
        self._server: asyncio.AbstractServer | None = None
        # decision futures keyed by server-issued approval id
        self._pending_approvals: dict[int, asyncio.Future[Any]] = {}
        # connections that have completed handshake
        self.active_connections: list[Connection] = []
        # disconnect simulation: if set, drop the next connection after N messages
        self.drop_after_messages: int | None = None
        self._messages_seen = 0

    @contextlib.asynccontextmanager
    async def serve(self) -> "Iterator[HarnessAppServer]":
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(self.socket_path))
        try:
            yield self
        finally:
            self._server.close()
            await self._server.wait_closed()
            with contextlib.suppress(FileNotFoundError):
                self.socket_path.unlink()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        conn = Connection(reader, writer)
        self.active_connections.append(conn)
        try:
            while True:
                msg = await conn.recv()
                if msg is None:
                    break
                self._messages_seen += 1
                if (
                    self.drop_after_messages is not None
                    and self._messages_seen >= self.drop_after_messages
                ):
                    writer.close()
                    return

                if "method" in msg and "id" in msg:
                    # Client request — respond.
                    self.record.append(CapturedCall(
                        method=msg["method"], params=msg.get("params"),
                        ts=asyncio.get_event_loop().time(), direction="incoming",
                    ))
                    await self._dispatch_request(conn, msg)
                elif "method" in msg:
                    # Client notification — record, no response.
                    self.record.append(CapturedCall(
                        method=msg["method"], params=msg.get("params"),
                        ts=asyncio.get_event_loop().time(), direction="incoming",
                    ))
                elif "id" in msg and ("result" in msg or "error" in msg):
                    # Response to a server-initiated request (e.g. approval).
                    fut = self._pending_approvals.pop(msg["id"], None)
                    if fut is not None and not fut.done():
                        if "error" in msg and msg["error"] is not None:
                            fut.set_exception(RuntimeError(msg["error"]))
                        else:
                            fut.set_result(msg.get("result"))
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            if conn in self.active_connections:
                self.active_connections.remove(conn)

    async def _dispatch_request(self, conn: Connection, msg: dict[str, Any]) -> None:
        method = msg["method"]
        params = msg.get("params") or {}
        msg_id = msg["id"]
        try:
            result = await self._handle_method(conn, method, params)
            await conn.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        except Exception as exc:  # pragma: no cover - defensive
            await conn.send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32000, "message": repr(exc)},
            })

    async def _handle_method(
        self,
        conn: Connection,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        if method == "initialize":
            return {"serverInfo": self.SERVER_INFO, "experimentalApi": True}
        if method == "skills/list":
            return {"skills": [{"name": s} for s in self._discover_skills()]}
        if method == "thread/start":
            tid = f"thr_{uuid.uuid4().hex[:10]}"
            return {"threadId": tid}
        if method == "thread/goal/set":
            return {"ok": True}
        if method == "thread/inject_items":
            return {"ok": True, "count": len(params.get("items", []))}
        if method == "turn/start":
            return await self._handle_turn_start(conn, params)
        raise RuntimeError(f"unknown method: {method!r}")

    async def _handle_turn_start(self, conn: Connection, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = params["threadId"]
        script = self.turn_script or TurnScript()
        turn_id = f"turn_{uuid.uuid4().hex[:10]}"

        # Emit any scripted item events as notifications.
        for ev in script.item_events:
            await conn.send({
                "jsonrpc": "2.0",
                "method": f"item/{ev.get('lifecycle', 'updated')}",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "itemType": ev.get("itemType", "reasoning"),
                    "data": ev.get("data", {}),
                },
            })

        # Issue any scripted approval requests; wait for each decision before
        # continuing. If declined, mark as failed.
        for cmd in script.approval_commands:
            approval_id = conn.next_server_id()
            fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
            self._pending_approvals[approval_id] = fut
            await conn.send({
                "jsonrpc": "2.0",
                "id": approval_id,
                "method": "approval/request",
                "params": {
                    "kind": "command",
                    "command": cmd,
                    "threadId": thread_id,
                    "turnId": turn_id,
                },
            })
            try:
                decision = await asyncio.wait_for(fut, timeout=5.0)
            except asyncio.TimeoutError:
                return {
                    "turnId": turn_id,
                    "status": "failed",
                    "items": [],
                    "error": f"approval timeout for command: {cmd}",
                }
            if isinstance(decision, dict) and decision.get("decision") == "decline":
                return {
                    "turnId": turn_id,
                    "status": "blocked-by-policy",
                    "items": [],
                    "blocked_command": cmd,
                }

        return {
            "turnId": turn_id,
            "status": script.final_status,
            "items": [{"type": "text", "text": f"[harness] {script.skill_name} completed"}],
        }

    def _discover_skills(self) -> list[str]:
        return [
            "prj-orchestrator", "prj-discover", "prj-triage", "prj-verify-claim",
            "prj-review", "prj-detect-overlap", "prj-plan-fix",
            "prj-validate-adversarial", "prj-decision", "prj-implement-fix",
            "prj-report-daily",
        ]

    # ---- harness helpers for assertions ----

    def methods_called(self) -> list[str]:
        return [c.method for c in self.record if c.direction == "incoming"]

    def count(self, method: str) -> int:
        return sum(1 for c in self.record if c.method == method)

    def first_call(self, method: str) -> CapturedCall | None:
        for c in self.record:
            if c.method == method:
                return c
        return None

    def calls(self, method: str) -> list[CapturedCall]:
        return [c for c in self.record if c.method == method]
