"""OAuth 错误类型。

对应上游 ``packages/mcp/src/oauth/errors.ts``（v1.3.0，55 行）。
"""

from __future__ import annotations


class OAuthError(Exception):
    """授权服务器返回的 OAuth 错误（``error`` / ``error_description`` / ``error_uri``）。"""

    def __init__(self, code: str, message: str, error_uri: str | None = None) -> None:
        super().__init__(message or code)
        self.name = "OAuthError"
        self.code = code
        self.error_uri = error_uri


class OAuthIssuerMismatchError(Exception):
    """授权服务器的 issuer 与预期不符（RFC 8414 / RFC 9207 的 ``iss`` 校验失败）。"""

    def __init__(self, expected: str | None, received: str | None) -> None:
        def fmt(issuer: str | None) -> str:
            return "none" if issuer is None else repr(issuer)

        super().__init__(
            f"OAuth issuer mismatch: expected {fmt(expected)}, received {fmt(received)}"
        )
        self.name = "OAuthIssuerMismatchError"
        #: 未发现授权服务器元数据时为 ``None``。
        self.expected = expected
        #: 授权响应缺少其服务器承诺的 ``iss`` 参数（RFC 9207）时为 ``None``。
        self.received = received


class OAuthInsecureEndpointError(Exception):
    """拒绝把 OAuth 凭据发往非 HTTPS 端点。"""

    def __init__(self, endpoint: str) -> None:
        super().__init__(f"Refusing to send OAuth credentials to non-HTTPS endpoint {endpoint}")
        self.name = "OAuthInsecureEndpointError"
        self.endpoint = endpoint


class OAuthRegistrationError(Exception):
    """动态客户端注册失败。"""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"OAuth dynamic client registration failed with status {status}: {body}")
        self.name = "OAuthRegistrationError"
        self.status = status
        self.body = body


class McpOAuthAuthorizationRequiredError(Exception):
    """OAuth 授权需要用户交互（无法仅靠存储的凭据完成）。"""

    def __init__(self) -> None:
        super().__init__("MCP OAuth authorization requires user interaction")
        self.name = "McpOAuthAuthorizationRequiredError"


__all__ = [
    "McpOAuthAuthorizationRequiredError",
    "OAuthError",
    "OAuthInsecureEndpointError",
    "OAuthIssuerMismatchError",
    "OAuthRegistrationError",
]
