"""Streamable HTTP 传输（POST + 可选 SSE 流 + 断线重连）。

对应上游 ``packages/mcp/src/transports/streamable-http.ts``（v1.3.0，521 行）。

协议要点（对齐 MCP Streamable HTTP）：

- 每条消息以 ``POST`` 发出，``Accept: application/json, text/event-stream``。
- 答复可能是 ``application/json``（一次性 JSON，也可能是数组），也可能是
  ``text/event-stream``（SSE，逐事件回消息）。
- 服务端可在任一回应用 ``Mcp-Session-Id`` 建立会话；后续请求带该头。
- 协商出的协议版本通过 ``MCP-Protocol-Version`` 头回传。
- 「通知/响应」类消息（无 id 的请求）由服务端以 202 确认，无答复；
  ``notifications/initialized`` 之后才打开服务端→客户端的 GET SSE 流。
- SSE 响应流可能在答复到达前被服务端关闭；若服务端给了事件 id，就用 ``GET`` +
  ``Last-Event-ID`` 续读。

偏离（见 ``PORTING.md``）：WHATWG ``fetch`` + ``ReadableStream`` → ``httpx.AsyncClient``
（``send(..., stream=True)`` 与 ``aiter_bytes()``）；``AbortController`` → ``asyncio.Event``。
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..auth_provider import AuthProvider, UnauthorizedContext
from ..protocol.jsonrpc import (
    INTERNAL_ERROR,
    JsonRpcId,
    JsonRpcMessage,
    McpAbortError,
    McpConnectionClosedError,
    is_json_rpc_request,
    is_json_rpc_response,
    parse_json_rpc_message,
    to_error,
)
from .transport import (
    DEFAULT_MAX_MESSAGE_BYTES,
    McpTransportSendOptions,
    TransportEvents,
)

MAX_ERROR_BODY_BYTES = 8 * 1024
ERROR_MESSAGE_BODY_CHARS = 500
DEFAULT_RECONNECT_INITIAL_DELAY_MS = 1_000
DEFAULT_RECONNECT_MAX_DELAY_MS = 30_000
DEFAULT_RECONNECT_MAX_RETRIES = 5

#: 403 挑战里要求更多 scope 的标记（step-up 授权）。
_INSUFFICIENT_SCOPE = re.compile(r'(?:^|[\s,])error="?insufficient_scope"?', re.IGNORECASE)


@dataclass
class SseEvent:
    """一条 SSE 事件。"""

    data: str
    event: str | None = None
    id: str | None = None


@dataclass
class ConsumeSseOptions:
    """``consume_sse_stream`` 的回调与上限。"""

    on_event: Callable[[SseEvent], None]
    max_event_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    #: 每个 ``id`` 字段都会回调，**包括没有 data 的事件**（例如为续传预热的事件）。
    on_id: Callable[[str], None] | None = None
    #: 每个合法的 ``retry`` 字段都会回调（毫秒）。
    on_retry: Callable[[int], None] | None = None


async def consume_sse_stream(chunks: AsyncIterator[bytes], options: ConsumeSseOptions) -> None:
    """消费一个 SSE 字节流，按事件回调。

    对齐 WHATWG EventSource 的解析：``\\n`` 分帧（容忍 ``\\r\\n``），``:`` 开头是注释，
    ``field: value`` 去掉值前的一个空格，多行 ``data`` 用 ``\\n`` 连接，空行派发事件。
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffered = ""
    event_name: str | None = None
    event_id: str | None = None
    data_lines: list[str] = []
    #: 待派发事件的 data 字节数（含 ``\n`` 连接符），使「只发短 data 行、不发结束空行」的
    #: 事件流不能无限增长。
    data_bytes = 0
    max_event_bytes = options.max_event_bytes

    def dispatch() -> None:
        nonlocal event_name, event_id, data_lines, data_bytes
        if not data_lines:
            event_name = None
            event_id = None
            return
        data = "\n".join(data_lines)
        options.on_event(SseEvent(data=data, event=event_name, id=event_id))
        event_name = None
        event_id = None
        data_lines = []
        data_bytes = 0

    def process_line(raw_line: str) -> None:
        nonlocal event_name, event_id, data_bytes
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if line == "":
            dispatch()
            return
        if line.startswith(":"):
            return
        colon = line.find(":")
        field_name = line if colon < 0 else line[:colon]
        value = "" if colon < 0 else line[colon + 1 :]
        if value.startswith(" "):
            value = value[1:]
        if field_name == "data":
            data_bytes += len(value.encode("utf-8")) + (1 if data_lines else 0)
            if data_bytes > max_event_bytes:
                raise ValueError(f"MCP SSE event exceeds {max_event_bytes} bytes")
            data_lines.append(value)
        elif field_name == "event":
            event_name = value
        elif field_name == "id" and "\0" not in value:
            event_id = value
            if options.on_id is not None:
                options.on_id(value)
        elif field_name == "retry" and value.isdigit():
            if options.on_retry is not None:
                options.on_retry(int(value))

    async for chunk in chunks:
        buffered += decoder.decode(chunk)
        newline = buffered.find("\n")
        while newline >= 0:
            process_line(buffered[:newline])
            buffered = buffered[newline + 1 :]
            newline = buffered.find("\n")
        if len(buffered.encode("utf-8")) > max_event_bytes:
            raise ValueError(f"MCP SSE event exceeds {max_event_bytes} bytes")
    buffered += decoder.decode(b"", True)
    if buffered:
        process_line(buffered)
    dispatch()


@dataclass
class StreamableHttpReconnectOptions:
    """SSE 流断线重连策略。"""

    #: 首次重连前的延迟，除非服务端给了 ``retry`` 字段。默认 1000ms。
    initial_delay_ms: int = DEFAULT_RECONNECT_INITIAL_DELAY_MS
    #: 指数退避上限。默认 30000ms。
    max_delay_ms: int = DEFAULT_RECONNECT_MAX_DELAY_MS
    #: 一条流连续失败多少次后放弃。默认 5。
    max_retries: int = DEFAULT_RECONNECT_MAX_RETRIES


@dataclass
class StreamableHttpTransportOptions:
    """``StreamableHttpTransport`` 的配置。"""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    #: 注入的 httpx 客户端；不提供则自建（并在 ``close()`` 时关闭）。
    client: httpx.AsyncClient | None = None
    #: 初始化之后是否打开服务端→客户端的 GET 流。默认 ``True``。
    open_get_stream: bool = True
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    auth_provider: AuthProvider | None = None
    reconnect: StreamableHttpReconnectOptions = field(
        default_factory=StreamableHttpReconnectOptions
    )
    #: 单次请求的超时（秒）。``None`` 表示 httpx 默认。
    timeout: float | None = None


class McpHttpError(Exception):
    """HTTP 层失败。"""

    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(message)
        self.name = "McpHttpError"
        self.status = status
        self.body = body


class McpAuthRequiredError(McpHttpError):
    """401（需要认证）。"""

    def __init__(self, www_authenticate: str | None, body: str = "") -> None:
        super().__init__(401, "MCP server requires authentication", body)
        self.name = "McpAuthRequiredError"
        self.www_authenticate = www_authenticate


class McpSessionExpiredError(McpHttpError):
    """建立过会话之后收到 404（会话已失效）。"""

    def __init__(self, body: str = "") -> None:
        super().__init__(404, "MCP session expired", body)
        self.name = "McpSessionExpiredError"


def _content_type(response: httpx.Response) -> str | None:
    raw: str | None = response.headers.get("content-type")
    if raw is None:
        return None
    return raw.split(";", 1)[0].strip().lower()


def _needs_authorization(response: httpx.Response) -> bool:
    """401，或挑战里带 ``insufficient_scope`` 的 403（step-up 授权）。"""
    if response.status_code == 401:
        return True
    if response.status_code != 403:
        return False
    challenge = response.headers.get("www-authenticate") or ""
    return _INSUFFICIENT_SCOPE.search(challenge) is not None


def _is_transient_status(status: int) -> bool:
    """流（重）开失败时值得重试的状态码。"""
    return status in (408, 429) or status >= 500


async def _discard(response: httpx.Response) -> None:
    """读完并释放响应（对应上游 ``response.body?.cancel()``）。"""
    with contextlib.suppress(Exception):
        await response.aread()
    with contextlib.suppress(Exception):
        await response.aclose()


def _describe_http_failure(status: int, body: str) -> str:
    text = body.strip()
    snippet = (
        f"{text[: ERROR_MESSAGE_BODY_CHARS - 3]}..."
        if len(text) > ERROR_MESSAGE_BODY_CHARS
        else text
    )
    return f"MCP HTTP request failed with status {status}{f': {snippet}' if snippet else ''}"


@dataclass
class _StreamCursor:
    last_event_id: str | None = None
    retry_ms: int | None = None
    #: 这条流自（重）开以来是否送达过事件。
    received: bool = False


class StreamableHttpTransport(TransportEvents):
    """MCP Streamable HTTP 传输。"""

    def __init__(self, options: StreamableHttpTransportOptions) -> None:
        super().__init__()
        self.options = options
        self.url = httpx.URL(options.url)
        self._owns_client = options.client is None
        self._client = options.client or httpx.AsyncClient(
            timeout=options.timeout if options.timeout is not None else httpx.Timeout(30.0)
        )
        self._closed_event = asyncio.Event()
        self._started = False
        self._closed = False
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._get_stream_started = False
        #: 最近一次请求用的访问令牌；``close()`` 复用它，不再询问授权提供者。
        self._last_token: str | None = None
        self._streams: set[asyncio.Task[None]] = set()
        self._open_responses: set[httpx.Response] = set()

    # ---- 只读属性 ----

    @property
    def session_id(self) -> str | None:
        """服务端分配的会话 id。"""
        return self._session_id

    @property
    def closed(self) -> bool:
        """是否已关闭。"""
        return self._closed

    # ---- 生命周期 ----

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("MCP Streamable HTTP transport already started")
        if self._closed:
            raise McpConnectionClosedError()
        self._started = True

    def set_protocol_version(self, version: str) -> None:
        """记录协商出的协议版本，后续请求带 ``MCP-Protocol-Version``。"""
        self._protocol_version = version

    async def send(
        self, message: JsonRpcMessage, options: McpTransportSendOptions | None = None
    ) -> None:
        if not self._started or self._closed:
            raise McpConnectionClosedError()
        response = await self._authorized_fetch(
            "POST",
            {"accept": "application/json, text/event-stream", "content-type": "application/json"},
            json.dumps(message, separators=(",", ":")).encode("utf-8"),
            options.signal if options is not None else None,
        )
        await self._check_response(response)
        self._capture_session(response)

        if not is_json_rpc_request(message):
            # 通知与响应以 202 确认，不带答复；忽略任何响应体。
            await _discard(response)
            # 服务端→客户端流只可能在会话初始化之后打开。
            if message.get("method") == "notifications/initialized":
                self._start_get_stream()
            return
        if response.status_code in (202, 204):
            await _discard(response)
            raise McpHttpError(
                response.status_code,
                f"MCP server accepted request {message.get('method')} without a response",
            )
        kind = _content_type(response)
        if kind == "application/json":
            try:
                body = await response.aread()
            finally:
                await response.aclose()
            parsed: Any = json.loads(body or b"null")
            for item in parsed if isinstance(parsed, list) else [parsed]:
                self._emit_message(parse_json_rpc_message(item))
            return
        if kind == "text/event-stream":
            self._track(self._consume_response_stream(response, message["id"]))
            return
        await _discard(response)
        raise McpHttpError(
            response.status_code, f"Unsupported MCP response content type: {kind or 'missing'}"
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()
        for response in list(self._open_responses):
            with contextlib.suppress(Exception):
                await response.aclose()
        self._open_responses.clear()
        if self._started and self._session_id:
            # 尽力而为：会话在服务端本来也会过期。授权提供者可能走网络刷新令牌，
            # 因此这里不询问它，关闭也从不等待刷新。
            with contextlib.suppress(Exception):
                await self._client.request(
                    "DELETE",
                    self.url,
                    headers=self._build_headers({}, self._last_token),
                    timeout=1.0,
                )
        if self._owns_client:
            with contextlib.suppress(Exception):
                await self._client.aclose()
        self._emit_close()

    # ---- HTTP 细节 ----

    async def _authorized_fetch(
        self,
        method: str,
        extra_headers: dict[str, str],
        body: bytes | None = None,
        signal: asyncio.Event | None = None,
    ) -> httpx.Response:
        """带认证头的请求。

        401（或要求更多 scope 的 403）交给授权提供者处理一次，然后用它留下的凭据重发。

        ``signal`` 只用于**阻止派发**：取令牌或授权恢复可能耗时，发送方在此期间放弃了请求，
        那次请求就不能再到达服务端。它不会中止共享的恢复过程（其他请求可能在等它）。
        """
        provider = self.options.auth_provider
        on_unauthorized = (
            getattr(provider, "on_unauthorized", None) if provider is not None else None
        )
        attempt = 0
        while True:
            headers, token = await self._headers(extra_headers)
            if signal is not None and signal.is_set():
                raise McpAbortError()
            request = self._client.build_request(method, self.url, headers=headers, content=body)
            response = await self._client.send(request, stream=True)
            if attempt > 0 or on_unauthorized is None or not _needs_authorization(response):
                return response
            raw_body = (await response.aread())[:MAX_ERROR_BODY_BYTES]
            context = UnauthorizedContext(
                status=response.status_code,
                server_url=str(self.url),
                www_authenticate=response.headers.get("www-authenticate"),
                body=raw_body.decode("utf-8", errors="replace"),
                token=token,
                # 对应上游 `fetch: this.fetch`：OAuth 流程复用同一个客户端。
                client=self._client,
            )
            await _discard(response)
            await on_unauthorized(context)
            attempt += 1

    async def _headers(
        self, extra: dict[str, str] | None = None
    ) -> tuple[dict[str, str], str | None]:
        provider = self.options.auth_provider
        token = await provider.token() if provider is not None else None
        self._last_token = token
        return self._build_headers(extra or {}, token), token

    def _build_headers(self, extra: dict[str, str], token: str | None) -> dict[str, str]:
        headers = dict(self.options.headers)
        headers.update(extra)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _capture_session(self, response: httpx.Response) -> None:
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

    async def _check_response(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        raw = (await response.aread())[:MAX_ERROR_BODY_BYTES]
        with contextlib.suppress(Exception):
            await response.aclose()
        body = raw.decode("utf-8", errors="replace")
        if response.status_code == 401:
            raise McpAuthRequiredError(response.headers.get("www-authenticate"), body)
        if response.status_code == 404 and self._session_id:
            raise McpSessionExpiredError(body)
        raise McpHttpError(
            response.status_code, _describe_http_failure(response.status_code, body), body
        )

    # ---- SSE ----

    async def _consume_sse(
        self,
        response: httpx.Response,
        cursor: _StreamCursor,
        on_message: Callable[[JsonRpcMessage], None] | None = None,
    ) -> None:
        def handle_event(event: SseEvent) -> None:
            cursor.received = True
            # 没有 data 的事件用于预热续传；其它事件类型不是 JSON-RPC。
            if not event.data.strip() or (event.event is not None and event.event != "message"):
                return
            try:
                message = parse_json_rpc_message(json.loads(event.data))
            except Exception as error:  # noqa: BLE001 - 坏事件只上报
                self.emit_error(error)
                return
            if on_message is not None:
                on_message(message)
            self._emit_message(message)

        self._open_responses.add(response)
        try:

            def handle_id(value: str) -> None:
                cursor.last_event_id = value

            def handle_retry(delay: int) -> None:
                cursor.retry_ms = delay

            await consume_sse_stream(
                response.aiter_bytes(),
                ConsumeSseOptions(
                    on_event=handle_event,
                    max_event_bytes=self.options.max_message_bytes,
                    on_id=handle_id,
                    on_retry=handle_retry,
                ),
            )
        finally:
            self._open_responses.discard(response)
            with contextlib.suppress(Exception):
                await response.aclose()

    async def _consume_response_stream(
        self, response: httpx.Response, request_id: JsonRpcId
    ) -> None:
        """读取答复某条请求的 SSE 流。

        流在答复到达前结束或断开、且服务端分配了事件 id 时，用 ``GET`` + ``Last-Event-ID``
        续读 —— 服务端可以随时关闭响应流。否则只让这一条请求失败。
        """
        cursor = _StreamCursor()
        answered = False

        def on_message(message: JsonRpcMessage) -> None:
            nonlocal answered
            if is_json_rpc_response(message) and message["id"] == request_id:
                answered = True

        current: httpx.Response | None = response
        failure: BaseException | None = None
        attempt = 0
        while True:
            if current is not None:
                try:
                    await self._consume_sse(current, cursor, on_message)
                    failure = None
                except Exception as error:  # noqa: BLE001 - 断流即失败，按需重连
                    failure = error
            if answered or self._closed:
                return
            if failure is not None and not self._is_retryable(failure):
                break
            if cursor.last_event_id is None or attempt >= self._max_retries():
                break
            if cursor.received:
                attempt = 0
            cursor.received = False
            if not await self._sleep(self._reconnect_delay(attempt, cursor.retry_ms)):
                return
            attempt += 1
            try:
                current = await self._open_sse_stream(cursor.last_event_id)
            except Exception as error:  # noqa: BLE001 - 重开失败按需重试
                failure = error
                if not self._is_retryable(error):
                    break
                current = None
        if self._closed:
            return
        reason = "stream ended without a response" if failure is None else str(to_error(failure))
        self._emit_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": INTERNAL_ERROR,
                    "message": f"MCP response stream failed: {reason}",
                },
            }
        )

    def _start_get_stream(self) -> None:
        if not self.options.open_get_stream or self._get_stream_started or self._closed:
            return
        self._get_stream_started = True
        self._track(self._run_get_stream())

    async def _run_get_stream(self) -> None:
        """保持服务端→客户端流常开，断开时按退避重连。"""
        cursor = _StreamCursor()
        attempt = 0
        while not self._closed:
            try:
                current = await self._open_sse_stream(cursor.last_event_id)
                # 服务端不提供 GET 流。
                if current is None:
                    return
                opened_at = asyncio.get_running_loop().time()
                await self._consume_sse(current, cursor)
                # 存活一段时间的流算健康，即使它一直空闲。
                if cursor.received or (
                    asyncio.get_running_loop().time() - opened_at > self._max_delay() / 1000
                ):
                    attempt = 0
            except Exception as error:  # noqa: BLE001 - 断流按可重试性分类
                if self._closed:
                    return
                if not self._is_retryable(error):
                    self.emit_error(error)
                    return
            cursor.received = False
            if attempt >= self._max_retries():
                self.emit_error(
                    Exception("MCP server-to-client stream dropped and could not be reopened")
                )
                return
            if not await self._sleep(self._reconnect_delay(attempt, cursor.retry_ms)):
                return
            attempt += 1

    async def _open_sse_stream(self, last_event_id: str | None) -> httpx.Response | None:
        """打开一个 GET SSE 流。服务端答 405（不提供 GET 流）时返回 ``None``。"""
        extra = {"accept": "text/event-stream"}
        if last_event_id is not None:
            extra["last-event-id"] = last_event_id
        response = await self._authorized_fetch("GET", extra)
        if response.status_code == 405:
            await _discard(response)
            return None
        await self._check_response(response)
        self._capture_session(response)
        kind = _content_type(response)
        if kind != "text/event-stream":
            await _discard(response)
            raise McpHttpError(
                response.status_code,
                f"Unsupported MCP GET response content type: {kind or 'missing'}",
            )
        return response

    def _is_retryable(self, error: BaseException) -> bool:
        """网络故障与瞬时状态码可重试；认证、会话与协议错误不可。"""
        if isinstance(error, McpHttpError):
            return _is_transient_status(error.status)
        # httpx 把网络故障（含读到一半连接被断）报成 TransportError / TimeoutException。
        return isinstance(error, (httpx.TransportError, httpx.TimeoutException))

    def _reconnect_delay(self, attempt: int, server_delay_ms: int | None) -> int:
        if server_delay_ms is not None:
            return server_delay_ms
        initial: int = int(self.options.reconnect.initial_delay_ms)
        # ``2**attempt`` 在 mypy 里是 Any（int.__pow__ 的 stub 过宽），显式收敛回 int。
        return min(int(initial * 2**attempt), self._max_delay())

    def _max_delay(self) -> int:
        return int(self.options.reconnect.max_delay_ms)

    def _max_retries(self) -> int:
        return self.options.reconnect.max_retries

    async def _sleep(self, ms: float) -> bool:
        """等待；传输在等待期间关闭时返回 ``False``。"""
        if self._closed:
            return False
        try:
            await asyncio.wait_for(self._closed_event.wait(), timeout=ms / 1000)
        except TimeoutError:
            return True
        return False

    def _track(self, coro: Any) -> None:
        """后台任务的登记与回收（对应上游 ``void this.run...()``）。"""
        task = asyncio.ensure_future(coro)
        self._streams.add(task)
        task.add_done_callback(self._streams.discard)


__all__ = [
    "DEFAULT_RECONNECT_INITIAL_DELAY_MS",
    "DEFAULT_RECONNECT_MAX_DELAY_MS",
    "DEFAULT_RECONNECT_MAX_RETRIES",
    "MAX_ERROR_BODY_BYTES",
    "ConsumeSseOptions",
    "McpAuthRequiredError",
    "McpHttpError",
    "McpSessionExpiredError",
    "SseEvent",
    "StreamableHttpReconnectOptions",
    "StreamableHttpTransport",
    "StreamableHttpTransportOptions",
    "consume_sse_stream",
]
