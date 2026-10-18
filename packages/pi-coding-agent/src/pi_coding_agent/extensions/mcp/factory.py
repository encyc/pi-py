"""MCP 扩展的工厂：6 个生命周期钩子 + ``create_mcp_extension``。

对应上游 ``extensions/mcp/index.ts`` 的 **1114–1259 行**（6 个 ``pi.on(...)``）与
**643–655 行**（``reportProblems``，钩子唯一消费的 server 动作），外加 ``279–341`` 的闭包状态。

**不移植**（消费路径判定，见 ``PORTING.md`` 的〈越界判断是双向的〉）：

* ``index.ts`` 787–1011 —— Manager（``/mcp`` in the TUI）：依赖 TUI 渲染宿主。
* ``index.ts`` 1016–1113 —— ``formatStatus`` / ``pickServer`` / ``loginCommand``：**消费点只有**
  ``/mcp`` 命令处理器（``index.ts:1289/1300/1305/1317/1301``）与 Manager，且需要
  ``ctx.ui.notify`` / ``ctx.ui.select`` / ``showMcpManager``（本包无 UI 宿主）。
* ``index.ts`` 1260–1346 —— ``pi.registerCommand("mcp")``：CLI。
* ``index.ts`` 656–783 的 6 个 server 动作（``saveConfig`` / ``signIn`` / ``signOut`` /
  ``reconnect`` / ``setEnabled`` / ``setExposure``）：消费点同上（Manager 与 ``/mcp`` 命令），
  属动作层而非钩子层，本包无消费者 → 未移植；``reportProblems`` 例外，它被三个钩子调用。

**有意偏离**

* **钩子签名合一**：上游 ``pi.on(name, (event, ctx) => …)`` 是两个参数；本仓宿主的
  :meth:`ToolRegistry.emit` 只传一个 payload，故把 ``ctx`` 放进事件对象
  （:class:`AgentStartPayload` / :class:`ToolCallPayload`），其余四个事件直接以 ``ctx`` 为
  payload。这是**失效类偏离**：照抄两参数签名会直接 ``TypeError``。
* **``reportProblems`` 去掉呈现、保留判定**（与 ``ensure_discovery_active`` 同口径）：
  上游 ``ctx.ui.notify(...)``（``index.ts:651``）改为**返回**该文本，判定本身完整计算并可测。
* **``session.signal`` → ``asyncio.Event``**（本仓约定，E5）；上游初始为**已中止**
  （``index.ts:318``），本端同样初始置位，由 ``session_start`` 换成新的。
* **``setImmediate`` → 让出一轮事件循环**（``index.ts:1140``）：先让首帧渲染发生，再加载并连接。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ...extensions.types import ExtensionAPI, ExtensionContext, ExtensionFactory
from ...mcp_servers import mcp_namespace
from .config import LoadedMcpConfig, McpServerEntry
from .index import (
    DEFAULT_STARTUP_WAIT_MS,
    MCP_SERVERS_SECTION,
    RESOURCE_TOOL_NAMES,
    McpExtensionOptions,
    McpServer,
    McpServerListing,
    default_load_config,
    describe_state,
    has_direct_tools,
    is_enabled,
    reaches_server,
    render_servers_section,
)
from .lifecycle import McpConnectionLifecycle
from .oauth import McpOAuthCredentialStore
from .registration import McpToolRegistration
from .runtime import McpServerConnection, create_default_transport

__all__ = [
    "AgentStartPayload",
    "McpExtension",
    "ToolCallPayload",
    "create_mcp_extension",
]


@dataclass
class AgentStartPayload:
    """``before_agent_start`` 的 payload（上游 ``(event, ctx)`` 的合一）。

    ``system_prompt_options`` 需要暴露一个可改写的 ``sections`` 映射
    （上游 ``event.systemPromptOptions.sections``）。
    """

    system_prompt_options: Any
    ctx: ExtensionContext


@dataclass
class ToolCallPayload:
    """``tool_call`` 的 payload（上游 ``(event, ctx)`` 的合一）。"""

    tool_name: str
    input: Any
    ctx: ExtensionContext


@dataclass
class McpExtension:
    """一个会话内的 MCP 扩展实例。

    持有上游 ``createMcpExtension`` 闭包里的全部可变状态，并把注册器（C3b-2a）、
    连接生命周期（C3b-2b-α）与 6 个钩子接在一起。
    """

    options: McpExtensionOptions = field(default_factory=McpExtensionOptions)

    #: 当前 server 列表（配置的 + 扩展注册的）。
    servers: list[McpServer] = field(default_factory=list)
    configured_entries: list[McpServerEntry] = field(default_factory=list)
    config_errors: list[str] = field(default_factory=list)
    #: 受信项目的 ``mcp.json`` 路径。
    project_config: str | None = None
    #: 被 ``mcp.json`` 覆盖的注册 server 的说明行。
    overridden: list[str] = field(default_factory=list)
    auto_enable_codemode: bool = True
    #: 启动连接的整体 promise（上游 ``pending``）。
    pending: asyncio.Task[None] | None = None
    #: 首次 prompt 是否已经等过启动连接。
    waited_for_startup: bool = False
    session_cwd: str = ""
    credentials: McpOAuthCredentialStore | None = None
    model_registry: Any = None
    server_log: Any = None
    #: 当前会话的存活信号；**初始即为已中止**（上游 ``index.ts:318``）。
    session_aborted: asyncio.Event = field(default_factory=asyncio.Event)
    #: 由 ``track()`` 登记的后台工作，关闭时等它们收尾。
    background_actions: set[asyncio.Task[Any]] = field(default_factory=set)
    listeners: list[Callable[[], None]] = field(default_factory=list)

    _registration: McpToolRegistration | None = field(default=None, repr=False)
    _lifecycle: McpConnectionLifecycle | None = field(default=None, repr=False)
    _pi: ExtensionAPI | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.session_aborted.set()

    # ---- 状态与通知 ----

    @property
    def startup_wait_ms(self) -> int:
        """首个 prompt 的等待上限（毫秒）。"""
        return self.options.startup_wait_ms or DEFAULT_STARTUP_WAIT_MS

    def emit_change(self) -> None:
        """通知订阅者状态变了（上游 ``emitChange``）。"""
        for listener in list(self.listeners):
            listener()

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """订阅状态变化，返回取消订阅函数。"""
        self.listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self.listeners:
                self.listeners.remove(listener)

        return _unsubscribe

    def track(self, work: Awaitable[Any]) -> asyncio.Task[Any]:
        """登记后台工作，使 ``session_shutdown`` 能等它收尾（上游 ``track``）。"""
        task = asyncio.ensure_future(work)
        self.background_actions.add(task)
        task.add_done_callback(self.background_actions.discard)
        return task

    def connections(self) -> list[McpServerConnection]:
        """当前有连接的 server 的连接对象。"""
        return [server.connection for server in self.servers if server.connection is not None]

    def find_server(self, name: str) -> McpServer | None:
        """按名字找 server。"""
        for server in self.servers:
            if server.entry.name == name:
                return server
        return None

    def registered_servers(self) -> tuple[list[McpServer], list[str]]:
        """扩展注册的 server（``mcp.json`` 里定义的同名项优先）。

        返回 ``(servers, overridden)``。
        """
        if self._pi is None:
            return [], []
        registered: list[McpServer] = []
        overridden_names: list[str] = []
        for item in self._pi.get_mcp_servers():
            name = item.name
            configured = next(
                (
                    entry
                    for entry in self.configured_entries
                    if mcp_namespace(entry.name) == mcp_namespace(name)
                ),
                None,
            )
            if configured is not None:
                overridden_names.append(
                    f'"{name}" registered by {item.extension_path} is overridden by '
                    f'"{configured.name}" in {configured.source}'
                )
                continue
            registered.append(
                McpServer(
                    entry=McpServerEntry(
                        name=name,
                        config=item.config,
                        source=item.extension_path,
                        scope="extension",
                    ),
                    registered_config=_json_of(item.config),
                )
            )
        return registered, overridden_names

    # ---- reportProblems（钩子唯一消费的 server 动作） ----

    def report_problems(self, only: list[McpServer] | None = None) -> str | None:
        """需要用户注意的 server 汇总成一段文本。

        上游 ``index.ts:643-655`` 在这里调用 ``ctx.ui.notify``；本包无 UI 宿主，改为**返回**
        文本（判定完整保留）。返回 ``None`` 表示没有需要报告的问题。
        """
        lines = [] if only is not None else [f"config: {error}" for error in self.config_errors]
        for server in only if only is not None else self.servers:
            state = server.connection.state if server.connection is not None else None
            if state in ("needs-auth", "failed"):
                lines.append(f"{server.entry.name}: {describe_state(server)}")
        if not lines:
            return None
        body = "\n".join(f"  {line}" for line in lines)
        return f"MCP servers need attention:\n{body}\nRun /mcp to fix."

    # ---- 装配 ----

    def install(self, pi: ExtensionAPI) -> None:
        """把扩展装到宿主上（注册 6 个钩子）。"""
        self._pi = pi
        registration = McpToolRegistration(
            pi=pi,
            find_server=self.find_server,
            servers=lambda: self.servers,
            resource_servers=lambda: [
                server.connection
                for server in self.servers
                if server.connection is not None and server.connection.has_resources
            ],
            auto_enable_codemode=lambda: self.auto_enable_codemode,
        )
        self._registration = registration
        self._lifecycle = McpConnectionLifecycle(
            servers=lambda: self.servers,
            session_cwd=lambda: self.session_cwd,
            create_connection=self._create_connection,
            credentials=lambda: self.credentials,
            session_aborted=lambda: self.session_aborted.is_set(),
            emit_change=self.emit_change,
            on_tools=registration.register_tools,
            ensure_discovery_active=registration.ensure_discovery_active,
        )
        pi.on("session_start", self.on_session_start)
        pi.on("before_agent_start", self.on_before_agent_start)
        pi.on("tool_call", self.on_tool_call)
        pi.on("turn_start", self.on_turn_start)
        pi.on("mcp_servers_change", self.on_mcp_servers_change)
        pi.on("session_shutdown", self.on_session_shutdown)

    async def _create_connection(
        self, server: McpServer, is_current: Callable[[], bool]
    ) -> McpServerConnection | None:
        """建 server 的连接（上游 ``createConnection``）。"""
        if not is_current():
            return None
        registration = self._registration

        def _on_tools(conn: McpServerConnection) -> None:
            if is_current() and registration is not None:
                registration.register_tools(conn)

        connection = McpServerConnection(
            entry=server.entry,
            cwd=self.session_cwd,
            create_transport=self.options.create_transport or create_default_transport,  # type: ignore[arg-type]
            credentials=self._credentials_store(),
            on_tools=_on_tools,
            provider_token=self._provider_token,
            on_change=lambda conn: self._on_connection_change(conn, is_current),
            log=self.server_log,
        )
        server.connection = connection
        self.emit_change()
        return connection

    async def _provider_token(self, provider: str) -> str | None:
        if self.model_registry is None:
            return None
        getter = getattr(self.model_registry, "get_api_key_for_provider", None)
        if getter is None:
            return None
        result = getter(provider)
        if inspect.isawaitable(result):
            awaited: Any = await result
            return None if awaited is None else str(awaited)
        return None if result is None else str(result)

    def _on_connection_change(
        self, connection: McpServerConnection, is_current: Callable[[], bool]
    ) -> None:
        if self._lifecycle is None:
            return
        if is_current():
            self._lifecycle.on_connection_change(connection)
        else:
            self._lifecycle.tokens_at_sign_in.pop(id(connection), None)

    def _credentials_store(self) -> McpOAuthCredentialStore:
        if self.credentials is None:
            self.credentials = self.options.credentials or McpOAuthCredentialStore()
        return self.credentials

    # ---- 6 个钩子（上游 1114–1258） ----

    async def on_session_start(self, ctx: ExtensionContext) -> None:
        """``session_start``（上游 1114–1151）。"""
        loader = self.options.load_config or default_load_config
        loaded: LoadedMcpConfig = loader(ctx)
        self.config_errors = list(loaded.errors)
        self.project_config = loaded.project_config
        self.auto_enable_codemode = (
            loaded.auto_enable_codemode if loaded.auto_enable_codemode is not None else True
        )
        self.waited_for_startup = False
        self.session_cwd = ctx.cwd
        self.model_registry = ctx.model_registry
        self.session_aborted = asyncio.Event()
        self.configured_entries = list(loaded.servers)
        registered, overridden = self.registered_servers()
        self.overridden = overridden
        self.servers = [*[McpServer(entry=entry) for entry in loaded.servers], *registered]
        self.emit_change()
        # 从配置就把 codemode / tool_search 打开：首个 prompt 不等「工具不向模型声明」的 server。
        if self._registration is not None:
            self._registration.ensure_discovery_active()
        enabled = [server for server in self.servers if is_enabled(server)]
        if not enabled:
            self.report_problems()
            return
        # 只有在真的有 server 时才加载 MCP 客户端；让出一轮事件循环，使首帧先渲染。
        await asyncio.sleep(0)
        self.pending = self.track(self._start_all(enabled))

    async def _start_all(self, enabled: list[McpServer]) -> None:
        lifecycle = self._lifecycle
        if lifecycle is None:
            return
        # 先同步发起全部连接（上游同样先设置 server.ready），再等它们落定。
        attempts = [lifecycle.start(server) for server in enabled]
        with contextlib.suppress(Exception):
            await asyncio.gather(*attempts)
        if not self.session_aborted.is_set():
            self.report_problems()

    async def on_before_agent_start(self, payload: AgentStartPayload) -> None:
        """``before_agent_start``（上游 1178–1184）。"""
        await self._wait_for_direct_servers()
        sections = getattr(payload.system_prompt_options, "sections", None)
        if sections is None:
            return
        section = render_servers_section([_listing_of(server) for server in self.servers])
        if section:
            sections[MCP_SERVERS_SECTION] = section
        else:
            sections.pop(MCP_SERVERS_SECTION, None)

    async def _wait_for_direct_servers(self) -> None:
        """首个 prompt 等「声明了 direct 工具」的 server（上游 1156–1174）。

        超时后不再等：慢或挂住的 server 不能拖住 prompt。
        """
        if self.waited_for_startup:
            return
        self.waited_for_startup = True
        ready = [
            server.ready
            for server in self.servers
            if is_enabled(server) and has_direct_tools(server.entry) and server.ready is not None
        ]
        if not ready:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*ready, return_exceptions=True), self.startup_wait_ms / 1000
            )

    async def on_tool_call(self, payload: ToolCallPayload) -> None:
        """``tool_call``（上游 1191–1209）：等这个工具可能触及的 server。"""
        if self._pi is None or self._lifecycle is None:
            return
        tool = next(
            (
                candidate
                for candidate in self._pi.get_all_tools()
                if candidate.name == payload.tool_name
            ),
            None,
        )
        if tool is None:
            return
        ready_servers = [
            server for server in self.servers if is_enabled(server) and server.ready is not None
        ]
        if not ready_servers:
            return
        reaches = getattr(tool, "reaches_tools", None)
        reach = reaches(payload.input) if reaches is not None else None
        if reach is not None:
            waiting = [
                server for server in ready_servers if reaches_server(reach, server.entry.name)
            ]
        elif tool.name in RESOURCE_TOOL_NAMES:
            waiting = ready_servers
        else:
            owner = None
            if self._registration is not None:
                assigned = self._registration.tool_owners.get(payload.tool_name)
                owner = assigned.split("\0", 1)[0] if assigned else None
            waiting = [server for server in ready_servers if server.entry.name == owner]
        await self._lifecycle.wait_for_servers(waiting, None)

    async def on_turn_start(self, ctx: ExtensionContext) -> None:
        """``turn_start``（上游 1212–1214）：捡起会话外完成的登录。"""
        if self._lifecycle is not None and self._lifecycle.tokens_at_sign_in:
            await self._lifecycle.reconnect_signed_in()

    async def on_mcp_servers_change(self, ctx: ExtensionContext) -> None:
        """``mcp_servers_change``（上游 1217–1250）：注册/注销的 server 立即连/断。"""
        if self.session_aborted.is_set() or self._registration is None or self._lifecycle is None:
            return
        registered, overridden = self.registered_servers()
        self.overridden = overridden
        next_by_name = {server.entry.name: server for server in registered}
        removed = [
            server
            for server in self.servers
            if server.entry.scope == "extension"
            and (
                next_by_name.get(server.entry.name) is None
                or next_by_name[server.entry.name].registered_config != server.registered_config
            )
        ]
        self.servers = [server for server in self.servers if server not in removed]
        for server in removed:
            self._registration.hide_tools(server.entry.name)
        added = [server for server in registered if self.find_server(server.entry.name) is None]
        self.servers.extend(added)
        self.emit_change()
        self._registration.ensure_discovery_active()
        await asyncio.gather(
            *(server.connection.close() for server in removed if server.connection is not None),
            return_exceptions=True,
        )
        connecting = [server for server in added if is_enabled(server)]
        if self.session_aborted.is_set() or not connecting:
            return
        try:
            await asyncio.gather(*(self._lifecycle.start(server) for server in connecting))
        except Exception:  # noqa: BLE001 - 与上游一致：加载失败只报告
            return
        if self.session_aborted.is_set():
            await asyncio.gather(
                *(
                    server.connection.close()
                    for server in connecting
                    if server.connection is not None
                ),
                return_exceptions=True,
            )
            return
        self.report_problems(connecting)

    async def on_session_shutdown(self, ctx: ExtensionContext) -> None:
        """``session_shutdown``（上游 1252–1258）：停会话、清 server、等后台工作收尾。"""
        self.session_aborted.set()
        closing = self.connections()
        self.servers = []
        self.emit_change()
        pending = [*self.background_actions]
        await asyncio.gather(
            *pending,
            *(connection.close() for connection in closing),
            return_exceptions=True,
        )


def _listing_of(server: McpServer) -> McpServerListing:
    """``McpServer`` → ``McpServerListing``（``render_servers_section`` 的入参）。"""
    return McpServerListing(entry=server.entry, connection=server.connection)


def _json_of(config: Any) -> str:
    """扩展注册的配置快照，用来发现「同名但配置变了」的重复注册。"""
    return json.dumps(config, sort_keys=True, default=str)


def create_mcp_extension(options: McpExtensionOptions | None = None) -> ExtensionFactory:
    """把 MCP 集成装成扩展（上游 ``createMcpExtension``，``index.ts:279``）。

    用法与其它内置扩展一致::

        registry.install(create_mcp_extension())
    """
    extension = McpExtension(options=options or McpExtensionOptions())

    def factory(pi: ExtensionAPI) -> None:
        extension.install(pi)

    return factory
