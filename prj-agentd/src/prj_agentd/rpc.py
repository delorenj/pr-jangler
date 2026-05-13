"""Minimal async JSON-RPC 2.0 client over a Unix socket.

Framing: LSP-style `Content-Length: N\\r\\n\\r\\n{json}`.  This matches the
local-app-server transport convention; cf. WHERE_WE_WANT_TO_BE.md.

The client is bidirectional:
    - request(method, params)        -> awaitable result
    - notify(method, params)         -> fire-and-forget
    - on_server_request(handler)     -> server-initiated requests (e.g. approvals)
    - on_notification(handler)       -> server-initiated notifications (events)

For tests and offline mode, `InMemoryTransport` lets you wire a fake server
into the same Connection object — no socket required.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable


JsonRpcId = int | str
JsonRpcParams = dict[str, Any] | list[Any] | None


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.data = data


# ---- Transports ----


class Transport(ABC):
    """A bidirectional message transport. Concrete implementations frame and ship JSON dicts."""

    @abstractmethod
    async def send(self, message: dict[str, Any]) -> None: ...

    @abstractmethod
    async def recv(self) -> dict[str, Any] | None:
        """Return the next message, or None when the transport closes."""

    @abstractmethod
    async def close(self) -> None: ...


class UnixSocketTransport(Transport):
    """Production transport: framed JSON over a Unix domain socket."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._send_lock = asyncio.Lock()

    @classmethod
    async def connect(cls, socket_path: Path) -> "UnixSocketTransport":
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        return cls(reader, writer)

    async def send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        async with self._send_lock:
            self._writer.write(header + body)
            await self._writer.drain()

    async def recv(self) -> dict[str, Any] | None:
        # Read headers
        content_length = None
        while True:
            line = await self._reader.readline()
            if not line:
                return None
            stripped = line.strip()
            if not stripped:
                break  # blank line ends headers
            if stripped.lower().startswith(b"content-length:"):
                content_length = int(stripped.split(b":", 1)[1].strip())
        if content_length is None:
            raise JsonRpcError(-32700, "missing Content-Length header")
        body = await self._reader.readexactly(content_length)
        return json.loads(body.decode("utf-8"))

    async def close(self) -> None:
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass


# ---- WebSocket constants (RFC 6455) ----

_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_OP_CONT = 0x0
_WS_OP_TEXT = 0x1
_WS_OP_BIN = 0x2
_WS_OP_CLOSE = 0x8
_WS_OP_PING = 0x9
_WS_OP_PONG = 0xA


class UnixWebSocketTransport(Transport):
    """Production transport: WebSocket (RFC 6455) over a Unix domain socket.

    Codex's `app-server` accepts incoming Unix-socket connections with
    `tokio_tungstenite::accept_async`, so the wire protocol is a normal
    WebSocket handshake (HTTP/1.1 Upgrade) followed by WebSocket frames.

    Client→server frames MUST be masked per RFC 6455 §5.3. Server→client
    frames are unmasked. We always send single-frame text messages (FIN=1).
    We handle PING from the server by replying with PONG.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._send_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def connect(
        cls,
        socket_path: Path,
        *,
        host: str = "localhost",
        resource: str = "/",
    ) -> "UnixWebSocketTransport":
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        await cls._perform_handshake(reader, writer, host=host, resource=resource)
        return cls(reader, writer)

    @staticmethod
    async def _perform_handshake(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        host: str,
        resource: str,
    ) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        expected_accept = base64.b64encode(
            hashlib.sha1((key + _WS_MAGIC).encode("ascii")).digest()
        ).decode("ascii")
        request = (
            f"GET {resource} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        ).encode("ascii")
        writer.write(request)
        await writer.drain()

        status_line = await reader.readline()
        if not status_line:
            raise JsonRpcError(-32000, "WebSocket handshake failed: server closed connection")
        if not status_line.startswith(b"HTTP/1.1 101"):
            # Drain remaining headers/body for the error message
            tail = b""
            while True:
                line = await reader.readline()
                if not line or line == b"\r\n":
                    break
                tail += line
            raise JsonRpcError(
                -32000,
                f"WebSocket handshake failed: {status_line.strip().decode('latin-1')} "
                f"headers={tail.decode('latin-1')[:200]!r}",
            )

        accept_value: str | None = None
        while True:
            line = await reader.readline()
            if not line:
                raise JsonRpcError(-32000, "WebSocket handshake truncated headers")
            if line == b"\r\n":
                break
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"sec-websocket-accept":
                accept_value = value.strip().decode("ascii")
        if accept_value != expected_accept:
            raise JsonRpcError(
                -32000,
                f"WebSocket handshake Sec-WebSocket-Accept mismatch: "
                f"got {accept_value!r}, expected {expected_accept!r}",
            )

    async def send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        frame = self._encode_frame(_WS_OP_TEXT, body, mask=True)
        async with self._send_lock:
            self._writer.write(frame)
            await self._writer.drain()

    async def recv(self) -> dict[str, Any] | None:
        while True:
            try:
                opcode, payload = await self._read_frame()
            except (asyncio.IncompleteReadError, ConnectionError):
                return None
            if opcode is None:
                return None
            if opcode == _WS_OP_TEXT:
                return json.loads(payload.decode("utf-8"))
            if opcode == _WS_OP_BIN:
                # Codex emits text JSON; defensively decode as UTF-8.
                return json.loads(payload.decode("utf-8"))
            if opcode == _WS_OP_PING:
                pong = self._encode_frame(_WS_OP_PONG, payload, mask=True)
                async with self._send_lock:
                    self._writer.write(pong)
                    await self._writer.drain()
                continue
            if opcode == _WS_OP_PONG:
                continue
            if opcode == _WS_OP_CLOSE:
                return None
            # Unknown opcode — close defensively.
            return None

    async def _read_frame(self) -> tuple[int | None, bytes]:
        header = await self._reader.readexactly(2)
        b1, b2 = header[0], header[1]
        # We don't support fragmentation: every text/binary frame has FIN=1.
        fin = (b1 >> 7) & 1
        opcode = b1 & 0x0F
        masked = (b2 >> 7) & 1
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", await self._reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", await self._reader.readexactly(8))[0]
        mask_key = b""
        if masked:
            mask_key = await self._reader.readexactly(4)
        payload = await self._reader.readexactly(length) if length else b""
        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        if not fin and opcode in (_WS_OP_TEXT, _WS_OP_BIN):
            # Server sent a fragmented frame; codex doesn't, but be defensive.
            raise JsonRpcError(-32000, "fragmented WebSocket frames not supported")
        return opcode, payload

    @staticmethod
    def _encode_frame(opcode: int, payload: bytes, *, mask: bool) -> bytes:
        # FIN=1, RSV=0, opcode
        b1 = 0x80 | (opcode & 0x0F)
        length = len(payload)
        mask_bit = 0x80 if mask else 0x00
        if length < 126:
            header = struct.pack("!BB", b1, mask_bit | length)
        elif length < (1 << 16):
            header = struct.pack("!BBH", b1, mask_bit | 126, length)
        else:
            header = struct.pack("!BBQ", b1, mask_bit | 127, length)
        if mask:
            mask_key = os.urandom(4)
            masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            return header + mask_key + masked
        return header + payload

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Send a CLOSE frame (best-effort).
        try:
            close_frame = self._encode_frame(_WS_OP_CLOSE, b"", mask=True)
            async with self._send_lock:
                self._writer.write(close_frame)
                await self._writer.drain()
        except Exception:
            pass
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass


class InMemoryTransport(Transport):
    """Test/offline transport: two asyncio.Queues for inbound/outbound messages.

    Pair two instances back-to-back with `InMemoryTransport.pair()` to wire a
    fake server inside the same process.
    """

    def __init__(
        self,
        inbox: "asyncio.Queue[dict[str, Any] | None]",
        outbox: "asyncio.Queue[dict[str, Any] | None]",
    ):
        self._inbox = inbox
        self._outbox = outbox
        self._closed = False

    @classmethod
    def pair(cls) -> tuple["InMemoryTransport", "InMemoryTransport"]:
        """Return (client_transport, server_transport) wired back-to-back."""
        a: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        b: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        return cls(inbox=a, outbox=b), cls(inbox=b, outbox=a)

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("transport closed")
        await self._outbox.put(message)

    async def recv(self) -> dict[str, Any] | None:
        msg = await self._inbox.get()
        return msg

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Signal both directions so each side's pump can exit cleanly:
        # the peer reads None from their inbox (= our outbox), AND our own
        # pump reads None from our inbox. Without the second put, aclose()
        # on the first-closer side hangs awaiting the local pump.
        await self._outbox.put(None)
        await self._inbox.put(None)


# ---- Connection (full duplex JSON-RPC over a Transport) ----


ServerRequestHandler = Callable[[str, JsonRpcParams], Awaitable[Any]]
NotificationHandler = Callable[[str, JsonRpcParams], Awaitable[None]]


@dataclass
class _PendingCall:
    future: "asyncio.Future[Any]"


class Connection:
    """Full-duplex JSON-RPC client.

    `request` returns a coroutine that resolves to the result (or raises
    JsonRpcError). `notify` is fire-and-forget. Server-initiated requests are
    dispatched to `on_server_request`. Notifications go to `on_notification`.

    The pump task (`run_forever`) MUST be started before any traffic.
    """

    def __init__(self, transport: Transport):
        self._transport = transport
        self._next_id = 1
        self._pending: dict[JsonRpcId, _PendingCall] = {}
        self._server_request_handler: ServerRequestHandler | None = None
        self._notification_handler: NotificationHandler | None = None
        self._pump_task: asyncio.Task | None = None
        self._closed = asyncio.Event()

    def on_server_request(self, handler: ServerRequestHandler) -> None:
        self._server_request_handler = handler

    def on_notification(self, handler: NotificationHandler) -> None:
        self._notification_handler = handler

    async def start(self) -> None:
        if self._pump_task is None:
            self._pump_task = asyncio.create_task(self._pump(), name="rpc-pump")

    async def aclose(self) -> None:
        await self._transport.close()
        self._closed.set()
        if self._pump_task is not None:
            try:
                await self._pump_task
            except Exception:
                pass

    async def request(self, method: str, params: JsonRpcParams = None, *, timeout: float | None = 60.0) -> Any:
        msg_id = self._next_id
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[msg_id] = _PendingCall(future=fut)
        await self._transport.send(msg)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(msg_id, None)

    async def notify(self, method: str, params: JsonRpcParams = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._transport.send(msg)

    async def respond(self, msg_id: JsonRpcId, result: Any = None, error: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        await self._transport.send(msg)

    async def _pump(self) -> None:
        while True:
            msg = await self._transport.recv()
            if msg is None:
                # transport closed; fail all pending
                for pending in self._pending.values():
                    if not pending.future.done():
                        pending.future.set_exception(
                            JsonRpcError(-32000, "transport closed before reply")
                        )
                self._pending.clear()
                return

            # Response to one of our requests
            if "id" in msg and ("result" in msg or "error" in msg):
                msg_id = msg["id"]
                pending = self._pending.pop(msg_id, None)
                if pending is None:
                    continue
                if "error" in msg and msg["error"] is not None:
                    err = msg["error"]
                    pending.future.set_exception(
                        JsonRpcError(err.get("code", -32000), err.get("message", "?"), err.get("data"))
                    )
                else:
                    pending.future.set_result(msg.get("result"))
                continue

            method = msg.get("method")
            params = msg.get("params")
            if method is None:
                continue

            if "id" in msg:
                # Server-initiated request — needs a response.
                handler = self._server_request_handler
                if handler is None:
                    await self.respond(
                        msg["id"],
                        error={"code": -32601, "message": f"no handler for {method!r}"},
                    )
                else:
                    try:
                        result = await handler(method, params)
                        await self.respond(msg["id"], result=result)
                    except JsonRpcError as e:
                        await self.respond(
                            msg["id"], error={"code": e.code, "message": str(e), "data": e.data}
                        )
                    except Exception as e:  # pragma: no cover - defensive
                        await self.respond(
                            msg["id"], error={"code": -32000, "message": f"handler raised: {e!r}"}
                        )
            else:
                # Notification — fire-and-forget.
                handler_n = self._notification_handler
                if handler_n is not None:
                    try:
                        await handler_n(method, params)
                    except Exception:
                        pass  # notifications never throw upward
