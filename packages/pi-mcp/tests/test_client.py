"""``McpClient`` 的行为规格。

逐条对齐上游 ``packages/mcp/test/client.test.ts``（v1.3.0，339 行）。对应用例编号写在
每个测试的 docstring 里；本端补充的用例另行标注。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from pi_mcp import (
    LATEST_PROTOCOL_VERSION,
    McpAbortError,
    McpClient,
    McpClientOptions,
    McpError,
    McpRequestOptions,
    McpTimeoutError,
)
from pi_mcp.testing import InMemoryTransport, create_in_memory_transport_pair


class _TestServer:
    """上游 ``createServer()`` 的等价物：内存传输 + 方法处理器 + 收到的消息。"""

    def __init__(self, transport: InMemoryTransport) -> None:
        self.transport = transport
        self.messages: list[dict[str, Any]] = []
        self._handlers: dict[str, Any] = {}

    def set_handler(self, method: str, handler: Any) -> None:
        self._handlers[method] = handler

    def _on_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        if "id" not in message or "method" not in message:
            return
        # 对齐上游测试服务端的 ``queueMicrotask(async () => ...)``：异步应答，不阻塞投递。
        task = asyncio.ensure_future(self._respond_async(message))
        _background.add(task)
        task.add_done_callback(_background.discard)

    async def _respond_async(self, request: dict[str, Any]) -> None:
        handler = self._handlers.get(request["method"])
        if handler is None:
            await self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32601, "message": f"Method not found: {request['method']}"},
                }
            )
            return
        try:
            result = handler(request)
            if inspect.isawaitable(result):
                result = await result
            await self.transport.send({"jsonrpc": "2.0", "id": request["id"], "result": result})
        except McpError as error:
            await self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": error.code, "message": str(error), "data": error.data},
                }
            )
        except Exception as error:  # noqa: BLE001 - 与上游测试服务端一致
            await self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32603, "message": str(error)},
                }
            )


_background: set[asyncio.Task[Any]] = set()


async def _create_server() -> tuple[InMemoryTransport, _TestServer]:
    client_transport, server_transport = create_in_memory_transport_pair()
    server = _TestServer(server_transport)
    server_transport.on_message(server._on_message)
    await server_transport.start()
    server.set_handler(
        "initialize",
        lambda request: {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "test-server", "version": "1.0.0"},
            "instructions": "Use test tools.",
        },
    )
    return client_transport, server


async def _connect() -> tuple[McpClient, _TestServer]:
    client_transport, server = await _create_server()
    client = McpClient(McpClientOptions(name="test-client", version="2.0.0"))
    await client.connect(client_transport)
    return client, server


async def _never(request: dict[str, Any]) -> dict[str, Any]:
    """永不答复的处理器（模拟挂住的工具调用）。"""
    await asyncio.Future()
    raise AssertionError("unreachable")


async def _tick() -> None:
    """让出若干轮事件循环，等内存传输投递 + 测试服务端起任务 + 应答回投。"""
    for _ in range(4):
        await asyncio.sleep(0)


# ============================================================
# client.test.ts:70 「initializes the connection before exposing server information」
# ============================================================


async def test_initializes_the_connection_before_exposing_server_information() -> None:
    client, server = await _connect()
    assert client.connection_state == "connected"
    assert client.protocol_version == LATEST_PROTOCOL_VERSION
    assert client.server_info == {"name": "test-server", "version": "1.0.0"}
    assert client.server_capabilities == {"tools": {"listChanged": True}}
    assert client.instructions == "Use test tools."
    assert server.messages == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "2.0.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    await client.close()


# ============================================================
# client.test.ts:94 「paginates tools and preserves protocol tool definitions」
# ============================================================


async def test_paginates_tools_and_preserves_protocol_tool_definitions() -> None:
    client, server = await _connect()

    def tools_list(request: dict[str, Any]) -> dict[str, Any]:
        cursor = (request.get("params") or {}).get("cursor")
        if cursor is None:
            return {
                "tools": [
                    {"name": "search", "description": "Search", "inputSchema": {"type": "object"}}
                ],
                "nextCursor": "page-2",
            }
        return {
            "tools": [
                {
                    "name": "read",
                    "inputSchema": {"type": "object"},
                    "outputSchema": {"type": "object"},
                    "annotations": {"readOnlyHint": True},
                }
            ],
            # 有些服务端用空 cursor 结束分页而不是省略它。
            "nextCursor": "",
        }

    server.set_handler("tools/list", tools_list)
    assert await client.list_tools() == [
        {"name": "search", "description": "Search", "inputSchema": {"type": "object"}},
        {
            "name": "read",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True},
        },
    ]
    await client.close()


# ============================================================
# client.test.ts:128 「lists and reads resources」
# ============================================================


async def test_lists_and_reads_resources() -> None:
    client, server = await _connect()

    def resources_list(request: dict[str, Any]) -> dict[str, Any]:
        cursor = (request.get("params") or {}).get("cursor")
        if cursor is None:
            return {
                "resources": [{"uri": "file:///a", "name": "a", "mimeType": "text/plain"}],
                "nextCursor": "2",
            }
        return {"resources": [{"uri": "file:///b"}]}

    server.set_handler("resources/list", resources_list)
    server.set_handler(
        "resources/templates/list",
        lambda request: {
            "resourceTemplates": [{"uriTemplate": "repo://{owner}/{repo}", "name": "repo"}]
        },
    )
    server.set_handler(
        "resources/read",
        lambda request: {"contents": [{"uri": request["params"]["uri"], "text": "hello"}]},
    )

    # 缺 name 时回退成 uri。
    assert await client.list_resources() == [
        {"uri": "file:///a", "name": "a", "mimeType": "text/plain"},
        {"uri": "file:///b", "name": "file:///b"},
    ]
    assert await client.list_resource_templates() == [
        {"uriTemplate": "repo://{owner}/{repo}", "name": "repo"}
    ]
    # 单页接口把 cursor 透传。
    assert await client.list_resources_page() == {
        "resources": [{"uri": "file:///a", "name": "a", "mimeType": "text/plain"}],
        "nextCursor": "2",
    }
    assert await client.list_resources_page("2") == {
        "resources": [{"uri": "file:///b", "name": "file:///b"}]
    }
    assert await client.read_resource("file:///a") == {
        "contents": [{"uri": "file:///a", "text": "hello"}]
    }

    server.set_handler("resources/read", lambda request: {"contents": [{"uri": "file:///a"}]})
    with pytest.raises(Exception, match="Invalid contents in MCP resources/read result"):
        await client.read_resource("file:///a")
    server.set_handler("resources/list", lambda request: {"resources": [{"name": "no uri"}]})
    with pytest.raises(Exception, match="Invalid entry in MCP resources/list result"):
        await client.list_resources()
    await client.close()


# ============================================================
# client.test.ts:162 「returns structured tool content and surfaces JSON-RPC errors」
# ============================================================


async def test_returns_structured_tool_content_and_surfaces_json_rpc_errors() -> None:
    client, server = await _connect()

    def tools_call(request: dict[str, Any]) -> dict[str, Any]:
        params = request["params"]
        if params["name"] == "fail":
            raise McpError(1234, "tool failed", {"retryable": False})
        return {
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": {"count": (params.get("arguments") or {}).get("count")},
        }

    server.set_handler("tools/call", tools_call)
    assert await client.call_tool("count", {"count": 3}) == {
        "content": [{"type": "text", "text": "ok"}],
        "structuredContent": {"count": 3},
    }
    with pytest.raises(McpError) as excinfo:
        await client.call_tool("fail")
    assert (excinfo.value.name, excinfo.value.code, str(excinfo.value), excinfo.value.data) == (
        "McpError",
        1234,
        "tool failed",
        {"retryable": False},
    )
    await client.close()


# ============================================================
# client.test.ts:185 「renews the timeout on progress」
# ============================================================


async def test_renews_the_timeout_on_progress() -> None:
    client, server = await _connect()

    async def tools_call(request: dict[str, Any]) -> dict[str, Any]:
        token = request["params"]["_meta"]["progressToken"]
        loop = asyncio.get_running_loop()

        def _progress() -> None:
            task = asyncio.ensure_future(
                server.transport.send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {"progressToken": token, "progress": 1, "total": 2},
                    }
                )
            )
            _background.add(task)
            task.add_done_callback(_background.discard)

        loop.call_later(0.04, _progress)
        # 总共 80ms > 50ms 的超时，靠进度通知续命。
        await asyncio.sleep(0.04)
        await asyncio.sleep(0.04)
        return {"content": [{"type": "text", "text": "done"}]}

    server.set_handler("tools/call", tools_call)
    progress: list[Any] = []
    result = await client.call_tool(
        "slow", {}, McpRequestOptions(timeout_ms=50, on_progress=progress.append)
    )
    assert result == {"content": [{"type": "text", "text": "done"}]}
    assert progress == [{"progressToken": 2, "progress": 1, "total": 2}]
    await client.close()


# ============================================================
# client.test.ts:210 「cancels aborted and timed-out requests」
# ============================================================


async def test_cancels_aborted_and_timed_out_requests() -> None:
    client, server = await _connect()
    server.set_handler("tools/call", _never)

    cancel = asyncio.Event()
    aborted = asyncio.ensure_future(
        client.call_tool("wait", {}, McpRequestOptions(signal=cancel, cancel_reason="stop"))
    )
    await _tick()
    cancel.set()
    with pytest.raises(McpAbortError):
        await aborted
    await _tick()
    assert {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 2, "reason": "stop"},
    } in server.messages

    with pytest.raises(McpTimeoutError):
        await client.call_tool("wait", {}, McpRequestOptions(timeout_ms=5))
    await client.close()


# ============================================================
# client.test.ts:228 「reports transport errors without failing pending requests」
# ============================================================


async def test_reports_transport_errors_without_failing_pending_requests() -> None:
    client_transport, server = await _create_server()
    client = McpClient(McpClientOptions(name="test-client", version="1.0.0"))
    await client.connect(client_transport)
    errors: list[Exception] = []
    client.on_error(errors.append)

    resolved: list[Any] = []

    async def tools_call(request: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        resolved.append(future)
        return await future

    server.set_handler("tools/call", tools_call)
    call = asyncio.ensure_future(client.call_tool("wait"))
    await _tick()
    client_transport.emit_error(Exception("stray log line"))
    resolved[0].set_result({"content": []})
    assert await call == {"content": []}
    assert [error.args[0] for error in errors] == ["stray log line"]
    await client.close()


# ============================================================
# client.test.ts:251 「accepts servers that answer with an older protocol version」
# ============================================================


async def test_accepts_servers_that_answer_with_an_older_protocol_version() -> None:
    client_transport, server = await _create_server()
    server.set_handler(
        "initialize",
        lambda request: {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "old-server", "version": "0.1.0"},
        },
    )
    client = McpClient(McpClientOptions(name="test-client", version="1.0.0"))
    await client.connect(client_transport)
    assert client.protocol_version == "2024-11-05"
    await client.close()

    unsupported_transport, unsupported_server = await _create_server()
    unsupported_server.set_handler(
        "initialize",
        lambda request: {
            "protocolVersion": "1999-01-01",
            "capabilities": {},
            "serverInfo": {"name": "ancient-server", "version": "0.1.0"},
        },
    )
    rejected = McpClient(McpClientOptions(name="test-client", version="1.0.0"))
    with pytest.raises(Exception, match="unsupported protocol version"):
        await rejected.connect(unsupported_transport)
    assert rejected.connection_state == "closed"


# ============================================================
# client.test.ts:274 「defaults missing tool result content to an empty list」
# ============================================================


async def test_defaults_missing_tool_result_content_to_an_empty_list() -> None:
    client, server = await _connect()
    server.set_handler("tools/call", lambda request: {"structuredContent": {"ok": True}})
    assert await client.call_tool("structured") == {
        "content": [],
        "structuredContent": {"ok": True},
    }
    server.set_handler("tools/call", lambda request: {"content": "not a list"})
    with pytest.raises(Exception, match="Invalid MCP tools/call result"):
        await client.call_tool("broken")
    await client.close()


# ============================================================
# client.test.ts:284 「accepts structured content that is not an object」（#10416）
# ============================================================


@pytest.mark.parametrize("structured", [[{"id": 1}, {"id": 2}], None, "text", 3, False])
async def test_accepts_structured_content_that_is_not_an_object(structured: Any) -> None:
    client, server = await _connect()
    server.set_handler(
        "tools/call", lambda request: {"content": [], "structuredContent": structured}
    )
    assert await client.call_tool("structured") == {"content": [], "structuredContent": structured}
    await client.close()


# ============================================================
# client.test.ts:293 「does not send notifications/cancelled for a timed-out initialize」
# ============================================================


async def test_does_not_send_notifications_cancelled_for_a_timed_out_initialize() -> None:
    client_transport, server = await _create_server()
    server.set_handler("initialize", _never)
    client = McpClient(McpClientOptions(name="test-client", version="1.0.0", request_timeout_ms=5))
    with pytest.raises(McpTimeoutError):
        await client.connect(client_transport)
    await _tick()
    assert not any(
        message.get("method") == "notifications/cancelled" for message in server.messages
    )


# ============================================================
# client.test.ts:304 「notifies close listeners once when the transport drops」
# ============================================================


async def test_notifies_close_listeners_once_when_the_transport_drops() -> None:
    client, server = await _connect()
    closed: list[int] = []
    client.on_close(lambda: closed.append(1))
    server.set_handler("tools/call", _never)
    pending = asyncio.ensure_future(client.call_tool("wait"))
    await _tick()
    await server.transport.close()
    with pytest.raises(Exception, match="MCP connection closed"):
        await pending
    assert client.connection_state == "closed"
    await client.close()
    assert closed == [1]


# ============================================================
# client.test.ts:318 「answers roots/list and dispatches notifications」
# ============================================================


async def test_answers_roots_list_and_dispatches_notifications() -> None:
    client_transport, server = await _create_server()
    client = McpClient(
        McpClientOptions(
            name="test-client",
            version="1.0.0",
            roots=[{"uri": "file:///workspace", "name": "workspace"}],
        )
    )
    await client.connect(client_transport)
    changed: list[Any] = []
    client.on_notification("notifications/tools/list_changed", changed.append)
    await server.transport.send({"jsonrpc": "2.0", "id": "roots", "method": "roots/list"})
    await server.transport.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    await _tick()
    assert {
        "jsonrpc": "2.0",
        "id": "roots",
        "result": {"roots": [{"uri": "file:///workspace", "name": "workspace"}]},
    } in server.messages
    assert changed == [None]
    await client.close()


# ============================================================
# 本端补充（上游未覆盖）
# ============================================================


async def test_declares_roots_capability_when_roots_are_configured() -> None:
    """配了 roots 时，``initialize`` 的 capabilities 里要带上 ``roots``。"""
    client_transport, server = await _create_server()
    client = McpClient(
        McpClientOptions(name="c", version="1", roots=lambda: [{"uri": "file:///r"}])
    )
    await client.connect(client_transport)
    initialize = server.messages[0]
    assert initialize["params"]["capabilities"] == {"roots": {}}
    await client.close()


async def test_unknown_server_request_is_answered_with_method_not_found() -> None:
    """服务端发来的未知请求回 ``-32601``，客户端不崩。"""
    client, server = await _connect()
    await server.transport.send({"jsonrpc": "2.0", "id": "x", "method": "sampling/createMessage"})
    await _tick()
    assert {
        "jsonrpc": "2.0",
        "id": "x",
        "error": {"code": -32601, "message": "Method not found: sampling/createMessage"},
    } in server.messages
    await client.close()


async def test_served_request_is_cancelled_by_notifications_cancelled() -> None:
    """服务端可以对它发起的请求发 ``notifications/cancelled``。"""
    client_transport, server = await _create_server()
    client = McpClient(McpClientOptions(name="c", version="1"))
    await client.connect(client_transport)
    seen: list[bool] = []

    async def handler(params: Any, cancel_event: asyncio.Event) -> Any:
        await cancel_event.wait()
        seen.append(True)
        return {"ok": True}

    client.set_request_handler("do/work", handler)
    await server.transport.send({"jsonrpc": "2.0", "id": 42, "method": "do/work"})
    await _tick()
    await server.transport.send(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 42}}
    )
    await _tick()
    assert seen == [True]
    await client.close()


async def test_client_answers_server_ping() -> None:
    """服务端发来的 ``ping`` 由客户端内建处理器应答（上游 ``client.ts:175``）。"""
    client, server = await _connect()
    await server.transport.send({"jsonrpc": "2.0", "id": "p", "method": "ping"})
    await _tick()
    assert {"jsonrpc": "2.0", "id": "p", "result": {}} in server.messages
    await client.close()


async def test_ping_request_round_trip() -> None:
    """客户端主动 ``ping``：请求真的发出去，结果照常返回。"""
    client, server = await _connect()
    server.set_handler("ping", lambda request: {})
    await client.ping()
    assert {"jsonrpc": "2.0", "id": 2, "method": "ping"} in server.messages
    await client.close()


async def test_duplicate_cursor_is_rejected() -> None:
    """同一 cursor 再次出现即报错，避免无限翻页。"""
    client, server = await _connect()
    server.set_handler(
        "tools/list",
        lambda request: {"tools": [{"name": "t", "inputSchema": {}}], "nextCursor": "same"},
    )
    with pytest.raises(Exception, match="duplicate cursor"):
        await client.list_tools()
    await client.close()
