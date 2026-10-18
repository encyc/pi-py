"""传输实现：内存、stdio 子进程、Streamable HTTP。

对应上游 ``packages/mcp/src/transports/``。
"""

from __future__ import annotations

from .in_memory import InMemoryTransport, create_in_memory_transport_pair
from .stdio import StdioTransport, StdioTransportOptions
from .streamable_http import (
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
from .transport import (
    DEFAULT_MAX_MESSAGE_BYTES,
    McpTransport,
    McpTransportCloseListener,
    McpTransportErrorListener,
    McpTransportMessageListener,
    McpTransportSendOptions,
    TransportEvents,
)

__all__ = [
    "DEFAULT_MAX_MESSAGE_BYTES",
    "ConsumeSseOptions",
    "InMemoryTransport",
    "McpAuthRequiredError",
    "McpHttpError",
    "McpSessionExpiredError",
    "McpTransport",
    "McpTransportCloseListener",
    "McpTransportErrorListener",
    "McpTransportMessageListener",
    "McpTransportSendOptions",
    "SseEvent",
    "StdioTransport",
    "StdioTransportOptions",
    "StreamableHttpReconnectOptions",
    "StreamableHttpTransport",
    "StreamableHttpTransportOptions",
    "TransportEvents",
    "consume_sse_stream",
    "create_in_memory_transport_pair",
]
