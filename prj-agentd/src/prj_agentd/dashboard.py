"""prj-agentd dashboard: a stdlib-only async HTTP + WebSocket server.

Serves a single-page web UI that streams the daemon's timeline events live
and exposes pending-approval actions. Runs independently of the daemon —
both processes read/write the same `_bmad-output/pr-workflow/agentd/`
artifacts (timeline JSONL, SQLite store), so you can keep one
`prj-agentd run` process driving ticks while the dashboard observes.

Endpoints:
    GET  /                -> index.html (the dashboard UI)
    GET  /api/status      -> { repo, project_root, pending_approvals,
                               approval_count, repo_thread_count,
                               pr_thread_count, runs_today, recent_runs }
    POST /api/approve     -> body { approval_id, decision, reason }
    GET  /ws              -> WebSocket; streams timeline events as JSON
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AgentdConfig
from .store import AgentdStore


# WebSocket constants (RFC 6455)
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_OP_TEXT = 0x1
_WS_OP_BIN = 0x2
_WS_OP_CLOSE = 0x8
_WS_OP_PING = 0x9
_WS_OP_PONG = 0xA


# --------------------------------------------------------------------- broadcast


class EventBroadcaster:
    """Fan-out queue. Each subscriber gets every published event."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any] | None]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any] | None]) -> None:
        self._subs.discard(q)

    async def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow subscriber — drop. The dashboard's reconnect logic
                # will catch up on the next refresh.
                pass


# --------------------------------------------------------------------- tailer


async def tail_timeline(
    timeline_dir: Path,
    broadcaster: EventBroadcaster,
    *,
    replay_last: int = 100,
    poll_interval: float = 0.4,
) -> None:
    """Tail the most recent timeline JSONL and broadcast new events.

    On startup, replay the last `replay_last` events so a freshly-connected
    dashboard has immediate context. After that, poll once every
    `poll_interval` seconds for new lines.
    """
    timeline_dir.mkdir(parents=True, exist_ok=True)
    offsets: dict[Path, int] = {}

    # Initial replay from the most recent file
    files = sorted(timeline_dir.glob("*.jsonl"))
    if files:
        last = files[-1]
        try:
            data = last.read_bytes()
        except OSError:
            data = b""
        offsets[last] = len(data)
        lines = data.decode("utf-8", errors="replace").splitlines()[-replay_last:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev["_replay"] = True
            await broadcaster.publish(ev)

    while True:
        try:
            current = sorted(timeline_dir.glob("*.jsonl"))
            for path in current:
                start = offsets.get(path, 0)
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size <= start:
                    continue
                with path.open("rb") as fh:
                    fh.seek(start)
                    new_bytes = fh.read()
                offsets[path] = start + len(new_bytes)
                for raw in new_bytes.decode("utf-8", errors="replace").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    await broadcaster.publish(ev)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let the tailer kill the server. Sleep and retry.
            pass
        await asyncio.sleep(poll_interval)


# --------------------------------------------------------------------- WS framing


def _encode_ws_frame(opcode: int, payload: bytes) -> bytes:
    """Server-to-client frame: FIN=1, no mask."""
    b1 = 0x80 | (opcode & 0x0F)
    length = len(payload)
    if length < 126:
        return struct.pack("!BB", b1, length) + payload
    if length < (1 << 16):
        return struct.pack("!BBH", b1, 126, length) + payload
    return struct.pack("!BBQ", b1, 127, length) + payload


async def _read_ws_frame(
    reader: asyncio.StreamReader,
) -> tuple[int | None, bytes]:
    """Read one client-to-server frame. Client frames MUST be masked."""
    try:
        header = await reader.readexactly(2)
    except asyncio.IncompleteReadError:
        return None, b""
    b1, b2 = header[0], header[1]
    opcode = b1 & 0x0F
    masked = (b2 >> 7) & 1
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask_key = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length) if length else b""
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


# --------------------------------------------------------------------- HTTP


def _http_response(
    status: str,
    body: bytes,
    *,
    content_type: str = "text/plain; charset=utf-8",
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "Cache-Control": "no-store",
        "Connection": "close",
    }
    if extra_headers:
        headers.update(extra_headers)
    header_block = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    return f"HTTP/1.1 {status}\r\n{header_block}\r\n".encode("latin-1") + body


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")


# --------------------------------------------------------------------- handlers


async def _serve_index(writer: asyncio.StreamWriter, html_bytes: bytes) -> None:
    writer.write(_http_response("200 OK", html_bytes, content_type="text/html; charset=utf-8"))
    await writer.drain()


async def _serve_status(
    writer: asyncio.StreamWriter,
    store: AgentdStore,
    config: AgentdConfig,
) -> None:
    payload = {
        "repo": config.prj_repo,
        "project_root": str(config.project_root),
        "approval_mode": config.approval_mode,
        "pending_approvals": store.pending_approvals(),
        "approval_count": len(store.pending_approvals()),
        "repo_thread_count": store.count_repo_threads(),
        "pr_thread_count": store.count_pr_threads(),
        "runs_today": store.count_runs_since(_today_iso()),
        "recent_runs": store.recent_runs(limit=10),
    }
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    writer.write(_http_response(
        "200 OK", body,
        content_type="application/json; charset=utf-8",
        extra_headers={"Access-Control-Allow-Origin": "*"},
    ))
    await writer.drain()


async def _serve_approve(
    writer: asyncio.StreamWriter,
    body: bytes,
    store: AgentdStore,
) -> None:
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
        approval_id = str(payload["approval_id"])
        decision = str(payload.get("decision", "approve"))
        reason = str(payload.get("reason") or "via dashboard")
    except Exception as exc:
        writer.write(_http_response(
            "400 Bad Request",
            json.dumps({"error": repr(exc)}).encode(),
            content_type="application/json",
        ))
        await writer.drain()
        return
    if decision not in ("approve", "deny"):
        writer.write(_http_response(
            "400 Bad Request",
            json.dumps({"error": f"invalid decision: {decision!r}"}).encode(),
            content_type="application/json",
        ))
        await writer.drain()
        return
    store.record_approval_decision(
        approval_id=approval_id,
        decision=decision,
        reason=reason,
        decided_by="dashboard",
    )
    writer.write(_http_response(
        "200 OK",
        json.dumps({"approval_id": approval_id, "decision": decision}).encode(),
        content_type="application/json",
    ))
    await writer.drain()


async def _serve_ws(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    headers: dict[str, str],
    broadcaster: EventBroadcaster,
) -> None:
    key = headers.get("sec-websocket-key", "")
    if not key:
        writer.write(_http_response("400 Bad Request", b"missing Sec-WebSocket-Key"))
        await writer.drain()
        return
    accept = base64.b64encode(
        hashlib.sha1((key + _WS_MAGIC).encode("ascii")).digest()
    ).decode("ascii")
    handshake = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    writer.write(handshake.encode("ascii"))
    await writer.drain()

    queue = broadcaster.subscribe()
    send_lock = asyncio.Lock()

    async def writer_loop() -> None:
        while True:
            ev = await queue.get()
            if ev is None:
                return
            data = json.dumps(ev).encode("utf-8")
            async with send_lock:
                writer.write(_encode_ws_frame(_WS_OP_TEXT, data))
                try:
                    await writer.drain()
                except (ConnectionError, BrokenPipeError):
                    return

    async def reader_loop() -> None:
        while True:
            opcode, payload = await _read_ws_frame(reader)
            if opcode is None or opcode == _WS_OP_CLOSE:
                return
            if opcode == _WS_OP_PING:
                async with send_lock:
                    writer.write(_encode_ws_frame(_WS_OP_PONG, payload))
                    await writer.drain()

    rtask = asyncio.create_task(reader_loop())
    wtask = asyncio.create_task(writer_loop())
    try:
        done, pending = await asyncio.wait(
            {rtask, wtask}, return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        broadcaster.unsubscribe(queue)


# --------------------------------------------------------------------- entry


def _load_index_html() -> bytes:
    asset = Path(__file__).parent / "dashboard_assets" / "index.html"
    return asset.read_bytes()


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    config: AgentdConfig,
    store: AgentdStore,
    broadcaster: EventBroadcaster,
    html_bytes: bytes,
) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    except asyncio.TimeoutError:
        writer.close()
        return
    if not request_line:
        writer.close()
        return
    try:
        method, target, _ = request_line.decode("latin-1").rstrip("\r\n").split(" ", 2)
    except ValueError:
        writer.write(_http_response("400 Bad Request", b"malformed request line"))
        await writer.drain()
        writer.close()
        return

    headers: dict[str, str] = {}
    while True:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except asyncio.TimeoutError:
            writer.close()
            return
        if not line or line == b"\r\n":
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()

    body = b""
    try:
        content_length = int(headers.get("content-length", "0") or "0")
    except ValueError:
        content_length = 0
    if content_length:
        body = await reader.readexactly(content_length)

    try:
        if target == "/ws" and headers.get("upgrade", "").lower() == "websocket":
            await _serve_ws(reader, writer, headers, broadcaster)
            return
        if target in ("/", "/index.html"):
            await _serve_index(writer, html_bytes)
        elif target == "/api/status" and method == "GET":
            await _serve_status(writer, store, config)
        elif target == "/api/approve" and method == "POST":
            await _serve_approve(writer, body, store)
        else:
            writer.write(_http_response("404 Not Found", b"not found"))
            await writer.drain()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def serve_dashboard(
    config: AgentdConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Run the dashboard server until cancelled."""
    store = AgentdStore(config.store_path)
    broadcaster = EventBroadcaster()
    html_bytes = _load_index_html()
    timeline_dir = config.agentd_state_dir / "timeline"

    async def _client_cb(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await _handle_client(
            r, w,
            config=config, store=store, broadcaster=broadcaster,
            html_bytes=html_bytes,
        )

    server = await asyncio.start_server(_client_cb, host=host, port=port)
    tailer = asyncio.create_task(tail_timeline(timeline_dir, broadcaster))

    print(f"prj-agentd dashboard listening on http://{host}:{port}")
    print(f"  project: {config.project_root}")
    print(f"  timeline: {timeline_dir}")
    print(f"  store: {config.store_path}")
    print()
    print(f"  open http://{host}:{port}/ in a browser. ctrl-c to stop.")

    try:
        async with server:
            await server.serve_forever()
    finally:
        tailer.cancel()
        try:
            await tailer
        except Exception:
            pass
