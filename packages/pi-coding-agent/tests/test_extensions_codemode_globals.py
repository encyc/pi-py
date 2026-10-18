"""Tier-B④-2b-2（前半）：`createDiscoveryGlobals` —— `searchTools` / `describeTool` / `describeNamespace`。

对应上游 `execute.ts:591-657`。三个全局都声明 `spread: true`
（脚本传的是**实参列表**，见 `prelude.js:155` 的 `spread ? args : args[0]`）。
"""

from __future__ import annotations

from typing import Any

import pytest

from pi_coding_agent.extensions.codemode.execute import (
    create_discovery_globals,
    is_namespace_name,
)
from pi_coding_agent.extensions.codemode.tool import CodemodeToolOptions
from pi_coding_agent.extensions.types import ToolDefinition, ToolNamespace


class _Tool(ToolDefinition):
    def __init__(self, name: str, description: str = "does a thing") -> None:
        self.name = name
        self.description = description
        self.parameters: dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - 不执行
        raise AssertionError("not executed")


def _tools() -> list[_Tool]:
    return [_Tool("mcp__docs__search", "search the docs"), _Tool("read", "read a file")]


def _namespaces() -> dict[str, ToolNamespace]:
    return {
        "mcp__docs__search": ToolNamespace(
            name="mcp__docs",
            description="Docs server",
            instructions="Use it well.",
            aliases=["docs"],
        )
    }


def _globals() -> list[Any]:
    samples = {"mcp__docs__search": "SAMPLE-search", "read": "SAMPLE-read"}
    return create_discovery_globals(
        _tools(), samples, CodemodeToolOptions(get_tool_namespace=_namespaces().get)
    )


def test_is_namespace_name_matches_by_name_alias_and_script_identifier():
    namespace = ToolNamespace(name="mcp__docs", aliases=["docs"])
    assert is_namespace_name(namespace, "mcp__docs")
    assert is_namespace_name(namespace, "docs")  # 别名
    assert is_namespace_name(ToolNamespace(name="dev-radius"), "dev_radius")  # 标识符等价
    assert not is_namespace_name(namespace, "other")


def test_all_three_globals_are_spread():
    """**关键**：脚本按上游文档写 `searchTools(q, opts)`，靠 spread 才能拿到全部实参。"""
    assert [tool.name for tool in _globals()] == [
        "searchTools",
        "describeTool",
        "describeNamespace",
    ]
    assert [tool.spread for tool in _globals()] == [True, True, True]


def test_search_tools_ranks_and_returns_identifier_and_description():
    search = _globals()[0].execute
    results = search(["docs"])
    assert [entry["name"] for entry in results] == ["mcp__docs__search"]
    assert results[0]["description"] == "SAMPLE-search"


def test_search_tools_honours_the_limit():
    search = _globals()[0].execute
    assert len(search(["read"])) == 1
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        search(["read", {"limit": 0}])
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        search(["read", {"limit": True}])  # bool 不是整数


def test_search_tools_filters_by_namespace():
    search = _globals()[0].execute
    # 查询词要先命中（BM25 只返回命中的文档），再谈 namespace 过滤。
    assert [entry["name"] for entry in search(["search", {"namespace": "mcp__docs"}])] == [
        "mcp__docs__search"
    ]
    assert search(["search", {"namespace": "nope"}]) == []
    # 命中但被 namespace 排除：`read` 在别处
    assert search(["read", {"namespace": "mcp__docs"}]) == []
    with pytest.raises(ValueError, match="namespace must be a string"):
        search(["search", {"namespace": 7}])


def test_search_tools_rejects_a_non_string_query():
    with pytest.raises(ValueError, match="expects a query string"):
        _globals()[0].execute([7])


def test_describe_tool_accepts_raw_name_or_identifier():
    describe = _globals()[1].execute
    assert describe(["read"]) == "SAMPLE-read"
    assert describe(["mcp__docs__search"]) == "SAMPLE-search"
    assert describe(["nope"]) is None
    with pytest.raises(ValueError, match="expects a tool name"):
        describe([7])


def test_describe_namespace_returns_name_description_instructions_and_tools():
    described = _globals()[2].execute(["docs"])  # 用别名查
    assert described == {
        "name": "mcp__docs",
        "description": "Docs server",
        "instructions": "Use it well.",
        "tools": ["mcp__docs__search"],
    }
    assert _globals()[2].execute(["nope"]) is None
    with pytest.raises(ValueError, match="expects a namespace name"):
        _globals()[2].execute([7])


def test_describe_namespace_omits_absent_optional_fields():
    """上游用条件展开 —— 字段缺席时**不写键**，而不是写成空串。"""
    samples = {"plain": "S"}
    tools = [_Tool("plain")]
    options = CodemodeToolOptions(get_tool_namespace=lambda _name: ToolNamespace(name="bare"))
    described = create_discovery_globals(tools, samples, options)[2].execute(["bare"])
    assert described == {"name": "bare", "tools": ["plain"]}
    assert "description" not in described
    assert "instructions" not in described
