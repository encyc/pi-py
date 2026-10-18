"""MCP server 配置，以及扩展通过 ``pi.registerMcpServer()`` 注册的 server。

对应上游 ``core/mcp-servers.ts``（346 行，**完整移植**）。

**core 只做校验与存储**：真正的连接由 MCP 扩展（内置的，或另一个处理
``mcp_servers_change`` 的扩展）与 ``mcp.json`` 里的 server 一起建立。

上游用 TS 接口 + 把原始 record 直接 cast 成接口（未知键会保留）。本端用 pydantic 模型
表达同样的形状：``extra="allow"`` 保留未知键（``mcp/config.ts`` 回写 ``mcp.json`` 需要），
字段用 snake_case + camelCase 别名（``populate_by_name=True``），与 ``pi-ai`` 的既有约定一致。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, TypeGuard
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "LIST_MCP_RESOURCES_TOOL",
    "LIST_MCP_RESOURCE_TEMPLATES_TOOL",
    "MCP_EXPOSURES",
    "READ_MCP_RESOURCE_TOOL",
    "McpExposure",
    "McpHttpServerConfig",
    "McpOAuthConfig",
    "McpServerConfig",
    "McpServerRegistry",
    "McpStdioServerConfig",
    "RegisteredMcpServer",
    "create_tool_name_matcher",
    "get_mcp_tool_exposure",
    "is_loopback_redirect_uri",
    "is_mcp_tool_name",
    "mcp_namespace",
    "validate_mcp_server_config",
]

#: 正则元字符中上游会转义的那一组（对齐 ``toolPatternRegExp`` 的字符类）。
_REGEX_METACHARS = re.compile(r"([.+?^${}()|\[\]\\])")

#: ``URL.canParse`` 要求有 scheme；Python 的 ``urlsplit`` 过于宽松，故显式校验。
_HAS_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _tool_pattern_regexp(pattern: str) -> re.Pattern[str]:
    """``*`` 匹配任意字符的工具名模式 → 正则（对齐上游 ``toolPatternRegExp``）。"""
    source = ".*".join(_REGEX_METACHARS.sub(r"\\\1", part) for part in pattern.split("*"))
    return re.compile(f"^{source}$")


def create_tool_name_matcher(entries: list[str]) -> Any:
    """返回一个判断「工具名是否命中 entries 中任一条目」的函数；条目为精确名或含 ``*`` 的模式。"""
    names = {entry for entry in entries if "*" not in entry}
    patterns = [_tool_pattern_regexp(entry) for entry in entries if "*" in entry]
    return lambda name: name in names or any(pattern.match(name) for pattern in patterns)


#: MCP 资源工具，触达每个提供资源的 server。
LIST_MCP_RESOURCES_TOOL = "list_mcp_resources"
LIST_MCP_RESOURCE_TEMPLATES_TOOL = "list_mcp_resource_templates"
READ_MCP_RESOURCE_TOOL = "read_mcp_resource"

_MCP_RESOURCE_TOOLS: frozenset[str] = frozenset(
    {LIST_MCP_RESOURCES_TOOL, LIST_MCP_RESOURCE_TEMPLATES_TOOL, READ_MCP_RESOURCE_TOOL}
)


def is_mcp_tool_name(name: str) -> bool:
    """工具是否来自 MCP：server 工具（``mcp__<server>__<tool>``）或资源工具。"""
    return name.startswith("mcp__") or name in _MCP_RESOURCE_TOOLS


#: 一个 server 的工具如何暴露给模型。
#:
#: - ``codemode``：脚本可调用，但既不声明给模型、也不列进 codemode 描述（只列 namespace）。
#:   脚本用 ``searchTools()`` 找它们。``codemode-deferred`` 是接受的别名。
#: - ``deferred``：在 ``tool_search`` 装载之前不声明给模型；之后模型可直接调用。不需要 codemode。
#: - ``direct``：像普通工具一样声明给模型（脚本也可调用）。
#: - ``hidden``：已注册但不可达。
McpExposure = Literal["codemode", "deferred", "direct", "hidden"]

MCP_EXPOSURES: tuple[str, ...] = ("codemode", "deferred", "direct", "hidden")

#: 旧的 exposure 名；校验时会被替换成当前名。
MCP_EXPOSURE_ALIASES: dict[str, str] = {"codemode-deferred": "codemode"}

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")

_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def _js_hostname(url: str) -> str | None:
    """JS ``URL.hostname`` 的等价物：IPv6 保留方括号。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.hostname
    if host is None:
        return None
    # Python 的 hostname 会剥掉方括号，JS 不会。
    if ":" in host:
        return f"[{host}]"
    return host


def _can_parse_url(value: str) -> bool:
    """``URL.canParse`` 的近似：必须有 scheme 且能解析出 host 之外的结构。"""
    if not isinstance(value, str) or not _HAS_SCHEME.match(value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return bool(parts.scheme)


def _js_scheme(url: str) -> str:
    """JS ``URL.protocol`` 带尾冒号（``"https:"``）；这里统一成同一形状。"""
    return f"{urlsplit(url).scheme}:"


def is_loopback_redirect_uri(value: str) -> bool:
    """该 redirect URI 能否由 pi 的 loopback 回调服务器承载。"""
    if not _can_parse_url(value):
        return False
    parts = urlsplit(value)
    return (
        _js_scheme(value) == "http:"
        and _js_hostname(value) in _LOOPBACK_HOSTS
        and parts.query == ""
        and parts.fragment == ""
    )


class McpOAuthConfig(BaseModel):
    """不支持动态客户端注册的 server 的 OAuth 客户端设置。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    #: 预注册的 client id；不填则由 pi 向授权服务器注册客户端。
    client_id: str | None = Field(default=None, alias="clientId")
    #: 可引用环境变量（``${NAME}``）或命令（``!cmd``）。
    client_secret: str | None = Field(default=None, alias="clientSecret")
    #: loopback 回调服务器端口；用于注册了固定 redirect URI 的客户端。
    callback_port: int | None = Field(default=None, alias="callbackPort")
    #: 为 ``clientId`` 注册的 redirect URI，必须是 localhost/127.0.0.1/[::1] 上的 http URI。
    callback_url: str | None = Field(default=None, alias="callbackUrl")
    #: 请求的 scope，空格分隔；默认用 server 自己声明的。
    scope: str | None = None
    #: 动态客户端注册时发送的 ``client_name``，默认 ``pi``。
    client_name: str | None = Field(default=None, alias="clientName")
    #: 没有 ``clientId`` 时 pi 如何标识自己：``dcr``（默认）或 ``cimd``。
    client_registration: str | None = Field(default=None, alias="clientRegistration")
    #: 授权服务器元数据文档，用于替代通过 server 的发现流程。
    auth_server_metadata_url: str | None = Field(default=None, alias="authServerMetadataUrl")


class McpServerConfigBase(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    #: 默认 ``codemode``。
    exposure: McpExposure | None = None
    #: 一句话说明该 server 提供什么；``mcp_servers`` 系统提示段与 tool search 排序都用它。
    description: str | None = None
    #: 单个工具的 exposure，覆盖 ``exposure``。键是 server 提供的工具名或含 ``*`` 的模式；
    #: 精确名优先于模式，模式中先匹配者胜。
    tool_exposure: dict[str, McpExposure] | None = Field(default=None, alias="toolExposure")
    #: 设为 false 可保留条目但不连接。默认 true。
    enabled: bool | None = None
    #: 每次请求的超时秒数；server 的 progress 通知会重置它。默认 60。
    timeout: float | None = None


class McpStdioServerConfig(McpServerConfigBase):
    type: Literal["stdio"] | None = None
    command: str
    args: list[str] | None = None
    #: 值可引用环境变量（``${NAME}``）或命令（``!cmd``）。
    env: dict[str, str] | None = None
    #: 相对路径按会话工作目录解析。
    cwd: str | None = None


class McpHttpServerConfig(McpServerConfigBase):
    type: Literal["http"] | None = None
    url: str
    #: 值可引用环境变量（``${NAME}``）或命令（``!cmd``）。
    headers: dict[str, str] | None = None
    oauth: McpOAuthConfig | None = None
    #: 使用某个 pi provider（``/login <provider>``）的 token 而不走 OAuth。
    #: 不允许出现在项目级 ``mcp.json``，且除 loopback 外必须是 https。
    auth: dict[str, str] | None = None


#: 一个 server 条目。上游是 TS 联合类型；Python 用 ``|`` 表达同样的两种形态。
McpServerConfig = McpStdioServerConfig | McpHttpServerConfig


def mcp_namespace(server: str) -> str:
    """server 工具的 namespace：``mcp__<server>``，其中 ``-`` 换成 ``_``（与工具名一致）。"""
    return f"mcp__{server.replace('-', '_')}"


def _is_record(value: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def _is_string_record(value: Any) -> TypeGuard[dict[str, str]]:
    return _is_record(value) and all(isinstance(entry, str) for entry in value.values())


def _validate_oauth(value: Any) -> str | None:
    if value is None:
        return None
    if not _is_record(value):
        return "oauth must be an object"
    if value.get("clientId") is not None and not isinstance(value["clientId"], str):
        return "oauth.clientId must be a string"
    if value.get("clientSecret") is not None and not isinstance(value["clientSecret"], str):
        return "oauth.clientSecret must be a string"
    port = value.get("callbackPort")
    if port is not None and (
        isinstance(port, bool) or not isinstance(port, int) or port < 1 or port > 65535
    ):
        return "oauth.callbackPort must be a port number"
    if value.get("callbackUrl") is not None:
        callback_url = value["callbackUrl"]
        if not isinstance(callback_url, str) or not is_loopback_redirect_uri(callback_url):
            return (
                "oauth.callbackUrl must be an http URI on localhost, 127.0.0.1, or [::1] "
                "without query or fragment"
            )
        url_port = urlsplit(callback_url).port
        if url_port and port is not None and url_port != port:
            return "oauth.callbackUrl and oauth.callbackPort name different ports"
    if value.get("scope") is not None and not isinstance(value["scope"], str):
        return "oauth.scope must be a string"
    if value.get("clientName") is not None and (
        not isinstance(value["clientName"], str) or not value["clientName"].strip()
    ):
        return "oauth.clientName must be a non-empty string"
    registration = value.get("clientRegistration")
    if registration is not None and registration != "dcr":
        if registration != "cimd":
            return 'oauth.clientRegistration must be "dcr" or "cimd"'
        if value.get("clientId") is not None or value.get("clientName") is not None:
            return 'oauth.clientRegistration "cimd" cannot be combined with oauth.clientId or oauth.clientName'
        callback_raw = value.get("callbackUrl")
        if isinstance(callback_raw, str):
            callback = urlsplit(callback_raw)
            if _js_hostname(callback_raw) == "[::1]" or callback.path != "/callback":
                return (
                    'oauth.clientRegistration "cimd" requires oauth.callbackUrl on localhost '
                    "or 127.0.0.1 with path /callback"
                )
    metadata_url = value.get("authServerMetadataUrl")
    if metadata_url is not None:
        parsed = (
            urlsplit(metadata_url)
            if isinstance(metadata_url, str) and _can_parse_url(metadata_url)
            else None
        )
        host_ok = parsed is not None and _js_hostname(metadata_url) in _LOOPBACK_HOSTS
        if parsed is None or not (
            _js_scheme(metadata_url) == "https:"
            or (_js_scheme(metadata_url) == "http:" and host_ok)
        ):
            return (
                "oauth.authServerMetadataUrl must be an https URL, or http on localhost, "
                "127.0.0.1, or [::1]"
            )
    return None


def _is_exposure(value: Any) -> bool:
    return isinstance(value, str) and value in MCP_EXPOSURES


def _resolve_exposure_alias(value: Any) -> Any:
    return MCP_EXPOSURE_ALIASES.get(value, value) if isinstance(value, str) else value


def _resolve_exposure_aliases(value: dict[str, Any]) -> dict[str, Any]:
    resolved: dict[str, Any] = dict(value)
    if resolved.get("exposure") is not None:
        resolved["exposure"] = _resolve_exposure_alias(resolved["exposure"])
    tool_exposure = resolved.get("toolExposure")
    if _is_record(tool_exposure):
        resolved["toolExposure"] = {
            tool: _resolve_exposure_alias(entry) for tool, entry in tool_exposure.items()
        }
    return resolved


def get_mcp_tool_exposure(config: McpServerConfig, tool_name: str) -> McpExposure:
    """某 server 单个工具的 exposure：先看 ``toolExposure`` 条目，否则用 server 的 ``exposure``。"""
    overrides = config.tool_exposure or {}
    exact = overrides.get(tool_name)
    if exact is not None:
        return exact
    for pattern, exposure in overrides.items():
        if "*" in pattern and _tool_pattern_regexp(pattern).match(tool_name):
            return exposure
    return config.exposure or "codemode"


def validate_mcp_server_config(name: str, raw: Any) -> McpServerConfig | str:
    """校验 ``mcpServers`` 形状里的一个 server 条目。

    返回解析过 exposure 别名的配置副本，或一条错误消息。
    """
    if not _SERVER_NAME.match(name):
        return f'invalid server name "{name}" (use letters, digits, "_" and "-")'
    if not _is_record(raw):
        return f'server "{name}" must be an object'
    value = _resolve_exposure_aliases(raw)
    server_type = value.get("type")
    exposure = value.get("exposure")
    enabled = value.get("enabled")
    timeout = value.get("timeout")
    tool_exposure = value.get("toolExposure")
    description = value.get("description")
    exposures = ", ".join(f'"{item}"' for item in MCP_EXPOSURES)

    if exposure is not None and not _is_exposure(exposure):
        return f'server "{name}": exposure must be one of {exposures}'
    if tool_exposure is not None:
        if not _is_record(tool_exposure):
            return f'server "{name}": toolExposure must map tool names to exposures'
        for tool, entry in tool_exposure.items():
            if not _is_exposure(entry):
                return f'server "{name}": toolExposure "{tool}" must be one of {exposures}'
    if enabled is not None and not isinstance(enabled, bool):
        return f'server "{name}": enabled must be a boolean'
    if description is not None and not isinstance(description, str):
        return f'server "{name}": description must be a string'
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0
    ):
        return f'server "{name}": timeout must be a positive number of seconds'
    if server_type == "sse":
        return (
            f'server "{name}": legacy SSE transport is not supported; use the streamable HTTP URL'
        )

    url = value.get("url")
    if isinstance(url, str) and (server_type is None or server_type in ("http", "streamable-http")):
        if not _can_parse_url(url) or _js_scheme(url) not in ("http:", "https:"):
            return f'server "{name}": url must be an http or https URL'
        if value.get("headers") is not None and not _is_string_record(value["headers"]):
            return f'server "{name}": headers must map names to strings'
        oauth_error = _validate_oauth(value.get("oauth"))
        if oauth_error:
            return f'server "{name}": {oauth_error}'
        auth = value.get("auth")
        if auth is not None:
            if (
                not _is_record(auth)
                or not isinstance(auth.get("provider"), str)
                or not auth["provider"]
            ):
                return f'server "{name}": auth.provider must be a provider name'
            if _js_scheme(url) != "https:" and _js_hostname(url) not in _LOOPBACK_HOSTS:
                return (
                    f'server "{name}": auth requires an https URL, or http on localhost, '
                    "127.0.0.1, or [::1]"
                )
        return McpHttpServerConfig.model_validate(value)

    command = value.get("command")
    if isinstance(command, str) and (server_type is None or server_type == "stdio"):
        args = value.get("args")
        if args is not None and not (
            isinstance(args, list) and all(isinstance(arg, str) for arg in args)
        ):
            return f'server "{name}": args must be an array of strings'
        if value.get("env") is not None and not _is_string_record(value["env"]):
            return f'server "{name}": env must map names to strings'
        if value.get("cwd") is not None and not isinstance(value["cwd"], str):
            return f'server "{name}": cwd must be a string'
        return McpStdioServerConfig.model_validate(value)

    return f'server "{name}" needs either "command" (stdio) or "url" (streamable HTTP)'


@dataclass
class RegisteredMcpServer:
    """扩展通过 ``pi.registerMcpServer()`` 注册的 server。"""

    name: str
    config: McpServerConfig
    #: 注册该 server 的扩展路径。
    extension_path: str


@dataclass
class McpServerRegistry:
    """某个 runtime 的扩展注册的 server 集合。"""

    _servers: dict[str, RegisteredMcpServer] = field(default_factory=dict)
    _change_listener: Any = None

    def register(self, server: RegisteredMcpServer) -> None:
        """注册或替换一个 server。归属检查由调用方负责。"""
        self._servers[server.name] = server
        if self._change_listener is not None:
            self._change_listener()

    def unregister(self, name: str, extension_path: str) -> None:
        """移除由 ``extension_path`` 注册的 server；其它扩展的 server 不动。"""
        current = self._servers.get(name)
        if current is None or current.extension_path != extension_path:
            return
        del self._servers[name]
        if self._change_listener is not None:
            self._change_listener()

    def get(self, name: str) -> RegisteredMcpServer | None:
        return self._servers.get(name)

    def list(self) -> list[RegisteredMcpServer]:
        """已注册 server 的副本，保持注册顺序（config 深拷贝）。"""
        return [
            RegisteredMcpServer(
                name=server.name,
                config=server.config.model_copy(deep=True),
                extension_path=server.extension_path,
            )
            for server in self._servers.values()
        ]

    def set_change_listener(self, listener: Any) -> None:
        """每次变更后调用；runner 在绑定时设置它，用来发 ``mcp_servers_change``。"""
        self._change_listener = listener
