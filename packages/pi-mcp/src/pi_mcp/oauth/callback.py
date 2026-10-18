"""本地回环 OAuth 回调服务器（浏览器重定向的接收端）。

对应上游 ``packages/mcp/src/oauth/callback.ts``（v1.3.0，164 行）。

> ⚠️ **安全边界**：这个类会在**本机回环地址**上绑定一个 TCP 端口，并接收浏览器带来的
> ``?code=&state=``；它还会渲染一个页面到浏览器。逐条的差异与风险处置见 ``PORTING.md``
> 的「OAuthCallbackServer 的安全边界」一节。

实现说明：上游用 ``node:http``；Python 侧用 ``http.server.ThreadingHTTPServer``
（每连接一线程）跑在**后台线程**里，HTTP 处理器通过 ``loop.call_soon_threadsafe``
把回调交回 asyncio 循环 —— 因为 :class:`OAuthCallbackServer` 的等待方在事件循环里。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

#: 单次授权的等待上限（默认 5 分钟）。
DEFAULT_CALLBACK_TIMEOUT_MS = 5 * 60_000


@dataclass(frozen=True)
class OAuthCallback:
    """浏览器带回来的授权响应。对应上游 ``OAuthCallback``。"""

    code: str
    state: str
    #: RFC 9207 的 ``iss`` 参数（缺失时为 ``None``）。
    iss: str | None = None


@dataclass(frozen=True)
class OAuthCallbackPage:
    """重定向之后展示给用户的页面结果。对应上游 ``OAuthCallbackPage``。

    上游是 ``{ok: true} | {ok: false; message; details?}`` 的联合；Python 侧用一个
    dataclass（``ok`` 做判别），并提供 :meth:`success` / :meth:`failure` 两个构造器。
    """

    ok: bool
    message: str = ""
    details: str | None = None

    @classmethod
    def success(cls) -> OAuthCallbackPage:
        """成功页。"""
        return cls(ok=True)

    @classmethod
    def failure(cls, message: str, details: str | None = None) -> OAuthCallbackPage:
        """失败页。"""
        return cls(ok=False, message=message, details=details)


@dataclass
class OAuthCallbackServerOptions:
    """``OAuthCallbackServer.listen`` 的选项。对应上游 ``OAuthCallbackServerOptions``。"""

    #: 监听地址，默认 ``127.0.0.1``（**只绑回环**）。
    host: str = "127.0.0.1"
    #: ``redirect_url`` 里用的主机名。例如客户端按 ``localhost`` 注册、而实际监听 ``127.0.0.1``。
    #: 默认等于 ``host``。
    redirect_host: str | None = None
    #: 监听端口。**默认 0 = 让内核分配空闲端口**（推荐；见 PORTING.md 的端口一节）。
    port: int = 0
    path: str = "/callback"
    #: 还能接收回调的其它路径（例如某个服务端专属的 redirect URI 路径）。
    extra_paths: list[str] | None = None
    timeout_ms: int = DEFAULT_CALLBACK_TIMEOUT_MS
    #: 用 HTML 渲染浏览器页面。默认是纯文本消息。
    render_page: Callable[[OAuthCallbackPage], str] | None = None


def _plain_text(page: OAuthCallbackPage) -> str:
    if page.ok:
        return "Authorization complete. You may close this window."
    return f"{page.message}\n\n{page.details}" if page.details else page.message


@dataclass
class _Pending:
    future: asyncio.Future[OAuthCallback]
    loop: asyncio.AbstractEventLoop
    timer: asyncio.TimerHandle
    path: str | None


class _CallbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pi-mcp-oauth"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 按名字分发
        server: OAuthCallbackServer = self.server.owner  # type: ignore[attr-defined]
        server._handle(self.path, self)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 对齐基类签名
        """静音访问日志（授权码等敏感信息不进日志）。"""

    # ---- 供 OAuthCallbackServer 使用的应答原语 ----

    def _reply(
        self,
        status: int,
        page: OAuthCallbackPage,
        render: Callable[[OAuthCallbackPage], str] | None,
    ) -> None:
        if render is not None:
            body = render(page).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        else:
            body = _plain_text(page).encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        with contextlib.suppress(OSError, ValueError):
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()


class OAuthCallbackServer:
    """接收 OAuth 授权响应的回环 HTTP 服务器。"""

    def __init__(
        self,
        server: ThreadingHTTPServer,
        redirect_url: str,
        paths: list[str],
        timeout_ms: int,
        render_page: Callable[[OAuthCallbackPage], str] | None,
    ) -> None:
        self._server = server
        self.redirect_url = redirect_url
        self._paths = paths
        self._timeout_ms = timeout_ms
        self._render_page = render_page
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @classmethod
    async def listen(cls, options: OAuthCallbackServerOptions | None = None) -> OAuthCallbackServer:
        """在回环地址上起一个回调服务器。端口默认由内核分配。"""
        opts = options or OAuthCallbackServerOptions()
        host = opts.host
        redirect_host = opts.redirect_host or host
        path = opts.path
        try:
            server = ThreadingHTTPServer((host, opts.port), _CallbackHandler)
        except OSError as error:
            # 端口被占（例如 Windows 上 Hyper-V/WSL 的端口排除名单）时**明确报错**，
            # 不静默换端口 —— 换端口会让已注册的 redirect URI 对不上。见 PORTING.md。
            raise OSError(
                f"OAuth callback server could not bind {host}:{opts.port}: {error}"
            ) from error
        server.daemon_threads = True
        server.owner = None  # type: ignore[attr-defined]
        port = server.server_address[1]
        display_host = f"[{redirect_host}]" if ":" in redirect_host else redirect_host
        instance = cls(
            server,
            f"http://{display_host}:{port}{path}",
            [path, *(opts.extra_paths or [])],
            opts.timeout_ms,
            opts.render_page,
        )
        server.owner = instance  # type: ignore[attr-defined]
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        instance._thread = thread
        return instance

    def wait_for_callback(
        self, state: str, path: str | None = None
    ) -> asyncio.Future[OAuthCallback]:
        """等待带 ``state`` 的授权响应。

        给了 ``path`` 时，落在别的路径上的响应会失败 —— 让服务端专属的 redirect URI 能区分开
        （RFC 9700 §4.4.2.2）。返回的 ``Future`` 在超时或服务器关闭时会收到异常。
        """
        with self._lock:
            if state in self._pending:
                raise RuntimeError("OAuth state is already pending")
            loop = asyncio.get_running_loop()
            future: asyncio.Future[OAuthCallback] = loop.create_future()
            timer = loop.call_later(
                self._timeout_ms / 1000, self._expire, state, "OAuth callback timed out"
            )
            self._pending[state] = _Pending(future=future, loop=loop, timer=timer, path=path)
        return future

    async def close(self) -> None:
        """关闭服务器，并让所有在等的回调失败。"""
        with self._lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for _state, item in pending:
            item.loop.call_soon_threadsafe(item.timer.cancel)
            _settle(item.future, item.loop, RuntimeError("OAuth callback server closed"))
        await asyncio.to_thread(self._server.shutdown)
        self._server.server_close()

    # ---- HTTP 处理（在后台线程里被调用） ----

    def _handle(self, raw_url: str, handler: _CallbackHandler) -> None:
        url = urlsplit(raw_url)
        if url.path not in self._paths:
            handler._reply(404, OAuthCallbackPage.failure("Not found"), self._render_page)
            return
        query = parse_qs(url.query, keep_blank_values=True)
        state = _first(query, "state")
        with self._lock:
            pending = self._pending.pop(state, None) if state else None
        if not state or pending is None:
            handler._reply(
                400, OAuthCallbackPage.failure("Invalid or expired OAuth state"), self._render_page
            )
            return
        pending.loop.call_soon_threadsafe(pending.timer.cancel)
        if pending.path is not None and url.path != pending.path:
            _settle(
                pending.future,
                pending.loop,
                RuntimeError("The authorization response arrived on another redirect URI"),
            )
            handler._reply(
                400, OAuthCallbackPage.failure("Unexpected redirect URI"), self._render_page
            )
            return
        error = _first(query, "error")
        if error:
            description = _first(query, "error_description") or error
            _settle(pending.future, pending.loop, RuntimeError(description))
            handler._reply(
                200,
                OAuthCallbackPage.failure(
                    "Authorization failed. You may close this window.", description
                ),
                self._render_page,
            )
            return
        code = _first(query, "code")
        if not code:
            _settle(
                pending.future,
                pending.loop,
                RuntimeError("OAuth callback did not include an authorization code"),
            )
            handler._reply(
                400, OAuthCallbackPage.failure("Missing authorization code"), self._render_page
            )
            return
        iss = _first(query, "iss")
        callback = OAuthCallback(code=code, state=state, iss=iss or None)
        pending.loop.call_soon_threadsafe(_resolve, pending.future, callback)
        handler._reply(200, OAuthCallbackPage.success(), self._render_page)

    def _expire(self, state: str, message: str) -> None:
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is not None and not pending.future.done():
            pending.future.set_exception(RuntimeError(message))


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _settle(
    future: asyncio.Future[OAuthCallback], loop: asyncio.AbstractEventLoop, error: Exception
) -> None:
    loop.call_soon_threadsafe(_reject, future, error)


def _resolve(future: asyncio.Future[OAuthCallback], callback: OAuthCallback) -> None:
    if not future.done():
        future.set_result(callback)


def _reject(future: asyncio.Future[OAuthCallback], error: Exception) -> None:
    if not future.done():
        future.set_exception(error)


__all__ = [
    "DEFAULT_CALLBACK_TIMEOUT_MS",
    "OAuthCallback",
    "OAuthCallbackPage",
    "OAuthCallbackServer",
    "OAuthCallbackServerOptions",
]
