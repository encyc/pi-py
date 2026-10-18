"""骨架冒烟测试：包可 import、元数据正确、exports 子路径与上游对齐。"""

from __future__ import annotations

import importlib


def test_import() -> None:
    import pi_mcp

    assert pi_mcp.__version__ == "1.3.0"
    assert pi_mcp.__upstream_ref__ == "earendil-works/pi@v1.3.0"


def test_upstream_exports_subpaths_are_mirrored() -> None:
    """上游 ``package.json`` 的 exports：``.`` / ``./testing`` / ``./oauth`` —— 三者都要在。"""
    assert importlib.import_module("pi_mcp") is not None
    assert importlib.import_module("pi_mcp.testing") is not None
    assert importlib.import_module("pi_mcp.transports") is not None
    assert importlib.import_module("pi_mcp.protocol") is not None
    oauth = importlib.import_module("pi_mcp.oauth")
    assert oauth.__name__ == "pi_mcp.oauth"


def test_oauth_exports_the_symbols_coding_agent_consumes() -> None:
    """``pi-coding-agent`` 的 mcp 扩展直接消费这些名字（见 PORTING.md 的交接清单）。"""
    import pi_mcp.oauth as oauth

    for name in (
        "AuthorizationServerMetadata",
        "parse_www_authenticate",
        "McpOAuthAuthorizationRequiredError",
        "OAuthChallenge",
        "McpOAuthProvider",
        "authorize_mcp",
        "McpOAuthState",
        "McpOAuthStateStore",
        "OAuthCallback",
        "OAuthCallbackPage",
        "OAuthCallbackServer",
        "OAuthClientInformationMixed",
        "OAuthClientMetadataDocument",
        "step_up_scope",
    ):
        assert hasattr(oauth, name), f"pi_mcp.oauth 缺少导出: {name}"


def test_public_surface_matches_upstream_index() -> None:
    """上游 ``src/index.ts`` 导出的名字在根包都能拿到。"""
    import pi_mcp

    for name in (
        "AuthProvider",
        "McpClient",
        "McpClientOptions",
        "McpRequestOptions",
        "UnauthorizedContext",
        "JSON_RPC_ERROR_CODES",
        "McpAbortError",
        "McpConnectionClosedError",
        "McpError",
        "McpTimeoutError",
        "to_llm_content",
        "parse_json_rpc_message",
        "LATEST_PROTOCOL_VERSION",
        "SUPPORTED_PROTOCOL_VERSIONS",
        "StdioTransport",
        "StdioTransportOptions",
        "McpAuthRequiredError",
        "McpHttpError",
        "McpSessionExpiredError",
        "StreamableHttpTransport",
        "StreamableHttpTransportOptions",
        "McpTransport",
        "McpTransportSendOptions",
    ):
        assert hasattr(pi_mcp, name), f"根包缺少导出: {name}"


def test_http_dependency_is_declared() -> None:
    """``streamable-http`` 需要 httpx；它必须是**显式**依赖，不能靠传递带入。"""
    from importlib.metadata import requires

    declared = requires("pi-py-mcp") or []
    assert any(requirement.startswith("httpx") for requirement in declared), declared
