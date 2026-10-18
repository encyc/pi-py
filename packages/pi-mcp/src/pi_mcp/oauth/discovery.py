"""OAuth 元数据发现（受保护资源 + 授权服务器）。

对应上游 ``packages/mcp/src/oauth/discovery.ts``（v1.3.0，215 行）。

上游注释：*"Adapted from modelcontextprotocol/typescript-sdk v1.29.0 src/client/auth.ts.
Copyright (c) 2024 Anthropic, PBC. Licensed under MIT. Modified to remove Zod/CORS shims and
enforce authorization-server issuer validation."*

移植约定（见 ``PORTING.md``）：``McpFetch`` → 注入 ``httpx.AsyncClient``（不注入则临时自建并关闭）；
``AbortSignal`` → ``asyncio.Event``，置位时抛 :class:`McpAbortError`。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .._cancel import await_or_abort
from ..protocol.jsonrpc import McpAbortError
from ..protocol.types import LATEST_PROTOCOL_VERSION
from .errors import OAuthIssuerMismatchError
from .types import (
    AuthorizationServerMetadata,
    OAuthChallenge,
    OAuthProtectedResourceMetadata,
    OAuthServerInfo,
    parse_authorization_server_metadata,
    parse_protected_resource_metadata,
)

#: ``/.well-known/<kind>`` 的候选（``type`` 用于诊断）。
DiscoveryUrl = tuple[str, Literal["oauth", "oidc"]]

#: 未注入客户端时自建客户端的超时（上游用 ``globalThis.fetch``，无此概念）。
DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 30.0


@contextlib.asynccontextmanager
async def _client_scope(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    """借用注入的客户端，或临时自建一个并在结束时关闭。"""
    if client is not None:
        yield client
        return
    owned = httpx.AsyncClient(timeout=DEFAULT_DISCOVERY_TIMEOUT_SECONDS)
    try:
        yield owned
    finally:
        await owned.aclose()


async def _discard(response: httpx.Response | None) -> None:
    if response is None:
        return
    with contextlib.suppress(Exception):
        await response.aread()
    with contextlib.suppress(Exception):
        await response.aclose()


def _is_discovery_miss(status: int) -> bool:
    """4xx 与 502 表示「这里没有」，发现逻辑就试下一个候选 URL。"""
    return 400 <= status < 500 or status == 502


def _trim_slash(value: str) -> str:
    """URL 解析会给裸 origin 补尾斜杠，比较 issuer 时两边都去掉。"""
    return value[:-1] if value.endswith("/") else value


def _path_suffix(pathname: str) -> str:
    """``/.well-known/<kind><path>`` 的路径后缀；根路径时为空。"""
    return pathname[:-1] if pathname.endswith("/") else pathname


_field_cache: dict[str, re.Pattern[str]] = {}


def _field_pattern(name: str) -> re.Pattern[str]:
    pattern = _field_cache.get(name)
    if pattern is None:
        pattern = re.compile(
            rf'(?:^|[,\s]){re.escape(name)}=(?:"([^"]*)"|([^\s,]+))', re.IGNORECASE
        )
        _field_cache[name] = pattern
    return pattern


def _field(header: str, name: str) -> str | None:
    match = _field_pattern(name).search(header)
    if match is None:
        return None
    # 空值（``scope=""``）不带信息，算缺席。
    return match.group(1) or match.group(2) or None


def parse_www_authenticate(header: str | None) -> OAuthChallenge:
    """解析 ``WWW-Authenticate``（仅 ``Bearer`` / ``DPoP``；其它 scheme 返回空挑战）。"""
    if not header:
        return {}
    scheme = re.split(r"\s+", header.lstrip(), maxsplit=1)[0].lower()
    if scheme not in ("bearer", "dpop"):
        return {}
    challenge: OAuthChallenge = {}
    resource_metadata = _field(header, "resource_metadata")
    if resource_metadata:
        parts = urlsplit(resource_metadata)
        if parts.scheme and (parts.scheme not in ("http", "https") or parts.netloc):
            challenge["resourceMetadataUrl"] = resource_metadata
    for key, name in (
        ("scope", "scope"),
        ("error", "error"),
        ("errorDescription", "error_description"),
    ):
        value = _field(header, name)
        if value is not None:
            challenge[key] = value  # type: ignore[literal-required]
    return challenge


async def _fetch_metadata(
    url: str,
    client: httpx.AsyncClient,
    protocol_version: str,
    signal: asyncio.Event | None,
) -> httpx.Response:
    if signal is not None and signal.is_set():
        raise McpAbortError()
    # 竞速取消：上游把 signal 交给 fetch，在途请求会被中止。
    return await await_or_abort(
        client.get(
            url,
            headers={"Accept": "application/json", "MCP-Protocol-Version": protocol_version},
        ),
        signal,
    )


async def discover_protected_resource_metadata(
    server_url: str,
    *,
    resource_metadata_url: str | None = None,
    protocol_version: str | None = None,
    client: httpx.AsyncClient | None = None,
    signal: asyncio.Event | None = None,
) -> OAuthProtectedResourceMetadata:
    """RFC 9728：发现 MCP 服务端的受保护资源元数据。

    未显式给出 ``resource_metadata_url`` 时，先试 ``/.well-known/oauth-protected-resource<path>``，
    命中 4xx/502 再退回根路径。
    """
    version = protocol_version or LATEST_PROTOCOL_VERSION
    server = urlsplit(server_url)
    origin = f"{server.scheme}://{server.netloc}"
    async with _client_scope(client) as http:
        if resource_metadata_url is not None:
            target = resource_metadata_url
        else:
            target = urljoin(
                origin, f"/.well-known/oauth-protected-resource{_path_suffix(server.path)}"
            )
        response = await _fetch_metadata(target, http, version, signal)
        if (
            resource_metadata_url is None
            and server.path != "/"
            and _is_discovery_miss(response.status_code)
        ):
            await _discard(response)
            response = await _fetch_metadata(
                urljoin(origin, "/.well-known/oauth-protected-resource"), http, version, signal
            )
        if not response.is_success:
            status = response.status_code
            await _discard(response)
            raise RuntimeError(f"HTTP {status} loading OAuth protected resource metadata")
        try:
            return parse_protected_resource_metadata(response.json())
        finally:
            await _discard(response)


def build_authorization_server_discovery_urls(authorization_server_url: str) -> list[DiscoveryUrl]:
    """RFC 8414 / OIDC 发现文档的候选 URL（按顺序尝试）。"""
    issuer = urlsplit(authorization_server_url)
    origin = f"{issuer.scheme}://{issuer.netloc}"
    path = _path_suffix(issuer.path)
    urls: list[DiscoveryUrl] = [
        (urljoin(origin, f"/.well-known/oauth-authorization-server{path}"), "oauth"),
        (urljoin(origin, f"/.well-known/openid-configuration{path}"), "oidc"),
    ]
    if path:
        urls.append((urljoin(origin, f"{path}/.well-known/openid-configuration"), "oidc"))
    return urls


async def discover_authorization_server_metadata(
    authorization_server_url: str,
    *,
    client: httpx.AsyncClient | None = None,
    protocol_version: str | None = None,
    skip_issuer_validation: bool = False,
    signal: asyncio.Event | None = None,
) -> AuthorizationServerMetadata | None:
    """按候选顺序发现授权服务器元数据；全都没命中时返回 ``None``。"""
    version = protocol_version or LATEST_PROTOCOL_VERSION
    async with _client_scope(client) as http:
        for url, _kind in build_authorization_server_discovery_urls(authorization_server_url):
            response = await _fetch_metadata(url, http, version, signal)
            if not response.is_success:
                status = response.status_code
                await _discard(response)
                if _is_discovery_miss(status):
                    continue
                raise RuntimeError(
                    f"HTTP {status} loading authorization server metadata from {url}"
                )
            try:
                metadata = parse_authorization_server_metadata(response.json())
            finally:
                await _discard(response)
            # URL 解析会给裸 origin 补尾斜杠，比较时两边都去掉。
            if not skip_issuer_validation and _trim_slash(metadata["issuer"]) != _trim_slash(
                authorization_server_url
            ):
                raise OAuthIssuerMismatchError(authorization_server_url, metadata["issuer"])
            return metadata
    return None


async def discover_oauth_server_info(
    server_url: str,
    *,
    resource_metadata_url: str | None = None,
    authorization_server_metadata_url: str | None = None,
    client: httpx.AsyncClient | None = None,
    skip_issuer_validation: bool = False,
    signal: asyncio.Event | None = None,
) -> OAuthServerInfo:
    """发现受保护资源 + 授权服务器信息。

    ``authorization_server_metadata_url`` 给定时直接用它（**按配置信任**，不校验 issuer）。
    网络故障（``httpx.TransportError``）往上抛；「文档不存在」当作没有该文档继续。
    """
    async with _client_scope(client) as http:
        resource_metadata: OAuthProtectedResourceMetadata | None = None
        try:
            resource_metadata = await discover_protected_resource_metadata(
                server_url,
                resource_metadata_url=resource_metadata_url,
                client=http,
                signal=signal,
            )
        except httpx.TransportError:
            raise
        except Exception:  # noqa: BLE001 - 没有资源元数据也能继续
            resource_metadata = None

        if authorization_server_metadata_url is not None:
            url = authorization_server_metadata_url
            response = await _fetch_metadata(url, http, LATEST_PROTOCOL_VERSION, signal)
            if not response.is_success:
                status = response.status_code
                await _discard(response)
                raise RuntimeError(
                    f"HTTP {status} loading authorization server metadata from {url}"
                )
            try:
                metadata = parse_authorization_server_metadata(response.json())
            finally:
                await _discard(response)
            info: OAuthServerInfo = {
                "authorizationServerUrl": metadata["issuer"],
                "authorizationServerMetadata": metadata,
            }
            if resource_metadata is not None:
                info["resourceMetadata"] = resource_metadata
            return info

        servers = (
            resource_metadata.get("authorization_servers")
            if resource_metadata is not None
            else None
        )
        authorization_server_url = (servers[0] if servers else None) or _origin_root(server_url)
        discovered = await discover_authorization_server_metadata(
            authorization_server_url,
            client=http,
            skip_issuer_validation=skip_issuer_validation,
            signal=signal,
        )
        result: OAuthServerInfo = {"authorizationServerUrl": authorization_server_url}
        if discovered is not None:
            result["authorizationServerMetadata"] = discovered
        if resource_metadata is not None:
            result["resourceMetadata"] = resource_metadata
        return result


def _origin_root(value: str) -> str:
    """``new URL("/", serverUrl).href``：丢弃路径与查询。"""
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def _js_href(value: str) -> str:
    """尽量还原 JS ``new URL(value).href``（含 fragment 时由调用方先清掉）。

    显式补的可观察差异只有一个：JS 会给空路径补 ``/``（``new URL("https://a.example").href``
    是 ``https://a.example/``），而 ``urlunsplit`` 不会。
    """
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def resource_url_from_server_url(value: str) -> str:
    """MCP 服务端 URL → 资源标识 URL（去掉 fragment）。"""
    parts = urlsplit(value)
    return _js_href(urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, "")))


def select_resource(
    server_url: str, metadata: OAuthProtectedResourceMetadata | None = None
) -> str | None:
    """校验受保护资源与服务端同源、且路径前缀匹配，返回资源标识。"""
    if not metadata:
        return None
    requested = urlsplit(resource_url_from_server_url(server_url))
    configured = urlsplit(metadata["resource"])
    # 上游把 URL 对象插进模板字符串（``${requested}``），拿到的是规范化后的 href。
    message = (
        f"Protected resource {metadata['resource']} does not match MCP server "
        f"{_js_href(urlunsplit((requested.scheme, requested.netloc, requested.path, requested.query, '')))}"
    )
    if requested.scheme != configured.scheme or requested.netloc != configured.netloc:
        raise RuntimeError(message)
    requested_path = requested.path if requested.path.endswith("/") else f"{requested.path}/"
    configured_path = configured.path if configured.path.endswith("/") else f"{configured.path}/"
    if not requested_path.startswith(configured_path):
        raise RuntimeError(message)
    return metadata["resource"]


__all__ = [
    "DEFAULT_DISCOVERY_TIMEOUT_SECONDS",
    "DiscoveryUrl",
    "build_authorization_server_discovery_urls",
    "discover_authorization_server_metadata",
    "discover_oauth_server_info",
    "discover_protected_resource_metadata",
    "parse_www_authenticate",
    "resource_url_from_server_url",
    "select_resource",
]
