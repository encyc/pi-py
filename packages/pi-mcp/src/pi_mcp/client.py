"""MCP 客户端：初始化、请求/通知、分页列表、工具调用与取消。

对应上游 ``packages/mcp/src/client.ts``（v1.3.0，621 行）。

关键语义（与上游一致）：

- ``connect()`` 先跑 ``initialize`` 握手，再发 ``notifications/initialized``；期间只允许
  握手自身的请求（``allow_connecting``）。
- 每个请求有 id（从 1 单调递增）、可选超时、可选 ``asyncio.Event`` 取消信号、可选进度回调。
  进度通知会**重置超时**。
- 取消/超时会给服务端发 ``notifications/cancelled`` —— **但 ``initialize`` 除外**（规范禁止取消它）。
- 请求结束时置位该请求的 ``sendController``，传输据此不再投递（例如授权刷新之后的重试）。
- 传输错误只上报，不使在途请求失败；传输关闭才让在途请求全部失败。
- 服务端发来的 ``ping`` 与 ``roots/list`` 由内建处理器应答。

偏离（见 ``PORTING.md``）：``AbortSignal`` → ``asyncio.Event``（本仓约定）；上游
``signal.reason`` → ``McpRequestOptions.cancel_reason``。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from .protocol.content import CallToolResult
from .protocol.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    JsonRpcErrorObject,
    JsonRpcId,
    JsonRpcMessage,
    JsonRpcRequest,
    JsonRpcResponse,
    McpAbortError,
    McpConnectionClosedError,
    McpError,
    McpTimeoutError,
    is_json_rpc_id,
    is_json_rpc_notification,
    is_json_rpc_request,
    is_json_rpc_response,
    is_object,
    to_error,
)
from .protocol.types import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    ClientCapabilities,
    Implementation,
    InitializeResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ProgressNotification,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    Root,
    ServerCapabilities,
    SupportedProtocolVersion,
    Tool,
)
from .transports.transport import McpTransport, McpTransportSendOptions

DEFAULT_REQUEST_TIMEOUT_MS = 30_000
MAX_LIST_PAGES = 1_000

#: 客户端连接状态。
ClientState = Literal["idle", "connecting", "connected", "closed"]

NotificationListener = Callable[[Any], None]
ErrorListener = Callable[[Exception], None]
CloseListener = Callable[[], None]
#: 服务端→客户端请求的处理器。可以是同步函数，也可以是协程函数
#: （上游 ``RequestHandler`` 返回 ``unknown | Promise<unknown>``）。
RequestHandler = Callable[[Any, asyncio.Event], Any]


async def _maybe_await(value: Any) -> Any:
    """同步/异步统一处理（对应上游对 ``unknown | Promise<unknown>`` 的直接 ``await``）。"""
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class McpClientOptions:
    """``McpClient`` 的配置。对应上游 ``McpClientOptions``（``extends Implementation``）。"""

    name: str
    version: str
    title: str | None = None
    capabilities: ClientCapabilities = field(default_factory=dict)  # type: ignore[assignment]
    protocol_version: SupportedProtocolVersion = LATEST_PROTOCOL_VERSION
    request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS
    #: 客户端能提供的根目录；可以是值，也可以是取值函数（sync 或 async）。
    roots: list[Root] | Callable[[], list[Root] | Awaitable[list[Root]]] | None = None


@dataclass
class McpRequestOptions:
    """单次请求的选项。"""

    #: 取消信号（本仓约定：``asyncio.Event``）。
    signal: asyncio.Event | None = None
    timeout_ms: int | None = None
    on_progress: Callable[[ProgressNotification], None] | None = None
    #: 取消时随 ``notifications/cancelled`` 发给服务端的原因（上游取 ``signal.reason``）。
    cancel_reason: str = "Aborted"


@dataclass
class _PendingRequest:
    resolve: asyncio.Future[Any]
    timeout_ms: int
    signal: asyncio.Event | None
    send_signal: asyncio.Event
    cancellable: bool
    on_progress: Callable[[ProgressNotification], None] | None
    progress_token: JsonRpcId | None
    timer: asyncio.TimerHandle | None = None
    watcher: asyncio.Task[None] | None = None


def _validate_initialize_result(value: object) -> InitializeResult:
    if (
        not is_object(value)
        or not isinstance(value.get("protocolVersion"), str)
        or not is_object(value.get("capabilities"))
        or not is_object(value.get("serverInfo"))
        or not isinstance(value["serverInfo"].get("name"), str)
        or not isinstance(value["serverInfo"].get("version"), str)
        or ("instructions" in value and not isinstance(value.get("instructions"), str))
    ):
        raise McpError(INVALID_REQUEST, "Invalid MCP initialize result")
    return value  # type: ignore[return-value]


def _invalid(message: str) -> McpError:
    return McpError(INVALID_REQUEST, message)


def _validate_list_page(
    method: str,
    key: str,
    value: object,
    is_item: Callable[[dict[str, Any]], bool],
) -> tuple[list[dict[str, Any]], str | None]:
    """分页列表的一页：``key`` 下的条目，逐个用 ``is_item`` 校验。"""
    items = value.get(key) if is_object(value) else None
    if not is_object(value) or not isinstance(items, list):
        raise _invalid(f"Invalid MCP {method} result")
    for item in items:
        if not is_object(item) or not is_item(item):
            raise _invalid(f"Invalid entry in MCP {method} result")
    # 有些服务端用 null 或 "" 结束分页，而不是省略 cursor。
    next_cursor = value.get("nextCursor")
    if next_cursor is None or next_cursor == "":
        next_cursor = None
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise _invalid(f"Invalid MCP {method} cursor")
    return items, next_cursor


def _is_tool(tool: dict[str, Any]) -> bool:
    return isinstance(tool.get("name"), str) and is_object(tool.get("inputSchema"))


def _is_resource(resource: dict[str, Any]) -> bool:
    # ``name`` 规范要求必填，但有服务端省略；用 uri 顶上。
    return isinstance(resource.get("uri"), str) and (
        "name" not in resource or isinstance(resource.get("name"), str)
    )


def _is_resource_template(template: dict[str, Any]) -> bool:
    return isinstance(template.get("uriTemplate"), str) and (
        "name" not in template or isinstance(template.get("name"), str)
    )


def _to_resource(item: dict[str, Any]) -> Resource:
    if not isinstance(item.get("name"), str):
        item = {**item, "name": item.get("uri")}
    return item  # type: ignore[return-value]


def _to_resource_template(item: dict[str, Any]) -> ResourceTemplate:
    if not isinstance(item.get("name"), str):
        item = {**item, "name": item.get("uriTemplate")}
    return item  # type: ignore[return-value]


def _validate_read_resource_result(value: object) -> ReadResourceResult:
    if not is_object(value) or not isinstance(value.get("contents"), list):
        raise _invalid("Invalid MCP resources/read result")
    for contents in value["contents"]:
        if (
            not is_object(contents)
            or not isinstance(contents.get("uri"), str)
            or (
                not isinstance(contents.get("text"), str)
                and not isinstance(contents.get("blob"), str)
            )
        ):
            raise _invalid("Invalid contents in MCP resources/read result")
    return value  # type: ignore[return-value]


def _validate_call_tool_result(value: object) -> CallToolResult:
    """``content`` 规范要求必填，但只返回 ``structuredContent`` 的服务端会省略它。"""
    content = value.get("content") if is_object(value) else None
    if not is_object(value) or ("content" in value and not isinstance(content, list)):
        raise McpError(INVALID_REQUEST, "Invalid MCP tools/call result")
    if "content" not in value:
        value = {**value, "content": []}
    return value  # type: ignore[return-value]


class McpClient:
    """MCP 客户端。"""

    def __init__(self, options: McpClientOptions) -> None:
        self.options = options
        self._state: ClientState = "idle"
        self._transport: McpTransport | None = None
        self._next_request_id = 1
        self._server_info: Implementation | None = None
        self._server_capabilities: ServerCapabilities | None = None
        self._instructions: str | None = None
        self._protocol_version: str | None = None
        self._pending: dict[JsonRpcId, _PendingRequest] = {}
        self._progress_requests: dict[JsonRpcId, JsonRpcId] = {}
        self._incoming: dict[JsonRpcId, asyncio.Event] = {}
        self._request_handlers: dict[str, RequestHandler] = {}
        self._notification_listeners: dict[str, list[NotificationListener]] = {}
        self._error_listeners: list[ErrorListener] = []
        self._close_listeners: list[CloseListener] = []
        self._disposers: list[Callable[[], None]] = []
        #: 在途的 ``transport.send()`` 任务。上游**不 await** send（只挂 ``.catch``），
        #: 因此「请求 promise」与「消息是否已投递」解耦 —— 取消一个正卡在授权刷新里的
        #: 请求时，调用方要能立刻拿到 rejection，而不是等 send 自己回来。
        self._send_tasks: set[asyncio.Task[None]] = set()

        self._request_handlers["ping"] = lambda params, signal: {}
        roots = options.roots
        if roots is not None:

            async def _list_roots(params: Any, signal: asyncio.Event) -> dict[str, Any]:
                resolved = roots() if callable(roots) else roots
                return {"roots": list(await _maybe_await(resolved))}

            self._request_handlers["roots/list"] = _list_roots

    # ---- 只读状态 ----

    @property
    def connection_state(self) -> ClientState:
        """连接状态。"""
        return self._state

    @property
    def server_info(self) -> Implementation | None:
        """服务端实现信息（``initialize`` 之后可用）。"""
        return self._server_info

    @property
    def server_capabilities(self) -> ServerCapabilities | None:
        """服务端能力。"""
        return self._server_capabilities

    @property
    def instructions(self) -> str | None:
        """服务端给的用法说明。"""
        return self._instructions

    @property
    def protocol_version(self) -> str | None:
        """协商出的协议版本。"""
        return self._protocol_version

    # ---- 连接 ----

    async def connect(self, transport: McpTransport) -> InitializeResult:
        """启动传输并完成 ``initialize`` 握手。"""
        if self._state != "idle":
            raise RuntimeError(f"Cannot connect MCP client in {self._state} state")
        self._state = "connecting"
        self._transport = transport
        self._disposers = [
            transport.on_message(self._handle_message),
            # 传输错误只上报；在途请求由传输关闭时统一失败。
            transport.on_error(self._emit_error),
            transport.on_close(self._handle_transport_close),
        ]

        try:
            await transport.start()
            capabilities: ClientCapabilities = {**self.options.capabilities}
            if self.options.roots is not None and "roots" not in capabilities:
                capabilities["roots"] = {}
            raw = await self._request_internal(
                "initialize",
                {
                    "protocolVersion": self.options.protocol_version,
                    "capabilities": capabilities,
                    "clientInfo": self._client_info(),
                },
                McpRequestOptions(),
                allow_connecting=True,
            )
            result = _validate_initialize_result(raw)
            if result["protocolVersion"] not in SUPPORTED_PROTOCOL_VERSIONS:
                raise RuntimeError(
                    f"MCP server selected unsupported protocol version {result['protocolVersion']}"
                )
            self._protocol_version = result["protocolVersion"]
            self._server_info = result["serverInfo"]
            self._server_capabilities = result["capabilities"]
            self._instructions = result.get("instructions")
            transport.set_protocol_version(result["protocolVersion"])
            await self._notify_internal("notifications/initialized", None, allow_connecting=True)
            self._state = "connected"
            return result
        except BaseException:
            # 关闭失败不掩盖原始错误
            with contextlib.suppress(Exception):
                await self.close()
            raise

    def _client_info(self) -> Implementation:
        info: Implementation = {"name": self.options.name, "version": self.options.version}
        if self.options.title is not None:
            info["title"] = self.options.title
        return info

    # ---- 请求 / 通知 / 处理器 ----

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        options: McpRequestOptions | None = None,
    ) -> Any:
        """发一条请求并等待结果。"""
        return await self._request_internal(method, params, options or McpRequestOptions(), False)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """发一条通知。"""
        await self._notify_internal(method, params, False)

    def set_request_handler(self, method: str, handler: RequestHandler) -> Callable[[], None]:
        """注册服务端→客户端请求的处理器，返回取消注册函数。"""
        self._request_handlers[method] = handler

        def _unregister() -> None:
            if self._request_handlers.get(method) is handler:
                del self._request_handlers[method]

        return _unregister

    def on_notification(self, method: str, listener: NotificationListener) -> Callable[[], None]:
        """订阅某个方法的通知，返回取消订阅函数。"""
        listeners = self._notification_listeners.setdefault(method, [])
        listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in listeners:
                listeners.remove(listener)
            if not listeners:
                self._notification_listeners.pop(method, None)

        return _unsubscribe

    def on_error(self, listener: ErrorListener) -> Callable[[], None]:
        """订阅错误，返回取消订阅函数。"""
        self._error_listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._error_listeners:
                self._error_listeners.remove(listener)

        return _unsubscribe

    def on_close(self, listener: CloseListener) -> Callable[[], None]:
        """订阅关闭（传输断开或 ``close()``），只回调一次。"""
        self._close_listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._close_listeners:
                self._close_listeners.remove(listener)

        return _unsubscribe

    # ---- 便捷方法 ----

    async def ping(self, options: McpRequestOptions | None = None) -> None:
        """发 ``ping``。"""
        await self.request("ping", None, options)

    async def list_tools(self, options: McpRequestOptions | None = None) -> list[Tool]:
        """列出全部工具（自动翻页）。"""
        items = await self._list_all(
            "tools/list", "tools", _is_tool, options or McpRequestOptions()
        )
        return [cast("Tool", item) for item in items]

    async def list_resources(self, options: McpRequestOptions | None = None) -> list[Resource]:
        """列出全部资源（自动翻页）。"""
        items = await self._list_all(
            "resources/list", "resources", _is_resource, options or McpRequestOptions()
        )
        return [_to_resource(item) for item in items]

    async def list_resources_page(
        self, cursor: str | None = None, options: McpRequestOptions | None = None
    ) -> ListResourcesResult:
        """资源列表的一页。"""
        items, next_cursor = await self._list_page(
            "resources/list", "resources", _is_resource, cursor, options or McpRequestOptions()
        )
        result: ListResourcesResult = {"resources": [_to_resource(item) for item in items]}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        return result

    async def list_resource_templates(
        self, options: McpRequestOptions | None = None
    ) -> list[ResourceTemplate]:
        """列出全部资源模板（自动翻页）。"""
        items = await self._list_all(
            "resources/templates/list",
            "resourceTemplates",
            _is_resource_template,
            options or McpRequestOptions(),
        )
        return [_to_resource_template(item) for item in items]

    async def list_resource_templates_page(
        self, cursor: str | None = None, options: McpRequestOptions | None = None
    ) -> ListResourceTemplatesResult:
        """资源模板列表的一页。"""
        items, next_cursor = await self._list_page(
            "resources/templates/list",
            "resourceTemplates",
            _is_resource_template,
            cursor,
            options or McpRequestOptions(),
        )
        result: ListResourceTemplatesResult = {
            "resourceTemplates": [_to_resource_template(item) for item in items]
        }
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        return result

    async def read_resource(
        self, uri: str, options: McpRequestOptions | None = None
    ) -> ReadResourceResult:
        """读一个资源。"""
        return _validate_read_resource_result(
            await self.request("resources/read", {"uri": uri}, options)
        )

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        options: McpRequestOptions | None = None,
    ) -> CallToolResult:
        """调用一个工具。"""
        params: dict[str, Any] = {"name": name}
        if args is not None:
            params["arguments"] = args
        return _validate_call_tool_result(await self.request("tools/call", params, options))

    async def close(self) -> None:
        """关闭客户端与传输。"""
        transport = self._transport
        self._transport = None
        self._dispose_transport_listeners()
        self._mark_closed(McpConnectionClosedError())
        if transport is not None:
            await transport.close()

    # ---- 分页 ----

    async def _list_page(
        self,
        method: str,
        key: str,
        is_item: Callable[[dict[str, Any]], bool],
        cursor: str | None,
        options: McpRequestOptions,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params = None if cursor is None else {"cursor": cursor}
        return _validate_list_page(
            method, key, await self.request(method, params, options), is_item
        )

    async def _list_all(
        self,
        method: str,
        key: str,
        is_item: Callable[[dict[str, Any]], bool],
        options: McpRequestOptions,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            page_items, next_cursor = await self._list_page(method, key, is_item, cursor, options)
            items.extend(page_items)
            if next_cursor is None:
                return items
            if next_cursor in cursors:
                raise RuntimeError(f"MCP {method} returned duplicate cursor: {next_cursor}")
            cursors.add(next_cursor)
            cursor = next_cursor
        raise RuntimeError(f"MCP {method} exceeded {MAX_LIST_PAGES} pages")

    # ---- 请求生命周期 ----

    async def _request_internal(
        self,
        method: str,
        params: dict[str, Any] | None,
        options: McpRequestOptions,
        allow_connecting: bool,
    ) -> Any:
        transport = self._require_transport(allow_connecting)
        if options.signal is not None and options.signal.is_set():
            raise McpAbortError()
        request_id: JsonRpcId = self._next_request_id
        self._next_request_id += 1
        progress_token = request_id if options.on_progress is not None else None
        request_params: dict[str, Any] | None = params
        if progress_token is not None:
            raw_meta = params.get("_meta") if params is not None else None
            meta: dict[str, Any] = dict(raw_meta) if is_object(raw_meta) else {}
            request_params = {
                **(params or {}),
                "_meta": {**meta, "progressToken": progress_token},
            }
        message: JsonRpcRequest = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if request_params is not None:
            message["params"] = request_params

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        entry = _PendingRequest(
            resolve=future,
            timeout_ms=options.timeout_ms or self.options.request_timeout_ms,
            signal=options.signal,
            send_signal=asyncio.Event(),
            # 规范禁止取消 initialize。
            cancellable=method != "initialize",
            on_progress=options.on_progress,
            progress_token=progress_token,
        )
        self._pending[request_id] = entry
        if progress_token is not None:
            self._progress_requests[progress_token] = request_id
        if options.signal is not None:
            entry.watcher = asyncio.create_task(
                self._watch_signal(request_id, options.signal, options.cancel_reason)
            )
        self._arm_timeout(request_id, entry)
        send_task = asyncio.ensure_future(
            self._send_request(transport, message, entry.send_signal, request_id)
        )
        self._send_tasks.add(send_task)
        send_task.add_done_callback(self._send_tasks.discard)
        return await future

    async def _send_request(
        self,
        transport: McpTransport,
        message: JsonRpcMessage,
        send_signal: asyncio.Event,
        request_id: JsonRpcId,
    ) -> None:
        """投递一条请求；失败即让该请求失败（对应上游 ``transport.send(...).catch(...)``）。"""
        try:
            await transport.send(message, McpTransportSendOptions(signal=send_signal))
        except Exception as error:  # noqa: BLE001 - 发送失败即该请求失败
            self._cancel_pending(request_id, error, notify_server=False)

    async def _watch_signal(
        self, request_id: JsonRpcId, signal: asyncio.Event, reason: str
    ) -> None:
        await signal.wait()
        entry = self._pending.get(request_id)
        self._cancel_pending(
            request_id, McpAbortError(), entry.cancellable if entry is not None else True, reason
        )

    async def _notify_internal(
        self,
        method: str,
        params: dict[str, Any] | None,
        allow_connecting: bool,
    ) -> None:
        notification: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            notification["params"] = params
        await self._require_transport(allow_connecting).send(cast("JsonRpcMessage", notification))

    def _require_transport(self, allow_connecting: bool) -> McpTransport:
        if self._transport is not None and (
            self._state == "connected" or (allow_connecting and self._state == "connecting")
        ):
            return self._transport
        raise McpConnectionClosedError(f"MCP client is {self._state}")

    # ---- 入站消息 ----

    def _handle_message(self, message: JsonRpcMessage) -> None:
        if is_json_rpc_response(message):
            self._handle_response(message)
            return
        if is_json_rpc_request(message):
            self._spawn(self._handle_request(message))
            return
        if is_json_rpc_notification(message):
            self._handle_notification(message["method"], message.get("params"))
            return
        self._emit_error(McpError(INVALID_REQUEST, "Received invalid JSON-RPC message"))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    def _handle_response(self, message: JsonRpcResponse) -> None:
        entry = self._pending.get(message["id"])
        if entry is None:
            self._emit_error(
                Exception(f"Received response for unknown MCP request {message['id']}")
            )
            return
        self._remove_pending(message["id"], entry)
        # 成功响应与错误响应是判别联合，按 ``error`` 键区分（上游同样是 `"error" in message`）。
        raw = cast("dict[str, Any]", message)
        error = raw.get("error")
        if is_object(error):
            entry.resolve.set_exception(
                McpError(error["code"], error["message"], error.get("data"))
            )
        else:
            entry.resolve.set_result(raw.get("result"))

    async def _handle_request(self, message: JsonRpcRequest) -> None:
        transport = self._transport
        if transport is None:
            return
        handler = self._request_handlers.get(message["method"])
        if handler is None:
            try:
                await transport.send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": METHOD_NOT_FOUND,
                            "message": f"Method not found: {message['method']}",
                        },
                    }
                )
            except Exception as error:  # noqa: BLE001 - 只上报
                self._emit_error(error)
            return
        cancel_event = asyncio.Event()
        self._incoming[message["id"]] = cancel_event
        try:
            result = await _maybe_await(handler(message.get("params"), cancel_event))
            await transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": result if result is not None else {},
                }
            )
        except Exception as error:  # noqa: BLE001 - 回错误响应
            response_error: JsonRpcErrorObject = (
                {"code": error.code, "message": str(error), "data": error.data}
                if isinstance(error, McpError)
                else {"code": INTERNAL_ERROR, "message": str(to_error(error))}
            )
            try:
                await transport.send(
                    {"jsonrpc": "2.0", "id": message["id"], "error": response_error}
                )
            except Exception as send_error:  # noqa: BLE001 - 只上报
                self._emit_error(send_error)
        finally:
            self._incoming.pop(message["id"], None)

    def _handle_notification(self, method: str, params: Any) -> None:
        if method == "notifications/progress":
            self._handle_progress(params)
        elif method == "notifications/cancelled":
            self._handle_cancelled(params)
        for listener in list(self._notification_listeners.get(method, [])):
            try:
                listener(params)
            except Exception as error:  # noqa: BLE001 - 监听器抛错只上报
                self._emit_error(error)

    def _handle_progress(self, params: Any) -> None:
        if not is_object(params) or not is_json_rpc_id(params.get("progressToken")):
            return
        if not isinstance(params.get("progress"), (int, float)):
            return
        progress_token = params["progressToken"]
        request_id = self._progress_requests.get(progress_token)
        entry = self._pending.get(request_id) if request_id is not None else None
        if request_id is None or entry is None:
            return
        self._arm_timeout(request_id, entry)
        if entry.on_progress is not None:
            try:
                entry.on_progress(params)  # type: ignore[arg-type]
            except Exception as error:  # noqa: BLE001 - 回调抛错只上报
                self._emit_error(error)

    def _handle_cancelled(self, params: Any) -> None:
        if is_object(params) and is_json_rpc_id(params.get("requestId")):
            request_id = params["requestId"]
            cancel_event = self._incoming.get(request_id)
            if cancel_event is not None:
                cancel_event.set()

    # ---- 超时 / 取消 / 关闭 ----

    def _arm_timeout(self, request_id: JsonRpcId, entry: _PendingRequest) -> None:
        if entry.timer is not None:
            entry.timer.cancel()
            entry.timer = None
        if entry.timeout_ms <= 0:
            return
        loop = asyncio.get_running_loop()
        entry.timer = loop.call_later(
            entry.timeout_ms / 1000,
            self._cancel_pending,
            request_id,
            McpTimeoutError(entry.timeout_ms),
            entry.cancellable,
            "Request timed out",
        )

    def _cancel_pending(
        self, request_id: JsonRpcId, error: object, notify_server: bool, reason: str | None = None
    ) -> None:
        entry = self._pending.get(request_id)
        if entry is None:
            return
        self._remove_pending(request_id, entry)
        if not entry.resolve.done():
            entry.resolve.set_exception(
                error if isinstance(error, BaseException) else Exception(str(error))
            )
        transport = self._transport
        if notify_server and transport is not None:
            notification: JsonRpcMessage = {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": request_id, **({"reason": reason} if reason else {})},
            }
            self._spawn(self._send_quietly(transport, notification))

    async def _send_quietly(self, transport: McpTransport, message: JsonRpcMessage) -> None:
        try:
            await transport.send(message)
        except Exception as error:  # noqa: BLE001 - 只上报
            self._emit_error(error)

    def _remove_pending(self, request_id: JsonRpcId, entry: _PendingRequest) -> None:
        self._pending.pop(request_id, None)
        if entry.timer is not None:
            entry.timer.cancel()
            entry.timer = None
        if entry.progress_token is not None:
            self._progress_requests.pop(entry.progress_token, None)
        if entry.watcher is not None:
            entry.watcher.cancel()
            entry.watcher = None
        entry.send_signal.set()

    def _reject_pending(self, error: BaseException) -> None:
        for request_id, entry in list(self._pending.items()):
            self._remove_pending(request_id, entry)
            if not entry.resolve.done():
                entry.resolve.set_exception(error)

    def _handle_transport_close(self) -> None:
        self._mark_closed(McpConnectionClosedError())

    def _mark_closed(self, error: Exception) -> None:
        """幂等：让在途请求失败、取消我们正在处理的服务端请求，并翻转状态。"""
        was_closed = self._state == "closed"
        self._state = "closed"
        self._reject_pending(error)
        for cancel_event in list(self._incoming.values()):
            cancel_event.set()
        self._incoming.clear()
        if was_closed:
            return
        for listener in list(self._close_listeners):
            try:
                listener()
            except Exception as listener_error:  # noqa: BLE001 - 只上报
                self._emit_error(listener_error)

    def _emit_error(self, error: object) -> None:
        normalized = to_error(error)
        for listener in list(self._error_listeners):
            listener(normalized)

    def _dispose_transport_listeners(self) -> None:
        for dispose in self._disposers:
            dispose()
        self._disposers = []


#: 客户端内部派发的后台任务（``notifications/cancelled``、服务端请求应答）。
#: 持有强引用，避免任务被 GC；完成即移除。
_background_tasks: set[asyncio.Task[Any]] = set()


__all__ = [
    "DEFAULT_REQUEST_TIMEOUT_MS",
    "MAX_LIST_PAGES",
    "ClientState",
    "CloseListener",
    "ErrorListener",
    "McpClient",
    "McpClientOptions",
    "McpRequestOptions",
    "NotificationListener",
    "RequestHandler",
]
