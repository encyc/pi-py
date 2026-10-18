"""``tool_search`` 内置扩展。

对应上游 ``src/extensions/tool-search/``（262 行）。

``tool_search`` 以**未激活**状态注册；用 ``defaultTools`` 设置、``--tools`` 或
``setActiveTools()`` 激活它。
"""

from __future__ import annotations

from ..types import ExtensionAPI, ExtensionFactory
from .tool import (
    DEFAULT_TOOL_SEARCH_LIMIT,
    TOOL_SEARCH_DESCRIPTION,
    TOOL_SEARCH_TOOL_NAME,
    Bm25Ranker,
    ToolSearchDocument,
    ToolSearchMatch,
    ToolSearchResultTool,
    create_tool_search_document,
    create_tool_search_tool_definition,
    tokenize,
)


def create_tool_search_extension() -> ExtensionFactory:
    """返回 ``tool_search`` 的扩展工厂。

    SDK 用法::

        session = create_agent_session(..., extension_factories=[create_tool_search_extension()])
    """

    def factory(pi: ExtensionAPI) -> None:
        pi.register_tool(
            create_tool_search_tool_definition(pi),
            default_active=False,
        )

    return factory


__all__ = [
    "DEFAULT_TOOL_SEARCH_LIMIT",
    "TOOL_SEARCH_DESCRIPTION",
    "TOOL_SEARCH_TOOL_NAME",
    "Bm25Ranker",
    "ToolSearchDocument",
    "ToolSearchMatch",
    "ToolSearchResultTool",
    "create_tool_search_document",
    "create_tool_search_extension",
    "create_tool_search_tool_definition",
    "tokenize",
]
