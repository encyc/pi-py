"""默认的有状态 ``OAuthClientProvider`` 实现。

对应上游 ``packages/mcp/src/oauth/provider.ts``（v1.3.0，168 行）。
"""

from __future__ import annotations

import asyncio
import copy
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict, cast

from .flow import OAuthClientMetadataDocument, OAuthClientProvider
from .types import (
    AuthorizationServerMetadata,
    OAuthClientInformationMixed,
    OAuthClientMetadata,
    OAuthDiscoveryState,
    OAuthTokens,
)


class McpOAuthState(TypedDict, total=False):
    """一个 MCP 服务端的持久化授权状态。"""

    serverUrl: str
    clientInformation: OAuthClientInformationMixed
    tokens: OAuthTokens
    #: 访问令牌的过期时刻（自 epoch 起的毫秒），来自保存时的 ``expires_in``。
    tokensExpireAt: float
    codeVerifier: str
    oauthState: str
    discovery: OAuthDiscoveryState


class McpOAuthStateStore(Protocol):
    """状态存储。``load`` / ``save`` 都可以是同步或异步的（对应上游的联合返回类型）。"""

    def load(self) -> McpOAuthState | None | Awaitable[McpOAuthState | None]: ...

    def save(self, state: McpOAuthState) -> None | Awaitable[None]: ...


@dataclass
class McpOAuthProviderOptions:
    """``McpOAuthProvider`` 的构造参数。对应上游 ``McpOAuthProviderOptions``。"""

    server_url: str
    redirect_url: str
    client_metadata: OAuthClientMetadata
    #: 见 ``OAuthClientProvider.client_metadata_document``。
    client_metadata_document: (
        Callable[[AuthorizationServerMetadata | None], OAuthClientMetadataDocument | None] | None
    ) = None
    client_id: str | None = None
    client_secret: str | None = None
    store: McpOAuthStateStore | None = None
    on_redirect: Callable[[str], None | Awaitable[None]] = lambda url: None


class MemoryOAuthStateStore:
    """进程内状态存储（默认）。深拷贝进出，避免调用方改到内部对象。"""

    def __init__(self) -> None:
        self._value: McpOAuthState | None = None

    def load(self) -> McpOAuthState | None:
        return None if self._value is None else copy.deepcopy(self._value)

    def save(self, state: McpOAuthState) -> None:
        self._value = copy.deepcopy(state)


async def _maybe_await(value: object) -> object:
    if isinstance(value, Awaitable):
        return await value
    return value


class McpOAuthProvider(OAuthClientProvider):
    """针对**某一个确切 MCP 服务端 URL** 的默认有状态 provider。

    应用需要持久化时注入自己的 :class:`McpOAuthStateStore`。
    """

    def __init__(self, options: McpOAuthProviderOptions) -> None:
        self._server_url = options.server_url
        self.redirect_url = options.redirect_url
        metadata: OAuthClientMetadata = {
            **options.client_metadata,
            "redirect_uris": options.client_metadata.get("redirect_uris") or [self.redirect_url],
            "grant_types": options.client_metadata.get("grant_types")
            or ["authorization_code", "refresh_token"],
            "response_types": options.client_metadata.get("response_types") or ["code"],
            "token_endpoint_auth_method": options.client_metadata.get("token_endpoint_auth_method")
            or ("client_secret_post" if options.client_secret else "none"),
        }
        self.client_metadata = metadata
        self._client_metadata_document = options.client_metadata_document
        self._configured_client: OAuthClientInformationMixed | None = (
            cast(
                "OAuthClientInformationMixed",
                {
                    "client_id": options.client_id,
                    **({"client_secret": options.client_secret} if options.client_secret else {}),
                },
            )
            if options.client_id
            else None
        )
        self._store: McpOAuthStateStore = options.store or MemoryOAuthStateStore()
        self._on_redirect = options.on_redirect
        self._lock = asyncio.Lock()
        self.add_client_authentication = None

    # ---- OAuthClientProvider ----

    def client_metadata_document(
        self, metadata: AuthorizationServerMetadata | None
    ) -> OAuthClientMetadataDocument | None:
        if self._client_metadata_document is None:
            return None
        return self._client_metadata_document(metadata)

    def can_save_client_information(self) -> bool:
        return True

    async def state(self) -> str:
        existing = (await self._load()).get("oauthState")
        if existing:
            return existing
        state = secrets.token_hex(32)
        await self._update(lambda value: {**value, "oauthState": state})
        return state

    async def client_information(self) -> OAuthClientInformationMixed | None:
        return self._configured_client or (await self._load()).get("clientInformation")

    async def save_client_information(self, information: OAuthClientInformationMixed) -> None:
        if self._configured_client:
            return
        await self._update(lambda value: {**value, "clientInformation": information})

    async def tokens(self) -> OAuthTokens | None:
        return (await self._load()).get("tokens")

    async def save_tokens(self, tokens: OAuthTokens) -> None:
        expires_in = tokens.get("expires_in")
        expires_at = None if expires_in is None else _now_ms() + expires_in * 1000

        def _apply(value: McpOAuthState) -> McpOAuthState:
            next_value: McpOAuthState = {**value, "tokens": tokens}
            if expires_at is None:
                next_value.pop("tokensExpireAt", None)
            else:
                next_value["tokensExpireAt"] = expires_at
            return next_value

        await self._update(_apply)

    async def redirect_to_authorization(self, url: str) -> None:
        await _maybe_await(self._on_redirect(url))

    async def save_code_verifier(self, verifier: str) -> None:
        await self._update(lambda value: {**value, "codeVerifier": verifier})

    async def code_verifier(self) -> str:
        verifier = (await self._load()).get("codeVerifier")
        if not verifier:
            raise RuntimeError("No OAuth PKCE code verifier is stored")
        return verifier

    async def invalidate_credentials(
        self, kind: Literal["all", "client", "tokens", "verifier", "discovery"] = "all"
    ) -> None:
        def _apply(value: McpOAuthState) -> McpOAuthState:
            next_value: McpOAuthState = {**value}
            if kind in ("all", "client"):
                next_value.pop("clientInformation", None)
            if kind in ("all", "tokens"):
                next_value.pop("tokens", None)
                next_value.pop("tokensExpireAt", None)
            if kind in ("all", "verifier"):
                next_value.pop("codeVerifier", None)
            if kind in ("all", "discovery"):
                next_value.pop("discovery", None)
            if kind == "all":
                next_value.pop("oauthState", None)
            return next_value

        await self._update(_apply)

    async def save_discovery_state(self, state: OAuthDiscoveryState) -> None:
        await self._update(lambda value: {**value, "discovery": state})

    async def discovery_state(self) -> OAuthDiscoveryState | None:
        return (await self._load()).get("discovery")

    # ---- 内部 ----

    async def _load(self) -> McpOAuthState:
        async with self._lock:
            stored = cast("McpOAuthState | None", await _maybe_await(self._store.load()))
            return self._own(stored)

    async def _update(self, update: Callable[[McpOAuthState], McpOAuthState]) -> None:
        # 用锁把「读-改-写」串起来（对应上游把 promise 串成一条链）。
        async with self._lock:
            stored = cast("McpOAuthState | None", await _maybe_await(self._store.load()))
            await _maybe_await(self._store.save(update(self._own(stored))))

    def _own(self, state: McpOAuthState | None) -> McpOAuthState:
        """别个服务端的存储状态一律忽略，凭据不会跨服务端泄漏。"""
        if state is not None and state.get("serverUrl") == self._server_url:
            return state
        return {"serverUrl": self._server_url}


def _now_ms() -> float:
    """当前时间（自 epoch 起的毫秒）。"""
    return time.time() * 1000


__all__ = [
    "McpOAuthProvider",
    "McpOAuthProviderOptions",
    "McpOAuthState",
    "McpOAuthStateStore",
    "MemoryOAuthStateStore",
]
