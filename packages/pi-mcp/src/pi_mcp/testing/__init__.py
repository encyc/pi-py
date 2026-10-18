"""测试辅助：内存传输。

对应上游 ``packages/mcp/src/testing/index.ts``（v1.3.0，**只有 1 行**）::

    export { createInMemoryTransportPair, InMemoryTransport } from "../transports/in-memory.ts";

> 上游 ``src/testing/`` **不是**可执行规格（不像 ``pi-durable`` 的
> ``env-conformance`` / ``storage-conformance``）；它只是把内存传输再导出一遍，
> 方便测试从 ``@earendil-works/pi-mcp/testing`` 取。本包照做，见 ``PORTING.md``。
"""

from __future__ import annotations

from ..transports.in_memory import InMemoryTransport, create_in_memory_transport_pair

__all__ = ["InMemoryTransport", "create_in_memory_transport_pair"]
