"""MCP 连接的生命周期管理。

对应上游 ``extensions/mcp/index.ts`` **543–645 行**（C3b-2b 的连接生命周期切片）。

**尚未移植**（见 ``PORTING.md`` 的 Tier-B③）：C3b-2b 的**后半**——6 个 ``pi.on(...)`` 生命周期钩子
（``session_start`` / ``before_agent_start`` / ``tool_call`` / ``turn_start`` /
``mcp_servers_change`` / ``session_shutdown``）。``create_mcp_extension`` 因此仍不导出。

**有意偏离**
* 上游 ``createConnection`` 经 ``loadMcpRuntime()`` **懒加载** runtime；Python 侧模块导入本就惰性，
  故直接接收一个已构造的 ``McpServerConnection`` 工厂（``create_connection``），由调用方注入。
* 上游 ``waitForServers`` 用 ``AbortSignal``；本端用 ``asyncio.Event``（本仓约定，见 E5）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .index import McpServer, is_enabled
from .oauth import McpOAuthCredentialStore
from .runtime import McpServerConnection

__all__ = ["McpConnectionLifecycle"]


@dataclass
class McpConnectionLifecycle:
    """建连、重连与「等 server 就绪」的编排。

    对应上游 ``index.ts:543-645``。对宿主的读取全走回调，因此可在测试里注入假 server。
    """

    #: 全部 server（上游闭包里的 ``servers``）。
    servers: Callable[[], Sequence[McpServer]]
    #: 会话工作目录。
    session_cwd: Callable[[], str]
    #: 构造一个 ``McpServerConnection``（上游 ``createConnection`` 里 new 的那一步）。
    create_connection: Callable[
        [McpServer, Callable[[], bool]], Awaitable[McpServerConnection | None]
    ]
    #: OAuth 凭据存储；为 None 时「已登录检测」恒为未变更。
    credentials: Callable[[], McpOAuthCredentialStore | None]
    #: 会话被中止（上游 ``session.signal``）。
    session_aborted: Callable[[], bool]
    #: 状态变化时通知外界（上游 ``emitChange``）。
    emit_change: Callable[[], None]
    #: 连接的工具变化时调用（上游 ``onTools``）——通常交给注册器。
    on_tools: Callable[[McpServerConnection], None]
    #: 就绪后重新激活发现工具（上游在 ``reconnectSignedIn`` 末尾调用）。
    ensure_discovery_active: Callable[[], Any] = lambda: None

    #: 等待登录的 server 在「需要登录时」的令牌快照，用来发现别的进程完成了登录。
    tokens_at_sign_in: dict[int, str] = field(default_factory=dict)

    # ---- 令牌快照 ----

    def stored_tokens(self, connection: McpServerConnection) -> str:
        """该连接的已存令牌（JSON 串）；不用 OAuth 时为 ``"null"``。"""
        url = connection.oauth_url
        store = self.credentials()
        if not url or store is None:
            return "null"
        return json.dumps(store.tokens(connection.name, url))

    def on_connection_change(self, connection: McpServerConnection) -> None:
        """连接状态变化：维护「等待登录」快照。"""
        key = id(connection)
        if connection.state != "needs-auth":
            self.tokens_at_sign_in.pop(key, None)
        elif key not in self.tokens_at_sign_in:
            self.tokens_at_sign_in[key] = self.stored_tokens(connection)
        self.emit_change()

    async def reconnect_signed_in(self) -> None:
        """凭据在等待期间被写入的 server → 重连。"""
        pending = [
            (key, connection)
            for key, connection in self._snapshot_connections()
            if key in self.tokens_at_sign_in
            and self.stored_tokens(connection) != self.tokens_at_sign_in[key]
        ]
        if not pending:
            return
        for key, _connection in pending:
            self.tokens_at_sign_in.pop(key, None)
        await asyncio.gather(
            *(connection.reconnect() for _key, connection in pending), return_exceptions=True
        )
        self.ensure_discovery_active()

    def _snapshot_connections(self) -> list[tuple[int, McpServerConnection]]:
        return [
            (id(server.connection), server.connection)
            for server in self.servers()
            if server.connection is not None
        ]

    # ---- 建连 ----

    async def create(
        self, server: McpServer, is_current: Callable[[], bool]
    ) -> McpServerConnection | None:
        """为 ``server`` 建连接（上游 ``createConnection``）。"""
        connection = await self.create_connection(server, is_current)
        if connection is None:
            return None
        server.connection = connection
        self.emit_change()
        return connection

    def start(self, server: McpServer, after: Any = None) -> Awaitable[None]:
        """后台为 ``server`` 建连（上游 ``startConnection``）。

        ``server.ready`` 在连上或失败时落定；失败体现在它的状态里。它在 ``after`` 之后才开始，
        除非期间被禁用/替换/关闭。返回的 awaitable 在 runtime 无法加载时抛错。
        """
        attempt = object()
        server.attempt = attempt

        def is_current() -> bool:
            return (
                not self.session_aborted()
                and server.attempt is attempt
                and is_enabled(server)
                and any(candidate is server for candidate in self.servers())
            )

        async def ready() -> None:
            if after is not None:
                with contextlib.suppress(Exception):
                    await after
            if not is_current():
                return
            connection = await self.create(server, is_current)
            if connection is None or not is_current():
                return
            with contextlib.suppress(Exception):
                await connection.get_client()

        task = asyncio.ensure_future(ready())
        server.ready = asyncio.ensure_future(_swallow(task))
        return task

    def is_current_attempt(self, server: McpServer) -> bool:
        """该 server 的 ``ready`` 是否仍代表当前尝试。"""
        return server.ready is not None

    async def wait_for_servers(
        self, waiting: Sequence[McpServer], signal: asyncio.Event | None = None
    ) -> None:
        """等最新一轮连接尝试（含等待期间新排入的），或被中止。

        **E5**：上游用 ``AbortSignal``；本端用 ``asyncio.Event``。
        """
        while True:
            # 只等**尚未落定**的尝试：已落定的会被下一轮重新选中，导致空转。
            pending = [
                server.ready
                for server in waiting
                if is_enabled(server)
                and any(candidate is server for candidate in self.servers())
                and server.ready is not None
                and not server.ready.done()
            ]
            if not pending or (signal is not None and signal.is_set()):
                return
            tasks: list[asyncio.Future[Any]] = list(pending)
            if signal is not None:
                tasks.append(asyncio.ensure_future(signal.wait()))
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    if task not in pending and not task.done():
                        task.cancel()


async def _swallow(awaitable: Awaitable[Any]) -> None:
    """把失败吞掉（上游 ``server.ready = ready.catch(() => undefined)``）。"""
    with contextlib.suppress(BaseException):
        await awaitable
