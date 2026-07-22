"""pi-server: Python port of @earendil-works/pi-server.

实验性 agent 服务化层：Unix socket + JSONL 协议，管理多个 agent 实例。

对应上游 ``packages/server``（TypeScript）。

关键偏离：上游 spawn 子进程（pi --mode rpc），Python 版直接在进程内
管理 ``CodingAgent`` 实例。

快速上手：
    import asyncio
    from pi_server import serve

    asyncio.run(serve())  # 启动常驻服务
"""

from __future__ import annotations

__version__ = "0.81.1"
__upstream_ref__ = "earendil-works/pi@v0.81.1"

from .config import get_server_dir, get_socket_path
from .ipc import handle_request, send_request, serve
from .protocol import InstanceStatus, InstanceSummary, encode_message, parse_line
from .supervisor import AgentInstance, Supervisor, supervisor

__all__ = [
    "__version__",
    "__upstream_ref__",
    "serve",
    "send_request",
    "handle_request",
    "supervisor",
    "Supervisor",
    "AgentInstance",
    "InstanceStatus",
    "InstanceSummary",
    "encode_message",
    "parse_line",
    "get_socket_path",
    "get_server_dir",
]
