"""OAuth 授权码流程（PKCE）、动态客户端注册、令牌刷新，以及 ``AuthProvider`` 适配。

对应上游 ``packages/mcp/src/oauth/flow.ts``（v1.3.0，482 行）。

上游注释：*"Adapted from modelcontextprotocol/typescript-sdk v1.29.0 src/client/auth.ts.
Copyright (c) 2024 Anthropic, PBC. Licensed under MIT. Modified to remove SDK/Zod dependencies
and use WebCrypto for PKCE."*

移植约定（见 ``PORTING.md``）：
- ``McpFetch`` → 注入 ``httpx.AsyncClient``
- ``AbortSignal`` → ``asyncio.Event``（置位即抛 ``McpAbortError``，且**不**回退到 redirect）
- ``Headers`` + ``URLSearchParams`` → ``dict[str, str]`` + ``list[tuple[str, str]]``
- WebCrypto PKCE（``crypto.subtle.digest``）→ ``secrets`` + ``hashlib``
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx

from .._cancel import await_or_abort
from ..auth_provider import AuthProvider, UnauthorizedContext
from ..protocol.jsonrpc import McpAbortError, is_object
from .discovery import (
    discover_authorization_server_metadata,
    discover_oauth_server_info,
    parse_www_authenticate,
    select_resource,
)
from .errors import (
    McpOAuthAuthorizationRequiredError,
    OAuthError,
    OAuthInsecureEndpointError,
    OAuthIssuerMismatchError,
    OAuthRegistrationError,
)
from .types import (
    AuthorizationServerMetadata,
    OAuthClientInformation,
    OAuthClientInformationMixed,
    OAuthClientMetadata,
    OAuthDiscoveryState,
    OAuthServerInfo,
    OAuthTokens,
    parse_client_information,
    parse_oauth_tokens,
)

#: 端点请求的超时。上游没有（Node 的 fetch 默认无超时）；本端补一个以免永久挂住。
DEFAULT_FLOW_TIMEOUT_SECONDS = 30.0

#: 自定义客户端认证回调：可改写请求头与表单参数。对应上游 ``AddClientAuthentication``。
AddClientAuthentication = Callable[
    [dict[str, str], list[tuple[str, str]], str, AuthorizationServerMetadata | None],
    None | Awaitable[None],
]

#: 客户端认证方式。
ClientAuthMethod = Literal["client_secret_basic", "client_secret_post", "none"]

#: 流程结果：已拿到令牌（``AUTHORIZED``），或需要用户去授权（``REDIRECT``，已跳转）。
OAuthFlowResult = Literal["AUTHORIZED", "REDIRECT"]


@dataclass(frozen=True)
class OAuthClientMetadataDocument:
    """Client ID Metadata Document：用 https URL 当 ``client_id``，并给出它列出的 redirect URI。"""

    url: str
    redirect_url: str


@dataclass
class OAuthFlowOptions:
    """``authorize_mcp`` 的入参。对应上游 ``OAuthFlowOptions``。"""

    server_url: str
    authorization_code: str | None = None
    #: 交付 ``authorization_code`` 的授权响应的 ``iss`` 参数（RFC 9207）。
    iss: str | None = None
    scope: str | None = None
    resource_metadata_url: str | None = None
    #: 直接使用该授权服务器元数据文档而不是做发现（**按配置信任**）。除回环外必须 https。
    authorization_server_metadata_url: str | None = None
    client: httpx.AsyncClient | None = None
    #: 中止流程里的每个请求（请求自身没有时限，需要时请配超时）。
    signal: asyncio.Event | None = None
    skip_issuer_validation: bool = False
    #: 跳过刷新、直接走授权跳转 —— 例如服务端要求当前授权没有的 scope（刷新会保留旧 scope）。
    skip_refresh: bool = False


@dataclass
class TokenRequestOptions:
    """令牌端点的调用参数。对应上游 ``TokenRequestOptions``。"""

    client_information: OAuthClientInformationMixed
    metadata: AuthorizationServerMetadata | None = None
    resource: str | None = None
    add_client_authentication: AddClientAuthentication | None = None
    client: httpx.AsyncClient | None = None
    signal: asyncio.Event | None = None


class OAuthClientProvider:
    """流程需要的客户端状态。对应上游 ``OAuthClientProvider``（TS 接口）。

    必需成员在基类里抛 ``NotImplementedError``；可选成员给出中性默认值
    （返回 ``None`` 或什么也不做），与上游的 ``method?()`` 语义一致。
    最小实现见 :class:`pi_mcp.oauth.provider.McpOAuthProvider`。
    """

    #: 授权服务器把用户导回来的 redirect URI。
    redirect_url: str
    #: 注册/标识用的客户端元数据。
    client_metadata: OAuthClientMetadata
    #: 自定义客户端认证回调（可选）。
    add_client_authentication: AddClientAuthentication | None = None

    def client_metadata_document(
        self, metadata: AuthorizationServerMetadata | None
    ) -> OAuthClientMetadataDocument | None:
        """改用 Client ID Metadata Document 标识自己（而不是动态注册）时返回它。

        在没有存储的客户端信息时调用；文档**不会**被存储。
        """
        _ = metadata
        return None

    def can_save_client_information(self) -> bool:
        """能否持久化动态注册结果（上游用「方法是否存在」表示）。"""
        return type(self).save_client_information is not OAuthClientProvider.save_client_information

    async def state(self) -> str | None:
        """本次授权的 ``state``；``None`` 表示不带 ``state``。"""
        return None

    async def client_information(self) -> OAuthClientInformationMixed | None:
        """已存储的客户端凭据。"""
        raise NotImplementedError

    async def save_client_information(self, information: OAuthClientInformationMixed) -> None:
        """保存动态注册结果。"""
        _ = information
        raise NotImplementedError

    async def tokens(self) -> OAuthTokens | None:
        """已存储的令牌。"""
        raise NotImplementedError

    async def save_tokens(self, tokens: OAuthTokens) -> None:
        """保存令牌。"""
        _ = tokens
        raise NotImplementedError

    async def redirect_to_authorization(self, url: str) -> None:
        """把用户导向授权 URL。"""
        _ = url
        raise NotImplementedError

    async def save_code_verifier(self, verifier: str) -> None:
        """保存 PKCE code verifier。"""
        _ = verifier
        raise NotImplementedError

    async def code_verifier(self) -> str:
        """取回 PKCE code verifier。"""
        raise NotImplementedError

    async def invalidate_credentials(
        self, kind: Literal["all", "client", "tokens", "verifier", "discovery"]
    ) -> None:
        """作废存储的凭据（可选）。"""
        _ = kind

    async def save_discovery_state(self, state: OAuthDiscoveryState) -> None:
        """缓存发现结果（可选）。"""
        _ = state

    async def discovery_state(self) -> OAuthDiscoveryState | None:
        """读取缓存的发现结果（可选）。"""
        return None


@contextlib.asynccontextmanager
async def _client_scope(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    """借用注入的客户端，或临时自建一个并在结束时关闭。"""
    if client is not None:
        yield client
        return
    owned = httpx.AsyncClient(timeout=DEFAULT_FLOW_TIMEOUT_SECONDS)
    try:
        yield owned
    finally:
        await owned.aclose()


async def _maybe_await(value: Any) -> Any:
    if isinstance(value, Awaitable):
        return await value
    return value


def _loopback(hostname: str) -> bool:
    return hostname in ("localhost", "127.0.0.1", "[::1]", "::1")


def _application_type(redirect_uris: list[str]) -> Literal["native", "web"]:
    """``redirect_uris`` 对应的 OIDC ``application_type``（MCP SEP-837）。

    没有它时 OIDC 服务器按 ``web`` 处理，会拒绝 http 回环 redirect URI；
    回环主机与自定义 scheme 属于 ``native``（RFC 8252）。
    """
    for uri in redirect_uris:
        parts = urlsplit(uri)
        if not parts.scheme:
            continue
        if parts.scheme not in ("http", "https") or _loopback(parts.hostname or ""):
            return "native"
    return "web"


def _secure_endpoint(value: str) -> str:
    """端点必须 https（回环除外）。"""
    parts = urlsplit(value)
    if parts.scheme != "https" and not _loopback(parts.hostname or ""):
        raise OAuthInsecureEndpointError(value)
    return value


def _urljoin_path(base: str, path: str) -> str:
    parts = urlsplit(base)
    return f"{parts.scheme}://{parts.netloc}{path}"


def _select_client_auth_method(
    information: OAuthClientInformationMixed, supported: list[str]
) -> ClientAuthMethod:
    hinted = information.get("token_endpoint_auth_method")
    if (
        hinted
        and hinted in ("client_secret_basic", "client_secret_post", "none")
        and (not supported or hinted in supported)
    ):
        return hinted
    if not supported:
        return "client_secret_basic" if information.get("client_secret") else "none"
    if information.get("client_secret") and "client_secret_basic" in supported:
        return "client_secret_basic"
    if information.get("client_secret") and "client_secret_post" in supported:
        return "client_secret_post"
    if "none" in supported:
        return "none"
    return "client_secret_post" if information.get("client_secret") else "none"


def _apply_client_authentication(
    method: ClientAuthMethod,
    information: OAuthClientInformation,
    headers: dict[str, str],
    params: list[tuple[str, str]],
) -> None:
    if method == "client_secret_basic":
        secret = information.get("client_secret")
        if not secret:
            raise RuntimeError("client_secret_basic requires a client secret")
        raw = f"{information['client_id']}:{secret}".encode()
        headers["Authorization"] = f"Basic {base64.b64encode(raw).decode('ascii')}"
    else:
        params.append(("client_id", information["client_id"]))
        if method == "client_secret_post" and information.get("client_secret"):
            params.append(("client_secret", information["client_secret"]))


def _pkce() -> tuple[str, str]:
    """生成 PKCE ``(verifier, challenge)``（S256）。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


async def start_authorization(
    authorization_server_url: str,
    *,
    client_information: OAuthClientInformationMixed,
    redirect_url: str,
    metadata: AuthorizationServerMetadata | None = None,
    scope: str | None = None,
    state: str | None = None,
    resource: str | None = None,
) -> tuple[str, str]:
    """构造授权 URL，返回 ``(authorization_url, code_verifier)``。"""
    if metadata is not None and "code" not in metadata.get("response_types_supported", []):
        raise RuntimeError("Authorization server does not support authorization codes")
    challenges = metadata.get("code_challenge_methods_supported") if metadata is not None else None
    if challenges and "S256" not in challenges:
        raise RuntimeError("Authorization server does not support PKCE S256")
    endpoint = (metadata or {}).get("authorization_endpoint") or _urljoin_path(
        authorization_server_url, "/authorize"
    )
    verifier, challenge = _pkce()
    query: list[tuple[str, str]] = [
        ("response_type", "code"),
        ("client_id", client_information["client_id"]),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
        ("redirect_uri", redirect_url),
    ]
    if state:
        query.append(("state", state))
    if scope:
        query.append(("scope", scope))
    if scope and "offline_access" in scope.split():
        query.append(("prompt", "consent"))
    if resource:
        query.append(("resource", resource))
    glue = "&" if urlsplit(endpoint).query else "?"
    return f"{endpoint}{glue}{urlencode(query)}", verifier


async def _token_request(
    authorization_server_url: str,
    options: TokenRequestOptions,
    params: list[tuple[str, str]],
) -> OAuthTokens:
    endpoint = (options.metadata or {}).get("token_endpoint") or _urljoin_path(
        authorization_server_url, "/token"
    )
    url = _secure_endpoint(endpoint)
    headers = {"Accept": "application/json", "content-type": "application/x-www-form-urlencoded"}
    if options.resource:
        params.append(("resource", options.resource))
    if options.add_client_authentication is not None:
        await _maybe_await(
            options.add_client_authentication(headers, params, url, options.metadata)
        )
    else:
        method = _select_client_auth_method(
            options.client_information,
            (options.metadata or {}).get("token_endpoint_auth_methods_supported", []),
        )
        _apply_client_authentication(method, options.client_information, headers, params)
    if options.signal is not None and options.signal.is_set():
        raise McpAbortError()
    async with _client_scope(options.client) as http:
        response = await await_or_abort(
            http.post(url, headers=headers, content=urlencode(params)), options.signal
        )
        text = response.text
    value: Any = None
    with contextlib.suppress(ValueError):
        value = json.loads(text)
    # 服务端可能用任意状态码报 OAuth 错误，所以先看 body 再看状态码。
    if is_object(value) and isinstance(value.get("error"), str):
        raise OAuthError(
            value["error"],
            value["error_description"]
            if isinstance(value.get("error_description"), str)
            else value["error"],
            value["error_uri"] if isinstance(value.get("error_uri"), str) else None,
        )
    if not response.is_success:
        raise OAuthError("server_error", f"HTTP {response.status_code}: {text}")
    return parse_oauth_tokens(value)


async def register_client(
    authorization_server_url: str,
    *,
    client_metadata: OAuthClientMetadata,
    metadata: AuthorizationServerMetadata | None = None,
    scope: str | None = None,
    client: httpx.AsyncClient | None = None,
    signal: asyncio.Event | None = None,
) -> OAuthClientInformation:
    """动态客户端注册（RFC 7591）。"""
    endpoint = (metadata or {}).get("registration_endpoint")
    if metadata is not None and not endpoint:
        raise RuntimeError("Authorization server does not support dynamic client registration")
    body: dict[str, Any] = {
        **client_metadata,
        "application_type": client_metadata.get("application_type")
        or _application_type(client_metadata.get("redirect_uris", [])),
    }
    if scope:
        body["scope"] = scope
    target = (
        _secure_endpoint(endpoint)
        if endpoint
        else _urljoin_path(authorization_server_url, "/register")
    )
    if signal is not None and signal.is_set():
        raise McpAbortError()
    async with _client_scope(client) as http:
        response = await await_or_abort(
            http.post(
                target,
                headers={"Accept": "application/json", "content-type": "application/json"},
                content=json.dumps(body),
            ),
            signal,
        )
    if not response.is_success:
        raise OAuthRegistrationError(response.status_code, response.text)
    return parse_client_information(response.json())


async def exchange_authorization_code(
    authorization_server_url: str,
    *,
    code: str,
    code_verifier: str,
    redirect_url: str,
    client_information: OAuthClientInformationMixed,
    metadata: AuthorizationServerMetadata | None = None,
    resource: str | None = None,
    add_client_authentication: AddClientAuthentication | None = None,
    client: httpx.AsyncClient | None = None,
    signal: asyncio.Event | None = None,
) -> OAuthTokens:
    """用授权码换令牌。"""
    return await _token_request(
        authorization_server_url,
        TokenRequestOptions(
            client_information=client_information,
            metadata=metadata,
            resource=resource,
            add_client_authentication=add_client_authentication,
            client=client,
            signal=signal,
        ),
        [
            ("grant_type", "authorization_code"),
            ("code", code),
            ("code_verifier", code_verifier),
            ("redirect_uri", redirect_url),
        ],
    )


async def refresh_authorization(
    authorization_server_url: str,
    *,
    refresh_token: str,
    client_information: OAuthClientInformationMixed,
    metadata: AuthorizationServerMetadata | None = None,
    resource: str | None = None,
    add_client_authentication: AddClientAuthentication | None = None,
    client: httpx.AsyncClient | None = None,
    signal: asyncio.Event | None = None,
) -> OAuthTokens:
    """用 refresh token 换新令牌（返回结果里保留原 refresh token）。"""
    tokens = await _token_request(
        authorization_server_url,
        TokenRequestOptions(
            client_information=client_information,
            metadata=metadata,
            resource=resource,
            add_client_authentication=add_client_authentication,
            client=client,
            signal=signal,
        ),
        [("grant_type", "refresh_token"), ("refresh_token", refresh_token)],
    )
    # 对应上游 ``{ refresh_token: options.refreshToken, ...tokens }``：响应里带了新的
    # refresh_token 就用新的，没带才沿用旧值。
    return {"refresh_token": refresh_token, **tokens}


def _with_scope(tokens: OAuthTokens, scope: str | None) -> OAuthTokens:
    if tokens.get("scope") is None and scope:
        return {**tokens, "scope": scope}
    return tokens


def step_up_scope(granted: str | None, challenged: str | None) -> str | None:
    """step-up 授权的 scope：受挑战的 scope 加上已授权过的。

    挑战可能只列缺失的 scope，而只带这些的新令牌会丢掉旧令牌已有的访问权（SEP-2350）。
    没有受挑战的 scope 时返回 ``None``，让流程用自己的默认值。
    """
    if not challenged:
        return None
    scopes: list[str] = []
    for scope in (granted, challenged):
        scopes.extend(scope.split() if scope else [])
    ordered = list(dict.fromkeys(scopes))
    return " ".join(ordered)


async def _run_flow(provider: OAuthClientProvider, options: OAuthFlowOptions) -> OAuthFlowResult:
    metadata_url = (
        _secure_endpoint(options.authorization_server_metadata_url)
        if options.authorization_server_metadata_url
        else None
    )
    # 配了元数据 URL 时不缓存发现结果，改 URL 立即生效。
    cached = None if metadata_url else await provider.discovery_state()
    discovered: OAuthServerInfo
    if cached is not None and cached.get("authorizationServerUrl"):
        cached_url = cached["authorizationServerUrl"]
        cached_metadata = cached.get("authorizationServerMetadata") or (
            await discover_authorization_server_metadata(
                cached_url,
                client=options.client,
                skip_issuer_validation=options.skip_issuer_validation,
                signal=options.signal,
            )
        )
        discovered = {"authorizationServerUrl": cached_url}
        if cached_metadata is not None:
            discovered["authorizationServerMetadata"] = cached_metadata
        if cached.get("resourceMetadata") is not None:
            discovered["resourceMetadata"] = cached["resourceMetadata"]
    else:
        discovered = await discover_oauth_server_info(
            options.server_url,
            resource_metadata_url=options.resource_metadata_url,
            authorization_server_metadata_url=metadata_url,
            client=options.client,
            skip_issuer_validation=options.skip_issuer_validation,
            signal=options.signal,
        )
    if not metadata_url:
        state: OAuthDiscoveryState = {
            "authorizationServerUrl": discovered["authorizationServerUrl"],
        }
        if discovered.get("authorizationServerMetadata") is not None:
            state["authorizationServerMetadata"] = discovered["authorizationServerMetadata"]
        if discovered.get("resourceMetadata") is not None:
            state["resourceMetadata"] = discovered["resourceMetadata"]
        if options.resource_metadata_url:
            state["resourceMetadataUrl"] = options.resource_metadata_url
        await provider.save_discovery_state(state)

    metadata = discovered.get("authorizationServerMetadata")
    resource = select_resource(options.server_url, discovered.get("resourceMetadata"))
    # 用 ``or`` 而不是「有没有」：空 scope（例如 scopes_supported: []）要落到下一个来源。
    scopes_supported = (discovered.get("resourceMetadata") or {}).get("scopes_supported")
    scope = (
        options.scope
        or (" ".join(scopes_supported) if scopes_supported else None)
        or provider.client_metadata.get("scope")
    )
    stored = await provider.client_information()
    client_document = None if stored else provider.client_metadata_document(metadata)
    if client_document is not None:
        parts = urlsplit(client_document.url)
        if parts.scheme != "https" or parts.path == "/":
            raise RuntimeError("Invalid OAuth client metadata URL")
    client: OAuthClientInformationMixed | None = stored or (
        {"client_id": client_document.url} if client_document else None
    )
    if client is None:
        if options.authorization_code:
            raise RuntimeError("OAuth client information is missing during code exchange")
        if not provider.can_save_client_information():
            raise RuntimeError("OAuth client information cannot be persisted")
        registered = await register_client(
            discovered["authorizationServerUrl"],
            metadata=metadata,
            client_metadata=provider.client_metadata,
            scope=scope,
            client=options.client,
            signal=options.signal,
        )
        client = registered
        await provider.save_client_information(registered)
    # 文档给出的 redirect URI 可能与 provider 的不同（例如带服务端专属路径）。
    redirect_url = client_document.redirect_url if client_document else provider.redirect_url
    add_auth = provider.add_client_authentication

    if options.authorization_code:
        # RFC 9207：绝不把别的授权服务器的 code 发给这个。没有元数据就没有 issuer 可比，
        # 此时任何 ``iss`` 都不接受。
        iss = options.iss
        iss_supported = (
            metadata.get("authorization_response_iss_parameter_supported") if metadata else None
        )
        expected_issuer = metadata.get("issuer") if metadata else None
        if (iss is not None or iss_supported) and iss != expected_issuer:
            raise OAuthIssuerMismatchError(expected_issuer, iss)
        tokens = await exchange_authorization_code(
            discovered["authorizationServerUrl"],
            code=options.authorization_code,
            code_verifier=await provider.code_verifier(),
            redirect_url=redirect_url,
            client_information=client,
            metadata=metadata,
            resource=resource,
            add_client_authentication=add_auth,
            client=options.client,
            signal=options.signal,
        )
        # 响应没有 ``scope`` 表示授予了所请求的 scope（RFC 6749 §5.1）。记下来以便 step-up 保留。
        await provider.save_tokens(_with_scope(tokens, scope))
        return "AUTHORIZED"

    existing = None if options.skip_refresh else await provider.tokens()
    if existing is not None and existing.get("refresh_token"):
        try:
            tokens = await refresh_authorization(
                discovered["authorizationServerUrl"],
                refresh_token=existing["refresh_token"],
                client_information=client,
                metadata=metadata,
                resource=resource,
                add_client_authentication=add_auth,
                client=options.client,
                signal=options.signal,
            )
            # 刷新响应没有 ``scope`` 表示沿用原授权的 scope（RFC 6749 §6）。
            await provider.save_tokens(_with_scope(tokens, existing.get("scope")))
            return "AUTHORIZED"
        except Exception as error:  # noqa: BLE001 - 按可恢复性分类
            if (options.signal is not None and options.signal.is_set()) or isinstance(
                error, OAuthInsecureEndpointError
            ):
                raise
            if isinstance(error, OAuthError) and error.code != "server_error":
                raise

    state_value = await provider.state()
    authorization_url, verifier = await start_authorization(
        discovered["authorizationServerUrl"],
        client_information=client,
        redirect_url=redirect_url,
        metadata=metadata,
        scope=scope,
        state=state_value,
        resource=resource,
    )
    await provider.save_code_verifier(verifier)
    await provider.redirect_to_authorization(authorization_url)
    return "REDIRECT"


async def authorize_mcp(
    provider: OAuthClientProvider, options: OAuthFlowOptions
) -> OAuthFlowResult:
    """跑一次授权流程：能刷新就刷新，否则跳到授权 URL。

    ``invalid_client`` / ``unauthorized_client`` 时作废全部凭据重试一次；
    ``invalid_grant`` 时只作废令牌重试一次。
    """
    try:
        return await _run_flow(provider, options)
    except OAuthError as error:
        if error.code in ("invalid_client", "unauthorized_client"):
            await provider.invalidate_credentials("all")
            return await _run_flow(provider, options)
        if error.code == "invalid_grant":
            await provider.invalidate_credentials("tokens")
            return await _run_flow(provider, options)
        raise


class _SharedRefresh:
    """并发 401 之间共享一次刷新。"""

    def __init__(self) -> None:
        self.done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.error: BaseException | None = None

    def finish(self, error: BaseException | None) -> None:
        self.error = error
        if not self.done.done():
            self.done.set_result(None)


def adapt_oauth_provider(provider: OAuthClientProvider) -> AuthProvider:
    """把 ``OAuthClientProvider`` 适配成 ``StreamableHttpTransport`` 用的 ``AuthProvider``。

    401 之后刷新令牌；需要用户授权时抛 ``McpOAuthAuthorizationRequiredError``。
    并发 401 **共享同一次刷新**；令牌已被别的请求换掉的请求直接重试 —— 轮换式 refresh token
    下，用旧 refresh token 再刷一次会失败并丢掉新授权。
    """
    in_flight: list[_SharedRefresh | None] = [None]

    async def _token() -> str | None:
        tokens = await provider.tokens()
        return tokens.get("access_token") if tokens else None

    async def _on_unauthorized(context: UnauthorizedContext) -> None:
        challenge = parse_www_authenticate(context.www_authenticate)
        insufficient_scope = challenge.get("error") == "insufficient_scope"
        current_in_flight = in_flight[0]
        if not insufficient_scope and current_in_flight is None and context.token is not None:
            current = await _token()
            if current is not None and current != context.token:
                return
        if current_in_flight is None:
            shared = _SharedRefresh()
            in_flight[0] = shared

            async def _refresh() -> None:
                try:
                    granted = await provider.tokens() if insufficient_scope else None
                    scope = (
                        step_up_scope(
                            granted.get("scope") if granted else None, challenge.get("scope")
                        )
                        if insufficient_scope
                        else challenge.get("scope")
                    )
                    result = await authorize_mcp(
                        provider,
                        OAuthFlowOptions(
                            server_url=context.server_url,
                            resource_metadata_url=challenge.get("resourceMetadataUrl"),
                            scope=scope,
                            client=context.client,
                            skip_refresh=insufficient_scope,
                        ),
                    )
                    if result == "REDIRECT":
                        raise McpOAuthAuthorizationRequiredError()
                except BaseException as error:  # noqa: BLE001 - 转发给所有等待者
                    shared.finish(error)
                    in_flight[0] = None
                    return
                shared.finish(None)
                in_flight[0] = None

            task = asyncio.ensure_future(_refresh())
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
            shared = shared
        else:
            shared = current_in_flight
        await shared.done
        if shared.error is not None:
            raise shared.error

    return _AdaptedAuthProvider(_token, _on_unauthorized)


@dataclass
class _AdaptedAuthProvider:
    """``adapt_oauth_provider`` 的返回值（显式类型，便于 mypy 与调用方阅读）。"""

    _token_fn: Callable[[], Awaitable[str | None]]
    _unauthorized_fn: Callable[[UnauthorizedContext], Awaitable[None]]

    async def token(self) -> str | None:
        return await self._token_fn()

    async def on_unauthorized(self, context: UnauthorizedContext) -> None:
        await self._unauthorized_fn(context)


#: 适配器内部派发的后台任务（持有强引用，避免被 GC）。
_background_tasks: set[asyncio.Task[Any]] = set()


__all__ = [
    "DEFAULT_FLOW_TIMEOUT_SECONDS",
    "AddClientAuthentication",
    "ClientAuthMethod",
    "OAuthClientMetadataDocument",
    "OAuthClientProvider",
    "OAuthFlowOptions",
    "OAuthFlowResult",
    "TokenRequestOptions",
    "adapt_oauth_provider",
    "authorize_mcp",
    "exchange_authorization_code",
    "refresh_authorization",
    "register_client",
    "start_authorization",
    "step_up_scope",
]
