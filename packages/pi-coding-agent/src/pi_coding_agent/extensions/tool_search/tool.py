"""``tool_search`` 工具：对工具元数据做 BM25 检索。

对应上游 ``src/extensions/tool-search/tool.ts``（244 行）。

`tool_search` 检索**没有声明给模型**的工具（``codemode`` 与 ``deferred`` exposure），
并把命中项加入活跃集合，使其在下一次模型调用时被声明。装载走的是活跃工具集合，
因此它会像其它工具变更一样被记录、在 ``/tree`` / resume / fork 后按分支存活。
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pi_agent_core.types import AgentToolResult
from pi_ai import TextContent

from ..types import (
    ToolDefinition,
    ToolExposure,
    ToolInfo,
    ToolNamespace,
)

__all__ = [
    "DEFAULT_TOOL_SEARCH_LIMIT",
    "TOOL_SEARCH_DESCRIPTION",
    "TOOL_SEARCH_TOOL_NAME",
    "Bm25Ranker",
    "ToolSearchDocument",
    "ToolSearchMatch",
    "ToolSearchResultTool",
    "create_tool_search_document",
    "create_tool_search_tool_definition",
    "tokenize",
]

TOOL_SEARCH_TOOL_NAME = "tool_search"
DEFAULT_TOOL_SEARCH_LIMIT = 8

#: 对齐上游 ``STOP_WORDS``。
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    }
)

_CAMEL_LOWER_UPPER = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_PLURAL_IES = re.compile(r"ches|shes|sses|xes|zes$")


def _stem(term: str) -> str:
    """朴素单数化：``issues`` → ``issue``，``searches`` → ``search``（对齐上游 ``stem``）。"""
    if len(term) > 4 and term.endswith("ies"):
        return f"{term[:-3]}y"
    if len(term) > 4 and _PLURAL_IES.search(term):
        return term[:-2]
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def tokenize(text: str) -> list[str]:
    """小写化、按 camelCase 边界与非字母数字切分、去停用词、再做词干化。"""
    spaced = _CAMEL_LOWER_UPPER.sub(r"\1 \2", text)
    spaced = _CAMEL_ACRONYM.sub(r"\1 \2", spaced)
    return [
        _stem(term) for term in _NON_ALNUM.split(spaced.lower()) if term and term not in _STOP_WORDS
    ]


def _is_object(value: Any) -> bool:
    return isinstance(value, dict)


def _schema_text(schema: Any, parts: list[str]) -> None:
    """递归收集 schema 的 description 与属性名（对齐上游 ``schemaText``）。"""
    if not _is_object(schema):
        return
    description = schema.get("description")
    if isinstance(description, str):
        parts.append(description)
    properties = schema.get("properties")
    if _is_object(properties):
        for name, prop in properties.items():
            parts.append(name)
            _schema_text(prop, parts)
    _schema_text(schema.get("items"), parts)
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                _schema_text(variant, parts)


@dataclass
class ToolSearchDocument:
    """检索器看到的工具：名字 + 由 :func:`create_tool_search_document` 构造的文本。"""

    name: str
    text: str


@dataclass
class ToolSearchMatch:
    name: str
    score: float


class ToolRanker(Protocol):
    """给工具排序。今天是 BM25；将来可换成带 embedding 的混合排序器。"""

    def rank(
        self, query: str, documents: Sequence[ToolSearchDocument], limit: int
    ) -> list[ToolSearchMatch]: ...


def create_tool_search_document(
    tool: ToolInfo, namespace: ToolNamespace | None = None
) -> ToolSearchDocument:
    """工具的检索文本：名字、下划线换空格的名字、描述、schema 描述与属性名、namespace。"""
    parts = [tool.name, tool.name.replace("_", " "), tool.description]
    _schema_text(tool.parameters, parts)
    if namespace is not None:
        parts.append(namespace.name)
        parts.append(namespace.description or "")
        parts.append(namespace.instructions or "")
    return ToolSearchDocument(name=tool.name, text=" ".join(part for part in parts if part.strip()))


@dataclass
class Bm25Ranker:
    """标准 Okapi BM25；同分时保持文档顺序。"""

    k1: float = 1.2
    b: float = 0.75

    def rank(
        self, query: str, documents: Sequence[ToolSearchDocument], limit: int
    ) -> list[ToolSearchMatch]:
        query_terms = list(dict.fromkeys(tokenize(query)))
        if not query_terms or not documents or limit <= 0:
            return []

        term_counts: list[dict[str, int]] = []
        for document in documents:
            counts: dict[str, int] = {}
            for term in tokenize(document.text):
                counts[term] = counts.get(term, 0) + 1
            term_counts.append(counts)

        lengths = [sum(counts.values()) for counts in term_counts]
        average_length = (sum(lengths) / len(documents)) or 1.0
        idf = {
            term: math.log(
                1
                + (len(documents) - sum(1 for counts in term_counts if term in counts) + 0.5)
                / (sum(1 for counts in term_counts if term in counts) + 0.5)
            )
            for term in query_terms
        }

        matches: list[ToolSearchMatch] = []
        for index, document in enumerate(documents):
            score = 0.0
            for term in query_terms:
                count = term_counts[index].get(term)
                if not count:
                    continue
                norm = self.k1 * (1 - self.b + (self.b * lengths[index]) / average_length)
                score += idf[term] * ((count * (self.k1 + 1)) / (count + norm))
            if score > 0:
                matches.append(ToolSearchMatch(name=document.name, score=score))

        matches.sort(key=lambda match: -match.score)
        return matches[:limit]


@dataclass
class ToolSearchResultTool:
    name: str
    description: str


def _is_searchable(exposure: ToolExposure) -> bool:
    return exposure in ("codemode", "deferred")


def search_and_load(
    tools: Any,
    query: str,
    limit: int,
    ranker: ToolRanker | None = None,
) -> list[ToolSearchResultTool]:
    """对尚未活跃的可检索工具排序，并把命中项加入活跃集合。

    活跃集合的变化会被记录进转写，与其它工具变更一致。
    """
    active = tools.get_active_tools()
    candidates = [
        tool
        for tool in tools.get_all_tools()
        if _is_searchable(tool.exposure) and tool.name not in active
    ]
    documents = [create_tool_search_document(tool, tool.namespace) for tool in candidates]
    matches = (ranker or Bm25Ranker()).rank(query, documents, limit)
    if matches:
        tools.set_active_tools([*active, *(match.name for match in matches)])
    by_name = {tool.name: tool for tool in candidates}
    return [
        ToolSearchResultTool(
            name=match.name,
            description=(by_name[match.name].description if match.name in by_name else ""),
        )
        for match in matches
    ]


#: ``tool_search`` 的描述：不列出可检索工具及其 namespace，因此在工具注册变化时保持稳定。
TOOL_SEARCH_DESCRIPTION = (
    "# Tool discovery\n\n"
    "Searches over deferred tool metadata with BM25 and exposes matching tools for the next "
    "model call.\n\n"
    "Some of the tools, such as tools of MCP servers, may not have been provided to you upfront, "
    f"and you should use this tool (`{TOOL_SEARCH_TOOL_NAME}`) to search for the required tools. "
    f"For MCP tool discovery, always use `{TOOL_SEARCH_TOOL_NAME}`."
)


def create_tool_search_tool_definition(
    tools: Any | None = None, ranker: ToolRanker | None = None
) -> ToolDefinition:
    """构造 ``tool_search`` 的定义。

    ``tools`` 需要满足 :class:`~pi_coding_agent.extensions.types.ExtensionAPI` 的
    ``get_all_tools`` / ``get_active_tools`` / ``set_active_tools``（``ExtensionAPI`` 本身即可）。
    不传则检索不到任何东西（对齐上游行为）。
    """

    class _ToolSearchTool(ToolDefinition):
        async def execute(
            self,
            tool_call_id: str,
            params: dict[str, Any],
            cancel_event: Any = None,
            on_update: Any = None,
            ctx: Any = None,
        ) -> AgentToolResult:
            query = params["query"]
            if str(query).strip() == "":
                raise ValueError("query must not be empty")
            limit = params.get("limit", DEFAULT_TOOL_SEARCH_LIMIT)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                raise ValueError("limit must be a positive integer")
            loaded = search_and_load(tools, query, limit, ranker) if tools is not None else []
            if not loaded:
                text = "No matching tools found."
            else:
                plural = "" if len(loaded) == 1 else "s"
                lines = "\n".join(
                    f"- {tool.name}: {tool.description.strip().splitlines()[0] if tool.description.strip() else ''}"
                    for tool in loaded
                )
                text = f"Loaded {len(loaded)} tool{plural}. They are available from your next call:\n{lines}"
            return AgentToolResult(
                content=[TextContent(text=text)],
                details={"loaded": [tool.name for tool in loaded]},
            )

    return _ToolSearchTool(
        name=TOOL_SEARCH_TOOL_NAME,
        label=TOOL_SEARCH_TOOL_NAME,
        description=TOOL_SEARCH_DESCRIPTION,
        prompt_snippet="Search for tools that are not loaded yet and load the matches",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for deferred tools."},
                "limit": {
                    "type": "number",
                    "description": (
                        "Maximum number of tools to return. "
                        f"Defaults to {DEFAULT_TOOL_SEARCH_LIMIT}."
                    ),
                },
            },
            "required": ["query"],
        },
        # 检索不是脚本需要的能力；它改变的是模型看到什么。
        exposure="model-only",
        # 一次查询可能命中任何尚未声明的工具。
        reaches_tools=lambda _input: "all",
    )
