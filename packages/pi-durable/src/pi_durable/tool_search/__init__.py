"""``tool_search`` 扩展 —— 移植 ``durable/src/tool-search/index.ts``（7 行）。"""

from __future__ import annotations

from pi_durable.harness import Extension, define_extension
from pi_durable.tool_search.tool_search import (
    DEFAULT_TOOL_SEARCH_LIMIT,
    TOOL_SEARCH_TOOL_NAME,
    ToolSearchToolDetails,
    ToolSearchToolInput,
    create_tool_search_tool,
)

__all__ = [
    "DEFAULT_TOOL_SEARCH_LIMIT",
    "TOOL_SEARCH_TOOL_NAME",
    "ToolSearchToolDetails",
    "ToolSearchToolInput",
    "create_tool_search_extension",
    "create_tool_search_tool",
]


def create_tool_search_extension() -> Extension[ToolSearchToolInput]:
    """``createToolSearchExtension()``（``tool-search.ts:89-92``）。

    装上它，并在希望模型能找到延迟工具的会话里选中它。
    """
    return define_extension(name="tool-search", tools=[create_tool_search_tool()])
