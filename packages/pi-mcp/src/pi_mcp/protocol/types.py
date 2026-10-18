"""MCP 协议版本、能力与 ``tools`` / ``resources`` 的线上下载类型。

对应上游 ``packages/mcp/src/protocol/types.ts``（v1.3.0，131 行）。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from .content import BlobResourceContents, ContentAnnotations, TextResourceContents

#: 受支持的协议版本字面量。
SupportedProtocolVersion = Literal["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

#: 本端请求的协议版本。
LATEST_PROTOCOL_VERSION: SupportedProtocolVersion = "2025-11-25"

#: 本端**接受**服务端返回的版本。不支持所请求版本的服务端会用自己的最新版本作答，
#: 因此为兼容基于旧 SDK 构建的服务端，旧版本一并接受。
SUPPORTED_PROTOCOL_VERSIONS: tuple[SupportedProtocolVersion, ...] = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)


class _ImplementationRequired(TypedDict):
    name: str
    version: str


class Implementation(_ImplementationRequired, total=False):
    """实现标识（客户端/服务端信息）。"""

    title: str


class _RootRequired(TypedDict):
    uri: str


class Root(_RootRequired, total=False):
    """根目录。"""

    name: str


class ClientCapabilities(TypedDict, total=False):
    """客户端能力。"""

    experimental: dict[str, Any]
    roots: dict[str, Any]
    sampling: dict[str, Any]
    elicitation: dict[str, Any]


class ServerCapabilities(TypedDict, total=False):
    """服务端能力。"""

    experimental: dict[str, Any]
    logging: dict[str, Any]
    prompts: dict[str, Any]
    resources: dict[str, Any]
    tools: dict[str, Any]
    completions: dict[str, Any]


class InitializeParams(TypedDict):
    """``initialize`` 请求参数。"""

    protocolVersion: str
    capabilities: ClientCapabilities
    clientInfo: Implementation


class _InitializeResultRequired(TypedDict):
    protocolVersion: str
    capabilities: ServerCapabilities
    serverInfo: Implementation


class InitializeResult(_InitializeResultRequired, total=False):
    """``initialize`` 结果。"""

    instructions: str


class _ProgressNotificationRequired(TypedDict):
    progressToken: str | int | float
    progress: float


class ProgressNotification(_ProgressNotificationRequired, total=False):
    """``notifications/progress`` 参数。"""

    total: float
    message: str


class _CancelledNotificationRequired(TypedDict):
    requestId: str | int | float


class CancelledNotification(_CancelledNotificationRequired, total=False):
    """``notifications/cancelled`` 参数。"""

    reason: str


class ToolAnnotations(TypedDict, total=False):
    """工具注解。"""

    title: str
    readOnlyHint: bool
    destructiveHint: bool
    idempotentHint: bool
    openWorldHint: bool


class ToolExecution(TypedDict, total=False):
    """工具执行要求。"""

    taskSupport: Literal["forbidden", "optional", "required"]


class _ToolRequired(TypedDict):
    name: str
    inputSchema: dict[str, Any]


class Tool(_ToolRequired, total=False):
    """工具定义。"""

    title: str
    description: str
    outputSchema: dict[str, Any]
    annotations: ToolAnnotations
    execution: ToolExecution
    _meta: dict[str, Any]


class _ListToolsResultRequired(TypedDict):
    tools: list[Tool]


class ListToolsResult(_ListToolsResultRequired, total=False):
    """``tools/list`` 结果。"""

    nextCursor: str
    _meta: dict[str, Any]


class _ResourceRequired(TypedDict):
    uri: str


class Resource(_ResourceRequired, total=False):
    """``resources/list`` 列出的资源。"""

    name: str
    title: str
    description: str
    mimeType: str
    size: int
    annotations: ContentAnnotations
    _meta: dict[str, Any]


class _ResourceTemplateRequired(TypedDict):
    uriTemplate: str


class ResourceTemplate(_ResourceTemplateRequired, total=False):
    """``resources/templates/list`` 列出的资源族（RFC 6570 URI 模板）。"""

    name: str
    title: str
    description: str
    mimeType: str
    annotations: ContentAnnotations
    _meta: dict[str, Any]


class _ListResourcesResultRequired(TypedDict):
    resources: list[Resource]


class ListResourcesResult(_ListResourcesResultRequired, total=False):
    """``resources/list`` 结果。"""

    nextCursor: str
    _meta: dict[str, Any]


class _ListResourceTemplatesResultRequired(TypedDict):
    resourceTemplates: list[ResourceTemplate]


class ListResourceTemplatesResult(_ListResourceTemplatesResultRequired, total=False):
    """``resources/templates/list`` 结果。"""

    nextCursor: str
    _meta: dict[str, Any]


class _ReadResourceResultRequired(TypedDict):
    contents: list[TextResourceContents | BlobResourceContents]


class ReadResourceResult(_ReadResourceResultRequired, total=False):
    """``resources/read`` 结果。"""

    _meta: dict[str, Any]


__all__ = [
    "LATEST_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "CancelledNotification",
    "ClientCapabilities",
    "Implementation",
    "InitializeParams",
    "InitializeResult",
    "ListResourceTemplatesResult",
    "ListResourcesResult",
    "ListToolsResult",
    "ProgressNotification",
    "ReadResourceResult",
    "Resource",
    "ResourceTemplate",
    "Root",
    "ServerCapabilities",
    "SupportedProtocolVersion",
    "Tool",
    "ToolAnnotations",
    "ToolExecution",
]
