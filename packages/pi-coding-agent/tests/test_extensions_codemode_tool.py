"""Tier-B④-1a 测试：``codemode/tool.ts`` 的纯函数（声明渲染 / 描述生成 / 目录预算）。

用假 ``ToolDefinition``，不起沙箱、不跑脚本。
"""

from __future__ import annotations

import os
from typing import Any

from pi_coding_agent.extensions.codemode.tool import (
    CHARS_PER_TOKEN,
    CODEMODE_DOCS_PATH,
    CODEMODE_SCHEMA,
    CODEMODE_STORE_ENTRY_TYPE,
    CODEMODE_TOOL_NAME,
    DEFAULT_CODEMODE_INLINE_BUDGET,
    TEXT_OUTPUT_SCHEMA,
    CodemodeDescriptionOptions,
    CodemodeToolOptions,
    create_codemode_description,
    describe_globals,
    describe_script_call,
    get_codemode_callable_tools,
    get_script_reach,
    prepare_codemode_loadout,
    to_codemode_declaration,
)
from pi_coding_agent.extensions.types import ToolDefinition, ToolNamespace


class _Tool(ToolDefinition):
    def __init__(
        self,
        name: str,
        *,
        description: str = "does a thing",
        parameters: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}
        self.output_schema = output_schema

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - 纯函数用不到
        raise AssertionError("not executed")


# ============================================================
# 常量
# ============================================================


def test_constants_match_upstream():
    assert CODEMODE_TOOL_NAME == "codemode"
    assert CODEMODE_STORE_ENTRY_TYPE == "codemode-store"
    assert TEXT_OUTPUT_SCHEMA == {"type": "string"}
    assert DEFAULT_CODEMODE_INLINE_BUDGET == 3000
    assert CHARS_PER_TOKEN == 4


def test_codemode_schema_is_the_code_input():
    """上游 typebox ``Type.Object({ code: Type.String({...}) })`` —— 含 ``required``。"""
    assert CODEMODE_SCHEMA["type"] == "object"
    assert CODEMODE_SCHEMA["required"] == ["code"]
    assert CODEMODE_SCHEMA["properties"]["code"]["type"] == "string"


def test_docs_path_points_at_a_real_non_empty_file():
    """**缺口一 (a) 的守护**：``models`` 那行指向的参考手册必须真的存在且非空。

    这条防的是「悄悄退回 (b)」——只给常量、文档不在。
    """
    assert os.path.isabs(CODEMODE_DOCS_PATH)
    assert CODEMODE_DOCS_PATH.endswith(os.path.join("docs", "codemode.md"))
    assert os.path.isfile(CODEMODE_DOCS_PATH), CODEMODE_DOCS_PATH
    assert os.path.getsize(CODEMODE_DOCS_PATH) > 5_000


def test_describe_globals_mentions_the_docs_path_only_with_models():
    without = describe_globals(False)
    with_models = describe_globals(True)
    assert CODEMODE_DOCS_PATH not in without
    assert CODEMODE_DOCS_PATH in with_models
    assert "`models`: classifiers and image generation" in with_models
    # 三个与模型无关的全局行始终在
    for topic in ("text(value)", "store(key, value)", "ALL_TOOLS"):
        assert topic in without


# ============================================================
# 声明
# ============================================================


def test_to_codemode_declaration_shape_and_defaults():
    declaration = to_codemode_declaration(_Tool("read"))
    assert declaration["name"] == "read"
    assert declaration["description"] == "does a thing"
    assert declaration["input_schema"] == {"type": "object", "properties": {}}
    # 没有 output schema 的工具解析成文本
    assert declaration["output_schema"] == TEXT_OUTPUT_SCHEMA


def test_to_codemode_declaration_appends_guidelines_as_bullets():
    declaration = to_codemode_declaration(_Tool("read"), ["Use it.", "  Also this.  "])
    assert declaration["description"] == "does a thing\n\n- Use it.\n- Also this."


def test_to_codemode_declaration_ignores_blank_guidelines():
    """上游 ``guideline.trim()`` —— 空白 guideline 不产生空 bullet。"""
    declaration = to_codemode_declaration(_Tool("read"), ["", "   ", "\n"])
    assert declaration["description"] == "does a thing"


def test_get_codemode_callable_tools_excludes_codemode_itself():
    tools = [_Tool("read"), _Tool(CODEMODE_TOOL_NAME), _Tool("bash")]
    assert [tool.name for tool in get_codemode_callable_tools(tools)] == ["read", "bash"]


# ============================================================
# 描述生成
# ============================================================


def test_description_without_tools_is_intro_plus_globals():
    text = create_codemode_description([])
    assert text.startswith("Run JavaScript that calls other tools.")
    assert "Nested tools:" not in text


def test_description_lists_tools_and_groups_namespaces():
    tools = [_Tool("read"), _Tool("search"), _Tool("mcp__docs__get")]
    namespaces = {"mcp__docs__get": ToolNamespace(name="mcp__docs", description="Docs server")}
    text = create_codemode_description(tools, CodemodeDescriptionOptions(namespaces=namespaces))
    assert "Nested tools:" in text
    # 无 namespace 的工具排在前面
    assert text.index("### `read`") < text.index("## mcp__docs")
    assert "## mcp__docs\nDocs server" in text
    assert "### `mcp__docs__get`" in text


def test_deferred_tools_are_never_listed_and_do_not_change_the_description():
    """``deferred`` 工具不列出、也不影响描述 —— 晚注册不改变它（上游注释的用意）。"""
    plain = create_codemode_description([_Tool("read")])
    with_deferred = create_codemode_description(
        [_Tool("read"), _Tool("late")],
        CodemodeDescriptionOptions(deferred=frozenset({"late"})),
    )
    assert with_deferred == plain


def test_inline_budget_omits_tools_that_do_not_fit():
    """预算是**硬**上限：放不下的工具一个都不列，不受「至少列一个」的兜底影响。"""
    tools = [_Tool(f"tool{i}", description="x" * 400) for i in range(10)]
    # 单段成本 ≈ (400 + 声明) / 4 ≈ 130 token；给 400 只够三段左右。
    text = create_codemode_description(tools, CodemodeDescriptionOptions(inline_budget=400))
    listed = sum(1 for i in range(10) if f"### `tool{i}`" in text)
    assert 0 < listed < 10

    # 预算连最便宜的一段都放不下时，**什么也不列**（上游同样没有兜底）。
    none_fit = create_codemode_description(tools, CodemodeDescriptionOptions(inline_budget=10))
    assert all(f"### `tool{i}`" not in none_fit for i in range(10))
    assert "Nested tools:" in none_fit


def test_namespace_heading_reports_unlisted_tools():
    """放不进预算的 namespace 工具才有「(tools not listed)」后缀。"""
    namespace = ToolNamespace(name="big")
    fits = {"big": namespace, "big2": namespace}
    small = [_Tool("big__a", description="short")]
    text = create_codemode_description(
        small, CodemodeDescriptionOptions(namespaces=fits, inline_budget=10_000)
    )
    assert "(tools not listed)" not in text

    many = [_Tool(f"big__{i}", description="y" * 900) for i in range(10)]
    text2 = create_codemode_description(
        many, CodemodeDescriptionOptions(namespaces={"big__0": namespace}, inline_budget=120)
    )
    assert "## big (tools not listed)" in text2


# ============================================================
# 输出描述
# ============================================================


def test_describe_script_call_renders_string_and_object_outputs():
    plain = describe_script_call(_Tool("read"))
    assert "`tools.read(args)` resolves to a string" in plain

    structured = describe_script_call(
        _Tool(
            "bash",
            output_schema={
                "type": "object",
                "properties": {"output": {"type": "string"}, "exit_code": {"type": "number"}},
                "required": ["output", "exit_code"],
            },
        )
    )
    assert "resolves to `{ output, exit_code }`" in structured


def test_describe_script_call_marks_optional_fields():
    """非 required 字段带 ``?``（上游 ``describeFields``）。"""
    structured = describe_script_call(
        _Tool(
            "bash",
            output_schema={
                "type": "object",
                "properties": {
                    "output": {"type": "string"},
                    "full_output_path": {"type": "string"},
                },
                "required": ["output"],
            },
        )
    )
    assert "full_output_path?" in structured


def test_describe_script_call_uses_the_codemode_identifier():
    """名字里的字符被净化成合法 JS 标识符（``toCodemodeIdentifier``）。"""
    rendered = describe_script_call(_Tool("mcp__docs__get-page"))
    assert "tools." in rendered
    assert "`tools.mcp__docs__get" in rendered


# ============================================================
# ④-1b-1：loadout 与可达性
# ============================================================


def _loadout(
    *,
    declared: list[Any] | None = None,
    callable_: list[Any] | None = None,
    exposures: dict[str, str] | None = None,
    namespaces: dict[str, Any] | None = None,
    guidelines: dict[str, list[str]] | None = None,
) -> Any:
    from pi_coding_agent.extensions.types import ToolLoadout

    return ToolLoadout(
        declared=declared or [],
        callable=callable_ or [],
        registered=(declared or []) + (callable_ or []),
        exposures=exposures or {},
        namespaces=namespaces or {},
        prompt_guidelines=guidelines or {},
    )


def test_get_script_reach_all_on_discovery_globals():
    for source in (
        "searchTools('x')",
        "ALL_TOOLS",
        "await describeTool('a')",
        "describeNamespace('n')",
    ):
        assert get_script_reach(source) == "all", source


def test_get_script_reach_extracts_words():
    assert get_script_reach("tools.mcp__docs__search({a: 1})") == [
        "tools",
        "mcp__docs__search",
        "a",
        "1",
    ]
    assert get_script_reach(123) == []
    assert get_script_reach(None) == []


def test_get_script_reach_uses_ascii_word_semantics():
    """JS 的 ``\\w`` 只含 ``[A-Za-z0-9_]``；Python 默认是 Unicode 的，必须加 ``re.ASCII``。

    否则中文标识符会被算进可达集，得到与上游不同的结果 —— 这是**静默不一致**。
    """
    assert get_script_reach("工具") == []
    assert get_script_reach("工具 read") == ["read"]


def test_prepare_loadout_on_mode_describes_declared_callable_tools():
    read = _Tool("read")
    bash = _Tool("bash")
    loadout = _loadout(
        declared=[read], callable_=[read, bash], exposures={"read": "direct", "bash": "direct"}
    )
    changes = prepare_codemode_loadout(loadout)
    assert set(changes.descriptions or {}) == {"read", CODEMODE_TOOL_NAME}
    assert "Codemode: `tools.read(args)`" in (changes.descriptions or {})["read"]
    # on 模式不隐藏声明
    assert changes.hidden_declarations == []
    # codemode 自己不出现在可调用列表里
    assert "### `codemode`" not in (changes.descriptions or {})[CODEMODE_TOOL_NAME]


def test_prepare_loadout_only_mode_lists_everything_and_hides_direct_declarations():
    read = _Tool("read")
    bash = _Tool("bash")
    loadout = _loadout(
        declared=[read, bash],
        callable_=[read, bash],
        exposures={"read": "direct", "bash": "codemode"},
    )
    changes = prepare_codemode_loadout(loadout, CodemodeToolOptions(get_mode=lambda: "only"))
    # only：请求里略去活跃 direct 工具的声明
    assert changes.hidden_declarations == ["read"]
    description = (changes.descriptions or {})[CODEMODE_TOOL_NAME]
    assert "### `read`" in description and "### `bash`" in description
    # only 模式下不再覆盖已声明工具的描述
    assert set(changes.descriptions or {}) == {CODEMODE_TOOL_NAME}


def test_prepare_loadout_excludes_deferred_tools_from_the_listing():
    listed = _Tool("read")
    deferred = _Tool("late")
    loadout = _loadout(
        callable_=[listed, deferred], exposures={"read": "codemode", "late": "deferred"}
    )
    changes = prepare_codemode_loadout(loadout)
    description = (changes.descriptions or {})[CODEMODE_TOOL_NAME]
    assert "### `read`" in description
    assert "### `late`" not in description


def test_prepare_loadout_passes_namespaces_and_guidelines():
    tool = _Tool("mcp__docs__get")
    loadout = _loadout(
        callable_=[tool],
        exposures={"mcp__docs__get": "codemode"},
        namespaces={"mcp__docs__get": ToolNamespace(name="mcp__docs", description="Docs")},
        guidelines={"mcp__docs__get": ["Be nice."]},
    )
    description = (prepare_codemode_loadout(loadout).descriptions or {})[CODEMODE_TOOL_NAME]
    assert "## mcp__docs" in description
    assert "Be nice." in description


def test_codemode_tool_options_defaults():
    options = CodemodeToolOptions()
    assert options.models is False
    assert options.get_mode is None and options.get_inline_budget is None
