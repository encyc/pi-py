"""``pi_mcp`` — 独立的 Model Context Protocol 客户端。

`@earendil-works/pi-mcp`_ v1.3.0 的 Python 移植（**第一期：核心面**）。
不依赖 pi 运行时，也不依赖 pi-py 的其它包：只依赖 ``httpx``。

上游 ``package.json`` 的 ``exports`` 与本包模块路径的对应：

| 上游 subpath | 本包 |
|---|---|
| `@earendil-works/pi-mcp` | ``pi_mcp`` |
| `@earendil-works/pi-mcp/testing` | ``pi_mcp.testing`` |
| `@earendil-works/pi-mcp/oauth` | ``pi_mcp.oauth`` |

范围：``McpClient``、JSON-RPC 与内容类型、三种传输（in-memory / stdio 子进程 /
Streamable HTTP，含 SSE 与断线重连）、``toLlmContent``，以及 ``pi_mcp.oauth``
（元数据发现、动态客户端注册、PKCE 授权码流程、令牌刷新、回环回调服务器）。

.. _@earendil-works/pi-mcp: https://github.com/earendil-works/pi
"""

from __future__ import annotations

from .auth_provider import AuthProvider, BearerAuthProvider, McpFetch, UnauthorizedContext
from .client import McpClient, McpClientOptions, McpRequestOptions
from .protocol.content import (
    AudioContent,
    BlobResourceContents,
    CallToolResult,
    ContentAnnotations,
    ContentBlock,
    EmbeddedResourceContent,
    ImageContent,
    LlmContent,
    LlmContentBlock,
    LlmImageContent,
    LlmTextContent,
    ResourceLinkContent,
    TextContent,
    TextResourceContents,
    to_llm_content,
)
from .protocol.jsonrpc import (
    JSON_RPC_ERROR_CODES,
    JsonRpcErrorObject,
    JsonRpcErrorResponse,
    JsonRpcId,
    JsonRpcMessage,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcSuccessResponse,
    McpAbortError,
    McpConnectionClosedError,
    McpError,
    McpTimeoutError,
    is_json_rpc_notification,
    is_json_rpc_request,
    is_json_rpc_response,
    parse_json_rpc_message,
)
from .protocol.types import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    CancelledNotification,
    ClientCapabilities,
    Implementation,
    InitializeParams,
    InitializeResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    ProgressNotification,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    Root,
    ServerCapabilities,
    SupportedProtocolVersion,
    Tool,
    ToolAnnotations,
    ToolExecution,
)
from .transports.stdio import StdioTransport, StdioTransportOptions
from .transports.streamable_http import (
    ConsumeSseOptions,
    McpAuthRequiredError,
    McpHttpError,
    McpSessionExpiredError,
    SseEvent,
    StreamableHttpReconnectOptions,
    StreamableHttpTransport,
    StreamableHttpTransportOptions,
    consume_sse_stream,
)
from .transports.transport import (
    McpTransport,
    McpTransportCloseListener,
    McpTransportErrorListener,
    McpTransportMessageListener,
    McpTransportSendOptions,
)

__version__ = "1.3.0"
__upstream_ref__ = "earendil-works/pi@v1.3.0"

__all__ = [
    "JSON_RPC_ERROR_CODES",
    "LATEST_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "AudioContent",
    "AuthProvider",
    "BearerAuthProvider",
    "BlobResourceContents",
    "CallToolResult",
    "CancelledNotification",
    "ClientCapabilities",
    "ConsumeSseOptions",
    "ContentAnnotations",
    "ContentBlock",
    "EmbeddedResourceContent",
    "ImageContent",
    "Implementation",
    "InitializeParams",
    "InitializeResult",
    "JsonRpcErrorObject",
    "JsonRpcErrorResponse",
    "JsonRpcId",
    "JsonRpcMessage",
    "JsonRpcNotification",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "JsonRpcSuccessResponse",
    "ListResourceTemplatesResult",
    "ListResourcesResult",
    "ListToolsResult",
    "LlmContent",
    "LlmContentBlock",
    "LlmImageContent",
    "LlmTextContent",
    "McpAbortError",
    "McpAuthRequiredError",
    "McpFetch",
    "McpClient",
    "McpClientOptions",
    "McpConnectionClosedError",
    "McpError",
    "McpHttpError",
    "McpRequestOptions",
    "McpSessionExpiredError",
    "McpTimeoutError",
    "McpTransport",
    "McpTransportCloseListener",
    "McpTransportErrorListener",
    "McpTransportMessageListener",
    "McpTransportSendOptions",
    "ProgressNotification",
    "ReadResourceResult",
    "Resource",
    "ResourceLinkContent",
    "ResourceTemplate",
    "Root",
    "ServerCapabilities",
    "SseEvent",
    "StdioTransport",
    "StdioTransportOptions",
    "StreamableHttpReconnectOptions",
    "StreamableHttpTransport",
    "StreamableHttpTransportOptions",
    "SupportedProtocolVersion",
    "TextContent",
    "TextResourceContents",
    "Tool",
    "ToolAnnotations",
    "ToolExecution",
    "UnauthorizedContext",
    "__upstream_ref__",
    "__version__",
    "consume_sse_stream",
    "is_json_rpc_notification",
    "is_json_rpc_request",
    "is_json_rpc_response",
    "parse_json_rpc_message",
    "to_llm_content",
]
