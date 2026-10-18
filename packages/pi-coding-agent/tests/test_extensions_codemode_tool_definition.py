"""Tier-B④-1b-2：`create_codemode_tool_definition` / `create_codemode_tool`（上游 `tool.ts:371-410`）。

形状照 **MCP 同目录先例**（函数内局部 `ToolDefinition` 子类）。
**不包层**：上游 `createCodemodeTool` 里那次 `wrapToolDefinition` 已判**不做**（纯转换）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from pi_coding_agent.extensions.codemode.tool import (
    CODEMODE_SCHEMA,
    CODEMODE_TOOL_NAME,
    create_codemode_tool,
    create_codemode_tool_definition,
)
from pi_coding_agent.extensions.types import ToolDefinition


class _Tool(ToolDefinition):
    def __init__(self, name: str, description: str = "does a thing") -> None:
        self.name = name
        self.description = description
        self.parameters: dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("not executed")


def test_definition_identity_and_exposure():
    definition = create_codemode_tool_definition()
    assert definition.name == CODEMODE_TOOL_NAME == "codemode"
    assert definition.label == "codemode"
    # 脚本不能启动别的脚本 → 只向模型曝光
    assert definition.exposure == "model-only"
    assert definition.parameters is CODEMODE_SCHEMA
    assert definition.parameters["required"] == ["code"]


def test_definition_carries_prompt_metadata_and_the_grammar():
    definition = create_codemode_tool_definition()
    assert definition.prompt_snippet
    assert definition.prompt_guidelines
    assert definition.constrained_sampling["type"] == "grammar"
    assert "openai_lark" in definition.constrained_sampling["variants"]


def test_reaches_tools_comes_from_the_script_source():
    """`reachesTools` 决定工具调用前要等哪些 server —— 用的是脚本**源码**。"""
    definition = create_codemode_tool_definition()
    assert definition.reaches_tools({"code": "return searchTools('x')"}) == "all"
    # 上游是 `code.match(/\w+/g)` —— 源码里**所有**词，`reachesServer` 之后按 server 名筛。
    assert definition.reaches_tools({"code": "return tools.read()"}) == ["return", "tools", "read"]
    # `tools.mcp__docs__search` 与 `tools["mcp__docs__" + name]` 都会贡献出
    # `mcp__docs__search` / `mcp__docs__` 这类词 —— 由 `reaches_server` 再决定匹配。
    assert "mcp__docs__search" in definition.reaches_tools({"code": "tools.mcp__docs__search({})"})
    assert "mcp__docs__" in definition.reaches_tools({"code": "tools['mcp__docs__' + name]({})"})
    assert definition.reaches_tools({}) == []  # 没有 code


def test_prepare_loadout_is_wired_to_the_options():
    from pi_coding_agent.extensions.types import ToolLoadout

    definition = create_codemode_tool_definition()
    changes = definition.prepare_loadout(ToolLoadout(declared=[], callable=[], registered=[]))
    assert CODEMODE_TOOL_NAME in (changes.descriptions or {})


def test_execute_routes_into_execute_codemode(monkeypatch):
    """`execute` 把入参原样交给 `execute_codemode`（含 ctx 与 options）。"""
    from pi_coding_agent.extensions.codemode import execute as mod

    seen: dict[str, Any] = {}

    async def fake(
        tool_call_id: str, params: Any, signal: Any, on_update: Any, ctx: Any, options: Any
    ) -> Any:
        seen.update(id=tool_call_id, params=params, ctx=ctx, options=options)
        return {"ok": True}

    monkeypatch.setattr(mod, "execute_codemode", fake)
    definition = create_codemode_tool_definition()
    result = asyncio.run(definition.execute("call-1", {"code": "return 1"}, None, None, "CTX"))
    assert result == {"ok": True}
    assert seen["id"] == "call-1"
    assert seen["ctx"] == "CTX"
    assert seen["params"] == {"code": "return 1"}


def test_create_codemode_tool_swaps_the_description_without_wrapping():
    """**不包层**：仍是同一个 `ToolDefinition`，只是 description 换成带声明的版本。"""
    tool = create_codemode_tool([_Tool("read")])
    assert isinstance(tool, ToolDefinition)
    assert "### `read`" in tool.description
    # 其它元数据不受影响
    assert tool.name == "codemode"
    assert tool.exposure == "model-only"
    # 空列表时退回「没有嵌套工具」的版本
    empty = create_codemode_tool([])
    assert "### `read`" not in empty.description


def test_create_codemode_tool_keeps_the_definition_usable():
    tool = create_codemode_tool([_Tool("read")])
    assert tool.reaches_tools({"code": "return 1"}) == ["return", "1"]
    assert callable(tool.execute)
