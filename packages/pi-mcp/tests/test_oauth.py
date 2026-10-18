"""MCP OAuth 的行为规格。

逐条对齐上游 ``packages/mcp/test/oauth.test.ts``（v1.3.0，639 行）。对应用例的上游行号
写在每个测试的 docstring 里；JS 专属行为**显式标注**。

服务端是**真实回环 HTTP 服务端**（``helpers.listen``），授权跳转用真实 HTTP 往返。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from pi_mcp import (
    McpClient,
    McpClientOptions,
    StreamableHttpTransport,
    StreamableHttpTransportOptions,
    UnauthorizedContext,
)
from pi_mcp.oauth import (
    McpOAuthAuthorizationRequiredError,
    McpOAuthProvider,
    McpOAuthProviderOptions,
    McpOAuthState,
    MemoryOAuthStateStore,
    OAuthCallbackPage,
    OAuthCallbackServer,
    OAuthCallbackServerOptions,
    OAuthClientInformationMixed,
    OAuthClientMetadata,
    OAuthClientMetadataDocument,
    OAuthClientProvider,
    OAuthDiscoveryState,
    OAuthFlowOptions,
    OAuthInsecureEndpointError,
    OAuthIssuerMismatchError,
    OAuthTokens,
    adapt_oauth_provider,
    authorize_mcp,
    discover_authorization_server_metadata,
    register_client,
)

from .helpers import RecordedRequest, Response, close_servers, listen

pytestmark = pytest.mark.usefixtures("_close_http_servers")


@pytest.fixture
def _close_http_servers() -> Any:
    yield
    close_servers()


def _origin(request: RecordedRequest) -> str:
    """从 Host 头重建 origin（对应上游 handler 的第三个参数 ``serverOrigin``）。"""
    return f"http://{request.headers['host']}"


def _params(body: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}


class StubOAuthProvider(OAuthClientProvider):
    """上游 ``TestOAuthProvider``（oauth.test.ts:19）。"""

    def __init__(self, redirect_url: str) -> None:
        self.redirect_url = redirect_url
        self.client_metadata: OAuthClientMetadata = {
            "redirect_uris": [redirect_url],
            "client_name": "pi-mcp-test",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        self.client: OAuthClientInformationMixed | None = None
        self.token_set: OAuthTokens | None = None
        self.verifier: str | None = None
        self.discovery: OAuthDiscoveryState | None = None
        self.authorization_url: str | None = None
        self.add_client_authentication = None

    async def state(self) -> str:
        return "expected-state"

    async def client_information(self) -> OAuthClientInformationMixed | None:
        return self.client

    async def save_client_information(self, information: OAuthClientInformationMixed) -> None:
        self.client = information

    async def tokens(self) -> OAuthTokens | None:
        return self.token_set

    async def save_tokens(self, tokens: OAuthTokens) -> None:
        self.token_set = tokens

    async def redirect_to_authorization(self, url: str) -> None:
        self.authorization_url = url

    async def save_code_verifier(self, verifier: str) -> None:
        self.verifier = verifier

    async def code_verifier(self) -> str:
        if not self.verifier:
            raise RuntimeError("Missing code verifier")
        return self.verifier

    async def invalidate_credentials(self, kind: str = "all") -> None:
        if kind in ("all", "client"):
            self.client = None
        if kind in ("all", "tokens"):
            self.token_set = None
        if kind in ("all", "verifier"):
            self.verifier = None
        if kind in ("all", "discovery"):
            self.discovery = None

    async def save_discovery_state(self, state: OAuthDiscoveryState) -> None:
        self.discovery = state

    async def discovery_state(self) -> OAuthDiscoveryState | None:
        return self.discovery


def _unauthorized(
    origin: str, status: int = 401, header: str = "Bearer", token: str | None = "a1"
) -> UnauthorizedContext:
    return UnauthorizedContext(
        status=status, server_url=f"{origin}/mcp", www_authenticate=header, token=token
    )


def _query(url: str, key: str) -> str | None:
    values = parse_qs(urlsplit(url).query).get(key)
    return values[0] if values else None


# ============================================================
# oauth.test.ts:95
# ============================================================


async def test_discovers_registers_authorizes_with_pkce_and_refreshes_on_401() -> None:
    """上游 :95 —— 发现 → 动态注册 → PKCE 授权 → 401 刷新，全链路。"""
    state: dict[str, Any] = {"expected_challenge": None, "refreshes": 0}

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        origin = _origin(request)
        path = urlsplit(request.path).path
        if path == "/.well-known/oauth-protected-resource/mcp":
            response.send(
                200,
                json.dumps(
                    {
                        "resource": f"{origin}/mcp",
                        "authorization_servers": [origin],
                        "scopes_supported": ["org:read"],
                    }
                ),
                {"content-type": "application/json"},
            )
            return
        if path == "/.well-known/oauth-authorization-server":
            response.send(
                200,
                json.dumps(
                    {
                        "issuer": origin,
                        "authorization_endpoint": f"{origin}/authorize",
                        "token_endpoint": f"{origin}/token",
                        "registration_endpoint": f"{origin}/register",
                        "response_types_supported": ["code"],
                        "grant_types_supported": ["authorization_code", "refresh_token"],
                        "token_endpoint_auth_methods_supported": ["none"],
                        "code_challenge_methods_supported": ["S256"],
                    }
                ),
                {"content-type": "application/json"},
            )
            return
        if path == "/register":
            metadata = json.loads(request.body)
            # 空串与 null 的可选字段算缺席（#10266）。
            response.send(
                201,
                json.dumps({**metadata, "client_id": "test-client", "client_secret": ""}),
                {"content-type": "application/json"},
            )
            return
        if path == "/authorize":
            query = parse_qs(urlsplit(request.path).query)
            state["expected_challenge"] = (query.get("code_challenge") or [None])[0]
            redirect = (query.get("redirect_uri") or [""])[0]
            separator = "&" if "?" in redirect else "?"
            location = f"{redirect}{separator}" + urlencode(
                {"code": "test-code", "state": (query.get("state") or [""])[0]}
            )
            response.send(302, "", {"location": location})
            return
        if path == "/token":
            params = _params(request.body)
            if params.get("grant_type") == "refresh_token":
                state["refreshes"] += 1
                response.send(
                    200,
                    json.dumps(
                        {
                            "access_token": "refreshed-token",
                            "token_type": "Bearer",
                            "refresh_token": "",
                            "expires_in": None,
                        }
                    ),
                    {"content-type": "application/json"},
                )
                return
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256((params.get("code_verifier") or "").encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            if params.get("code") != "test-code" or challenge != state["expected_challenge"]:
                response.send(
                    400,
                    json.dumps({"error": "invalid_grant"}),
                    {"content-type": "application/json"},
                )
                return
            response.send(
                200,
                json.dumps(
                    {
                        "access_token": "first-token",
                        "refresh_token": "refresh-token",
                        "token_type": "Bearer",
                        "scope": "",
                    }
                ),
                {"content-type": "application/json"},
            )
            return
        if path != "/mcp":
            response.send(404)
            return
        if request.method == "GET":
            response.send(405)
            return
        if request.method == "DELETE":
            response.send(200)
            return
        token = request.headers.get("authorization")
        if token not in ("Bearer first-token", "Bearer refreshed-token"):
            response.send(
                401,
                "Unauthorized",
                {
                    # 空 scope 会落到资源元数据的 scopes_supported。
                    "www-authenticate": (
                        f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource/mcp", scope=""'
                    )
                },
            )
            return
        message = json.loads(request.body)
        if "id" not in message:
            response.send(202)
            return
        result = (
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oauth-test", "version": "1.0.0"},
            }
            if message["method"] == "initialize"
            else {"tools": [{"name": "issues", "inputSchema": {"type": "object"}}]}
        )
        response.send(
            200,
            json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}),
            {"content-type": "application/json"},
        )

    origin = listen(handler)
    callback = await OAuthCallbackServer.listen()
    provider = StubOAuthProvider(callback.redirect_url)

    async with httpx.AsyncClient(timeout=10.0) as http:
        first = McpClient(McpClientOptions(name="oauth-test", version="1.0.0"))
        with pytest.raises(McpOAuthAuthorizationRequiredError):
            await first.connect(
                StreamableHttpTransport(
                    StreamableHttpTransportOptions(
                        url=f"{origin}/mcp",
                        headers={"Authorization": "Bearer caller-supplied-stale-token"},
                        auth_provider=adapt_oauth_provider(provider),
                        open_get_stream=False,
                        client=http,
                    )
                )
            )
        assert provider.authorization_url is not None
        assert _query(provider.authorization_url, "scope") == "org:read"
        assert _query(provider.authorization_url, "resource") == f"{origin}/mcp"

        pending = callback.wait_for_callback("expected-state")
        # 驱动一次真实重定向：/authorize → 回调服务器。
        authorization = await http.get(provider.authorization_url)
        assert authorization.status_code == 302
        await http.get(authorization.headers["location"])
        callback_result = await pending
        assert (
            await authorize_mcp(
                provider,
                OAuthFlowOptions(
                    server_url=f"{origin}/mcp",
                    authorization_code=callback_result.code,
                    client=http,
                ),
            )
            == "AUTHORIZED"
        )

        client = McpClient(McpClientOptions(name="oauth-test", version="1.0.0"))
        await client.connect(
            StreamableHttpTransport(
                StreamableHttpTransportOptions(
                    url=f"{origin}/mcp",
                    headers={"Authorization": "Bearer caller-supplied-stale-token"},
                    auth_provider=adapt_oauth_provider(provider),
                    open_get_stream=False,
                    client=http,
                )
            )
        )
        assert await client.list_tools() == [{"name": "issues", "inputSchema": {"type": "object"}}]
        await client.close()

        assert provider.token_set is not None
        provider.token_set = {**provider.token_set, "access_token": "stale-token"}
        refreshed = McpClient(McpClientOptions(name="oauth-test", version="1.0.0"))
        await refreshed.connect(
            StreamableHttpTransport(
                StreamableHttpTransportOptions(
                    url=f"{origin}/mcp",
                    headers={"Authorization": "Bearer caller-supplied-stale-token"},
                    auth_provider=adapt_oauth_provider(provider),
                    open_get_stream=False,
                    client=http,
                )
            )
        )
        # 两次令牌响应都没带 scope，所以授权沿用所请求的 scope。
        assert provider.token_set == {
            "access_token": "refreshed-token",
            "refresh_token": "refresh-token",
            "token_type": "Bearer",
            "scope": "org:read",
        }
        assert state["refreshes"] == 1
        await refreshed.close()
    await callback.close()


# ============================================================
# oauth.test.ts:281
# ============================================================


async def test_shares_one_refresh_between_concurrent_401s_when_refresh_tokens_rotate() -> None:
    """上游 :281 —— 并发 401 共享一次刷新；迟到的旧令牌 401 不再刷新。"""
    grants: list[str] = []

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        origin = _origin(request)
        path = urlsplit(request.path).path
        if path == "/.well-known/oauth-protected-resource/mcp":
            # 非法资源元数据回退到服务端 origin，而不是让发现失败。
            response.send(
                200,
                json.dumps({"resource": f"{origin}/mcp", "authorization_servers": ["not a url"]}),
                {"content-type": "application/json"},
            )
            return
        if path == "/.well-known/oauth-authorization-server":
            response.send(
                200,
                json.dumps(
                    {
                        # issuer 不带尾斜杠，而 URL 解析会给回退的服务端 URL 补一个。
                        "issuer": origin,
                        "authorization_endpoint": f"{origin}/authorize",
                        "token_endpoint": f"{origin}/token",
                        "response_types_supported": ["code"],
                    }
                ),
                {"content-type": "application/json"},
            )
            return
        if path == "/token":
            refresh_token = _params(request.body).get("refresh_token", "")
            grants.append(refresh_token)
            if refresh_token != "r1":
                response.send(
                    400,
                    json.dumps({"error": "invalid_grant"}),
                    {"content-type": "application/json"},
                )
                return
            response.sleep(0.02)
            response.send(
                200,
                json.dumps(
                    {
                        "access_token": "a2",
                        "refresh_token": "r2",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    }
                ),
                {"content-type": "application/json"},
            )
            return
        response.send(404)

    origin = listen(handler)
    store = MemoryOAuthStateStore()
    provider = McpOAuthProvider(
        McpOAuthProviderOptions(
            server_url=f"{origin}/mcp",
            redirect_url="http://127.0.0.1/callback",
            client_metadata={"client_name": "test"},
            client_id="client",
            store=store,
            on_redirect=lambda url: None,
        )
    )
    await provider.save_tokens(
        {"access_token": "a1", "refresh_token": "r1", "token_type": "Bearer"}
    )
    auth = adapt_oauth_provider(provider)

    async with httpx.AsyncClient(timeout=10.0) as http:
        context = UnauthorizedContext(
            status=401,
            server_url=f"{origin}/mcp",
            www_authenticate="Bearer",
            token="a1",
            client=http,
        )
        # 两个并发 401 必须共享同一次刷新。
        await asyncio.gather(auth.on_unauthorized(context), auth.on_unauthorized(context))  # type: ignore[attr-defined]
        # 迟到的、仍带旧令牌的 401 不得再刷一次。
        await auth.on_unauthorized(context)  # type: ignore[attr-defined]
    assert grants == ["r1"]
    assert await auth.token() == "a2"  # type: ignore[attr-defined]
    loaded = store.load()
    assert loaded is not None
    assert loaded["tokens"] is not None
    assert loaded["tokens"]["refresh_token"] == "r2"
    assert loaded["tokensExpireAt"] > _now_ms() + 3_500_000


def _now_ms() -> float:
    import time

    return time.time() * 1000


# ============================================================
# oauth.test.ts:350
# ============================================================


async def test_asks_for_authorization_instead_of_refreshing_when_the_server_needs_more_scope() -> (
    None
):
    """上游 :350 —— ``insufficient_scope`` 时不刷新而是请求授权，且新 scope 保留旧的。"""
    provider = StubOAuthProvider("http://127.0.0.1/callback")
    provider.client = {"client_id": "client"}
    provider.token_set = {
        "access_token": "a1",
        "refresh_token": "r1",
        "token_type": "Bearer",
        "scope": "repo read:org",
    }

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        origin = _origin(request)
        path = urlsplit(request.path).path
        if path == "/.well-known/oauth-authorization-server":
            response.send(
                200,
                json.dumps(
                    {
                        "issuer": origin,
                        "authorization_endpoint": f"{origin}/authorize",
                        "token_endpoint": f"{origin}/token",
                        "response_types_supported": ["code"],
                    }
                ),
                {"content-type": "application/json"},
            )
            return
        response.send(500 if path == "/token" else 404)

    origin = listen(handler)
    auth = adapt_oauth_provider(provider)
    with pytest.raises(McpOAuthAuthorizationRequiredError):
        await auth.on_unauthorized(  # type: ignore[attr-defined]
            _unauthorized(
                origin,
                status=403,
                header='Bearer error="insufficient_scope", scope="repo admin"',
                token="a1",
            )
        )
    # 挑战可能只列缺失的 scope；新授权要把旧的也带上。
    assert provider.authorization_url is not None
    assert _query(provider.authorization_url, "scope") == "repo read:org admin"
    # 用户授权新 scope 之前，可用的旧授权保持不变。
    assert provider.token_set is not None and provider.token_set["access_token"] == "a1"


# ============================================================
# oauth.test.ts:389
# ============================================================


async def test_binds_persisted_credentials_to_the_exact_mcp_server_url() -> None:
    """上游 :389 —— 凭据绑定到确切的 MCP 服务端 URL，不跨服务端复用。"""
    store = MemoryOAuthStateStore()
    first = McpOAuthProvider(
        McpOAuthProviderOptions(
            server_url="https://one.example/mcp",
            redirect_url="http://127.0.0.1/callback",
            client_metadata={"client_name": "test"},
            store=store,
            on_redirect=lambda url: None,
        )
    )
    await first.save_tokens({"access_token": "secret", "token_type": "Bearer"})
    assert (await first.tokens()) is not None
    assert (await first.tokens())["access_token"] == "secret"  # type: ignore[index]

    second = McpOAuthProvider(
        McpOAuthProviderOptions(
            server_url="https://two.example/mcp",
            redirect_url="http://127.0.0.1/callback",
            client_metadata={"client_name": "test"},
            store=store,
            on_redirect=lambda url: None,
        )
    )
    assert await second.tokens() is None


# ============================================================
# oauth.test.ts:412（#10493）
# ============================================================


async def test_registers_with_an_application_type_derived_from_the_redirect_uris_unless_one_is_set() -> (
    None
):
    """上游 :412 —— ``application_type`` 由 redirect_uris 推导，除非显式给出。"""
    bodies: list[dict[str, Any]] = []

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        metadata = json.loads(request.body)
        bodies.append(metadata)
        response.send(
            201,
            json.dumps({**metadata, "client_id": "client"}),
            {"content-type": "application/json"},
        )

    origin = listen(handler)

    async def register(redirect_uris: list[str], application_type: str | None = None) -> None:
        metadata: OAuthClientMetadata = {"redirect_uris": redirect_uris}
        if application_type:
            metadata["application_type"] = application_type
        await register_client(origin, client_metadata=metadata)

    await register(["http://127.0.0.1:1234/callback"])
    await register(["http://[::1]/callback"])
    await register(["com.example.app:/callback"])
    await register(["https://app.example/callback"])
    await register(["http://localhost/callback"], "web")
    assert [body["application_type"] for body in bodies] == [
        "native",
        "native",
        "native",
        "web",
        "web",
    ]


# ============================================================
# oauth.test.ts:432
# ============================================================


async def test_rejects_authorization_metadata_whose_issuer_does_not_match_discovery() -> None:
    """上游 :432 —— issuer 与发现目标不符时抛 ``OAuthIssuerMismatchError``。"""

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        origin = _origin(request)
        path = urlsplit(request.path).path
        if path == "/.well-known/oauth-authorization-server":
            response.send(
                200,
                json.dumps(
                    {
                        "issuer": "https://attacker.example",
                        "authorization_endpoint": f"{origin}/authorize",
                        "token_endpoint": f"{origin}/token",
                        "response_types_supported": ["code"],
                    }
                ),
                {"content-type": "application/json"},
            )
            return
        response.send(404)

    origin = listen(handler)
    with pytest.raises(OAuthIssuerMismatchError):
        await discover_authorization_server_metadata(origin)


# ============================================================
# oauth.test.ts:454（#10172）
# ============================================================


async def test_uses_a_configured_authorization_server_metadata_document_as_is() -> None:
    """上游 :454 —— 配置的元数据文档按原样信任（不校验 issuer），且必须 https（回环除外）。"""

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        origin = _origin(request)
        path = urlsplit(request.path).path
        if path == "/.well-known/oauth-protected-resource/mcp":
            # 指向 MCP 服务端自己，而它不提供授权服务器元数据。
            response.send(
                200,
                json.dumps({"resource": f"{origin}/mcp", "authorization_servers": [origin]}),
                {"content-type": "application/json"},
            )
        elif path == "/idp/metadata.json":
            response.send(
                200,
                json.dumps(
                    {
                        # 无法从文档 URL 推导出来；配置的文档不检查。
                        "issuer": "https://idp.example",
                        "authorization_endpoint": f"{origin}/idp/authorize",
                        "token_endpoint": f"{origin}/idp/token",
                        "response_types_supported": ["code"],
                    }
                ),
                {"content-type": "application/json"},
            )
        else:
            response.send(404)

    origin = listen(handler)
    provider = StubOAuthProvider("http://127.0.0.1/callback")
    provider.client = {"client_id": "client"}
    result = await authorize_mcp(
        provider,
        OAuthFlowOptions(
            server_url=f"{origin}/mcp",
            authorization_server_metadata_url=f"{origin}/idp/metadata.json",
        ),
    )
    assert result == "REDIRECT"
    assert provider.authorization_url is not None
    parts = urlsplit(provider.authorization_url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{origin}/idp/authorize"
    assert _query(provider.authorization_url, "resource") == f"{origin}/mcp"

    with pytest.raises(OAuthInsecureEndpointError):
        await authorize_mcp(
            provider,
            OAuthFlowOptions(
                server_url=f"{origin}/mcp",
                authorization_server_metadata_url="http://idp.example/metadata.json",
            ),
        )


# ============================================================
# oauth.test.ts:491
# ============================================================


async def test_exchanges_a_code_only_when_its_iss_parameter_names_the_authorization_server() -> (
    None
):
    """上游 :491 —— RFC 9207：``iss`` 必须指向本次流程的授权服务器。"""
    codes: list[str] = []

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        if request.method != "POST":
            response.send(404)
            return
        codes.append(_params(request.body).get("code", ""))
        response.send(
            200,
            json.dumps({"access_token": "token", "token_type": "Bearer"}),
            {"content-type": "application/json"},
        )

    origin = listen(handler)

    async def exchange(
        code: str, iss: str | None, iss_supported: bool, with_metadata: bool = True
    ) -> str:
        provider = StubOAuthProvider("http://127.0.0.1/callback")
        provider.client = {"client_id": "client"}
        provider.verifier = "verifier"
        provider.discovery = {
            "authorizationServerUrl": origin,
            "authorizationServerMetadata": (
                {
                    "issuer": origin,
                    "authorization_endpoint": f"{origin}/authorize",
                    "token_endpoint": f"{origin}/token",
                    "response_types_supported": ["code"],
                    "authorization_response_iss_parameter_supported": iss_supported,
                }
                if with_metadata
                else None
            ),
        }
        return await authorize_mcp(
            provider,
            OAuthFlowOptions(server_url=f"{origin}/mcp", authorization_code=code, iss=iss),
        )

    with pytest.raises(OAuthIssuerMismatchError):
        await exchange("other", "https://attacker.example", False)
    with pytest.raises(OAuthIssuerMismatchError):
        await exchange("missing", None, True)
    assert await exchange("matching", origin, True) == "AUTHORIZED"
    # 没有承诺该参数的服务端可以省略它。
    assert await exchange("omitted", None, False) == "AUTHORIZED"
    # 没有元数据就没有 issuer 可比，服务端不能靠「藏起元数据」绕过检查。
    with pytest.raises(OAuthIssuerMismatchError):
        await exchange("unverifiable", "https://attacker.example", False, False)
    assert await exchange("no-metadata", None, False, False) == "AUTHORIZED"
    assert codes == ["matching", "omitted", "no-metadata"]


# ============================================================
# oauth.test.ts:537（#10565）
# ============================================================


async def test_stops_when_its_signal_aborts_without_falling_back_to_a_redirect() -> None:
    """上游 :537 —— 信号中止即停，**不**回退成授权跳转。"""
    stalled: list[str] = []

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        # 收下请求但永不答复。
        stalled.append(urlsplit(request.path).path)

    origin = listen(handler)

    async def run(provider: StubOAuthProvider) -> str | None:
        signal = asyncio.Event()
        count = len(stalled)
        flow = asyncio.ensure_future(
            authorize_mcp(provider, OAuthFlowOptions(server_url=f"{origin}/mcp", signal=signal))
        )
        for _ in range(500):
            if len(stalled) >= count + 1:
                break
            await asyncio.sleep(0.01)
        signal.set()
        with pytest.raises(Exception) as excinfo:
            await asyncio.wait_for(flow, timeout=10)
        assert excinfo.value.name == "AbortError"
        assert provider.authorization_url is None
        return stalled[-1] if stalled else None

    # 发现阶段被中止。
    assert await run(StubOAuthProvider("http://127.0.0.1/callback")) == (
        "/.well-known/oauth-protected-resource/mcp"
    )

    # 刷新失败本来会回退到新的授权跳转，但中止后必须直接停。
    refreshing = StubOAuthProvider("http://127.0.0.1/callback")
    refreshing.client = {"client_id": "client"}
    refreshing.token_set = {"access_token": "a1", "refresh_token": "r1", "token_type": "Bearer"}
    refreshing.discovery = {
        "authorizationServerUrl": origin,
        "authorizationServerMetadata": {
            "issuer": origin,
            "authorization_endpoint": f"{origin}/authorize",
            "token_endpoint": f"{origin}/token",
            "response_types_supported": ["code"],
        },
    }
    assert await run(refreshing) == "/token"


# ============================================================
# oauth.test.ts:577 — OAuthCallbackServer 页面
# ============================================================


async def test_renders_plain_text_by_default() -> None:
    """上游 :578 —— 默认纯文本页面，且回调被正确解析。"""
    import httpx as _httpx

    callback = await OAuthCallbackServer.listen()
    try:
        pending = callback.wait_for_callback("s1")
        async with _httpx.AsyncClient(timeout=10.0) as http:
            response = await http.get(f"{callback.redirect_url}?code=abc&state=s1")
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        assert response.text == "Authorization complete. You may close this window."
        assert (await pending).code == "abc"
    finally:
        await callback.close()


async def test_rejects_a_response_on_another_path_than_the_expected_one() -> None:
    """上游 :592（#10302）—— 落在别的路径上的响应会让该次等待失败。"""
    import httpx as _httpx

    callback = await OAuthCallbackServer.listen(
        OAuthCallbackServerOptions(extra_paths=["/callback/server-id"])
    )
    try:
        origin = callback.redirect_url.split("/callback")[0]
        mixed_up = callback.wait_for_callback("s1", "/callback/server-id")
        async with _httpx.AsyncClient(timeout=10.0) as http:
            wrong = await http.get(f"{origin}/callback?code=abc&state=s1")
            assert wrong.status_code == 400
            with pytest.raises(Exception, match="arrived on another redirect URI"):
                await mixed_up

            pending = callback.wait_for_callback("s2", "/callback/server-id")
            right = await http.get(f"{origin}/callback/server-id?code=abc&state=s2")
            assert right.status_code == 200
            assert (await pending).code == "abc"
    finally:
        await callback.close()


async def test_renders_pages_through_render_page() -> None:
    """上游 :611 —— ``render_page`` 接管页面渲染，失败也用页面表达。"""
    import httpx as _httpx

    pages: list[OAuthCallbackPage] = []

    def render(page: OAuthCallbackPage) -> str:
        pages.append(page)
        return "<p>ok</p>" if page.ok else f"<p>{page.message}</p>"

    callback = await OAuthCallbackServer.listen(OAuthCallbackServerOptions(render_page=render))
    try:
        denied = callback.wait_for_callback("s1")
        async with _httpx.AsyncClient(timeout=10.0) as http:
            failure = await http.get(
                f"{callback.redirect_url}?error=access_denied&error_description=Denied&state=s1"
            )
            assert failure.headers["content-type"] == "text/html; charset=utf-8"
            with pytest.raises(Exception, match="Denied"):
                await denied
        assert pages[-1] == OAuthCallbackPage.failure(
            "Authorization failed. You may close this window.", "Denied"
        )

        pending = callback.wait_for_callback("s2")
        async with _httpx.AsyncClient(timeout=10.0) as http:
            success = await http.get(f"{callback.redirect_url}?code=abc&state=s2")
            assert success.text == "<p>ok</p>"
        assert (await pending).code == "abc"
    finally:
        await callback.close()


# ============================================================
# 本端补充
# ============================================================


async def test_callback_server_binds_an_ephemeral_port_by_default() -> None:
    """本端补充：默认端口为 0（内核分配），避免固定端口被占用（见 PORTING.md）。"""
    first = await OAuthCallbackServer.listen()
    second = await OAuthCallbackServer.listen()
    try:
        assert first.redirect_url != second.redirect_url
        assert first.redirect_url.startswith("http://127.0.0.1:")
    finally:
        await first.close()
        await second.close()


async def test_callback_server_fails_loudly_on_a_taken_port() -> None:
    """本端补充：端口被占时**明确报错**，不静默换端口（换端口会让已注册的 redirect URI 失效）。"""
    held = await OAuthCallbackServer.listen()
    taken = int(held.redirect_url.rsplit(":", 1)[1].split("/")[0])
    try:
        with pytest.raises(OSError, match="could not bind"):
            await OAuthCallbackServer.listen(OAuthCallbackServerOptions(port=taken))
    finally:
        await held.close()


async def test_callback_server_rejects_an_unknown_state() -> None:
    """本端补充：``state`` 对不上任何等待时返回 400（不泄漏是否存在该 state）。"""
    import httpx as _httpx

    callback = await OAuthCallbackServer.listen()
    try:
        async with _httpx.AsyncClient(timeout=10.0) as http:
            response = await http.get(f"{callback.redirect_url}?code=abc&state=nope")
            assert response.status_code == 400
            assert "Invalid or expired OAuth state" in response.text
    finally:
        await callback.close()


async def test_callback_server_rejects_duplicate_state_registration() -> None:
    """本端补充：同一个 ``state`` 不能注册两次。"""
    callback = await OAuthCallbackServer.listen()
    try:
        callback.wait_for_callback("dup")
        with pytest.raises(RuntimeError, match="already pending"):
            callback.wait_for_callback("dup")
    finally:
        await callback.close()


async def test_memory_store_returns_copies() -> None:
    """本端补充：``MemoryOAuthStateStore`` 进出都深拷贝，调用方改不到内部状态。"""
    store = MemoryOAuthStateStore()
    state: McpOAuthState = {"serverUrl": "https://one.example/mcp"}
    store.save(state)
    loaded = store.load()
    assert loaded is not None
    loaded["serverUrl"] = "mutated"
    assert store.load()["serverUrl"] == "https://one.example/mcp"  # type: ignore[index]


async def test_provider_state_is_generated_and_stable() -> None:
    """本端补充：``state()`` 首次生成 64 位十六进制串，之后保持稳定。"""
    provider = McpOAuthProvider(
        McpOAuthProviderOptions(
            server_url="https://one.example/mcp",
            redirect_url="http://127.0.0.1/callback",
            client_metadata={"client_name": "test"},
            on_redirect=lambda url: None,
        )
    )
    first = await provider.state()
    assert len(first) == 64 and all(char in "0123456789abcdef" for char in first)
    assert await provider.state() == first


async def test_provider_client_metadata_defaults() -> None:
    """本端补充：``client_metadata`` 的默认值（redirect_uris / grant_types / auth method）。"""
    with_secret = McpOAuthProvider(
        McpOAuthProviderOptions(
            server_url="https://one.example/mcp",
            redirect_url="http://127.0.0.1/callback",
            client_metadata={"client_name": "test"},
            client_secret="s3cret",
            on_redirect=lambda url: None,
        )
    )
    assert with_secret.client_metadata["redirect_uris"] == ["http://127.0.0.1/callback"]
    assert with_secret.client_metadata["grant_types"] == ["authorization_code", "refresh_token"]
    assert with_secret.client_metadata["response_types"] == ["code"]
    assert with_secret.client_metadata["token_endpoint_auth_method"] == "client_secret_post"

    without_secret = McpOAuthProvider(
        McpOAuthProviderOptions(
            server_url="https://one.example/mcp",
            redirect_url="http://127.0.0.1/callback",
            client_metadata={"client_name": "test"},
            on_redirect=lambda url: None,
        )
    )
    assert without_secret.client_metadata["token_endpoint_auth_method"] == "none"


async def test_configured_client_information_cannot_be_overwritten() -> None:
    """本端补充：显式配置的 client_id/secret 不会被 ``save_client_information`` 覆盖。"""
    provider = McpOAuthProvider(
        McpOAuthProviderOptions(
            server_url="https://one.example/mcp",
            redirect_url="http://127.0.0.1/callback",
            client_metadata={"client_name": "test"},
            client_id="configured",
            on_redirect=lambda url: None,
        )
    )
    await provider.save_client_information({"client_id": "other"})
    info = await provider.client_information()
    assert info is not None and info["client_id"] == "configured"


async def test_parse_www_authenticate_variants() -> None:
    """本端补充：``WWW-Authenticate`` 解析的边界（scheme、引号、空值）。"""
    from pi_mcp.oauth import parse_www_authenticate

    assert parse_www_authenticate(None) == {}
    assert parse_www_authenticate("Basic realm=x") == {}
    parsed = parse_www_authenticate(
        'Bearer resource_metadata="https://a.example/meta", scope="repo admin", error="insufficient_scope"'
    )
    assert parsed["resourceMetadataUrl"] == "https://a.example/meta"
    assert parsed["scope"] == "repo admin"
    assert parsed["error"] == "insufficient_scope"
    # 空值算缺席。
    assert "scope" not in parse_www_authenticate('Bearer scope=""')
    # DPoP 也认。
    assert parse_www_authenticate("DPoP scope=repo")["scope"] == "repo"
    # 无法解析的 resource_metadata 不进结果。
    assert "resourceMetadataUrl" not in parse_www_authenticate('Bearer resource_metadata=":::"')


async def test_client_metadata_document_is_used_instead_of_registration() -> None:
    """本端补充：返回 Client ID Metadata Document 时不做动态注册，且用文档的 redirect URI。"""
    requested: list[str] = []

    def handler(
        request: RecordedRequest, response: Response, requests: list[RecordedRequest]
    ) -> None:
        origin = _origin(request)
        path = urlsplit(request.path).path
        requested.append(path)
        if path == "/.well-known/oauth-authorization-server":
            response.send(
                200,
                json.dumps(
                    {
                        "issuer": origin,
                        "authorization_endpoint": f"{origin}/authorize",
                        "token_endpoint": f"{origin}/token",
                        "response_types_supported": ["code"],
                        "client_id_metadata_document_supported": True,
                    }
                ),
                {"content-type": "application/json"},
            )
            return
        response.send(404)

    origin = listen(handler)
    provider = StubOAuthProvider("http://127.0.0.1/callback")

    def document(metadata: Any) -> OAuthClientMetadataDocument | None:
        assert metadata is not None
        return OAuthClientMetadataDocument(
            url="https://pi.dev/oauth/client.json",
            redirect_url="http://127.0.0.1:1234/callback",
        )

    provider.client_metadata_document = document  # type: ignore[method-assign]
    assert (
        await authorize_mcp(
            provider, OAuthFlowOptions(server_url=f"{origin}/mcp", resource_metadata_url=None)
        )
        == "REDIRECT"
    )
    assert "/register" not in requested
    assert provider.client is None  # 文档不会被存成客户端信息
    assert provider.authorization_url is not None
    assert _query(provider.authorization_url, "client_id") == "https://pi.dev/oauth/client.json"
    assert _query(provider.authorization_url, "redirect_uri") == "http://127.0.0.1:1234/callback"
