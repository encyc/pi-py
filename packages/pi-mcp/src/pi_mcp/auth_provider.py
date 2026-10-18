"""认证提供者契约（**仅类型**）。

对应上游 ``packages/mcp/src/auth-provider.ts``（v1.3.0，20 行）。

> **期次说明**：本文件只移植**类型声明**，它是 ``transports/streamable_http.py`` 的硬依赖
> （401/403 的授权交接）。OAuth 的**实体实现**（上游 ``src/oauth/`` 7 个文件：
> flow / discovery / provider / callback / errors / types / index）属于**第二期**，
> 本包第一期不提供 —— 理由见 ``PORTING.md``。

偏离（见 ``PORTING.md`` D6）：上游的 ``McpFetch`` 是 WHATWG ``fetch``；本端 HTTP 传输直接用
``httpx.AsyncClient``，因此注入点是「一个 httpx 客户端」：``UnauthorizedContext.client``。

``McpFetch`` 这个名字仍然导出，但它就是 ``httpx.AsyncClient`` 的别名 —— 保留是为了让
下游（``pi-coding-agent`` 的 mcp 扩展）的 import 形状与上游一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import httpx

#: 上游 ``McpFetch``（WHATWG ``fetch`` 的可注入实现）。
#:
#: 本端没有 WHATWG fetch：HTTP 传输与 OAuth 流程都通过注入的 ``httpx.AsyncClient`` 工作，
#: 所以这个名字等价于 :class:`httpx.AsyncClient`（见 ``PORTING.md`` D6）。
McpFetch = httpx.AsyncClient


@dataclass(frozen=True)
class UnauthorizedContext:
    """被拒绝请求的上下文，交给 ``AuthProvider.on_unauthorized``。

    对应上游 ``UnauthorizedContext``：``status`` / ``www_authenticate`` / ``body`` 描述那次
    拒绝（401，或挑战里带 ``insufficient_scope`` 的 403），``server_url`` 是目标地址，
    ``token`` 是被拒请求携带的访问令牌（若有）。**另一个请求已经刷新过令牌时，
    ``token`` 会与当前令牌不同**，据此可以判断是否需要再刷。

    ``client`` 是上游的 ``fetch``：OAuth 流程要用**同一个** HTTP 客户端继续请求
    （连接池与超时一致，测试里也能注入假客户端）。
    """

    status: int
    server_url: str
    www_authenticate: str | None = None
    body: str = ""
    token: str | None = None
    client: httpx.AsyncClient | None = None


@runtime_checkable
class AuthProvider(Protocol):
    """为 MCP HTTP 传输提供 bearer 令牌，并可在 401 之后刷新。

    对应上游 ``AuthProvider``。
    """

    async def token(self) -> str | None:
        """下一次请求用的令牌。可能先走网络刷新，因此传输只在「等待其答复」的请求前调用它；
        关闭会话时复用最后一个请求的令牌，不再询问。"""
        ...

    async def on_unauthorized(self, context: UnauthorizedContext) -> None:
        """401（或要求更多 scope 的 403）被拒绝后调用一次，然后原样重发。

        可选：不实现即视为「不处理授权失败」。
        """
        ...


@dataclass
class BearerAuthProvider:
    """最简 ``AuthProvider``：固定令牌，不做刷新。

    第一期只随包提供一个**非交互**实现，供调用方注入现成令牌（交互式 OAuth 属第二期）。
    它也是 ``on_unauthorized`` 的默认行为示例：不覆写即不处理。
    """

    access_token: str | None = None
    refreshed: list[UnauthorizedContext] = field(default_factory=list)

    async def token(self) -> str | None:
        return self.access_token

    async def on_unauthorized(self, context: UnauthorizedContext) -> None:
        self.refreshed.append(context)


__all__ = ["AuthProvider", "BearerAuthProvider", "McpFetch", "UnauthorizedContext"]
