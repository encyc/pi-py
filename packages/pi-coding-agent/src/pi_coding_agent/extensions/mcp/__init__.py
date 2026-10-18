"""内置 MCP 集成（``@earendil-works/pi-coding-agent`` 的 ``extensions/mcp/``）。

SDK 入口是 :func:`create_mcp_extension`。``/mcp`` 的 TUI Manager 与 CLI 命令**不移植**
（见 ``PORTING.md`` 的〈越界判断是双向的〉）。
"""

from __future__ import annotations

from .factory import AgentStartPayload, McpExtension, ToolCallPayload, create_mcp_extension

__all__ = [
    "AgentStartPayload",
    "McpExtension",
    "ToolCallPayload",
    "create_mcp_extension",
]
