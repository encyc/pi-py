"""内存传输：一对互相投递的传输，供测试与进程内 server 使用。

对应上游 ``packages/mcp/src/transports/in-memory.ts``（v1.3.0，51 行）。
"""

from __future__ import annotations

import asyncio
import copy
from typing import TYPE_CHECKING

from ..protocol.jsonrpc import JsonRpcMessage, McpConnectionClosedError
from .transport import McpTransportSendOptions, TransportEvents

if TYPE_CHECKING:
    pass


class InMemoryTransport(TransportEvents):
    """把消息投递给对端的内存传输。

    投递是**异步**的（上游用 ``queueMicrotask``，这里用 ``loop.call_soon``），
    因此 ``send()`` 返回时对端还没看到消息 —— 与真实传输的时序一致。
    消息在投递前深拷贝（对应上游的 ``structuredClone``）。
    """

    def __init__(self) -> None:
        super().__init__()
        self._peer: InMemoryTransport | None = None
        self._started = False
        self._closed = False

    @property
    def started(self) -> bool:
        """是否已 ``start()``。"""
        return self._started

    @property
    def closed(self) -> bool:
        """是否已关闭。"""
        return self._closed

    def connect_peer(self, peer: InMemoryTransport) -> None:
        """连接对端（只能设置一次）。"""
        if self._peer is not None:
            raise RuntimeError("In-memory MCP transport already has a peer")
        self._peer = peer

    async def start(self) -> None:
        if self._closed:
            raise McpConnectionClosedError()
        self._started = True

    async def send(
        self, message: JsonRpcMessage, options: McpTransportSendOptions | None = None
    ) -> None:
        if not self._started or self._closed:
            raise McpConnectionClosedError()
        peer = self._peer
        if peer is None or not peer._started or peer._closed:
            raise McpConnectionClosedError("In-memory MCP peer is not connected")
        payload = copy.deepcopy(message)
        asyncio.get_running_loop().call_soon(peer._deliver, payload)
        # 让出一次事件循环，使排好的投递在 ``send()`` 返回前完成 —— 对应 Node 里
        # ``await send()`` 返回时 ``queueMicrotask`` 已经跑过（上游测试因此可以在
        # ``connect()`` 之后立即断言服务端收到的消息）。
        await asyncio.sleep(0)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._emit_close()
        if self._peer is not None:
            await self._peer.close()

    def _deliver(self, message: JsonRpcMessage) -> None:
        if self._closed:
            return
        self._emit_message(message)


def create_in_memory_transport_pair() -> tuple[InMemoryTransport, InMemoryTransport]:
    """建一对已互连的传输，返回 ``(client, server)``。"""
    client = InMemoryTransport()
    server = InMemoryTransport()
    client.connect_peer(server)
    server.connect_peer(client)
    return client, server


__all__ = ["InMemoryTransport", "create_in_memory_transport_pair"]
