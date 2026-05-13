"""RPC tests: drive the Connection against an in-process fake server."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prj_agentd.rpc import Connection, InMemoryTransport, JsonRpcError  # noqa: E402


class TestConnection(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        client_t, server_t = InMemoryTransport.pair()
        self.client = Connection(client_t)
        self.server = Connection(server_t)
        await self.client.start()
        await self.server.start()

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.server.aclose()

    async def test_request_response_roundtrip(self):
        async def handler(method: str, params):
            self.assertEqual(method, "echo")
            return {"echoed": params}
        self.server.on_server_request(handler)
        result = await self.client.request("echo", {"hello": "world"})
        self.assertEqual(result, {"echoed": {"hello": "world"}})

    async def test_server_request_handled_by_client(self):
        """Server-initiated requests (e.g. approval prompts) route to the
        client-side handler."""
        async def client_handler(method: str, params):
            if method == "approval/request":
                return {"decision": "approve"}
            raise JsonRpcError(-32601, "unknown")
        self.client.on_server_request(client_handler)
        result = await self.server.request("approval/request", {"cmd": "gh pr view 42"})
        self.assertEqual(result, {"decision": "approve"})

    async def test_notification_handler_invoked(self):
        seen: list[tuple[str, object]] = []

        async def handler(method, params):
            seen.append((method, params))

        self.client.on_notification(handler)
        await self.server.notify("item/added", {"id": 1})
        await asyncio.sleep(0.05)  # let pump deliver
        self.assertEqual(seen, [("item/added", {"id": 1})])

    async def test_handler_error_returns_jsonrpc_error(self):
        async def handler(method, params):
            raise JsonRpcError(-32602, "bad params")
        self.server.on_server_request(handler)
        with self.assertRaises(JsonRpcError) as ctx:
            await self.client.request("explode", {})
        self.assertEqual(ctx.exception.code, -32602)

    async def test_no_handler_returns_method_not_found(self):
        # server has no handler installed
        with self.assertRaises(JsonRpcError) as ctx:
            await self.client.request("nobody", {})
        self.assertEqual(ctx.exception.code, -32601)


if __name__ == "__main__":
    unittest.main()
