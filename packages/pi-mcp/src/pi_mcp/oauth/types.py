"""OAuth 线上对象类型与结构校验。

对应上游 ``packages/mcp/src/oauth/types.ts``（v1.3.0，209 行）。

上游注释说明了它的来历：*"Adapted from modelcontextprotocol/typescript-sdk v1.29.0.
Copyright (c) 2024 Anthropic, PBC. Licensed under MIT"*，并改成了无依赖的结构化校验。
本端同样不引入校验库：手写校验函数，**保留未知键**（上游到处 ``{ ...input }``）。

线上对象用 ``TypedDict``（只声明已知键）；``[key: string]: unknown`` 这类索引签名
Python 的 TypedDict 表达不了，但解析函数在运行时**确实**会把未知键原样带出。
"""

from __future__ import annotations

from typing import Any, TypedDict, cast
from urllib.parse import urlsplit

from ..protocol.jsonrpc import is_object


class OAuthProtectedResourceMetadata(TypedDict, total=False):
    """RFC 9728 受保护资源元数据。"""

    resource: str
    authorization_servers: list[str]
    scopes_supported: list[str]


class AuthorizationServerMetadata(TypedDict, total=False):
    """RFC 8414 授权服务器元数据（+ OIDC 发现）。"""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    scopes_supported: list[str]
    response_types_supported: list[str]
    grant_types_supported: list[str]
    token_endpoint_auth_methods_supported: list[str]
    code_challenge_methods_supported: list[str]
    client_id_metadata_document_supported: bool
    #: 授权响应是否携带 ``iss`` 参数（RFC 9207）。
    authorization_response_iss_parameter_supported: bool


class OAuthTokens(TypedDict, total=False):
    """令牌响应。"""

    access_token: str
    token_type: str
    expires_in: float
    scope: str
    refresh_token: str
    id_token: str


class OAuthClientMetadata(TypedDict, total=False):
    """客户端元数据（动态注册请求体）。"""

    redirect_uris: list[str]
    token_endpoint_auth_method: str
    grant_types: list[str]
    response_types: list[str]
    #: OIDC 客户端类型，``native`` 或 ``web``。动态注册在缺省时**从 redirect_uris 推导**（MCP SEP-837）。
    application_type: str
    client_name: str
    client_uri: str
    logo_uri: str
    scope: str
    contacts: list[str]
    tos_uri: str
    policy_uri: str
    jwks_uri: str
    jwks: Any
    software_id: str
    software_version: str
    software_statement: str


class OAuthClientInformation(TypedDict, total=False):
    """已注册客户端凭据。"""

    client_id: str
    client_secret: str
    client_id_issued_at: float
    client_secret_expires_at: float


#: 客户端信息（含元数据）。
OAuthClientInformationFull = OAuthClientInformation


#: 存储里可能是「只有凭据」也可能是「凭据 + 元数据」。
OAuthClientInformationMixed = OAuthClientInformation | OAuthClientInformationFull


class OAuthDiscoveryState(TypedDict, total=False):
    """可缓存的发现结果。"""

    authorizationServerUrl: str
    authorizationServerMetadata: AuthorizationServerMetadata
    resourceMetadata: OAuthProtectedResourceMetadata
    resourceMetadataUrl: str


class OAuthServerInfo(TypedDict, total=False):
    """发现结果。"""

    authorizationServerUrl: str
    authorizationServerMetadata: AuthorizationServerMetadata
    resourceMetadata: OAuthProtectedResourceMetadata


class OAuthChallenge(TypedDict, total=False):
    """``WWW-Authenticate`` 里解析出的挑战。"""

    resourceMetadataUrl: Any
    scope: str
    error: str
    errorDescription: str


# ============================================================
# 校验辅助
# ============================================================


def _object(value: object, name: str) -> dict[str, Any]:
    if not is_object(value):
        raise ValueError(f"Invalid {name}")
    return value


def _compact(value: dict[str, Any]) -> dict[str, Any]:
    """丢掉 ``None``，让可选字段**缺席**而不是「存在但为 None」。"""
    return {key: item for key, item in value.items() if item is not None}


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid {name}")
    return value


def _absent(value: object) -> bool:
    """``null`` 与 ``""`` 都当作缺席：服务端会用它们表示「没有值」，例如 ``scope: ""``。"""
    return value is None or value == ""


def _optional_string(value: object, name: str) -> str | None:
    if _absent(value):
        return None
    return _required_string(value, name)


def _optional_strings(value: object, name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"Invalid {name}")
    return list(value)


_INSECURE_SCHEMES = frozenset({"javascript", "data", "vbscript"})


def _safe_url(value: object, name: str) -> str:
    """校验一个 URL 字符串。

    JS 用 ``URL.canParse``（解析失败抛 ``TypeError``，发现逻辑专门用它区分网络失败），
    本端等价物是：必须有 scheme，且 http(s) 必须有 host —— ``urlsplit`` 本身太宽松，
    ``urlsplit("http://")`` 不报错而 ``new URL("http://")`` 会。
    """
    text = _required_string(value, name)
    parts = urlsplit(text)
    if not parts.scheme or parts.scheme in _INSECURE_SCHEMES:
        raise ValueError(f"Invalid {name}")
    if parts.scheme in ("http", "https") and not parts.netloc:
        raise ValueError(f"Invalid {name}")
    return text


def _optional_url(value: object, name: str) -> str | None:
    return None if _absent(value) else _safe_url(value, name)


# ============================================================
# 解析函数
# ============================================================


def parse_protected_resource_metadata(value: object) -> OAuthProtectedResourceMetadata:
    """校验受保护资源元数据。"""
    source = _object(value, "OAuth protected resource metadata")
    servers = _optional_strings(source.get("authorization_servers"), "authorization_servers")
    parsed = _compact(
        {
            **source,
            "resource": _safe_url(
                source.get("resource"), "OAuth protected resource metadata resource"
            ),
            "authorization_servers": (
                None
                if servers is None
                else [_safe_url(url, "authorization server URL") for url in servers]
            ),
            "scopes_supported": _optional_strings(
                source.get("scopes_supported"), "scopes_supported"
            ),
        }
    )
    return cast("OAuthProtectedResourceMetadata", parsed)


def parse_authorization_server_metadata(value: object) -> AuthorizationServerMetadata:
    """校验授权服务器元数据。``response_types_supported`` 必填。"""
    source = _object(value, "authorization server metadata")
    response_types = _optional_strings(
        source.get("response_types_supported"), "response_types_supported"
    )
    if response_types is None:
        raise ValueError("Invalid response_types_supported")
    parsed = _compact(
        {
            **source,
            "issuer": _safe_url(source.get("issuer"), "authorization server issuer"),
            "authorization_endpoint": _safe_url(
                source.get("authorization_endpoint"), "authorization endpoint"
            ),
            "token_endpoint": _safe_url(source.get("token_endpoint"), "token endpoint"),
            "registration_endpoint": _optional_url(
                source.get("registration_endpoint"), "registration endpoint"
            ),
            "scopes_supported": _optional_strings(
                source.get("scopes_supported"), "scopes_supported"
            ),
            "response_types_supported": response_types,
            "grant_types_supported": _optional_strings(
                source.get("grant_types_supported"), "grant_types_supported"
            ),
            "token_endpoint_auth_methods_supported": _optional_strings(
                source.get("token_endpoint_auth_methods_supported"),
                "token_endpoint_auth_methods_supported",
            ),
            "code_challenge_methods_supported": _optional_strings(
                source.get("code_challenge_methods_supported"),
                "code_challenge_methods_supported",
            ),
            "client_id_metadata_document_supported": (
                source["client_id_metadata_document_supported"]
                if isinstance(source.get("client_id_metadata_document_supported"), bool)
                else None
            ),
            "authorization_response_iss_parameter_supported": (
                source["authorization_response_iss_parameter_supported"]
                if isinstance(source.get("authorization_response_iss_parameter_supported"), bool)
                else None
            ),
        }
    )
    return cast("AuthorizationServerMetadata", parsed)


def parse_oauth_tokens(value: object) -> OAuthTokens:
    """校验令牌响应。"""
    source = _object(value, "OAuth token response")
    # ``Number(null)`` 是 0，会把令牌立刻判成已过期 —— 所以先判缺席。
    expires: float | None = None
    if not _absent(source.get("expires_in")):
        try:
            expires = float(source["expires_in"])
        except (TypeError, ValueError) as error:
            raise ValueError("Invalid expires_in") from error
        if expires != expires or expires in (float("inf"), float("-inf")):
            raise ValueError("Invalid expires_in")
    parsed = _compact(
        {
            "access_token": _required_string(source.get("access_token"), "access_token"),
            "token_type": _required_string(source.get("token_type"), "token_type"),
            "expires_in": expires,
            "scope": _optional_string(source.get("scope"), "scope"),
            "refresh_token": _optional_string(source.get("refresh_token"), "refresh_token"),
            "id_token": _optional_string(source.get("id_token"), "id_token"),
        }
    )
    return cast("OAuthTokens", parsed)


def parse_client_information(value: object) -> OAuthClientInformation:
    """校验动态客户端注册响应。"""
    source = _object(value, "OAuth client registration response")
    parsed = _compact(
        {
            **source,
            "client_id": _required_string(source.get("client_id"), "client_id"),
            "client_secret": _optional_string(source.get("client_secret"), "client_secret"),
            "client_id_issued_at": (
                source["client_id_issued_at"]
                if isinstance(source.get("client_id_issued_at"), (int, float))
                else None
            ),
            "client_secret_expires_at": (
                source["client_secret_expires_at"]
                if isinstance(source.get("client_secret_expires_at"), (int, float))
                else None
            ),
            "redirect_uris": _optional_strings(source.get("redirect_uris"), "redirect_uris") or [],
        }
    )
    return cast("OAuthClientInformation", parsed)


__all__ = [
    "AuthorizationServerMetadata",
    "OAuthChallenge",
    "OAuthClientInformation",
    "OAuthClientInformationFull",
    "OAuthClientInformationMixed",
    "OAuthClientMetadata",
    "OAuthDiscoveryState",
    "OAuthProtectedResourceMetadata",
    "OAuthServerInfo",
    "OAuthTokens",
    "parse_authorization_server_metadata",
    "parse_client_information",
    "parse_oauth_tokens",
    "parse_protected_resource_metadata",
]
