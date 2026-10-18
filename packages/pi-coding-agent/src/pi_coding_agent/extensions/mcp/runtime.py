"""MCP 集成里与 server 通信的那部分：连接、传输与 OAuth 登录。

对应上游 ``extensions/mcp/runtime.ts``（454 行，**完整移植**）。

它引入 MCP 客户端，所以上游让 ``index.ts`` 经 ``runtime.lazy.ts`` 只在配了 server 时才加载。
Python 侧没有等价的懒加载需求（模块导入本就是惰性的），故直接导入。

**跨包形状差异（E1–E6，见 ``pi-mcp/PORTING.md``）**
* **E3**：``Tool`` / ``ServerCapabilities`` / ``Resource`` 等线上对象都是 ``TypedDict`` → 一律下标访问
* **E5**：``AbortSignal`` → ``asyncio.Event``；``on_notification`` / ``on_close`` 返回**退订函数**
* **E6**：``McpClientOptions`` / ``StdioTransportOptions`` / ``StreamableHttpTransportOptions`` /
  ``McpRequestOptions`` 都是 ``@dataclass`` → 关键字 + snake_case

**session 续期与重连的判断依据（上游 ``runtime.ts:288-294`` 写明，逐条对齐）**
MCP server 重启/发版后不再认识旧 session，会以 ``McpSessionExpiredError`` 拒绝。上游注释明确：
「server 不再认识该 session，因此它**没有执行**这个请求」——所以**只重试一次**、换新 session 是安全的。
旧 client 被**脱钩但不关闭**：关掉会让它其它在途调用一起失败，而那些调用会拿到同样的 404 并各自按
同样方式重试。本端照此实现（``_with_client``）。

**401 升级路径的边界（``runtime.ts:284-298``）**
* 只有**只读**请求在**第一次**尝试遇到**瞬时** HTTP 错误时重试一次（250ms 后退避）——
  工具调用**不重试**，因为它可能已经执行过。
* ``McpSessionExpiredError`` 在第一次尝试时重连一次。
* 需要登录（``McpOAuthAuthorizationRequiredError``，或配了 auth provider 时的 ``McpAuthRequiredError``）
  → 丢弃 client、进入 ``needs-auth``、抛一条带指引的消息；**不再重放**。
* 其它错误原样抛出，不做归类。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from pi_mcp import (
    JSON_RPC_ERROR_CODES,
    McpAuthRequiredError,
    McpClient,
    McpClientOptions,
    McpError,
    McpHttpError,
    McpRequestOptions,
    McpSessionExpiredError,
    StdioTransport,
    StdioTransportOptions,
    StreamableHttpTransport,
    StreamableHttpTransportOptions,
)
from pi_mcp.oauth import McpOAuthAuthorizationRequiredError

from ... import __version__
from ...resolve_config_value import resolve_config_value_or_throw, resolve_headers_or_throw
from .config import McpServerEntry
from .log import McpServerLog
from .oauth import (
    McpAuthProvider,
    McpOAuthCredentialStore,
    McpOAuthSettings,
    McpSignInCancelledError,
    create_mcp_auth_provider,
    sign_in_mcp_server,
)

__all__ = [
    "CONNECT_RETRY_DELAYS_S",
    "DEFAULT_TIMEOUT_SECONDS",
    "McpServerConnection",
    "McpSignInCancelledError",
    "McpTransportFactory",
    "ServerState",
    "create_default_transport",
    "expand_home",
    "is_transient_error",
    "sign_in_required_message",
    "sign_in_mcp_server",
    "uses_oauth",
]

DEFAULT_TIMEOUT_SECONDS = 60
#: 连接失败的 stdio server 的 stderr 尾巴长度。
STDERR_TAIL_CHARS = 2_000
#: 连接瞬时失败的 HTTP server 时的重试间隔（秒；上游毫秒值 250 / 1000）。
CONNECT_RETRY_DELAYS_S: tuple[float, ...] = (0.25, 1.0)

#: ``disconnected``：连接掉了（例如 stdio server 退出）；下一次调用会重连。
ServerState = Literal["connecting", "connected", "disconnected", "needs-auth", "failed", "closed"]

#: 传输工厂。``auth_provider`` 为 None 表示该 server 不用 OAuth。
McpTransportFactory = Callable[
    [McpServerEntry, str, "McpAuthProvider | _ProviderTokenAuth | None"], Any
]


def _error_message(error: BaseException) -> str:
    return str(error) or error.__class__.__name__


def is_transient_error(error: BaseException) -> bool:
    """值得再试一次的网络故障与过载/重启中的 server。"""
    if isinstance(error, McpHttpError):
        status = error.status
        return status in (408, 429) or (status >= 500 and status != 501)
    # 上游用 `instanceof TypeError` 表示网络层失败；Python 侧 httpx 抛的是 OSError 家族。
    return isinstance(error, OSError)


def sign_in_required_message(entry: McpServerEntry) -> str:
    provider = getattr(entry.config, "auth", None)
    provider_name = provider.get("provider") if isinstance(provider, dict) else None
    hint = f"/login {provider_name}" if provider_name else "/mcp"
    return f'MCP server "{entry.name}" requires sign-in. Run {hint} to sign in.'


def uses_oauth(entry: McpServerEntry) -> bool:
    """HTTP server 默认用 OAuth，除非配置里给了 ``Authorization`` 头或 ``auth``。

    （上游 ``runtime.ts:83-87``；注意：函数名是 ``usesOAuth``，但本端按 PEP 8 用 snake_case。）
    """
    config = entry.config
    url = getattr(config, "url", None)
    if url is None or getattr(config, "auth", None):
        return False
    headers = getattr(config, "headers", None) or {}
    return not any(str(name).lower() == "authorization" for name in headers)


def expand_home(value: str) -> str:
    """``~`` 与 ``~/…``（Windows 上还有 ``~\\…``）表示家目录，与 shell 一致。"""
    if value == "~":
        return str(Path.home())
    if value.startswith("~/") or (os.name == "nt" and value.startswith("~\\")):
        return os.path.join(str(Path.home()), value[2:])
    return value


def create_default_transport(
    entry: McpServerEntry, cwd: str, auth_provider: McpAuthProvider | None
) -> Any:
    """按配置建传输：有 ``url`` 走 Streamable HTTP，否则走 stdio。"""
    config = entry.config
    name = entry.name
    url = getattr(config, "url", None)
    if url is not None:
        return StreamableHttpTransport(
            StreamableHttpTransportOptions(
                url=url,
                headers=resolve_headers_or_throw(
                    getattr(config, "headers", None), f'MCP server "{name}"'
                )
                or {},
                auth_provider=auth_provider,
            )
        )
    env: dict[str, str] = {}
    for key, value in (getattr(config, "env", None) or {}).items():
        env[key] = resolve_config_value_or_throw(value, f'MCP server "{name}" env "{key}"')
    args = getattr(config, "args", None) or []
    return StdioTransport(
        StdioTransportOptions(
            command=expand_home(str(getattr(config, "command", ""))),
            args=tuple(expand_home(arg) for arg in args),
            cwd=os.path.abspath(
                os.path.join(cwd, expand_home(getattr(config, "cwd", None) or "."))
            ),
            env=env,
            stderr="pipe",
        )
    )


async def without_templates(list_async: Callable[[], Awaitable[Any]], empty: Any) -> Any:
    """不实现 ``resources/templates/list`` 的 server 视为没有模板。"""
    try:
        return await list_async()
    except McpError as error:
        if getattr(error, "code", None) == JSON_RPC_ERROR_CODES["methodNotFound"]:
            return empty
        raise


@dataclass
class McpServerConnection:
    """一个已配置的 server。调用发现连接没了时会**惰性重连**。

    同时满足 ``pi_mcp`` 的 ``McpToolCaller`` 与 ``resources.McpResourceServer`` 两个形状。
    """

    entry: McpServerEntry
    cwd: str
    create_transport: McpTransportFactory
    credentials: McpOAuthCredentialStore
    on_tools: Callable[[McpServerConnection], None]
    #: 当前 pi provider 的令牌，供配了 ``auth.provider`` 的 server 用。
    provider_token: Callable[[str], Awaitable[str | None]] | None = None
    #: ``state`` / ``error`` / ``tools`` 变化时调用。
    on_change: Callable[[McpServerConnection], None] | None = None
    #: 接收 server 的日志消息（``notifications/message``）。
    log: McpServerLog | None = None

    state: ServerState = "connecting"
    error: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    #: server 是否提供资源。**不在连接时列举**——列全部资源可能很慢（比如一个大盘）。
    has_resources: bool = False
    #: ``initialize`` 返回的 server instructions，把它的工具作为一组来描述。
    instructions: str | None = None
    #: server 最后一次 OAuth 挑战；登录时用它的 resource metadata URL 与 scope。
    challenge: Any = None

    _client: McpClient | None = field(default=None, repr=False)
    _opening: asyncio.Task[Any] | None = field(default=None, repr=False)
    #: ``close()`` 时置位；取消进行中的连接（含重试之间的等待）。
    _shutdown: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    #: 最后一次连接失败的 stdio server 的 stderr。
    _stderr_tail: str | None = field(default=None, repr=False)
    _auth_provider: McpAuthProvider | _ProviderTokenAuth | None = field(default=None, repr=False)
    _auth_ready: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        # 与上游构造函数一致：OAuth server 建 provider；配了 auth.provider 的走 provider 令牌。
        url = self.oauth_url
        auth = getattr(self.entry.config, "auth", None)
        provider_name = auth.get("provider") if isinstance(auth, dict) else None
        if url is not None:
            self._auth_provider = create_mcp_auth_provider(
                server_url=url,
                store=self.credentials.for_server(self.entry.name, url),
                settings=self.oauth_settings,
                on_challenge=self._on_challenge,
            )
        elif provider_name:
            self._auth_provider = _ProviderTokenAuth(self.provider_token, provider_name)

    # ---- 基本属性 ----

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def closed(self) -> bool:
        return self._shutdown.is_set()

    @property
    def timeout_ms(self) -> int:
        timeout = getattr(self.entry.config, "timeout", None)
        return int((timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS) * 1000)

    @property
    def oauth_url(self) -> str | None:
        """该 server 用 OAuth 认证时的 URL。"""
        url = getattr(self.entry.config, "url", None)
        return url if (uses_oauth(self.entry) and url is not None) else None

    def oauth_settings(self) -> McpOAuthSettings:
        oauth = getattr(self.entry.config, "oauth", None)
        if oauth is None:
            return McpOAuthSettings()
        secret = oauth.client_secret
        return McpOAuthSettings(
            client_id=oauth.client_id,
            client_secret=(
                None
                if secret is None
                else resolve_config_value_or_throw(
                    secret, f'MCP server "{self.entry.name}" oauth.clientSecret'
                )
            ),
            callback_port=oauth.callback_port,
            callback_url=oauth.callback_url,
            scope=oauth.scope,
            client_name=oauth.client_name,
            client_registration=oauth.client_registration,
            auth_server_metadata_url=oauth.auth_server_metadata_url,
        )

    def _on_challenge(self, challenge: Any) -> None:
        self.challenge = challenge

    # ---- 连接 ----

    async def get_client(self) -> McpClient:
        if self.closed:
            raise RuntimeError(f'MCP server "{self.entry.name}" is shut down')
        if self._client is not None and self._client.connection_state == "connected":
            return self._client
        if self._opening is None:
            self._opening = asyncio.ensure_future(self.open())
        try:
            return await asyncio.shield(self._opening)
        finally:
            if self._opening is not None and self._opening.done():
                self._opening = None

    async def call_tool(self, name: str, args: dict[str, Any], options: Any) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._with_client(lambda client: client.call_tool(name, args, options)),
        )

    async def read_resource(self, uri: str, options: Any) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._with_client(lambda client: client.read_resource(uri, options), True),
        )

    async def resources_page(self, cursor: str | None, options: Any) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._with_client(
                lambda client: client.list_resources_page(cursor, options), True
            ),
        )

    async def resource_templates_page(self, cursor: str | None, options: Any) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._with_client(
                lambda client: without_templates(
                    lambda: client.list_resource_templates_page(cursor, options),
                    {"resourceTemplates": []},
                ),
                True,
            ),
        )

    async def all_resources(self, options: Any) -> list[dict[str, Any]]:
        return cast(
            "list[dict[str, Any]]",
            await self._with_client(lambda client: client.list_resources(options), True),
        )

    async def all_resource_templates(self, options: Any) -> list[dict[str, Any]]:
        return cast(
            "list[dict[str, Any]]",
            await self._with_client(
                lambda client: without_templates(
                    lambda: client.list_resource_templates(options), []
                ),
                True,
            ),
        )

    async def _with_client(
        self, run: Callable[[McpClient], Awaitable[Any]], read_only: bool = False
    ) -> Any:
        """跑一次请求，必要时重连。

        只读请求在瞬时 HTTP 错误后重试一次；工具调用**不重试**，因为它可能已经执行过。
        """
        attempt = 1
        while True:
            client = await self.get_client()
            try:
                return await run(client)
            except BaseException as error:  # noqa: BLE001 - 按上游逐类分流后再抛
                if (
                    read_only
                    and attempt == 1
                    and isinstance(error, McpHttpError)
                    and is_transient_error(error)
                ):
                    await asyncio.sleep(CONNECT_RETRY_DELAYS_S[0])
                    attempt += 1
                    continue
                if isinstance(error, McpSessionExpiredError) and attempt == 1:
                    # server 不再认识该 session（重启、发版），因此它没有执行这个请求。换新 session
                    # 重试一次。旧 client 脱钩但**不关闭**：关掉会让它其它在途调用失败，而那些调用
                    # 会拿到同样的 404 并各自按同样方式重试。
                    if self._client is client:
                        self._client = None
                    attempt += 1
                    continue
                if not self._needs_sign_in(error):
                    raise
                await self._drop_client(client)
                self._mark_needs_auth()
                raise RuntimeError(sign_in_required_message(self.entry)) from None

    async def reconnect(self) -> None:
        """用新凭据重连，例如登录之后。"""
        await self._settle_opening()
        if self._client is not None:
            await self._drop_client(self._client)
        await self.get_client()

    async def sign_out(self) -> None:
        """已存凭据被删除后断开。"""
        await self._settle_opening()
        if self._client is not None:
            await self._drop_client(self._client)
        if not self.closed:
            self._mark_needs_auth()

    def _needs_sign_in(self, error: BaseException) -> bool:
        """刷新之后仍被拒的 OAuth server 需要用户重新登录。"""
        return isinstance(error, McpOAuthAuthorizationRequiredError) or (
            self._auth_provider is not None and isinstance(error, McpAuthRequiredError)
        )

    def _mark_needs_auth(self) -> None:
        self.state = "needs-auth"
        self.error = None
        self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change(self)

    async def _settle_opening(self) -> None:
        opening = self._opening
        if opening is None:
            return
        with contextlib.suppress(BaseException):
            await asyncio.shield(opening)

    async def _drop_client(self, client: McpClient) -> None:
        if self._client is client:
            self._client = None
        with contextlib.suppress(Exception):
            await client.close()

    async def open(self) -> McpClient:
        """连接（必要时按 :data:`CONNECT_RETRY_DELAYS_S` 重试）。"""
        self.state = "connecting"
        self._changed()
        retries = CONNECT_RETRY_DELAYS_S if getattr(self.entry.config, "url", None) else ()
        attempt = 0
        while True:
            self._stderr_tail = None
            try:
                return await self._connect_once()
            except BaseException as error:  # noqa: BLE001 - 统一走 connect_failed 归类
                delay = retries[attempt] if attempt < len(retries) else None
                if self.closed or delay is None or not is_transient_error(error):
                    raise self._connect_failed(error) from None
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
                if self.closed:
                    raise self._connect_failed(error) from None
                attempt += 1

    async def _connect_once(self) -> McpClient:
        client = McpClient(
            McpClientOptions(
                name="pi",
                version=__version__,
                request_timeout_ms=self.timeout_ms,
                roots=[{"uri": Path(self.cwd).as_uri(), "name": os.path.basename(self.cwd)}],
            )
        )
        log = self.log
        if log is not None:
            client.on_notification(
                "notifications/message", lambda params: log.write(self.entry.name, params)
            )

        # 关闭 client 会让它在途的 initialize / 发现请求失败并关掉传输。用记忆化避免第二次
        # `close()` 在第一次还没关完时就返回。
        closing: dict[str, asyncio.Task[None]] = {}

        async def close_client() -> None:
            task = closing.get("task")
            if task is None:
                task = asyncio.ensure_future(_close_quietly(client))
                closing["task"] = task
            await asyncio.shield(task)

        shutdown_watcher = asyncio.ensure_future(self._watch_shutdown(close_client))
        transport: Any = None
        try:
            transport = self.create_transport(self.entry, self.cwd, self._auth_provider)
            await client.connect(transport)

            def _on_tools_changed(_params: Any) -> None:
                asyncio.ensure_future(self._refresh_tools(client))

            client.on_notification("notifications/tools/list_changed", _on_tools_changed)
            stdio = transport if isinstance(transport, StdioTransport) else None
            client.on_close(lambda: self._handle_client_close(client, stdio))
            # 没有 tools 能力（只有 prompts 或 resources）的 server 不答 tools/list。
            capabilities = client.server_capabilities or {}
            tools = await client.list_tools() if capabilities.get("tools") else []
            if self.closed:
                raise RuntimeError("shut down while connecting")
            if client.connection_state != "connected":
                raise RuntimeError("connection closed during setup")
            self._client = client
            self.tools = [dict(tool) for tool in tools]
            self.has_resources = capabilities.get("resources") is not None
            instructions = client.instructions
            self.instructions = (
                instructions.strip() if instructions and instructions.strip() else None
            )
            self.state = "connected"
            self.error = None
            self.on_tools(self)
            self._changed()
            return client
        except BaseException as error:
            await close_client()
            if isinstance(transport, StdioTransport):
                stderr = getattr(transport, "stderr", "") or ""
                tail = stderr.strip()[-STDERR_TAIL_CHARS:]
                self._stderr_tail = tail or None
            raise error
        finally:
            shutdown_watcher.cancel()
            with contextlib.suppress(BaseException):
                await shutdown_watcher

    async def _watch_shutdown(self, close_client: Callable[[], Awaitable[None]]) -> None:
        await self._shutdown.wait()
        with contextlib.suppress(BaseException):
            await close_client()

    def _connect_failed(self, error: BaseException) -> BaseException:
        if self._needs_sign_in(error) and not self.closed:
            self._mark_needs_auth()
            return RuntimeError(sign_in_required_message(self.entry))
        self.state = "closed" if self.closed else "failed"
        message = _error_message(error)
        self.error = f"{message}\n{self._stderr_tail}" if self._stderr_tail else message
        self._changed()
        return RuntimeError(f'MCP server "{self.entry.name}" failed to connect: {self.error}')

    def _handle_client_close(self, client: McpClient, stdio: StdioTransport | None) -> None:
        """传输掉了。下一次调用会重连；在那之前状态里显示原因。"""
        if self._client is not client or self.closed:
            return
        self._client = None
        self.state = "disconnected"
        stderr = (getattr(stdio, "stderr", "") or "").strip()[-STDERR_TAIL_CHARS:] if stdio else ""
        self.error = f"Connection closed\n{stderr}" if stderr else "Connection closed"
        self._changed()

    async def _refresh_tools(self, client: McpClient) -> None:
        try:
            tools = await client.list_tools()
            if self._client is not client or self.closed:
                return
            self.tools = [dict(tool) for tool in tools]
            self.on_tools(self)
        except BaseException as error:  # noqa: BLE001 - 刷新失败只记状态
            self.error = f"Failed to refresh tools: {_error_message(error)}"
        self._changed()

    async def close(self) -> None:
        """关掉该 server 的传输（含仍在连接中的那个）。"""
        self._shutdown.set()
        self.state = "closed"
        self._changed()
        client = self._client
        self._client = None
        pending: list[Awaitable[Any]] = []
        if client is not None:
            pending.append(_close_quietly(client))
        if self._opening is not None:
            pending.append(asyncio.shield(self._opening))
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # server 已答复的刷新可能已经轮换了刷新令牌；在保存新令牌之前退出会丢掉授权。
        if self._auth_provider is not None:
            await self._auth_provider.settled()


async def _close_quietly(client: McpClient) -> None:
    with contextlib.suppress(Exception):
        await client.close()


@dataclass
class _ProviderTokenAuth:
    """把某个 pi provider 的当前令牌当 bearer 用（``auth: {provider}``）。

    每次请求都读，因此 provider 的刷新会生效；MCP 侧不存副本。
    （对应上游 ``runtime.ts:196-199`` 的对象字面量。）
    """

    provider_token: Callable[[str], Awaitable[str | None]] | None
    provider: str

    async def token(self) -> str | None:
        if self.provider_token is None:
            return None
        return await self.provider_token(self.provider)

    async def on_unauthorized(self, context: Any) -> None:
        return None

    async def settled(self) -> None:
        return None


def _request_options(signal: Any, timeout_ms: int, on_progress: Any = None) -> McpRequestOptions:
    return McpRequestOptions(signal=signal, timeout_ms=timeout_ms, on_progress=on_progress)
