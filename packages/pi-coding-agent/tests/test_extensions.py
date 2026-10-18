"""内置扩展（SDK 面）测试：扩展声明面 + ToolRegistry 语义 + tool_search。"""

from __future__ import annotations

import pytest

from pi_coding_agent import create_tool_search_extension
from pi_coding_agent.extensions import (
    ExtensionAPI,
    ToolDefinition,
    ToolNamespace,
    ToolRegistry,
    to_agent_tool,
)
from pi_coding_agent.extensions.tool_search import (
    DEFAULT_TOOL_SEARCH_LIMIT,
    TOOL_SEARCH_TOOL_NAME,
    Bm25Ranker,
    ToolSearchDocument,
    create_tool_search_document,
    create_tool_search_tool_definition,
    tokenize,
)

# ============================================================
# 包入口导出（验收标准）
# ============================================================


def test_create_tool_search_extension_importable_from_package_entry():
    import pi_coding_agent

    assert callable(pi_coding_agent.create_tool_search_extension)
    assert "create_tool_search_extension" in pi_coding_agent.__all__


def test_tool_registry_satisfies_extension_api_protocol():
    assert isinstance(ToolRegistry(), ExtensionAPI)


def test_tool_definition_is_abstract_no_callable_stub():
    """``ToolDefinition`` 是抽象类：不能构造出一个「调用时才炸」的桩。"""
    with pytest.raises(TypeError, match="abstract"):
        ToolDefinition(name="x", description="d", parameters={})  # type: ignore[abstract]


def test_no_placeholder_extension_factories_are_exported():
    """未移植的扩展 → 名字必须不存在，而不是导入成功却抛错。

    ``create_mcp_extension`` 已**真实落地**（C3b-2b-β），故从本守卫移出；
    ``create_codemode_extension`` 仍未移植（前置阻塞见 ``PORTING.md``），继续守着。
    """
    import pi_coding_agent

    # 两个内置扩展工厂**都已真实落地**（Tier-B③ mcp / Tier-B④ codemode），所以这里
    # 不再断言「不存在」，而是**加严**为「必须可导入且是工厂」——断言随事实收紧。
    for name in ("create_mcp_extension", "create_codemode_extension"):
        assert hasattr(pi_coding_agent, name), name
        assert name in pi_coding_agent.__all__, name
        assert callable(getattr(pi_coding_agent, name)()), name


# ============================================================
# ToolRegistry：注册 / exposure / 活跃集合
# ============================================================


class _StubTool(ToolDefinition):
    """只做注册表夹具用的具体工具（``ToolDefinition`` 是抽象类，必须给 ``execute``）。"""

    async def execute(self, tool_call_id, params, cancel_event=None, on_update=None, ctx=None):  # type: ignore[no-untyped-def]
        from pi_agent_core.types import AgentToolResult

        return AgentToolResult(content=[])


def _def(name: str, description: str | None = None, **kwargs: object) -> ToolDefinition:
    return _StubTool(  # type: ignore[arg-type]
        name=name, description=description or f"{name} tool", parameters={}, **kwargs
    )


def test_direct_and_model_only_activate_on_registration():
    reg = ToolRegistry()
    reg.register_tool(_def("read"))
    reg.register_tool(_def("planner", exposure="model-only"))
    assert reg.get_active_tools() == ["read", "planner"]


def test_codemode_deferred_hidden_do_not_activate_on_registration():
    reg = ToolRegistry()
    for exposure in ("codemode", "deferred", "hidden"):
        reg.register_tool(_def(f"t_{exposure}", exposure=exposure))  # type: ignore[arg-type]
    assert reg.get_active_tools() == []


def test_default_active_false_suppresses_activation():
    reg = ToolRegistry()
    reg.register_tool(_def("tool_search", exposure="model-only"), default_active=False)
    assert reg.get_active_tools() == []
    # 显式参数优先于 definition 上的字段
    reg2 = ToolRegistry()
    reg2.register_tool(_def("x", default_active=True), default_active=False)
    assert reg2.get_active_tools() == []


def test_set_active_tools_drops_hidden_and_unknown_and_dedupes():
    reg = ToolRegistry()
    reg.register_tool(_def("a"))
    reg.register_tool(_def("ghost", exposure="hidden"))
    reg.set_active_tools(["a", "a", "ghost", "nope"])
    assert reg.get_active_tools() == ["a"]


def test_hidden_tool_cannot_be_activated():
    reg = ToolRegistry()
    reg.register_tool(_def("secret", exposure="hidden"))
    reg.set_active_tools(["secret"])
    assert reg.get_active_tools() == []
    assert reg.is_declarable("secret") is False


def test_callable_set_matches_upstream_semantics():
    """可调用 = active 的 direct ∪ 全部 codemode ∪ 全部 deferred。"""
    reg = ToolRegistry()
    reg.register_tool(_def("read"))  # direct, active
    reg.register_tool(_def("planner", exposure="model-only"))  # 声明但不可调用
    reg.register_tool(_def("code_tool", exposure="codemode"))
    reg.register_tool(_def("deferred_tool", exposure="deferred"))
    reg.register_tool(_def("secret", exposure="hidden"))
    assert reg.get_callable_tool_names() == ["read", "code_tool", "deferred_tool"]


def test_inactive_direct_tool_is_not_callable():
    reg = ToolRegistry()
    reg.register_tool(_def("read"))
    reg.set_active_tools([])
    assert reg.get_callable_tool_names() == []


def test_unregister_removes_from_active():
    reg = ToolRegistry()
    reg.register_tool(_def("read"))
    assert reg.unregister_tool("read") is True
    assert reg.get_active_tools() == []
    assert reg.unregister_tool("read") is False


# ============================================================
# ToolRegistry：loadout
# ============================================================


def test_get_loadout_reports_exposure_namespace_and_guidelines():
    reg = ToolRegistry()
    ns = ToolNamespace(name="mcp__docs", description="Docs server")
    reg.register_tool(
        _def("read", prompt_guidelines=["g1", "g2"]),
    )
    reg.register_tool(_def("mcp__docs__x", exposure="deferred", namespace=ns))
    loadout = reg.get_loadout()
    assert list(loadout.declared) == ["read"]
    assert set(loadout.registered) == {"read", "mcp__docs__x"}
    assert loadout.get_exposure("mcp__docs__x") == "deferred"
    assert loadout.get_exposure("missing") == "direct"
    assert loadout.get_namespace("mcp__docs__x") is ns
    assert loadout.get_prompt_guidelines("read") == ["g1", "g2"]
    assert loadout.get_prompt_guidelines("mcp__docs__x") == ()
    assert "read" in loadout.callable


def test_custom_exposure_defaults_to_direct_when_absent():
    reg = ToolRegistry()
    reg.register_tool(_def("read"))
    assert reg.exposure_of("read") == "direct"
    assert reg.exposure_of("unknown") == "direct"


def test_prepare_loadout_collects_non_none_changes():
    from pi_coding_agent.extensions import ToolLoadoutChanges

    reg = ToolRegistry()
    reg.register_tool(
        _def(
            "orchestrator",
            prepare_loadout=lambda _loadout: ToolLoadoutChanges(descriptions={"a": "b"}),
        )
    )
    reg.register_tool(_def("plain", prepare_loadout=lambda _loadout: None))
    changes = reg.run_prepare_loadout()
    assert set(changes) == {"orchestrator"}
    assert changes["orchestrator"].descriptions == {"a": "b"}


# ============================================================
# to_agent_tool 适配
# ============================================================


async def test_to_agent_tool_exposes_agent_tool_surface():
    class _Echo(ToolDefinition):
        async def execute(self, tool_call_id, params, cancel_event=None, on_update=None, ctx=None):  # type: ignore[no-untyped-def]
            from pi_agent_core.types import AgentToolResult
            from pi_ai import TextContent

            return AgentToolResult(content=[TextContent(text=f"echo:{params['x']}")])

    definition = _Echo(name="echo", description="d", parameters={"type": "object"})
    tool = to_agent_tool(definition)
    assert tool.name == "echo"
    assert tool.description == "d"
    assert tool.parameters == {"type": "object"}
    result = await tool.execute("id", {"x": "hi"})
    assert result.content[0].text == "echo:hi"


# ============================================================
# tool_search：分词与 BM25
# ============================================================


def test_tokenize_splits_camel_case_and_drops_stop_words():
    assert tokenize("SearchIssues for the repo") == ["search", "issue", "repo"]


def test_tokenize_stems_plurals():
    assert tokenize("issues") == ["issue"]
    assert tokenize("searches") == ["search"]
    # ss 结尾不改（如 class / address）
    assert tokenize("class") == ["class"]


def test_bm25_ranks_matching_document_first():
    documents = [
        ToolSearchDocument(name="read", text="read the contents of a file"),
        ToolSearchDocument(name="issue_search", text="search github issues in a repository"),
    ]
    matches = Bm25Ranker().rank("search issues", documents, 5)
    assert [m.name for m in matches] == ["issue_search"]


def test_bm25_returns_empty_for_empty_query_or_no_match():
    documents = [ToolSearchDocument(name="read", text="read a file")]
    assert Bm25Ranker().rank("", documents, 5) == []
    assert Bm25Ranker().rank("   ", documents, 5) == []
    assert Bm25Ranker().rank("kubernetes", documents, 5) == []
    assert Bm25Ranker().rank("read", [], 5) == []
    assert Bm25Ranker().rank("read", documents, 0) == []


def test_bm25_respects_limit_and_keeps_document_order_on_ties():
    documents = [
        ToolSearchDocument(name="a", text="alpha"),
        ToolSearchDocument(name="b", text="alpha"),
        ToolSearchDocument(name="c", text="alpha"),
    ]
    matches = Bm25Ranker().rank("alpha", documents, 2)
    assert [m.name for m in matches] == ["a", "b"]


# ============================================================
# tool_search：检索文档
# ============================================================


def test_create_tool_search_document_includes_schema_and_namespace():
    from pi_coding_agent.extensions import ToolInfo

    tool = ToolInfo(
        name="read_file",
        description="Read a file",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path"}},
        },
        exposure="direct",
    )
    document = create_tool_search_document(tool, ToolNamespace(name="fs", description="Filesystem"))
    assert document.name == "read_file"
    for fragment in (
        "read_file",
        "read file",
        "Read a file",
        "File path",
        "path",
        "fs",
        "Filesystem",
    ):
        assert fragment in document.text, fragment


# ============================================================
# tool_search：定义与执行
# ============================================================


def test_tool_search_definition_shape():
    definition = create_tool_search_tool_definition()
    assert definition.name == TOOL_SEARCH_TOOL_NAME
    assert definition.exposure == "model-only"
    assert definition.prompt_snippet
    assert definition.parameters["required"] == ["query"]
    assert definition.reaches_tools is not None
    assert definition.reaches_tools({}) == "all"


async def test_tool_search_without_tools_finds_nothing():
    definition = create_tool_search_tool_definition()
    result = await definition.execute("id", {"query": "anything"})
    assert result.content[0].text == "No matching tools found."
    assert result.details["loaded"] == []


async def test_tool_search_rejects_empty_query_and_bad_limit():
    definition = create_tool_search_tool_definition(ToolRegistry())
    with pytest.raises(ValueError, match="query must not be empty"):
        await definition.execute("id", {"query": "   "})
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        await definition.execute("id", {"query": "x", "limit": 0})
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        await definition.execute("id", {"query": "x", "limit": True})


async def test_tool_search_loads_matches_into_active_set():
    reg = ToolRegistry()
    reg.register_tool(
        _def(
            "mcp__docs__search_issues",
            description="Search GitHub issues in a repository",
            exposure="deferred",
        )
    )
    reg.register_tool(_def("read"))
    create_tool_search_extension()(reg)

    # tool_search 以未激活状态注册
    assert reg.get_active_tools() == ["read"]
    assert reg.exposure_of(TOOL_SEARCH_TOOL_NAME) == "model-only"

    tool = reg.get_tool_definition(TOOL_SEARCH_TOOL_NAME)
    assert tool is not None
    result = await tool.execute("id", {"query": "search issues"})
    assert result.details["loaded"] == ["mcp__docs__search_issues"]
    assert "Search GitHub issues" in result.content[0].text
    # 命中项被加入活跃集合，下一次模型调用即可见
    assert reg.get_active_tools() == ["read", "mcp__docs__search_issues"]


async def test_tool_search_does_not_reload_already_active_tools():
    reg = ToolRegistry()
    reg.register_tool(_def("deferred_tool", exposure="deferred"))
    reg.set_active_tools(["deferred_tool"])
    tool = create_tool_search_tool_definition(reg)
    result = await tool.execute("id", {"query": "deferred"})
    assert result.details["loaded"] == []


async def test_tool_search_uses_default_limit():
    reg = ToolRegistry()
    for index in range(DEFAULT_TOOL_SEARCH_LIMIT + 4):
        reg.register_tool(_def(f"bulk_tool_{index}", exposure="deferred"))
    tool = create_tool_search_tool_definition(reg)
    result = await tool.execute("id", {"query": "bulk tool"})
    assert len(result.details["loaded"]) == DEFAULT_TOOL_SEARCH_LIMIT
