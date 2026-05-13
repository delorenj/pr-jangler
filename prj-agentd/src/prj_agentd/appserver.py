"""High-level adapter for `codex app-server` over JSON-RPC.

Two concrete implementations:
    AppServerClient   - real connection to a live `codex app-server` socket.
    OfflineAppServer  - in-process fake; deterministic, no-op'ing, used for
                        smoke tests, dev loops, and Milestone-A cache-only runs.

Both implement `AppServerInterface`. The daemon depends only on the interface.
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .rpc import Connection, JsonRpcError, UnixSocketTransport, UnixWebSocketTransport


# Mappings from our human-friendly config vocabulary to codex's wire enums.
# See codex/codex-rs/protocol/src/protocol.rs (AskForApproval, kebab-case
# variants) and codex/codex-rs/protocol/src/config_types.rs (SandboxMode).
# Unknown values pass through unchanged so the user can already write codex's
# wire names directly in their config if they prefer.
_APPROVAL_POLICY_ALIASES: dict[str, str] = {
    "unlessTrusted": "untrusted",
    "untrusted": "untrusted",
    "always": "on-request",
    "onRequest": "on-request",
    "on-request": "on-request",
    "neverInteractive": "never",
    "never": "never",
    "onFailure": "on-failure",
    "on-failure": "on-failure",
    "granular": "granular",
}

_SANDBOX_MODE_ALIASES: dict[str, str] = {
    "workspaceWrite": "workspace-write",
    "workspace-write": "workspace-write",
    "readOnly": "read-only",
    "read-only": "read-only",
    "dangerFullAccess": "danger-full-access",
    "danger-full-access": "danger-full-access",
}


def _to_codex_approval_policy(value: str) -> str:
    return _APPROVAL_POLICY_ALIASES.get(value, value)


def _to_codex_sandbox_mode(value: str) -> str:
    return _SANDBOX_MODE_ALIASES.get(value, value)


@dataclass(frozen=True)
class ThreadHandle:
    thread_id: str
    cwd: Path
    approval_policy: str
    sandbox: str
    parent_thread_id: str | None = None


@dataclass(frozen=True)
class TurnResult:
    thread_id: str
    turn_id: str
    status: str               # "completed", "failed", "interrupted"
    items: list[dict[str, Any]]
    raw: dict[str, Any]       # last event from app-server (for diagnostics)


class AppServerInterface(ABC):
    """The surface area prj-agentd's daemon depends on."""

    @abstractmethod
    async def initialize(self) -> dict[str, Any]: ...

    @abstractmethod
    async def list_skills(self) -> list[dict[str, Any]]: ...

    def set_request_handler(self, handler: Callable[[str, Any], Awaitable[Any]]) -> None:
        """Install a handler for server-initiated requests (e.g. approval/request).

        No-op for implementations that never issue server-initiated requests.
        """

    def set_notification_handler(self, handler: Callable[[str, Any], Awaitable[None]]) -> None:
        """Install a handler for server-initiated notifications (e.g. item/*).

        No-op for implementations that never emit notifications.
        """

    @property
    def is_alive(self) -> bool:
        """Whether the underlying transport is still connected."""
        return True

    @abstractmethod
    async def start_thread(
        self,
        *,
        cwd: Path,
        approval_policy: str,
        sandbox: str,
        service_name: str,
        seed_text: str,
        parent_thread_id: str | None = None,
    ) -> ThreadHandle: ...

    @abstractmethod
    async def set_goal(self, thread_id: str, *, objective: str, token_budget: int = 200_000) -> None: ...

    @abstractmethod
    async def inject_items(self, thread_id: str, items: list[dict[str, Any]]) -> None: ...

    @abstractmethod
    async def run_turn(
        self,
        *,
        thread_id: str,
        cwd: Path,
        approval_policy: str,
        skill_name: str,
        skill_path: Path,
        user_text: str,
    ) -> TurnResult: ...

    @abstractmethod
    async def close(self) -> None: ...


# ---- Real implementation ----


class AppServerClient(AppServerInterface):
    """Connects to a live `codex app-server` over Unix socket and wraps JSON-RPC.

    Method names follow the destination doc's schema:
        initialize, skills/list, thread/start, thread/goal/set,
        thread/inject_items, turn/start, command/exec.
    """

    def __init__(
        self,
        connection: Connection,
        *,
        on_server_event: Callable[[str, Any], Awaitable[None]] | None = None,
    ):
        self._conn = connection
        self._on_event = on_server_event
        self._alive = True
        self._server_info: dict[str, Any] = {}

    @classmethod
    async def connect(
        cls,
        socket_path: Path,
        *,
        on_server_request: Callable[[str, Any], Awaitable[Any]] | None = None,
        on_notification: Callable[[str, Any], Awaitable[None]] | None = None,
        transport_kind: str = "websocket",
    ) -> "AppServerClient":
        """Connect to a live `codex app-server` over its Unix socket.

        `transport_kind`:
          - "websocket" (default): WebSocket over Unix socket. This is what
            real `codex app-server` instances speak (they accept
            tokio_tungstenite::accept_async on every connection).
          - "lsp": LSP-style Content-Length framing. Kept for the test
            harness; real codex servers reject this and silently close.
        """
        if transport_kind == "websocket":
            transport = await UnixWebSocketTransport.connect(socket_path)
        elif transport_kind == "lsp":
            transport = await UnixSocketTransport.connect(socket_path)
        else:
            raise ValueError(f"unknown transport_kind: {transport_kind!r}")
        conn = Connection(transport)
        if on_server_request is not None:
            conn.on_server_request(on_server_request)
        if on_notification is not None:
            conn.on_notification(on_notification)
        await conn.start()
        return cls(conn)

    def set_request_handler(self, handler: Callable[[str, Any], Awaitable[Any]]) -> None:
        self._conn.on_server_request(handler)

    def set_notification_handler(self, handler: Callable[[str, Any], Awaitable[None]]) -> None:
        self._conn.on_notification(handler)

    @property
    def is_alive(self) -> bool:
        return self._alive

    @property
    def server_info(self) -> dict[str, Any]:
        """The serverInfo dict returned by the initialize handshake."""
        return self._server_info

    async def initialize(self) -> dict[str, Any]:
        """Send the `initialize` request per app-server protocol v1.

        Schema (camelCase): `{clientInfo: {name, version}, capabilities: {...}}`.
        See codex/codex-rs/app-server-protocol/src/protocol/v1.rs `InitializeParams`.
        """
        from . import __version__
        params = {
            "clientInfo": {
                "name": "prj-agentd",
                "version": __version__,
            },
            "capabilities": {
                "experimentalApi": True,
            },
        }
        result = await self._conn.request("initialize", params)
        if isinstance(result, dict):
            # The response struct has `serverInfo` at the top level when
            # the server supplies it; codex may return it under different
            # casing or merge it into the root — keep the whole result
            # accessible to the timeline event regardless.
            self._server_info = (
                dict(result.get("serverInfo", {}))
                if isinstance(result.get("serverInfo"), dict)
                else dict(result)
            )
        return result

    async def list_skills(self) -> list[dict[str, Any]]:
        result = await self._conn.request("skills/list", {})
        return list(result.get("skills", [])) if isinstance(result, dict) else []

    async def start_thread(
        self,
        *,
        cwd: Path,
        approval_policy: str,
        sandbox: str,
        service_name: str,
        seed_text: str,
        parent_thread_id: str | None = None,
    ) -> ThreadHandle:
        """Send `thread/start` per codex app-server v2 ThreadStartParams.

        See codex/codex-rs/app-server-protocol/src/protocol/v2/thread.rs.
        Notes:
          - `approvalPolicy` must be one of `untrusted | on-failure |
            on-request | granular | never` (kebab-case).
          - `sandbox` must be one of `read-only | workspace-write |
            danger-full-access` (kebab-case).
          - `parentThreadId` is NOT a v2 field; codex models thread parentage
            via forking (`thread/fork`), not at start time. We carry it in
            our local store for our own audit, but do not send it.
          - There is no `input` field on `ThreadStartParams`. Seed prompts
            arrive on the first `turn/start` instead, so we drop seed_text
            on the wire.
          - Response: `{thread: {id, sessionId, ...}, model, ...}`.
        """
        params: dict[str, Any] = {
            "cwd": str(cwd),
            "approvalPolicy": _to_codex_approval_policy(approval_policy),
            "sandbox": _to_codex_sandbox_mode(sandbox),
            "serviceName": service_name,
        }
        result = await self._conn.request("thread/start", params)
        thread_block = result.get("thread") if isinstance(result, dict) else None
        thread_id = (
            thread_block.get("id") if isinstance(thread_block, dict)
            else (result.get("threadId") if isinstance(result, dict) else None)
        )
        if not thread_id:
            raise JsonRpcError(-32000, f"thread/start returned no thread id: {result!r}")
        return ThreadHandle(
            thread_id=thread_id,
            cwd=cwd,
            approval_policy=approval_policy,
            sandbox=sandbox,
            parent_thread_id=parent_thread_id,
        )

    async def set_goal(self, thread_id: str, *, objective: str, token_budget: int = 200_000) -> None:
        await self._conn.request(
            "thread/goal/set",
            {"threadId": thread_id, "objective": objective, "tokenBudget": token_budget},
        )

    async def inject_items(self, thread_id: str, items: list[dict[str, Any]]) -> None:
        await self._conn.request("thread/inject_items", {"threadId": thread_id, "items": items})

    async def run_turn(
        self,
        *,
        thread_id: str,
        cwd: Path,
        approval_policy: str,
        skill_name: str,
        skill_path: Path,
        user_text: str,
    ) -> TurnResult:
        """Send `turn/start` per codex app-server v2 TurnStartParams.

        See codex/codex-rs/app-server-protocol/src/protocol/v2/turn.rs.
        Required: `threadId`, `input` (Vec<UserInput>).
        UserInput is a tagged enum (camelCase): Text {text}, Skill {name, path}, ...
        """
        params = {
            "threadId": thread_id,
            "cwd": str(cwd),
            "approvalPolicy": _to_codex_approval_policy(approval_policy),
            "input": [
                {"type": "text", "text": user_text},
                {"type": "skill", "name": skill_name, "path": str(skill_path)},
            ],
        }
        try:
            result = await self._conn.request("turn/start", params, timeout=None)
        except JsonRpcError as e:
            return TurnResult(
                thread_id=thread_id,
                turn_id="",
                status="failed",
                items=[],
                raw={"error": {"code": e.code, "message": str(e)}},
            )
        return TurnResult(
            thread_id=thread_id,
            turn_id=result.get("turnId", ""),
            status=result.get("status", "completed"),
            items=list(result.get("items", [])),
            raw=result,
        )

    async def close(self) -> None:
        self._alive = False
        await self._conn.aclose()


# ---- Offline (in-process) implementation ----


class OfflineAppServer(AppServerInterface):
    """Deterministic no-op AppServerInterface.

    Used by:
      - tests (no socket required)
      - the daemon's `--offline` flag (smoke-test the full loop without
        a running app-server)
      - Milestone-A cache-only-review runs that intentionally skip GitHub
        and skip the LLM turn

    All "remote" operations are fully resolved in-process, return synthetic
    thread/turn IDs, and emit synthetic completion events to the optional
    observer.
    """

    def __init__(
        self,
        *,
        on_synthetic_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ):
        self._on_event = on_synthetic_event
        self._lock = asyncio.Lock()
        self._next_id = 1
        self._alive = True
        self._request_handler: Callable[[str, Any], Awaitable[Any]] | None = None
        self._notification_handler: Callable[[str, Any], Awaitable[None]] | None = None

    def set_request_handler(self, handler: Callable[[str, Any], Awaitable[Any]]) -> None:
        self._request_handler = handler

    def set_notification_handler(self, handler: Callable[[str, Any], Awaitable[None]]) -> None:
        self._notification_handler = handler

    @property
    def is_alive(self) -> bool:
        return self._alive

    def _mint(self, prefix: str) -> str:
        async def _async() -> None: ...  # placate type-checker about asyncio import
        with_id = f"{prefix}_{uuid.uuid4().hex[:10]}"
        return with_id

    async def initialize(self) -> dict[str, Any]:
        return {"serverInfo": {"name": "OfflineAppServer", "version": "0.1.0"}, "experimentalApi": True}

    async def list_skills(self) -> list[dict[str, Any]]:
        # The daemon's selector resolves skills by path off-line anyway; this is
        # informational only. Return an empty list so callers don't gate on
        # remote discovery.
        return []

    async def start_thread(
        self,
        *,
        cwd: Path,
        approval_policy: str,
        sandbox: str,
        service_name: str,
        seed_text: str,
        parent_thread_id: str | None = None,
    ) -> ThreadHandle:
        thread_id = self._mint("thr")
        if self._on_event:
            await self._on_event(
                "thread.started",
                {
                    "thread_id": thread_id,
                    "cwd": str(cwd),
                    "approval_policy": approval_policy,
                    "sandbox": sandbox,
                    "service_name": service_name,
                    "parent_thread_id": parent_thread_id,
                },
            )
        return ThreadHandle(
            thread_id=thread_id,
            cwd=cwd,
            approval_policy=approval_policy,
            sandbox=sandbox,
            parent_thread_id=parent_thread_id,
        )

    async def set_goal(self, thread_id: str, *, objective: str, token_budget: int = 200_000) -> None:
        if self._on_event:
            await self._on_event(
                "thread.goal.set",
                {"thread_id": thread_id, "objective": objective, "token_budget": token_budget},
            )

    async def inject_items(self, thread_id: str, items: list[dict[str, Any]]) -> None:
        if self._on_event:
            await self._on_event(
                "thread.items.injected",
                {"thread_id": thread_id, "item_count": len(items)},
            )

    async def run_turn(
        self,
        *,
        thread_id: str,
        cwd: Path,
        approval_policy: str,
        skill_name: str,
        skill_path: Path,
        user_text: str,
    ) -> TurnResult:
        turn_id = self._mint("turn")
        if self._on_event:
            await self._on_event(
                "turn.started",
                {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "skill_name": skill_name,
                    "skill_path": str(skill_path),
                    "approval_policy": approval_policy,
                },
            )
            await self._on_event(
                "turn.completed",
                {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "skill_name": skill_name,
                    "status": "completed-offline",
                },
            )
        return TurnResult(
            thread_id=thread_id,
            turn_id=turn_id,
            status="completed-offline",
            items=[
                {"type": "text", "text": f"[offline] would invoke {skill_name} for: {user_text}"},
            ],
            raw={"mode": "offline"},
        )

    async def close(self) -> None:
        self._alive = False
        return None
