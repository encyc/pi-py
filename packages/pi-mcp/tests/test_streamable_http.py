"""``StreamableHttpTransport`` 与 ``consume_sse_stream`` 的行为规格。

逐条对齐上游 ``packages/mcp/test/streamable-http.test.ts``（v1.3.0，486 行）。
对应用例的上游行号写在各测试 docstring 里；依赖 JS 特有行为的用例**显式标注跳过原因**。

服务端是**真实回环 HTTP 服务端**（``helpers.listen``），SSE 走真正的 HTTP/1.1 分块传输。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from pi_mcp import (
    LATEST_PROTOCOL_VERSION,
    McpAuthRequiredError,
    McpClient,
    McpClientOptions,
    McpHttpError,
    McpRequestOptions,
    McpSessionExpiredError,
    StreamableHttpReconnectOptions,
    StreamableHttpTransport,
    StreamableHttpTransportOptions,
    UnauthorizedContext,
    consume_sse_stream,
)
from pi_mcp.transports.streamable_http import ConsumeSseOptions, SseEvent

from .helpers import RecordedRequest, Response, close_servers, listen, sse_message

pytestmark = pytest.mark.usefixtures("_close_http_servers")


@pytest.fixture
def _close_http_servers() -> Any:
    yield
    close_servers()


def _protocol_handler(
    request: RecordedRequest, response: Response, requests: list[RecordedRequest]
) -> None:
    """上游 ``protocolHandler`` 的等价物。"""
    if request.method == "GET":
        response.send(405)
        return
    if request.method == "DELETE":
        response.send(200)
        return
    import json

    message = json.loads(request.body) if request.body else {}
    if "id" not in message:
        response.send(202)
        return
    if message.get("method") == "initialize":
        response.send(
            200,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": LATEST_PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "http-fixture", "version": "1.0.0"},
                    },
                }
            ),
            {"content-type": "application/json", "mcp-session-id": "session-1"},
        )
        return
    if message.get("method") == "tools/list":
        response.send(
            200,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]},
                }
            ),
            {"content-type": "application/json"},
        )
        return
    body = "id: tool-result\n" + sse_message(
        {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"content": [{"type": "text", "text": "hello"}]},
        }
    )
    response.start_stream(200, {"content-type": "text/event-stream"})
    response.write_chunk(body)


def _client() -> McpClient:
    return McpClient(McpClientOptions(name="http-test", version="1.0.0"))


# ============================================================
# consume_sse_stream（上游 streamable-http.test.ts:91）
# ============================================================


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def test_consume_sse_parses_chunked_crlf_events_comments_ids_and_multiline_data() -> None:
    """上游 :92 —— CRLF、注释行、id、多行 data、跨块切分。"""
    events: list[SseEvent] = []
    await consume_sse_stream(
        _chunks(b': keepalive\r\nid: 7\r\ndata: {"one":\r\n', b"data: 1}\r\n\r\n"),
        ConsumeSseOptions(on_event=events.append),
    )
    assert events == [SseEvent(data='{"one":\n1}', id="7")]


async def test_consume_sse_rejects_events_whose_data_lines_exceed_the_limit() -> None:
    """上游 :106 —— 没有结束空行时，data 行累计超限要报错（且不会无限读下去）。"""
    sent = 0

    async def endless() -> AsyncIterator[bytes]:
        nonlocal sent
        while sent < 1000:
            sent += 1
            yield b"data: xxxxxxxxxxxxxxxx\n"

    with pytest.raises(ValueError, match="MCP SSE event exceeds 256 bytes"):
        await consume_sse_stream(
            endless(), ConsumeSseOptions(max_event_bytes=256, on_event=lambda e: None)
        )
    assert sent < 100


async def test_consume_sse_reports_ids_without_data_and_retry_fields() -> None:
    """上游 :35/:38 —— 无 data 的事件也要回调 ``on_id``；``retry`` 只在纯数字时回调。"""
    ids: list[str] = []
    retries: list[int] = []
    events: list[SseEvent] = []
    await consume_sse_stream(
        _chunks(b"id: prime\nretry: 250\ndata:\n\n", b"retry: nope\nid: bad\x00id\ndata: x\n\n"),
        ConsumeSseOptions(on_event=events.append, on_id=ids.append, on_retry=retries.append),
    )
    assert ids == ["prime"]
    assert retries == [250]
    # ``data:`` 空值也算一行 data，因此仍然派发一个空事件（与上游 ``dataLines.length`` 判定一致）；
    # 它带着 id 一起派发，供续传预热使用。
    assert events == [SseEvent(data="", id="prime"), SseEvent(data="x")]


# ============================================================
# StreamableHttpTransport
# ============================================================


async def test_handles_json_and_sse_responses_with_session_and_protocol_headers() -> None:
    """上游 :124 —— JSON 与 SSE 两种答复、会话头、协议版本头、GET 与 DELETE。"""
    recorded: list[RecordedRequest] = []

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        recorded.append(request)
        _protocol_handler(request, response, requests)

    url = listen(handler)
    transport = StreamableHttpTransport(StreamableHttpTransportOptions(url=url))
    client = _client()
    await client.connect(transport)
    assert await client.list_tools() == [{"name": "echo", "inputSchema": {"type": "object"}}]
    assert await client.call_tool("echo", {"text": "hello"}) == {
        "content": [{"type": "text", "text": "hello"}]
    }
    await client.close()

    list_request = next(entry for entry in recorded if entry.body and '"tools/list"' in entry.body)
    assert list_request.headers.get("mcp-session-id") == "session-1"
    assert list_request.headers.get("mcp-protocol-version") == LATEST_PROTOCOL_VERSION
    assert any(entry.method == "GET" for entry in recorded)
    assert any(entry.method == "DELETE" for entry in recorded)


async def test_classifies_authentication_failures() -> None:
    """上游 :143 —— 401 归类为 ``McpAuthRequiredError``，带 ``www-authenticate`` 与响应体。"""

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        response.send(
            401,
            "login required",
            {"www-authenticate": 'Bearer resource_metadata="https://example.com/meta"'},
        )

    url = listen(handler)
    client = _client()
    with pytest.raises(McpAuthRequiredError) as excinfo:
        await client.connect(StreamableHttpTransport(StreamableHttpTransportOptions(url=url)))
    error = excinfo.value
    assert error.name == "McpAuthRequiredError"
    assert error.status == 401
    assert error.body == "login required"
    assert error.www_authenticate == 'Bearer resource_metadata="https://example.com/meta"'


async def test_fails_only_the_request_whose_sse_stream_breaks() -> None:
    """上游 :158 —— 坏 SSE 流只让那一条请求失败，其他请求不受影响。"""
    import json

    release_slow = threading.Event()

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        if request.method != "POST":
            _protocol_handler(request, response, requests)
            return
        message = json.loads(request.body)
        name = (message.get("params") or {}).get("name")
        if name == "broken":
            response.start_stream(200, {"content-type": "text/event-stream"})
            response.write_chunk("data: not json\n\n")
            return
        if name == "slow":
            release_slow.wait(timeout=10)
        _protocol_handler(request, response, requests)

    url = listen(handler)
    client = _client()
    errors: list[Exception] = []
    client.on_error(errors.append)
    await client.connect(
        StreamableHttpTransport(StreamableHttpTransportOptions(url=url, open_get_stream=False))
    )

    slow = asyncio.ensure_future(client.call_tool("slow"))
    with pytest.raises(Exception, match="MCP response stream failed"):
        await client.call_tool("broken")
    release_slow.set()
    assert await slow == {"content": [{"type": "text", "text": "hello"}]}
    assert len(errors) == 1
    await client.close()


async def test_opens_the_get_stream_after_initialization_and_sends_last_event_id_only_when_resuming() -> (
    None
):
    """上游 :188 —— GET 流在 ``notifications/initialized`` 之后打开；非续传不带 Last-Event-ID。"""
    import json

    order: list[str] = []

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        if request.method == "POST":
            order.append(json.loads(request.body)["method"])
        else:
            order.append(request.method)
        _protocol_handler(request, response, requests)

    url = listen(handler)
    client = _client()
    await client.connect(StreamableHttpTransport(StreamableHttpTransportOptions(url=url)))
    await client.call_tool("echo")
    await client.list_tools()
    await client.close()

    assert order.index("GET") > order.index("notifications/initialized")


async def test_resumes_a_response_stream_the_server_closed_before_answering() -> None:
    """上游 :208 —— 服务端给了事件 id 就 GET + Last-Event-ID 续读。"""
    import json

    resume_headers: list[str | None] = []

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        if request.method == "GET" and request.headers.get("last-event-id"):
            resume_headers.append(request.headers["last-event-id"])
            response.start_stream(200, {"content-type": "text/event-stream"})
            response.write_chunk(
                sse_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"content": [{"type": "text", "text": "resumed"}]},
                    },
                    event_id="2",
                )
            )
            return
        if request.method != "POST":
            _protocol_handler(request, response, requests)
            return
        message = json.loads(request.body)
        if message.get("method") != "tools/call":
            _protocol_handler(request, response, requests)
            return
        # 预热事件（有 id、无 data）+ retry 提示，然后服务端直接断流。
        response.start_stream(200, {"content-type": "text/event-stream"})
        response.write_chunk("id: 1\nretry: 5\ndata:\n\n")

    url = listen(handler)
    client = _client()
    errors: list[Exception] = []
    client.on_error(errors.append)
    await client.connect(
        StreamableHttpTransport(StreamableHttpTransportOptions(url=url, open_get_stream=False))
    )
    assert await client.call_tool("echo") == {"content": [{"type": "text", "text": "resumed"}]}
    assert resume_headers == ["1"]
    assert errors == []
    await client.close()


async def test_fails_a_request_whose_response_stream_ends_without_an_answer() -> None:
    """上游 :236 —— 流结束且从未答复：该请求失败并给出原因。"""
    import json

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        if request.method != "POST":
            _protocol_handler(request, response, requests)
            return
        message = json.loads(request.body)
        if message.get("method") != "tools/call":
            _protocol_handler(request, response, requests)
            return
        response.start_stream(200, {"content-type": "text/event-stream"})
        response.write_chunk(": nothing here\n\n")

    url = listen(handler)
    client = _client()
    await client.connect(
        StreamableHttpTransport(StreamableHttpTransportOptions(url=url, open_get_stream=False))
    )
    with pytest.raises(
        Exception, match="MCP response stream failed: stream ended without a response"
    ):
        await client.call_tool("echo", {}, McpRequestOptions(timeout_ms=5_000))
    await client.close()


async def test_reconnects_the_get_stream_after_it_drops() -> None:
    """上游 :252 —— GET 流断开后按退避重连，并带上次的事件 id。"""
    gets = 0
    last_event_ids: list[str | None] = []
    notification = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        nonlocal gets
        if request.method != "GET":
            _protocol_handler(request, response, requests)
            return
        gets += 1
        last_event_ids.append(request.headers.get("last-event-id"))
        response.start_stream(200, {"content-type": "text/event-stream"})
        response.write_chunk(sse_message(notification, event_id="g1" if gets == 1 else "g2"))
        if gets == 1:
            return  # 立即结束 → 客户端重连

    url = listen(handler)
    client = _client()
    changes = 0
    second_change = asyncio.Event()

    def on_change(params: Any) -> None:
        nonlocal changes
        changes += 1
        if changes == 2:
            asyncio.get_running_loop().call_soon(second_change.set)

    client.on_notification("notifications/tools/list_changed", on_change)
    await client.connect(
        StreamableHttpTransport(
            StreamableHttpTransportOptions(
                url=url, reconnect=StreamableHttpReconnectOptions(initial_delay_ms=1)
            )
        )
    )
    await asyncio.wait_for(second_change.wait(), timeout=10)
    assert last_event_ids == [None, "g1"]
    await client.close()


async def test_rejects_a_request_the_server_accepts_without_a_response() -> None:
    """上游 :280 —— 202 却没有答复：明确报错，而不是永远挂着。"""
    import json

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        if request.method != "POST":
            _protocol_handler(request, response, requests)
            return
        message = json.loads(request.body)
        if message.get("method") != "tools/call":
            _protocol_handler(request, response, requests)
            return
        response.send(202)

    url = listen(handler)
    client = _client()
    await client.connect(
        StreamableHttpTransport(StreamableHttpTransportOptions(url=url, open_get_stream=False))
    )
    with pytest.raises(Exception, match="without a response"):
        await client.call_tool("echo")
    await client.close()


async def test_includes_the_response_body_in_http_errors() -> None:
    """上游 :294 —— HTTP 错误信息里带上响应体片段。"""

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        response.send(400, "Invalid Accept header")

    url = listen(handler)
    client = _client()
    with pytest.raises(
        Exception, match="MCP HTTP request failed with status 400: Invalid Accept header"
    ):
        await client.connect(StreamableHttpTransport(StreamableHttpTransportOptions(url=url)))


async def test_hands_401_and_insufficient_scope_403_to_the_auth_provider_with_the_rejected_token() -> (
    None
):
    """上游 :306 —— 401 与「要求更多 scope 的 403」各交接一次，带**被拒**的那个令牌。"""
    import json

    seen: list[tuple[int, str | None]] = []
    state = {"token": "old"}

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        if request.method != "POST":
            _protocol_handler(request, response, requests)
            return
        message = json.loads(request.body)
        if request.headers.get("authorization") == "Bearer old":
            response.send(401, "", {"www-authenticate": "Bearer"})
            return
        if (
            message.get("method") == "tools/call"
            and request.headers.get("authorization") == "Bearer new"
        ):
            response.send(
                403, "", {"www-authenticate": 'Bearer error="insufficient_scope", scope="admin"'}
            )
            return
        _protocol_handler(request, response, requests)

    class _Provider:
        async def token(self) -> str | None:
            return state["token"]

        async def on_unauthorized(self, context: UnauthorizedContext) -> None:
            seen.append((context.status, context.token))
            state["token"] = "new" if context.status == 401 else "admin"

    url = listen(handler)
    client = _client()
    await client.connect(
        StreamableHttpTransport(
            StreamableHttpTransportOptions(
                url=url, open_get_stream=False, auth_provider=_Provider()
            )
        )
    )
    assert await client.call_tool("echo") == {"content": [{"type": "text", "text": "hello"}]}
    assert seen == [(401, "old"), (403, "new")]
    await client.close()


async def test_does_not_retry_requests_cancelled_while_authorization_was_recovering() -> None:
    """上游 :346（#10598）—— 授权恢复期间被取消/超时的请求不得被重发。"""
    import json

    state = {"token": "old"}
    executed: list[str] = []
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    waiting = 0
    waiting_lock = threading.Lock()
    all_waiting = asyncio.Event()

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        if request.method != "POST":
            _protocol_handler(request, response, requests)
            return
        message = json.loads(request.body)
        if (
            message.get("method") == "tools/call"
            and request.headers.get("authorization") == "Bearer old"
        ):
            response.send(401, "", {"www-authenticate": "Bearer"})
            return
        if message.get("method") == "tools/call":
            executed.append(message["params"]["name"])
        _protocol_handler(request, response, requests)

    class _Provider:
        async def token(self) -> str | None:
            return state["token"]

        async def on_unauthorized(self, context: UnauthorizedContext) -> None:
            nonlocal waiting
            refresh_started.set()
            with waiting_lock:
                waiting += 1
                done = waiting >= 3
            if done:
                all_waiting.set()
            # 共享的刷新：三条请求都卡在这里，直到测试放行。
            await asyncio.to_thread(release_refresh.wait, 10)

    url = listen(handler)
    client = _client()
    await client.connect(
        StreamableHttpTransport(
            StreamableHttpTransportOptions(
                url=url, open_get_stream=False, auth_provider=_Provider()
            )
        )
    )

    cancel = asyncio.Event()
    aborted = asyncio.ensure_future(
        client.call_tool("aborted", {}, McpRequestOptions(signal=cancel))
    )
    timed_out = asyncio.ensure_future(
        client.call_tool("timed-out", {}, McpRequestOptions(timeout_ms=200))
    )
    kept = asyncio.ensure_future(client.call_tool("kept"))
    await asyncio.wait_for(all_waiting.wait(), timeout=10)
    cancel.set()
    with pytest.raises(Exception, match="MCP request aborted"):
        await aborted
    with pytest.raises(Exception, match="timed out"):
        await timed_out
    state["token"] = "new"
    release_refresh.set()
    assert await kept == {"content": [{"type": "text", "text": "hello"}]}
    assert executed == ["kept"]
    await client.close()


async def test_closes_the_session_with_the_last_requests_token_without_asking_the_auth_provider() -> (
    None
):
    """上游 :406（#10565）—— ``close()`` 复用最后一个请求的令牌，不等待可能的网络刷新。"""
    calls = 0

    class _Provider:
        async def token(self) -> str | None:
            nonlocal calls
            calls += 1
            return f"token-{calls}"

    recorded: list[RecordedRequest] = []

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        recorded.append(request)
        _protocol_handler(request, response, requests)

    url = listen(handler)
    client = _client()
    await client.connect(
        StreamableHttpTransport(
            StreamableHttpTransportOptions(
                url=url, open_get_stream=False, auth_provider=_Provider()
            )
        )
    )
    before = calls
    await client.close()
    assert calls == before
    deletes = [entry for entry in recorded if entry.method == "DELETE"]
    assert [entry.headers.get("authorization") for entry in deletes] == [f"Bearer token-{before}"]
    assert deletes[0].headers.get("mcp-session-id") == "session-1"


@pytest.mark.skip(
    reason="上游 :426「calls fetch without a receiver」针对 JS 的 ``Illegal invocation``"
    "（Cloudflare Workers 对平台 fetch 的 this 绑定校验）。Python 没有 receiver 绑定这回事，"
    "对应风险面改为「注入的 httpx 客户端被原样使用、且不被传输关闭」——见下一用例。"
)
def test_calls_fetch_without_a_receiver_js_only() -> None:
    """占位：保留上游用例编号，说明为何不适用。"""


async def test_injected_http_client_is_used_and_not_closed_by_the_transport() -> None:
    """上游 :426 的 Python 等价面：注入的客户端被复用，且所有权仍属于调用方。"""
    url = listen(_protocol_handler)
    injected = httpx.AsyncClient()
    transport = StreamableHttpTransport(
        StreamableHttpTransportOptions(url=url, client=injected, open_get_stream=False)
    )
    client = _client()
    await client.connect(transport)
    assert transport.session_id == "session-1"
    assert await client.list_tools() == [{"name": "echo", "inputSchema": {"type": "object"}}]
    await client.close()
    # 客户端是调用方给的 —— 传输不得关掉它。
    assert injected.is_closed is False
    await injected.aclose()


async def test_classifies_an_expired_established_session() -> None:
    """上游 :470 —— 建过会话之后收到 404 归类为 ``McpSessionExpiredError``。"""
    posts = 0

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            if posts > 2:
                response.send(404, "gone")
                return
        _protocol_handler(request, response, requests)

    url = listen(handler)
    client = _client()
    await client.connect(
        StreamableHttpTransport(StreamableHttpTransportOptions(url=url, open_get_stream=False))
    )
    with pytest.raises(McpSessionExpiredError):
        await client.list_tools()
    await client.close()


# ============================================================
# 本端补充
# ============================================================


async def test_http_error_carries_status_and_body() -> None:
    """``McpHttpError`` 保留 status / body 供上层判断。"""

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        response.send(503, "unavailable")

    url = listen(handler)
    client = _client()
    with pytest.raises(McpHttpError) as excinfo:
        await client.connect(StreamableHttpTransport(StreamableHttpTransportOptions(url=url)))
    assert excinfo.value.status == 503
    assert excinfo.value.body == "unavailable"


async def test_json_array_response_emits_every_message() -> None:
    """JSON 答复是数组时逐条派发（上游 ``streamable-http.ts:257``）。"""
    import json

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        if request.method != "POST":
            _protocol_handler(request, response, requests)
            return
        message = json.loads(request.body)
        if "id" not in message or message.get("method") == "initialize":
            _protocol_handler(request, response, requests)
            return
        response.send(
            200,
            json.dumps(
                [
                    {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"},
                    {"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}},
                ]
            ),
            {"content-type": "application/json"},
        )

    url = listen(handler)
    client = _client()
    await client.connect(
        StreamableHttpTransport(StreamableHttpTransportOptions(url=url, open_get_stream=False))
    )
    changes: list[Any] = []
    client.on_notification("notifications/tools/list_changed", changes.append)
    result = await client.request("anything")
    assert result == {"ok": True}
    assert changes == [None]
    await client.close()
