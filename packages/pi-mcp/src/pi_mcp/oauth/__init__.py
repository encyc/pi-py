"""MCP OAuth：元数据发现、动态客户端注册、PKCE 授权码流程、令牌刷新、回调服务器。

对应上游 ``packages/mcp/src/oauth/index.ts``（v1.3.0，56 行），也是
``@earendil-works/pi-mcp/oauth`` 子路径在 Python 侧的落点（``pi_mcp.oauth``）。

上游注释说明这些模块的来历：*"Adapted from modelcontextprotocol/typescript-sdk v1.29.0.
Copyright (c) 2024 Anthropic, PBC. Licensed under MIT."*
"""

from __future__ import annotations

from .callback import (
    OAuthCallback,
    OAuthCallbackPage,
    OAuthCallbackServer,
    OAuthCallbackServerOptions,
)
from .discovery import (
    build_authorization_server_discovery_urls,
    discover_authorization_server_metadata,
    discover_oauth_server_info,
    discover_protected_resource_metadata,
    parse_www_authenticate,
    resource_url_from_server_url,
    select_resource,
)
from .errors import (
    McpOAuthAuthorizationRequiredError,
    OAuthError,
    OAuthInsecureEndpointError,
    OAuthIssuerMismatchError,
    OAuthRegistrationError,
)
from .flow import (
    AddClientAuthentication,
    OAuthClientMetadataDocument,
    OAuthClientProvider,
    OAuthFlowOptions,
    OAuthFlowResult,
    TokenRequestOptions,
    adapt_oauth_provider,
    authorize_mcp,
    exchange_authorization_code,
    refresh_authorization,
    register_client,
    start_authorization,
    step_up_scope,
)
from .provider import (
    McpOAuthProvider,
    McpOAuthProviderOptions,
    McpOAuthState,
    McpOAuthStateStore,
    MemoryOAuthStateStore,
)
from .types import (
    AuthorizationServerMetadata,
    OAuthChallenge,
    OAuthClientInformation,
    OAuthClientInformationFull,
    OAuthClientInformationMixed,
    OAuthClientMetadata,
    OAuthDiscoveryState,
    OAuthProtectedResourceMetadata,
    OAuthServerInfo,
    OAuthTokens,
)

__all__ = [
    "AddClientAuthentication",
    "AuthorizationServerMetadata",
    "McpOAuthAuthorizationRequiredError",
    "McpOAuthProvider",
    "McpOAuthProviderOptions",
    "McpOAuthState",
    "McpOAuthStateStore",
    "MemoryOAuthStateStore",
    "OAuthCallback",
    "OAuthCallbackPage",
    "OAuthCallbackServer",
    "OAuthCallbackServerOptions",
    "OAuthChallenge",
    "OAuthClientInformation",
    "OAuthClientInformationFull",
    "OAuthClientInformationMixed",
    "OAuthClientMetadata",
    "OAuthClientMetadataDocument",
    "OAuthClientProvider",
    "OAuthDiscoveryState",
    "OAuthError",
    "OAuthFlowOptions",
    "OAuthFlowResult",
    "OAuthInsecureEndpointError",
    "OAuthIssuerMismatchError",
    "OAuthProtectedResourceMetadata",
    "OAuthRegistrationError",
    "OAuthServerInfo",
    "OAuthTokens",
    "TokenRequestOptions",
    "adapt_oauth_provider",
    "authorize_mcp",
    "build_authorization_server_discovery_urls",
    "discover_authorization_server_metadata",
    "discover_oauth_server_info",
    "discover_protected_resource_metadata",
    "exchange_authorization_code",
    "parse_www_authenticate",
    "refresh_authorization",
    "register_client",
    "resource_url_from_server_url",
    "select_resource",
    "start_authorization",
    "step_up_scope",
]
