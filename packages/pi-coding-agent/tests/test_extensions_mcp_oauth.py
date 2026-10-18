"""C3a 测试：MCP OAuth（``extensions/mcp/oauth.ts``）。只测确定性逻辑，不打真实网络。"""

from __future__ import annotations

import asyncio
import json

import pytest

from pi_coding_agent.auth_storage import FileAuthStorageBackend
from pi_coding_agent.extensions.mcp.oauth import (
    APP_NAME,
    CALLBACK_HOST,
    FALLBACK_REDIRECT_URL,
    McpOAuthCredentialStore,
    McpOAuthSettings,
    McpSignInCancelledError,
    McpSignInPrompt,
    agent_dir,
    callback_id,
    callback_settings,
    client_metadata_document,
    create_mcp_auth_provider,
    merge_scopes,
    parse_states,
    response_from_redirect_url,
    serialize_states,
    sign_in_mcp_server,
    store_keys,
)


class _Prompt(McpSignInPrompt):
    """一个最小的 McpSignInPrompt 实现（Protocol 的结构化实现）。"""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.redirect: str | None = None

    def show_authorization_url(self, url: str) -> None:
        self.urls.append(url)

    async def prompt_for_redirect_url(self, signal: object) -> str | None:
        return self.redirect


def _store(tmp_path):  # type: ignore[no-untyped-def]
    backend = FileAuthStorageBackend(str(tmp_path / "mcp-auth.json"))
    return McpOAuthCredentialStore(backend=backend, lock_dir=str(tmp_path))


# ============================================================
# 常量与目录
# ============================================================


def test_constants():
    assert APP_NAME == "pi"
    assert CALLBACK_HOST == "127.0.0.1"
    assert FALLBACK_REDIRECT_URL == "http://127.0.0.1/callback"


def test_agent_dir_matches_auth_storage_rule(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "custom"))
    assert agent_dir() == str(tmp_path / "custom")
    monkeypatch.delenv("PI_CODING_AGENT_DIR")
    assert agent_dir().endswith(".pi/agent")


def test_mcp_sign_in_prompt_is_a_protocol_not_a_stub():
    """契约用 Protocol 声明：不可实例化，也不存在"导入成功但运行才炸"的实现。"""
    assert getattr(McpSignInPrompt, "_is_protocol", False) is True
    with pytest.raises(TypeError):
        McpSignInPrompt()  # type: ignore[abstract]
    assert isinstance(_Prompt(), McpSignInPrompt)


# ============================================================
# callback_settings
# ============================================================


def test_callback_settings_defaults():
    settings = callback_settings(McpOAuthSettings())
    assert settings.host == "127.0.0.1"
    assert settings.redirect_host == "127.0.0.1"
    assert settings.port is None
    assert settings.path == "/callback"
    assert settings.fixed_redirect_url is None


def test_callback_settings_localhost_listens_on_loopback():
    """localhost 实际监听 127.0.0.1（::1 拒绝时浏览器会回退）。"""
    settings = callback_settings(McpOAuthSettings(callback_url="http://localhost:8080/oauth/cb"))
    assert settings.host == "127.0.0.1"
    assert settings.redirect_host == "localhost"
    assert settings.port == 8080
    assert settings.path == "/oauth/cb"
    # 端口来自 URI → 原样发送
    assert settings.fixed_redirect_url == "http://localhost:8080/oauth/cb"


def test_callback_settings_port_from_callback_port_builds_fixed_url():
    settings = callback_settings(McpOAuthSettings(callback_port=7000))
    assert settings.port == 7000
    assert settings.fixed_redirect_url == "http://127.0.0.1:7000/callback"


def test_callback_settings_ipv6_brackets_are_stripped_for_listen():
    settings = callback_settings(McpOAuthSettings(callback_url="http://[::1]:9000/callback"))
    assert settings.host == "::1"
    assert settings.redirect_host == "::1"
    assert settings.port == 9000


# ============================================================
# scopes / keys / state
# ============================================================


def test_merge_scopes_dedupes_and_preserves_order():
    assert merge_scopes("a b", "b c") == "a b c"
    assert merge_scopes(None, " a  a ") == "a"
    assert merge_scopes(None, None) is None


def test_store_keys_by_name_and_url_plus_legacy():
    key, legacy = store_keys("my-server", "https://x.example/mcp")
    assert key == "mcp__my_server|https://x.example/mcp"
    assert legacy == "https://x.example/mcp"


def test_store_keys_normalizes_url_like_js():
    _, legacy = store_keys("s", "HTTPS://X.Example:443/mcp#frag")
    assert legacy == "https://x.example/mcp"


def test_parse_and_serialize_states():
    assert parse_states(None) == {}
    assert parse_states("   ") == {}
    assert parse_states("[1,2]") == {}
    assert serialize_states({"a": 1}) == '{\n  "a": 1\n}\n'
    assert parse_states(serialize_states({"a": 1})) == {"a": 1}


# ============================================================
# McpOAuthCredentialStore
# ============================================================


def test_store_save_load_roundtrip(tmp_path):
    store = _store(tmp_path)
    server = store.for_server("docs", "https://x.example/mcp")
    asyncio.run(server.save({"tokens": {"access_token": "t1"}}))
    assert asyncio.run(server.load())["tokens"]["access_token"] == "t1"


def test_store_takes_over_legacy_state_once(tmp_path):
    """按 URL 的旧键被第一个加载的 server 接管，之后旧键消失。"""
    store = _store(tmp_path)
    legacy_key = "https://x.example/mcp"
    backend = FileAuthStorageBackend(str(tmp_path / "mcp-auth.json"))
    backend.with_lock(
        lambda _current: __import__(
            "pi_coding_agent.auth_storage", fromlist=["LockResult"]
        ).LockResult(
            result=None, next=serialize_states({legacy_key: {"tokens": {"access_token": "old"}}})
        )
    )

    server = store.for_server("docs", "https://x.example/mcp")
    assert asyncio.run(server.load())["tokens"]["access_token"] == "old"
    # 旧键已迁走
    key, _ = store_keys("docs", "https://x.example/mcp")
    states = parse_states(
        backend.with_lock(
            lambda c: __import__(
                "pi_coding_agent.auth_storage", fromlist=["LockResult"]
            ).LockResult(result=c)
        )
    )
    assert key in states and legacy_key not in states


def test_store_tokens_and_remove(tmp_path):
    store = _store(tmp_path)
    server = store.for_server("docs", "https://x.example/mcp")
    asyncio.run(server.save({"tokens": {"access_token": "t1"}}))
    assert store.tokens("docs", "https://x.example/mcp")["access_token"] == "t1"
    assert store.remove("docs", "https://x.example/mcp") is True
    assert store.remove("docs", "https://x.example/mcp") is False
    assert store.tokens("docs", "https://x.example/mcp") is None


def test_two_servers_sharing_a_url_keep_separate_accounts(tmp_path):
    store = _store(tmp_path)
    a = store.for_server("a", "https://shared.example/mcp")
    b = store.for_server("b", "https://shared.example/mcp")
    asyncio.run(a.save({"tokens": {"access_token": "token-a"}}))
    asyncio.run(b.save({"tokens": {"access_token": "token-b"}}))
    assert asyncio.run(a.load())["tokens"]["access_token"] == "token-a"
    assert asyncio.run(b.load())["tokens"]["access_token"] == "token-b"


async def test_refresh_lock_serializes(tmp_path):
    store = _store(tmp_path)
    server = store.for_server("docs", "https://x.example/mcp")
    order: list[str] = []

    async def task(label: str) -> None:
        async def run() -> None:
            order.append(f"{label}-enter")
            await asyncio.sleep(0.02)
            order.append(f"{label}-exit")

        await server.with_refresh_lock(run)

    await asyncio.gather(task("a"), task("b"))
    # 互斥：两组 enter/exit 不交错
    assert order in (
        ["a-enter", "a-exit", "b-enter", "b-exit"],
        ["b-enter", "b-exit", "a-enter", "a-exit"],
    )


# ============================================================
# callback_id / client metadata document
# ============================================================


def test_callback_id_is_twelve_chars_and_url_specific():
    first = callback_id("https://a.example/mcp")
    assert len(first) == 12
    assert first == callback_id("https://a.example/mcp")
    assert first != callback_id("https://b.example/mcp")


def test_callback_id_ignores_fragment():
    assert callback_id("https://a.example/mcp#x") == callback_id("https://a.example/mcp")


def test_client_metadata_document_requires_support():
    with pytest.raises(ValueError, match="does not support Client ID Metadata Documents"):
        client_metadata_document("https://a/mcp", "http://127.0.0.1/callback", None)
    with pytest.raises(ValueError, match="does not support Client ID Metadata Documents"):
        client_metadata_document(
            "https://a/mcp",
            "http://127.0.0.1/callback",
            {
                "client_id_metadata_document_supported": True,
                "token_endpoint_auth_methods_supported": ["secret"],
            },
        )


def test_client_metadata_document_shared_when_iss_supported():
    document = client_metadata_document(
        "https://a.example/mcp",
        "http://127.0.0.1:1234/callback",
        {
            "client_id_metadata_document_supported": True,
            "token_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": True,
        },
    )
    assert document == {
        "url": "https://pi.dev/oauth/client.json",
        "redirectUrl": "http://127.0.0.1:1234/callback",
    }


def test_client_metadata_document_server_specific_without_iss():
    document = client_metadata_document(
        "https://a.example/mcp",
        "http://127.0.0.1:1234/callback",
        {
            "client_id_metadata_document_supported": True,
            "token_endpoint_auth_methods_supported": ["none"],
        },
    )
    identifier = callback_id("https://a.example/mcp")
    assert document is not None
    assert document["url"] == f"https://pi.dev/oauth/{identifier}/client.json"
    assert document["redirectUrl"] == f"http://127.0.0.1:1234/callback/{identifier}"


# ============================================================
# response_from_redirect_url（粘贴模式）
# ============================================================


REDIRECT = "http://127.0.0.1:8080/callback"


def test_response_from_redirect_url_happy_path():
    response = response_from_redirect_url(
        f"{REDIRECT}?code=abc&state=s1&iss=https%3A%2F%2Fiss", "s1", REDIRECT
    )
    assert response == {"code": "abc", "iss": "https://iss"}


def test_response_from_redirect_url_rejects_mismatched_redirect():
    with pytest.raises(ValueError, match="does not match this sign-in's redirect URI"):
        response_from_redirect_url("http://127.0.0.1:9999/callback?code=a&state=s1", "s1", REDIRECT)
    with pytest.raises(ValueError, match="does not match this sign-in's redirect URI"):
        response_from_redirect_url("http://127.0.0.1:8080/other?code=a&state=s1", "s1", REDIRECT)


def test_response_from_redirect_url_rejects_wrong_state():
    with pytest.raises(ValueError, match="belongs to a different sign-in"):
        response_from_redirect_url(f"{REDIRECT}?code=abc&state=other", "s1", REDIRECT)


def test_response_from_redirect_url_reports_server_error():
    with pytest.raises(ValueError, match="access_denied"):
        response_from_redirect_url(f"{REDIRECT}?error=access_denied&state=s1", "s1", REDIRECT)
    with pytest.raises(ValueError, match="nope"):
        response_from_redirect_url(
            f"{REDIRECT}?error=x&error_description=nope&state=s1", "s1", REDIRECT
        )


def test_response_from_redirect_url_requires_code_and_valid_url():
    with pytest.raises(ValueError, match="does not contain an authorization code"):
        response_from_redirect_url(f"{REDIRECT}?state=s1", "s1", REDIRECT)
    with pytest.raises(ValueError, match="Expected the full redirect URL"):
        response_from_redirect_url("not a url", "s1", REDIRECT)


# ============================================================
# provider 形状
# ============================================================


def test_create_mcp_auth_provider_shape(tmp_path):
    store = _store(tmp_path)
    provider = create_mcp_auth_provider(
        server_url="https://x.example/mcp",
        store=store.for_server("docs", "https://x.example/mcp"),
        settings=lambda: McpOAuthSettings(),
        on_challenge=lambda _c: None,
    )
    for name in ("token", "on_unauthorized", "settled"):
        assert callable(getattr(provider, name))


async def test_token_returns_none_without_stored_state(tmp_path):
    store = _store(tmp_path)
    provider = create_mcp_auth_provider(
        server_url="https://x.example/mcp",
        store=store.for_server("docs", "https://x.example/mcp"),
        settings=lambda: McpOAuthSettings(),
        on_challenge=lambda _c: None,
    )
    assert await provider.token() is None
    await provider.settled()


async def test_sign_in_cancelled_when_signal_already_set(tmp_path):
    store = _store(tmp_path)
    signal = asyncio.Event()
    signal.set()
    with pytest.raises(McpSignInCancelledError):
        await sign_in_mcp_server(
            server_url="https://x.example/mcp",
            store=store.for_server("docs", "https://x.example/mcp"),
            settings=McpOAuthSettings(),
            prompt=_Prompt(),
            signal=signal,
        )


def test_json_states_file_is_human_readable(tmp_path):
    """凭据文件是可读 JSON（含缩进），便于用户排查。"""
    store = _store(tmp_path)
    server = store.for_server("docs", "https://x.example/mcp")
    asyncio.run(server.save({"tokens": {"access_token": "t"}}))
    raw = (tmp_path / "mcp-auth.json").read_text()
    assert raw.endswith("\n")
    assert json.loads(raw)["mcp__docs|https://x.example/mcp"]["tokens"]["access_token"] == "t"
