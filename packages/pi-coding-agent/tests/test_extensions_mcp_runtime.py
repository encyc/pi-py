"""C2b 测试：MCP 连接/传输层（``extensions/mcp/runtime.ts``）。用假 client，不起真实 server。"""

from __future__ import annotations

import asyncio

import pytest

from pi_coding_agent.extensions.mcp.config import McpServerEntry
from pi_coding_agent.extensions.mcp.runtime import (
    CONNECT_RETRY_DELAYS_S,
    DEFAULT_TIMEOUT_SECONDS,
    McpServerConnection,
    _ProviderTokenAuth,
    _request_options,
    create_default_transport,
    expand_home,
    is_transient_error,
    sign_in_required_message,
    uses_oauth,
    without_templates,
)
from pi_coding_agent.mcp_servers import validate_mcp_server_config
from pi_mcp import McpAuthRequiredError, McpError, McpHttpError, McpSessionExpiredError
from pi_mcp.oauth import McpOAuthAuthorizationRequiredError


def _entry(name: str, raw: dict) -> McpServerEntry:  # type: ignore[type-arg]
    config = validate_mcp_server_config(name, raw)
    assert not isinstance(config, str), config
    return McpServerEntry(name=name, config=config, source="test")


# ============================================================
# 纯函数
# ============================================================


def test_is_transient_error_covers_retryable_statuses():
    assert is_transient_error(McpHttpError(408, "timeout")) is True
    assert is_transient_error(McpHttpError(429, "slow down")) is True
    assert is_transient_error(McpHttpError(500, "boom")) is True
    assert is_transient_error(McpHttpError(503, "restarting")) is True
    # 501 Not Implemented 是永久性的
    assert is_transient_error(McpHttpError(501, "nope")) is False
    assert is_transient_error(McpHttpError(400, "bad")) is False
    assert is_transient_error(OSError("network")) is True
    assert is_transient_error(ValueError("nope")) is False


def test_sign_in_required_message_names_provider_or_mcp():
    plain = sign_in_required_message(_entry("docs", {"url": "https://x/mcp"}))
    assert plain == 'MCP server "docs" requires sign-in. Run /mcp to sign in.'
    with_provider = sign_in_required_message(
        _entry("docs", {"url": "https://x/mcp", "auth": {"provider": "radius"}})
    )
    assert "/login radius" in with_provider


def test_uses_oauth_precedence():
    assert uses_oauth(_entry("a", {"url": "https://x/mcp"})) is True
    # 配置了 auth.provider → 不用 OAuth
    assert uses_oauth(_entry("a", {"url": "https://x/mcp", "auth": {"provider": "p"}})) is False
    # 自带 Authorization 头（大小写不敏感）→ 不用 OAuth
    assert (
        uses_oauth(_entry("a", {"url": "https://x/mcp", "headers": {"authorization": "Bearer x"}}))
        is False
    )
    assert (
        uses_oauth(_entry("a", {"url": "https://x/mcp", "headers": {"Authorization": "Bearer x"}}))
        is False
    )
    assert uses_oauth(_entry("a", {"url": "https://x/mcp", "headers": {"X-Other": "1"}})) is True
    # stdio 不用 OAuth
    assert uses_oauth(_entry("a", {"command": "npx"})) is False


def test_expand_home():
    home = expand_home("~")
    assert home and home != "~"
    assert expand_home("~/x").endswith("/x")
    assert expand_home("/abs/path") == "/abs/path"
    assert expand_home("relative") == "relative"


def test_create_default_transport_http_resolves_headers(monkeypatch):
    monkeypatch.setenv("PI_MCP_TOK", "secret")
    entry = _entry(
        "docs", {"url": "https://x/mcp", "headers": {"Authorization": "Bearer ${PI_MCP_TOK}"}}
    )
    transport = create_default_transport(entry, "/tmp", None)
    assert type(transport).__name__ == "StreamableHttpTransport"


def test_create_default_transport_stdio_resolves_env_and_home(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_MCP_ENV", "v")
    entry = _entry(
        "local",
        {"command": "npx", "args": ["-y", "srv"], "env": {"K": "${PI_MCP_ENV}"}, "cwd": "sub"},
    )
    transport = create_default_transport(entry, str(tmp_path), None)
    assert type(transport).__name__ == "StdioTransport"


def test_create_default_transport_raises_when_header_unresolvable():
    entry = _entry(
        "docs", {"url": "https://x/mcp", "headers": {"A": "${PI_MCP_DEFINITELY_MISSING}"}}
    )
    with pytest.raises(RuntimeError, match="from environment variable"):
        create_default_transport(entry, "/tmp", None)


async def test_without_templates_swallows_method_not_found():
    from pi_mcp import JSON_RPC_ERROR_CODES

    async def missing() -> object:
        raise McpError(JSON_RPC_ERROR_CODES["methodNotFound"], "nope")

    assert await without_templates(missing, []) == []

    async def other() -> object:
        raise McpError(-32000, "other")

    with pytest.raises(McpError):
        await without_templates(other, [])


def test_request_options_shape():
    options = _request_options(None, 5000)
    assert options.timeout_ms == 5000
    assert options.signal is None


async def test_provider_token_auth_reads_on_every_call():
    calls: list[str] = []

    async def token_for(provider: str) -> str | None:
        calls.append(provider)
        return "tok"

    auth = _ProviderTokenAuth(token_for, "radius")
    assert await auth.token() == "tok"
    assert await auth.token() == "tok"
    assert calls == ["radius", "radius"]  # 每次请求都读，provider 刷新会生效
    await auth.settled()
    await auth.on_unauthorized(object())


# ============================================================
# McpServerConnection：属性与状态
# ============================================================


def _connection(entry: McpServerEntry, tmp_path, **kwargs: object) -> McpServerConnection:
    from pi_coding_agent.auth_storage import FileAuthStorageBackend
    from pi_coding_agent.extensions.mcp.oauth import McpOAuthCredentialStore

    store = McpOAuthCredentialStore(
        backend=FileAuthStorageBackend(str(tmp_path / "mcp-auth.json")), lock_dir=str(tmp_path)
    )
    return McpServerConnection(
        entry=entry,
        cwd=str(tmp_path),
        create_transport=create_default_transport,
        credentials=store,
        on_tools=lambda _c: None,
        **kwargs,  # type: ignore[arg-type]
    )


def test_connection_basic_properties(tmp_path):
    conn = _connection(_entry("docs", {"url": "https://x/mcp", "timeout": 5}), tmp_path)
    assert conn.name == "docs"
    assert conn.timeout_ms == 5000
    assert conn.oauth_url == "https://x/mcp"
    assert conn.state == "connecting"
    assert conn.closed is False
    assert conn.tools == []
    assert conn.has_resources is False


def test_connection_default_timeout_and_no_oauth_url(tmp_path):
    conn = _connection(_entry("local", {"command": "npx"}), tmp_path)
    assert conn.timeout_ms == DEFAULT_TIMEOUT_SECONDS * 1000
    assert conn.oauth_url is None


def test_oauth_settings_resolves_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_MCP_SECRET", "s3cret")
    entry = _entry(
        "docs",
        {
            "url": "https://x/mcp",
            "oauth": {"clientId": "cid", "clientSecret": "${PI_MCP_SECRET}", "scope": "a b"},
        },
    )
    settings = _connection(entry, tmp_path).oauth_settings()
    assert settings.client_id == "cid"
    assert settings.client_secret == "s3cret"
    assert settings.scope == "a b"


def test_oauth_settings_empty_without_oauth_block(tmp_path):
    settings = _connection(_entry("docs", {"url": "https://x/mcp"}), tmp_path).oauth_settings()
    assert (
        settings
        == __import__(
            "pi_coding_agent.extensions.mcp.oauth", fromlist=["McpOAuthSettings"]
        ).McpOAuthSettings()
    )


# ============================================================
# _with_client：重试 / 续期 / 401 升级的边界
# ============================================================


class _FakeClient:
    def __init__(self, outcomes: list[BaseException | object]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.closed = False
        self.connection_state = "connected"

    async def run(self) -> object:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def close(self) -> None:
        self.closed = True


async def _run_with(
    conn: McpServerConnection, client: _FakeClient, read_only: bool = False
) -> object:
    conn._client = client  # type: ignore[assignment]
    return await conn._with_client(lambda _c: client.run(), read_only)


async def test_read_only_retries_once_on_transient_http_error(tmp_path):
    conn = _connection(_entry("docs", {"url": "https://x/mcp"}), tmp_path)
    client = _FakeClient([McpHttpError(503, "restarting"), "ok"])
    assert await _run_with(conn, client, read_only=True) == "ok"
    assert client.calls == 2


async def test_tool_calls_do_not_retry_on_transient_error(tmp_path):
    """工具调用可能已经执行过，绝不重试。"""
    conn = _connection(_entry("docs", {"url": "https://x/mcp"}), tmp_path)
    client = _FakeClient([McpHttpError(503, "restarting")])
    with pytest.raises(McpHttpError):
        await _run_with(conn, client, read_only=False)
    assert client.calls == 1


async def test_permanent_http_error_is_not_retried_even_when_read_only(tmp_path):
    conn = _connection(_entry("docs", {"url": "https://x/mcp"}), tmp_path)
    client = _FakeClient([McpHttpError(501, "nope")])
    with pytest.raises(McpHttpError):
        await _run_with(conn, client, read_only=True)
    assert client.calls == 1


async def test_session_expired_reconnects_once(tmp_path):
    """server 不再认识该 session → 它没执行过请求：换新 session 重试一次。"""
    conn = _connection(_entry("docs", {"url": "https://x/mcp"}), tmp_path)
    first = _FakeClient([McpSessionExpiredError("gone")])
    second = _FakeClient(["ok"])
    conn._client = first  # type: ignore[assignment]

    seen: list[str] = []

    async def get_client():  # type: ignore[no-untyped-def]
        seen.append("connect")
        conn._client = second  # type: ignore[assignment]
        return second

    conn.get_client = get_client  # type: ignore[method-assign]
    assert await conn._with_client(lambda c: c.run()) == "ok"
    # 旧 client 被脱钩但**没有**被关闭（关掉会让它其它在途调用一起失败）
    assert first.closed is False


async def test_auth_required_marks_needs_auth_and_raises_guidance(tmp_path):
    conn = _connection(_entry("docs", {"url": "https://x/mcp"}), tmp_path)
    client = _FakeClient([McpOAuthAuthorizationRequiredError()])
    with pytest.raises(RuntimeError, match="requires sign-in. Run /mcp"):
        await _run_with(conn, client)
    assert conn.state == "needs-auth"
    assert conn.error is None
    assert client.closed is True


async def test_mcp_auth_required_only_counts_with_an_auth_provider(tmp_path):
    """没配 auth provider 时，McpAuthRequiredError 原样抛出（不当作"需要登录"）。"""
    conn = _connection(_entry("local", {"command": "npx"}), tmp_path)
    assert conn._auth_provider is None
    client = _FakeClient([McpAuthRequiredError("Bearer")])
    with pytest.raises(McpAuthRequiredError):
        await _run_with(conn, client)


async def test_unexpected_error_propagates_unchanged(tmp_path):
    conn = _connection(_entry("docs", {"url": "https://x/mcp"}), tmp_path)
    client = _FakeClient([ValueError("boom")])
    with pytest.raises(ValueError, match="boom"):
        await _run_with(conn, client)
    assert conn.state == "connecting"  # 未改状态


async def test_get_client_raises_when_closed(tmp_path):
    conn = _connection(_entry("docs", {"url": "https://x/mcp"}), tmp_path)
    conn._shutdown.set()
    with pytest.raises(RuntimeError, match="is shut down"):
        await conn.get_client()


async def test_close_marks_closed_and_settles_auth(tmp_path):
    conn = _connection(_entry("docs", {"url": "https://x/mcp"}), tmp_path)
    await conn.close()
    assert conn.closed is True
    assert conn.state == "closed"


def test_connect_retry_delays_match_upstream():
    assert CONNECT_RETRY_DELAYS_S == (0.25, 1.0)
