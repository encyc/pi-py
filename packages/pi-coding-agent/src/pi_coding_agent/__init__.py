"""pi-coding-agent: Python port of @earendil-works/pi-coding-agent (core only).

编码 agent SDK：工具集（bash/read/edit/write/grep/find/ls）+ AgentSession 封装。

对应上游 ``packages/coding-agent/src/core/``。**不复刻 TUI/CLI**。

快速上手：
    from pi_coding_agent import CodingAgent
    agent = CodingAgent(model=my_model, api_key="...")
    await agent.prompt("列出当前目录的文件")
"""

from __future__ import annotations

__version__ = "0.81.1"
__upstream_ref__ = "earendil-works/pi@v0.81.1"

# ---- 工具 ----
# ---- SDK 入口 ----
from .sdk import DEFAULT_SYSTEM_PROMPT, CodingAgent, create_agent_session
from .tools import (
    BashTool,
    EditTool,
    FindTool,
    GrepTool,
    LsTool,
    ReadTool,
    WriteTool,
    create_all_tools,
    create_coding_tools,
    create_read_only_tools,
)

# ---- 截断工具 ----
from .truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    GREP_MAX_LINE_LENGTH,
    TruncationResult,
    format_size,
    truncate_head,
    truncate_line,
    truncate_tail,
)

__all__ = [
    "__version__",
    "__upstream_ref__",
    # 工具
    "BashTool",
    "EditTool",
    "FindTool",
    "GrepTool",
    "LsTool",
    "ReadTool",
    "WriteTool",
    "create_all_tools",
    "create_coding_tools",
    "create_read_only_tools",
    # SDK 入口
    "CodingAgent",
    "create_agent_session",
    "DEFAULT_SYSTEM_PROMPT",
    # 截断
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "GREP_MAX_LINE_LENGTH",
    "TruncationResult",
    "format_size",
    "truncate_head",
    "truncate_line",
    "truncate_tail",
]
