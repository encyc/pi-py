"""``tool_search`` 工具 —— 移植 ``durable/src/tool-search/tool-search.ts``（92 行）。

模型用它检索「尚未提供给自己」的延迟工具，命中的工具通过 ``control.addTools`` 加载，
下一次请求就会提供。加载会写进会话的 ``modelTools``（它的 ``add``），所以加载过的工具在
该分支上持续可见，压缩之后也是。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from pi_ai import TextContent
from pi_chord.context import Context
from pi_durable.harness import (
    ToolControl,
    ToolExecutionApi,
    ToolExecutionResult,
    ToolRegistration,
    define_tool,
)
from pi_durable.search import SearchDocument, rank_documents
from pi_durable.tools.args import json_schema_of

__all__ = [
    "DEFAULT_TOOL_SEARCH_LIMIT",
    "TOOL_SEARCH_TOOL_NAME",
    "ToolSearchToolDetails",
    "ToolSearchToolInput",
    "create_tool_search_tool",
]

TOOL_SEARCH_TOOL_NAME = "tool_search"
DEFAULT_TOOL_SEARCH_LIMIT = 8

#: 固定文案，这样在延迟工具注册过程中（例如 MCP server 陆续连上）它不会变
#: （``tool-search.ts:14``）。
TOOL_SEARCH_DESCRIPTION = (
    "# Tool discovery\n\n"
    "Searches over deferred tool metadata with BM25 and exposes matching tools for the next model "
    "call.\n\n"
    "Some of the tools, such as tools of MCP servers, may not have been provided to you upfront, "
    f"and you should use this tool (`{TOOL_SEARCH_TOOL_NAME}`) to search for the required tools. "
    f"For MCP tool discovery, always use `{TOOL_SEARCH_TOOL_NAME}`."
)


class ToolSearchToolInput(BaseModel):
    """``tool-search.ts:17-22`` 的 ``toolSearchSchema``。"""

    query: str = Field(description="Search query for deferred tools.")
    limit: int | None = Field(
        default=None,
        description=f"Maximum number of tools to return. Defaults to {DEFAULT_TOOL_SEARCH_LIMIT}",
    )


TOOL_SEARCH_SCHEMA: dict[str, Any] = json_schema_of(ToolSearchToolInput)


class ToolSearchToolDetails(BaseModel):
    """``tool-search.ts:25``：加载了哪些工具。"""

    loaded: list[str]


def _schema_text(schema: Any, parts: list[str]) -> None:
    """递归收集 schema 里的描述与属性名（``tool-search.ts:32-46``）。"""
    if not isinstance(schema, dict):
        return
    description = schema.get("description")
    if isinstance(description, str):
        parts.append(description)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, prop in properties.items():
            parts.append(name)
            _schema_text(prop, parts)
    _schema_text(schema.get("items"), parts)
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                _schema_text(variant, parts)


def _search_text(tool: ToolRegistration[Any]) -> str:
    """一个工具的可检索文本：名字、名字里的 ``_`` 换成空格、描述、参数文本
    （``tool-search.ts:49-53``）。"""
    parts = [tool.name, tool.name.replace("_", " "), tool.description]
    _schema_text(dict(tool.parameters), parts)
    return " ".join(part for part in parts if part.strip() != "")


async def _execute(
    args: ToolSearchToolInput,
    api: ToolExecutionApi,
    context: Context,
) -> ToolExecutionResult[Any]:
    """``tool-search.ts:63-84``。"""
    if args.query.strip() == "":
        raise RuntimeError("query must not be empty")
    limit = DEFAULT_TOOL_SEARCH_LIMIT if args.limit is None else args.limit
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise RuntimeError("limit must be a positive integer")

    candidates = (await api.agent(context)).deferred
    documents = [SearchDocument(name=tool.name, text=_search_text(tool)) for tool in candidates]
    loaded = rank_documents(args.query, documents, limit)

    if not loaded:
        text = "No matching tools found."
    else:
        lines = []
        for name in loaded:
            description = next((t.description for t in candidates if t.name == name), "")
            first_line = description.strip().split("\n")[0] if description.strip() else ""
            lines.append(f"- {name}: {first_line}")
        plural = "" if len(loaded) == 1 else "s"
        text = (
            f"Loaded {len(loaded)} tool{plural}. They are available from your next call:\n"
            + "\n".join(lines)
        )

    return ToolExecutionResult(
        output=[TextContent(text=text)],
        details=ToolSearchToolDetails(loaded=loaded),
        control=None if not loaded else ToolControl(add_tools=loaded),
    )


def create_tool_search_tool() -> ToolRegistration[ToolSearchToolInput]:
    """``createToolSearchTool()``（``tool-search.ts:55-87``）。

    ``callers=["model"]``：脚本有自己的 ``searchTools()``，不该再走这个工具
    （``tool-search.ts:60-61``）。``replay="safe"``：重放安全。
    """
    return define_tool(
        name=TOOL_SEARCH_TOOL_NAME,
        description=TOOL_SEARCH_DESCRIPTION,
        parameters=TOOL_SEARCH_SCHEMA,
        callers=["model"],
        replay="safe",
        execute=_execute,
    )
