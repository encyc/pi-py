"""远程 MCP server 的 OAuth 登录。

对应上游 ``extensions/mcp/oauth.ts``（558 行，**完整移植**）。

连接**不会自己启动浏览器流程**：它发送已存的访问令牌，401 之后再试已存的刷新令牌。都不行时以
``McpOAuthAuthorizationRequiredError`` 失败，由用户经 ``/mcp`` 走授权码流程（PKCE，Client ID
Metadata Document 或动态客户端注册），回调落在 loopback。

凭据存在 ``<agent-dir>/mcp-auth.json``，按 **server 名 + URL** 作键。

**复用的既有实现（按 Lead 指示，不自己起一套）**
* 回环回调服务器：``pi_mcp.oauth.OAuthCallbackServer``（已登记 S1–S12 安全边界，含「授权码在 URL 里，
  访问日志被静音」）
* HTML 页面：``pi_ai.oauth_page.oauth_success_html`` / ``oauth_error_html``（签名正好塞进 ``render_page``）
* 加锁文件后端：本包 ``auth_storage.FileAuthStorageBackend``（上游 ``oauth.ts:147`` 用的就是它）

**跨包形状差异（见 ``pi-mcp/PORTING.md`` 的〈跨包形状差异清单〉E1–E6）**
* **E3**：``McpOAuthState`` / ``OAuthTokens`` / ``AuthorizationServerMetadata`` 都是 ``TypedDict``
  → 一律下标访问（``state.get("tokens")``），**不要** ``state.tokens``
* **E6**：``OAuthFlowOptions`` / ``McpOAuthProviderOptions`` / ``OAuthCallbackServerOptions`` 是
  ``@dataclass`` → 关键字 + snake_case 构造
* **E4**：``can_save_client_information()`` 问能力，不用 ``hasattr``
* **E5**：``wait_for_callback()`` 先**同步**拿到 ``asyncio.Future`` 再 await
* **E2**：``McpFetch`` 是 ``httpx.AsyncClient`` 的**类型别名，不是可调用对象**；注入点是各
  ``OAuthFlowOptions.client``

**有意偏离**
1. **``APP_NAME`` 与 ``agent_dir()`` 落在此处**：上游取自 ``config.ts``。本包未移植该模块，
   按 Lead 指示**不新建 config 模块**，只在这里落最小实现并说明关系。
2. **``UnauthorizedContext`` 已解析好挑战**：上游读 ``context.response.headers.get("www-authenticate")``；
   Python 侧该值已解析为 ``context.www_authenticate``，故不经 headers。
3. **``timed_fetch()`` → ``_timed_client()``**：上游包装 fetch **函数**并把超时与调用方信号组合；
   Python 侧的对应旋钮是 ``httpx.AsyncClient(timeout=...)``。调用方注入的 client 保有自己的超时
   （无从叠加"信号组合"这一层），已在 docstring 标明。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

import httpx

from pi_ai.oauth_page import oauth_error_html, oauth_success_html
from pi_mcp.oauth import (
    McpOAuthAuthorizationRequiredError,
    McpOAuthProvider,
    McpOAuthProviderOptions,
    OAuthCallbackServer,
    OAuthCallbackServerOptions,
    OAuthFlowOptions,
    authorize_mcp,
    parse_www_authenticate,
    step_up_scope,
)

from ...auth_storage import (
    AuthStorageBackend,
    FileAuthStorageBackend,
    LockResult,
    default_auth_path,
)
from ...mcp_servers import mcp_namespace

__all__ = [
    "APP_NAME",
    "CALLBACK_HOST",
    "CALLBACK_PATH",
    "McpAuthProvider",
    "McpOAuthCredentialStore",
    "McpOAuthServerStore",
    "McpOAuthSettings",
    "McpSignInCancelledError",
    "McpSignInPrompt",
    "agent_dir",
    "create_mcp_auth_provider",
    "sign_in_mcp_server",
]

#: 上游 ``config.ts: APP_NAME`` —— 用作 OAuth 动态注册的 ``client_name`` 默认值。
APP_NAME = "pi"

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"

#: pi.dev 提供 pi 的 Client ID Metadata Document 的位置：``client.json`` 与 ``<callback ID>/client.json``。
CLIENT_METADATA_BASE_URL = "https://pi.dev/oauth"

#: 没有存储 redirect URI 时刷新用的地址。刷新**从不**把用户重定向。
FALLBACK_REDIRECT_URL = f"http://{CALLBACK_HOST}{CALLBACK_PATH}"

#: 距过期这么近的访问令牌会在发送前先刷新。
REFRESH_SKEW_MS = 30_000

#: 对授权服务器的每个请求的时限，免得无响应的服务器占住刷新锁、拖慢关闭或让登录久等。
OAUTH_REQUEST_TIMEOUT_S = 15.0

#: 刷新锁的持有者超过这么久没续期（进程被杀）即可被接管。
REFRESH_LOCK_STALE_S = 20.0
#: 等别的进程刷新最多这么久（长于过期锁的寿命）。
REFRESH_LOCK_WAIT_S = 25.0
REFRESH_LOCK_RETRY_S = 0.1


def agent_dir() -> str:
    """agent 目录。

    上游取自 ``config.ts: getAgentDir()``（``$<APP>_CODING_AGENT_DIR`` 优先，否则 ``~/<CONFIG_DIR>/agent``）。
    本包未移植 ``config.ts``，故复用 ``auth_storage.default_auth_path()`` 已确立的同一套规则。
    """
    return os.path.dirname(default_auth_path())


@dataclass
class McpOAuthSettings:
    """一个 server 的 OAuth 设置（``clientSecret`` 已解析）。"""

    client_id: str | None = None
    client_secret: str | None = None
    callback_port: int | None = None
    #: 回环 redirect URI；见 ``McpOAuthConfig.callbackUrl``。
    callback_url: str | None = None
    #: 请求的 scope，空格分隔。
    scope: str | None = None
    #: 动态客户端注册的 ``client_name``。默认 :data:`APP_NAME`。
    client_name: str | None = None
    #: 见 ``McpOAuthConfig.clientRegistration``。
    client_registration: str | None = None
    #: 见 ``McpOAuthConfig.authServerMetadataUrl``。
    auth_server_metadata_url: str | None = None


@dataclass
class CallbackSettings:
    """回环回调服务器监听在哪、服务哪个 redirect URI。"""

    host: str
    redirect_host: str
    port: int | None
    path: str
    #: 端口固定时那个**确切**的 redirect URI。
    fixed_redirect_url: str | None


def _split_url(value: str) -> tuple[str, str, int | None, str]:
    """``(scheme, hostname, port, path)``；hostname 去掉 IPv6 的方括号。"""
    parts = urlsplit(value)
    return parts.scheme, (parts.hostname or ""), parts.port, (parts.path or "")


def callback_settings(settings: McpOAuthSettings) -> CallbackSettings:
    value = settings.callback_url or f"http://{CALLBACK_HOST}{CALLBACK_PATH}"
    scheme, hostname, url_port, path = _split_url(value)
    port = url_port if url_port is not None else settings.callback_port

    fixed_redirect_url: str | None = None
    # 配了端口的 URI 原样发送，因为 server 是当字符串比的。
    if url_port is not None:
        fixed_redirect_url = settings.callback_url
    elif port is not None:
        netloc = hostname if ":" not in hostname else f"[{hostname}]"
        fixed_redirect_url = urlunsplit((scheme or "http", f"{netloc}:{port}", path, "", ""))

    return CallbackSettings(
        # `localhost` 实际监听 127.0.0.1；::1 拒绝时浏览器会回退。
        host=CALLBACK_HOST if hostname == "localhost" else hostname,
        redirect_host=hostname,
        port=port,
        path=path,
        fixed_redirect_url=fixed_redirect_url,
    )


def merge_scopes(*scopes: str | None) -> str | None:
    """两个列表的 scope 合集，各出现一次。"""
    merged: list[str] = []
    for scope in scopes:
        for item in (scope or "").split():
            if item not in merged:
                merged.append(item)
    return " ".join(merged) if merged else None


def parse_states(content: str | None) -> dict[str, Any]:
    if not content or not content.strip():
        return {}
    parsed = json.loads(content)
    return parsed if isinstance(parsed, dict) else {}


def serialize_states(states: dict[str, Any]) -> str:
    return f"{json.dumps(states, indent=2, ensure_ascii=False)}\n"


def store_keys(name: str, server_url: str) -> tuple[str, str]:
    """一个 server 状态的键：按名字与 URL，使共用 URL 的 server 各自独立；外加按 URL 的旧键。"""
    legacy_key = _normalized_url(server_url)
    return f"{mcp_namespace(name)}|{legacy_key}", legacy_key


def _normalized_url(value: str) -> str:
    """``String(new URL(v))`` 的等价物：scheme/host 小写、补根路径、去默认端口与 fragment。"""
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    netloc = host if ":" not in host else f"[{host}]"
    if parts.port is not None and not (
        (scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)
    ):
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


@dataclass
class McpOAuthServerStore:
    """一个 server 的 OAuth 状态存储（``load`` / ``save`` / ``with_refresh_lock``）。"""

    _load: Callable[[], Any]
    _save: Callable[[Any], Any]
    _with_refresh_lock: Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]]

    async def load(self) -> Any:
        return await _maybe_await(self._load())

    async def save(self, state: Any) -> Any:
        return await _maybe_await(self._save(state))

    async def with_refresh_lock(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        return await self._with_refresh_lock(fn)


async def _maybe_await(value: Any) -> Any:
    """``McpOAuthStateStore`` 的 ``load``/``save`` 可以同步也可以异步。"""
    if hasattr(value, "__await__"):
        return await value
    return value


@dataclass
class McpOAuthCredentialStore:
    """``mcp-auth.json`` 里按 server 存的 OAuth 状态（客户端注册、令牌、待用 PKCE verifier）。"""

    backend: AuthStorageBackend = field(
        default_factory=lambda: FileAuthStorageBackend(default_auth_path())
    )
    #: 刷新锁文件所在目录。没有它时刷新只在本进程内串行。
    lock_dir: str | None = None

    def __post_init__(self) -> None:
        if self.lock_dir is None:
            self.lock_dir = agent_dir()

    def for_server(self, name: str, server_url: str) -> McpOAuthServerStore:
        key, legacy_key = store_keys(name, server_url)

        def load() -> Any:
            def edit(current: str | None) -> LockResult[Any]:
                states = parse_states(current)
                # 第一个加载旧状态的 server 接管它；同 URL 的其它 server 重新登录。
                if states.get(key) or not states.get(legacy_key):
                    return LockResult(result=states.get(key))
                states[key] = states[legacy_key]
                del states[legacy_key]
                return LockResult(result=states[key], next=serialize_states(states))

            return self.backend.with_lock(edit)

        def save(state: Any) -> None:
            self._write(lambda states: states.__setitem__(key, state))

        return McpOAuthServerStore(
            _load=load,
            _save=save,
            _with_refresh_lock=lambda fn: self._with_refresh_lock(key, fn),
        )

    async def _with_refresh_lock(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        """每个 server 一个锁文件。

        进程正常退出时锁被释放；被杀时锁不再续期而变陈旧，下一个进程接管。
        （上游用 ``proper-lockfile`` 的 ``stale`` + 重试；本端用 ``O_EXCL`` + mtime 陈旧判定，
        与 ``auth_storage`` 同一套做法。）
        """
        if not self.lock_dir:
            return await fn()
        os.makedirs(self.lock_dir, mode=0o700, exist_ok=True)
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        lock_path = os.path.join(self.lock_dir, f"mcp-auth-refresh-{digest}.lock")
        await _acquire_lock(lock_path)
        try:
            return await fn()
        finally:
            with contextlib.suppress(OSError):
                os.rmdir(lock_path)

    def tokens(self, name: str, server_url: str) -> Any:
        """某 server 已存的令牌，用于发现别的进程做的登录。**不**接管旧状态。"""
        key, legacy_key = store_keys(name, server_url)
        states = self.backend.with_lock(lambda current: LockResult(result=parse_states(current)))
        entry = states.get(key) or states.get(legacy_key)
        return (entry or {}).get("tokens")

    def remove(self, name: str, server_url: str) -> bool:
        """删除该 server 的凭据。返回此前是否存在（含会被接管的旧状态）。"""
        key, legacy_key = store_keys(name, server_url)

        def edit(current: str | None) -> LockResult[bool]:
            states = parse_states(current)
            stored = key if key in states else (legacy_key if legacy_key in states else None)
            if stored is None:
                return LockResult(result=False)
            del states[stored]
            return LockResult(result=True, next=serialize_states(states))

        return self.backend.with_lock(edit)

    def _write(self, update: Callable[[dict[str, Any]], None]) -> None:
        def edit(current: str | None) -> LockResult[None]:
            states = parse_states(current)
            update(states)
            return LockResult(result=None, next=serialize_states(states))

        self.backend.with_lock(edit)


def _mtime_or_none(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


async def _acquire_lock(lock_path: str) -> None:
    """``O_EXCL`` 排他锁；陈旧（mtime 超过 :data:`REFRESH_LOCK_STALE_S`）则接管。"""
    deadline = time.monotonic() + REFRESH_LOCK_WAIT_S
    while True:
        try:
            os.mkdir(lock_path, 0o700)
            return
        except FileExistsError:
            mtime = await asyncio.to_thread(_mtime_or_none, lock_path)
            if mtime is not None and time.time() - mtime > REFRESH_LOCK_STALE_S:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(os.rmdir, lock_path)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Could not acquire MCP refresh lock: {lock_path}") from None
            await asyncio.sleep(REFRESH_LOCK_RETRY_S)


def _timed_client(client: httpx.AsyncClient | None = None) -> httpx.AsyncClient:
    """每个请求带 :data:`OAUTH_REQUEST_TIMEOUT_S` 的客户端。

    **偏离（E2）**：上游包装的是 fetch **函数**，并把超时与调用方信号组合。Python 侧的注入点是
    ``OAuthFlowOptions.client``，所以这里给出的是**带超时的客户端**；调用方自带的 client 保有自己的
    超时（没有"信号组合"这一层可叠加）。
    """
    if client is not None:
        return client
    return httpx.AsyncClient(timeout=OAUTH_REQUEST_TIMEOUT_S)


def _registered_redirect_urls(client_information: Any) -> list[str]:
    if isinstance(client_information, dict):
        urls = client_information.get("redirect_uris")
        return list(urls) if isinstance(urls, list) else []
    return []


def callback_id(server_url: str) -> str:
    """回调路径里标识一个 MCP server URL 的 12 个字符（算法同 Codex）。"""
    normalized = _normalized_url(server_url)  # 已去掉 fragment
    digest = hashlib.sha256(normalized.encode()).digest()[:9]
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def client_metadata_document(
    server_url: str,
    redirect_url: str,
    metadata: Any,
) -> dict[str, Any] | None:
    """``clientRegistration: "cimd"`` 时 pi 的 Client ID Metadata Document（选择方式同 Codex）。

    没有授权响应里的 ``iss`` 参数（RFC 9207）时，redirect URI 与文档都**针对该 MCP server**，
    因此响应不会与别的授权服务器的响应混淆（RFC 9700 §4.4.2.2）。
    """
    if not (isinstance(metadata, dict) and metadata.get("client_id_metadata_document_supported")):
        raise ValueError(
            "The authorization server does not support Client ID Metadata Documents for public "
            'clients; remove oauth.clientRegistration "cimd"'
        )
    methods = metadata.get("token_endpoint_auth_methods_supported")
    if not (isinstance(methods, list) and "none" in methods):
        raise ValueError(
            "The authorization server does not support Client ID Metadata Documents for public "
            'clients; remove oauth.clientRegistration "cimd"'
        )
    if metadata.get("authorization_response_iss_parameter_supported"):
        return {"url": f"{CLIENT_METADATA_BASE_URL}/client.json", "redirectUrl": redirect_url}
    identifier = callback_id(server_url)
    parts = urlsplit(redirect_url)
    redirect = urlunsplit((parts.scheme, parts.netloc, f"{CALLBACK_PATH}/{identifier}", "", ""))
    return {"url": f"{CLIENT_METADATA_BASE_URL}/{identifier}/client.json", "redirectUrl": redirect}


def _create_provider(
    server_url: str,
    store: Any,
    settings: McpOAuthSettings,
    redirect_url: str,
    on_redirect: Callable[[str], Any],
    *,
    use_cimd: bool = True,
) -> McpOAuthProvider:
    """构造 :class:`McpOAuthProvider`（E6：``@dataclass`` 入参 + snake_case 关键字）。"""
    document: Any = None
    if settings.client_registration == "cimd" and use_cimd:
        document = lambda metadata: client_metadata_document(server_url, redirect_url, metadata)  # noqa: E731
    return McpOAuthProvider(
        McpOAuthProviderOptions(
            server_url=server_url,
            redirect_url=redirect_url,
            client_metadata={"client_name": settings.client_name or APP_NAME},
            client_metadata_document=document,
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            store=store,
            on_redirect=on_redirect,
        )
    )


class McpAuthProvider:
    """MCP 连接的认证 provider：发送已存访问令牌，临近过期或 401 之后刷新。

    需要用户登录时抛 ``McpOAuthAuthorizationRequiredError``（含 server 要求更多 scope 的
    ``insufficient_scope``）。``on_challenge`` 收到 server 的 ``WWW-Authenticate`` 挑战，
    登录时可用它的 resource metadata URL 与 scope。``settings`` **只在需要刷新时**调用，
    因此解析失败的 secret 只会让刷新失败，而不是整个连接建立失败。

    许多 server 会轮换刷新令牌，所以拿同一个刷新令牌刷两次会丢失授权。本进程内共享一次刷新，
    其它进程由 store 的刷新锁挡住（从读令牌一直持到写回新令牌）。期间若令牌已被别人改过
    （别的进程刷过、或用户登录过），直接用新的、不再刷新。

    :meth:`token` / :meth:`on_unauthorized` 满足 ``pi_mcp.AuthProvider`` 协议。
    """

    def __init__(
        self,
        *,
        server_url: str,
        store: McpOAuthServerStore,
        settings: Callable[[], McpOAuthSettings],
        on_challenge: Callable[[Any], None],
    ) -> None:
        self._server_url = server_url
        self._store = store
        self._settings = settings
        self._on_challenge = on_challenge
        self._refreshing: asyncio.Task[None] | None = None

    async def _refresh(
        self,
        stale_token: str | None,
        client: httpx.AsyncClient | None = None,
        challenge: Any = None,
    ) -> None:
        """替换 ``stale_token``（那个过期或已被拒的访问令牌）。"""
        if self._refreshing is not None:
            await asyncio.shield(self._refreshing)
            return

        async def run() -> None:
            async def locked() -> None:
                state = await self._store.load()
                tokens = (state or {}).get("tokens") or {}
                # E3：TypedDict → 下标访问
                if tokens.get("access_token") != stale_token:
                    return
                if not tokens.get("refresh_token"):
                    raise McpOAuthAuthorizationRequiredError()
                settings = self._settings()
                client_information = (state or {}).get("clientInformation")
                registered_urls = _registered_redirect_urls(client_information)
                redirect_url = (
                    callback_settings(settings).fixed_redirect_url
                    or (registered_urls[0] if registered_urls else None)
                    or FALLBACK_REDIRECT_URL
                )
                provider = _create_provider(
                    self._server_url, self._store, settings, redirect_url, lambda _url: None
                )
                # 刷新令牌，或报告需要重新登录。**不可取消**：server 已答复的刷新可能已经轮换了
                # 刷新令牌，所以必须把它保存下来。
                result = await authorize_mcp(
                    provider,
                    OAuthFlowOptions(
                        server_url=self._server_url,
                        resource_metadata_url=getattr(challenge, "resource_metadata_url", None),
                        authorization_server_metadata_url=settings.auth_server_metadata_url,
                        scope=getattr(challenge, "scope", None),
                        client=_timed_client(client),
                    ),
                )
                if result == "REDIRECT":
                    raise McpOAuthAuthorizationRequiredError()

            await self._store.with_refresh_lock(locked)

        self._refreshing = asyncio.create_task(run())
        try:
            await asyncio.shield(self._refreshing)
        finally:
            self._refreshing = None

    async def token(self) -> str | None:
        if self._refreshing is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._refreshing)
        state = await self._store.load()
        tokens = (state or {}).get("tokens") or {}
        raw_token = tokens.get("access_token")
        token = str(raw_token) if raw_token is not None else None
        expire_at = (state or {}).get("tokensExpireAt")
        expired = expire_at is not None and expire_at - REFRESH_SKEW_MS <= time.time() * 1000
        if not expired or not tokens.get("refresh_token"):
            return token
        # 失败就往下走：请求带旧令牌发出去，由 401 决定后续。
        with contextlib.suppress(Exception):
            await self._refresh(token)
        reloaded = (await self._store.load()) or {}
        latest = reloaded.get("tokens") or {}
        current = latest.get("access_token")
        return str(current) if current is not None else None

    async def on_unauthorized(self, context: Any) -> None:
        challenge = parse_www_authenticate(context.www_authenticate)
        self._on_challenge(challenge)
        # 刷新会保留已授予的 scope，所以更多 scope 需要重新登录。
        if getattr(challenge, "error", None) == "insufficient_scope":
            raise McpOAuthAuthorizationRequiredError()
        await self._refresh(context.token, getattr(context, "client", None), challenge)

    async def settled(self) -> None:
        """没有刷新在跑时立即返回（关闭时不能丢掉已轮换的令牌）。"""
        if self._refreshing is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._refreshing)


def create_mcp_auth_provider(
    *,
    server_url: str,
    store: McpOAuthServerStore,
    settings: Callable[[], McpOAuthSettings],
    on_challenge: Callable[[Any], None],
) -> McpAuthProvider:
    """构造一个 :class:`McpAuthProvider`。"""
    return McpAuthProvider(
        server_url=server_url, store=store, settings=settings, on_challenge=on_challenge
    )


@runtime_checkable
class McpSignInPrompt(Protocol):
    """登录时与用户交互的两件事（宿主实现，CLI/TUI 各一套）。

    对应上游 ``McpSignInPrompt``（TS 接口）。本包无 CLI/TUI，因此这里**只声明契约、不提供实现**
    —— 既不是桩（不会"导入成功但运行才炸"），也不假装有 UI 宿主。
    """

    def show_authorization_url(self, url: str) -> None:
        """把授权 URL 展示给用户并在浏览器里打开。"""
        ...

    async def prompt_for_redirect_url(self, signal: Any) -> str | None:
        """让用户从浏览器地址栏粘贴 redirect URL。

        浏览器够不到 loopback 回调时（例如 SSH）用。回调到达或登录被取消后中止。
        用户取消时返回 ``None`` 或空串。
        """
        ...


class McpSignInCancelledError(Exception):
    """登录被用户或调用方取消。"""

    def __init__(self) -> None:
        super().__init__("Sign-in cancelled")


def response_from_redirect_url(value: str, state: str, redirect_url: str) -> dict[str, Any]:
    """从用户粘贴的 redirect URL 里取出授权响应。"""
    try:
        parts = urlsplit(value.strip())
        if not parts.scheme:
            raise ValueError
    except ValueError:
        raise ValueError("Expected the full redirect URL from the browser address bar") from None

    target = urlsplit(redirect_url)
    # server 专属的 redirect URI 用来区分授权服务器，因此必须精确匹配。
    origin = f"{parts.scheme}://{parts.netloc}"
    target_origin = f"{target.scheme}://{target.netloc}"
    if origin != target_origin or (parts.path or "") != (target.path or ""):
        raise ValueError("The redirect URL does not match this sign-in's redirect URI")

    query = _parse_query(parts.query)
    error = query.get("error")
    if error:
        raise ValueError(query.get("error_description") or error)
    if query.get("state") != state:
        raise ValueError("The redirect URL belongs to a different sign-in")
    code = query.get("code")
    if not code:
        raise ValueError("The redirect URL does not contain an authorization code")
    return {"code": code, "iss": query.get("iss")}


def _parse_query(query: str) -> dict[str, str]:
    from urllib.parse import parse_qs

    return {key: values[0] for key, values in parse_qs(query).items() if values}


async def wait_for_authorization_response(
    callback: OAuthCallbackServer,
    state: str,
    redirect_url: str,
    prompt: McpSignInPrompt,
    signal: Any,
) -> dict[str, Any]:
    """等浏览器回调或用户粘贴的 redirect URL，谁先到用谁。

    **E5**：``wait_for_callback`` 是**同步**返回 ``asyncio.Future``，然后再 await。
    """
    from_browser: asyncio.Future[Any] = callback.wait_for_callback(
        state, urlsplit(redirect_url).path
    )

    async def from_user() -> dict[str, Any]:
        value = await prompt.prompt_for_redirect_url(signal)
        if not value or not value.strip():
            raise McpSignInCancelledError()
        return response_from_redirect_url(value, state, redirect_url)

    user_task = asyncio.ensure_future(from_user())
    try:
        done, pending = await asyncio.wait(
            {from_browser, user_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            outcome = task.result()
            if isinstance(outcome, dict):
                return outcome
            return {"code": getattr(outcome, "code", None), "iss": getattr(outcome, "iss", None)}
        raise McpSignInCancelledError()
    finally:
        # 输的那一侧会在提示被中止或回调服务器关闭时失败。
        for task in (from_browser, user_task):
            if not task.done():
                task.cancel()
            with contextlib.suppress(BaseException):
                task.exception()


async def listen_for_callback(
    settings: CallbackSettings,
    extra_paths: list[str],
    port: int | None,
    required: bool,
) -> OAuthCallbackServer:
    """在 ``port`` 上监听；被占用且非 ``required`` 时改用空闲端口。"""
    options = OAuthCallbackServerOptions(
        host=settings.host,
        redirect_host=settings.redirect_host,
        path=settings.path,
        extra_paths=extra_paths,
        render_page=lambda page: (
            oauth_success_html("Signed in to the MCP server. You may now close this page.")
            if page.ok
            else oauth_error_html(page.message, page.details)
        ),
    )
    if port is None:
        return await OAuthCallbackServer.listen(options)
    try:
        return await OAuthCallbackServer.listen(
            OAuthCallbackServerOptions(
                host=options.host,
                redirect_host=options.redirect_host,
                port=port,
                path=options.path,
                extra_paths=options.extra_paths,
                render_page=options.render_page,
            )
        )
    except OSError:
        if required:
            raise
        return await OAuthCallbackServer.listen(options)


async def sign_in_mcp_server(
    *,
    server_url: str,
    store: McpOAuthServerStore,
    settings: McpOAuthSettings,
    prompt: McpSignInPrompt,
    challenge: Any = None,
    signal: asyncio.Event | None = None,
) -> None:
    """登录一个 MCP server。

    能刷新就刷新；否则走浏览器授权码流程。令牌存进 ``store``。``signal`` 置位会在任一步以
    :class:`McpSignInCancelledError` 停止登录；对授权服务器的请求也有时限。
    """
    if signal is not None and signal.is_set():
        raise McpSignInCancelledError()
    stored = await store.load()
    step_up = getattr(challenge, "error", None) == "insufficient_scope"
    callback_options = callback_settings(settings)
    # 复用已注册 redirect URI 的端口，让已注册的客户端继续有效。
    registered = _registered_redirect_urls((stored or {}).get("clientInformation"))
    registered_url = registered[0] if registered else None
    preferred_port = callback_options.port
    if preferred_port is None and registered_url:
        preferred_port = urlsplit(registered_url).port or None
    cimd = settings.client_registration == "cimd"
    callback = await listen_for_callback(
        callback_options,
        # server 专属 Client ID Metadata Document 的 redirect URI。
        [f"{CALLBACK_PATH}/{callback_id(server_url)}"] if cimd else [],
        preferred_port,
        callback_options.port is not None,
    )
    redirect_url = callback_options.fixed_redirect_url or callback.redirect_url
    try:
        if stored:
            nxt: dict[str, Any] = {**stored}
            # 每次登录都用新的 ``state`` 参数。
            nxt.pop("oauthState", None)
            # 已注册的客户端不能用别的 redirect URI，其令牌也属于它自己。Client ID Metadata
            # Document 不入库，所以用它时若库里有客户端，那是更早注册的，会被替换。
            keep_client = bool(settings.client_id) or (
                not _registered_redirect_urls((stored or {}).get("clientInformation"))
                if cimd
                else redirect_url
                in _registered_redirect_urls((stored or {}).get("clientInformation"))
            )
            if not keep_client:
                nxt.pop("clientInformation", None)
                nxt.pop("tokens", None)
                nxt.pop("tokensExpireAt", None)
            await store.save(nxt)

        authorization_url: dict[str, str] = {}

        def on_redirect(url: str) -> None:
            authorization_url["url"] = url

        provider = _create_provider(server_url, store, settings, redirect_url, on_redirect)
        tokens = (stored or {}).get("tokens") or {}

        def flow(**extra: Any) -> OAuthFlowOptions:
            return OAuthFlowOptions(
                server_url=server_url,
                client=_timed_client(),
                signal=signal,
                resource_metadata_url=getattr(challenge, "resource_metadata_url", None),
                authorization_server_metadata_url=settings.auth_server_metadata_url,
                # 要求更多 scope 的 server 会得到「配置的 scope + 已授予的 scope + 挑战里的 scope」
                # 的合集，因为挑战可能只列了缺的那些。
                scope=merge_scopes(
                    settings.scope,
                    step_up_scope(tokens.get("scope"), getattr(challenge, "scope", None))
                    if step_up
                    else getattr(challenge, "scope", None),
                ),
                **extra,
            )

        # 刷新会保留已授予的 scope；server 要求更多时得走浏览器流程。
        if await authorize_mcp(provider, flow(skip_refresh=step_up)) == "AUTHORIZED":
            return
        if not authorization_url:
            raise RuntimeError("OAuth flow did not produce an authorization URL")

        state = await provider.state()
        # 流程会挑 redirect URI，它可能是该 MCP server 专属的。
        authorization_redirect_url = (
            _parse_query(urlsplit(authorization_url["url"]).query).get("redirect_uri")
            or redirect_url
        )
        prompt.show_authorization_url(authorization_url["url"])
        response = await wait_for_authorization_response(
            callback, state or "", authorization_redirect_url, prompt, signal
        )
        code = response.get("code")
        issuer = response.get("iss")
        await authorize_mcp(
            provider,
            flow(
                authorization_code=str(code) if code is not None else None,
                iss=str(issuer) if issuer is not None else None,
            ),
        )
    except BaseException:
        # 被中止的请求以信号的缘由失败；把它们如实报告为"取消"。
        if signal is not None and signal.is_set():
            raise McpSignInCancelledError() from None
        raise
    finally:
        await callback.close()
