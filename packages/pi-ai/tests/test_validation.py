"""``validate_tool_arguments`` / ``validate_tool_call`` 测试。

**逐条移植上游** ``packages/ai/test/validation.test.ts``（v1.3.0，229 行），
并补边界（必填/可选、类型收窄、嵌套对象、额外字段）。

形状差异见 :mod:`pi_ai.validation` 模块 docstring：本端只走上游那条"非 typebox
schema"分支，用自研 JSON Schema 校验器替代 typebox ``Compile``。因此
**单条错误的 message 措辞不保证与 typebox 逐字相同**；断言聚焦于上游用例真正
依赖的部分（返回值、异常前缀、路径）。
"""

from __future__ import annotations

import pytest

from pi_ai.types import Tool, ToolCall
from pi_ai.validation import validate_tool_arguments, validate_tool_call


def _tool_call(name: str = "echo", arguments: dict | None = None, **kw: object) -> ToolCall:
    return ToolCall(
        id="tool-1",
        name=name,
        arguments=arguments if arguments is not None else {},
        **kw,  # type: ignore[arg-type]
    )


def _plain_schema_tool(property_schema: dict, *, required: bool = True) -> Tool:
    """上游 ``createToolCallWithPlainSchema`` 的等价物：``{value: <schema>}``。"""
    schema: dict = {"type": "object", "properties": {"value": property_schema}}
    if required:
        schema["required"] = ["value"]
    return Tool(name="echo", description="Echo tool", parameters=schema)


# ============================================================
# 上游用例：plain JSON schema 的 AJV 兼容基本转换
# ============================================================


@pytest.mark.parametrize(
    ("prop_schema", "value", "expected"),
    [
        ({"type": "number"}, "42", 42),
        ({"type": "number"}, True, 1),
        ({"type": "number"}, None, 0),
        ({"type": "integer"}, "42", 42),
        ({"type": "boolean"}, "true", True),
        ({"type": "boolean"}, "false", False),
        ({"type": "boolean"}, 1, True),
        ({"type": "boolean"}, 0, False),
        ({"type": "string"}, None, ""),
        ({"type": "string"}, True, "true"),
        ({"type": "null"}, "", None),
        ({"type": "null"}, 0, None),
        ({"type": "null"}, False, None),
        # 联合：原值已匹配某一分支 -> 原样保留，不做转换
        ({"type": ["number", "string"]}, "1", "1"),
        # 联合：原值不匹配任何分支 -> 按 schemaTypes 顺序尝试转换（boolean 不动，number 生效）
        ({"type": ["boolean", "number"]}, "1", 1),
    ],
)
def test_coerces_serialized_plain_json_schemas(
    prop_schema: dict, value: object, expected: object
) -> None:
    tool = _plain_schema_tool(prop_schema)
    assert validate_tool_arguments(tool, _tool_call(arguments={"value": value})) == {
        "value": expected
    }


@pytest.mark.parametrize(
    ("prop_schema", "value"),
    [
        ({"type": "boolean"}, "1"),
        ({"type": "boolean"}, "0"),
        ({"type": "null"}, "null"),
        ({"type": "integer"}, "42.1"),
    ],
)
def test_rejects_invalid_coercions(prop_schema: dict, value: object) -> None:
    tool = _plain_schema_tool(prop_schema)
    with pytest.raises(ValueError, match="Validation failed"):
        validate_tool_arguments(tool, _tool_call(arguments={"value": value}))


# ============================================================
# 上游用例：可选 + null 语义
# ============================================================


def test_treats_null_as_omission_for_optional_non_nullable_properties() -> None:
    """可选且不可空属性上的 null 被删除；可空/引用型保留。"""
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "number"},
                "nullable": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "metadata": {
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}},
                },
            },
            "required": ["path"],
        },
    )
    result = validate_tool_arguments(
        tool,
        _tool_call(
            arguments={
                "path": "file.txt",
                "offset": None,
                "nullable": None,
                "metadata": {"enabled": None},
            }
        ),
    )
    assert result == {"path": "file.txt", "nullable": None, "metadata": {}}


def test_preserves_optional_nulls_whose_referenced_schema_is_nullable() -> None:
    """``$ref`` 属性上的 null **永不**被删除（上游 :261 显式排除 ``$ref``）。"""
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={
            "type": "object",
            "properties": {"value": {"$ref": "#/$defs/value"}},
            "$defs": {"value": {"anyOf": [{"type": "number"}, {"type": "null"}]}},
        },
    )
    assert validate_tool_arguments(tool, _tool_call(arguments={"value": None})) == {"value": None}


def test_preserves_value_matching_a_nullable_union_arm() -> None:
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={
            "type": "object",
            "properties": {"value": {"anyOf": [{"type": "number"}, {"type": "null"}]}},
        },
    )
    assert validate_tool_arguments(tool, _tool_call(arguments={"value": None})) == {"value": None}


def test_preserves_value_matching_a_oneof_nullable_union_arm() -> None:
    tool = _plain_schema_tool({"oneOf": [{"type": "number"}, {"type": "null"}]})
    assert validate_tool_arguments(tool, _tool_call(arguments={"value": None})) == {"value": None}


def test_still_coerces_nullable_unions_when_original_does_not_match_any_arm() -> None:
    tool = _plain_schema_tool({"anyOf": [{"type": "number"}, {"type": "null"}]})
    assert validate_tool_arguments(tool, _tool_call(arguments={"value": "42"})) == {"value": 42}


def test_accepts_null_for_nullable_array_schemas_with_items() -> None:
    tool = _plain_schema_tool({"type": ["array", "null"], "items": {"type": "string"}})
    assert validate_tool_arguments(tool, _tool_call(arguments={"value": None})) == {"value": None}


# ============================================================
# 上游用例 #10897：坏 JSON 参数
# ============================================================


def test_reports_malformed_arguments_json_instead_of_validating_empty_arguments() -> None:
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}},
    )
    call = _tool_call(arguments={}, argumentsError="Unexpected token")
    with pytest.raises(ValueError) as excinfo:
        validate_tool_arguments(tool, call)
    assert 'Invalid arguments for tool "echo": Unexpected token' in str(excinfo.value)


# ============================================================
# 上游用例：类型/必填/嵌套/额外字段等边界
# ============================================================


def test_required_property_is_reported_with_its_name_in_the_path() -> None:
    tool = Tool(
        name="read",
        description="Read a file",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path", "limit"],
        },
    )
    with pytest.raises(ValueError) as excinfo:
        validate_tool_arguments(tool, _tool_call(name="read", arguments={"path": "a.txt"}))
    message = str(excinfo.value)
    assert 'Validation failed for tool "read":' in message
    assert "  - limit: " in message, message
    assert "Received arguments:" in message


def test_nested_object_error_path_uses_dot_notation() -> None:
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={
            "type": "object",
            "properties": {
                "metadata": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                }
            },
        },
    )
    with pytest.raises(ValueError) as excinfo:
        validate_tool_arguments(tool, _tool_call(arguments={"metadata": {}}))
    assert "  - metadata.count: " in str(excinfo.value)


def test_array_item_error_path_includes_index() -> None:
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "integer"}}},
        },
    )
    with pytest.raises(ValueError) as excinfo:
        validate_tool_arguments(tool, _tool_call(arguments={"items": [1, "x"]}))
    assert "  - items.1: " in str(excinfo.value)


def test_root_path_is_labelled_root_when_the_value_itself_is_wrong() -> None:
    """根值类型不符时路径渲染为 ``root``（上游 :291-292 ``return path || "root"``）。

    无法经 ``ToolCall`` 触发：``ToolCall.arguments`` 被约束为 ``dict``，传列表会在
    Pydantic 层就被拒。故直接测路径渲染函数（空 instancePath → ``"root"``）。
    """
    from pi_ai.validation import _format_validation_path, _ValidationError

    assert _format_validation_path(_ValidationError("type", "", "Expected object")) == "root"
    assert _format_validation_path(_ValidationError("type", "/a/b", "x")) == "a.b"
    # required 关键字：把缺失的属性名接到路径尾部
    required_error = _ValidationError("required", "", "x", {"requiredProperties": ["limit"]})
    assert _format_validation_path(required_error) == "limit"
    nested_required = _ValidationError(
        "required", "/metadata", "x", {"requiredProperties": ["count"]}
    )
    assert _format_validation_path(nested_required) == "metadata.count"


def test_additional_properties_false_rejects_unknown_keys() -> None:
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    with pytest.raises(ValueError) as excinfo:
        validate_tool_arguments(tool, _tool_call(arguments={"a": "x", "b": "y"}))
    assert "b" in str(excinfo.value)


def test_additional_properties_schema_validates_and_coerces_unknown_keys() -> None:
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "additionalProperties": {"type": "integer"},
        },
    )
    # 转换：额外键 "n" 的 "7" -> 7
    assert validate_tool_arguments(tool, _tool_call(arguments={"a": "x", "n": "7"})) == {
        "a": "x",
        "n": 7,
    }
    with pytest.raises(ValueError):
        validate_tool_arguments(tool, _tool_call(arguments={"a": "x", "n": "not-a-number"}))


def test_unknown_properties_are_allowed_when_schema_is_silent() -> None:
    """schema 未声明 additionalProperties 时不拒绝额外字段（JSON Schema 默认允许）。"""
    tool = Tool(
        name="echo", description="Echo tool", parameters={"type": "object", "properties": {}}
    )
    assert validate_tool_arguments(tool, _tool_call(arguments={"extra": 1})) == {"extra": 1}


def test_enum_and_const_are_enforced() -> None:
    enum_tool = _plain_schema_tool({"enum": ["a", "b"]})
    assert validate_tool_arguments(enum_tool, _tool_call(arguments={"value": "a"})) == {
        "value": "a"
    }
    with pytest.raises(ValueError):
        validate_tool_arguments(enum_tool, _tool_call(arguments={"value": "c"}))

    const_tool = _plain_schema_tool({"const": 7})
    assert validate_tool_arguments(const_tool, _tool_call(arguments={"value": 7})) == {"value": 7}
    with pytest.raises(ValueError):
        validate_tool_arguments(const_tool, _tool_call(arguments={"value": 8}))


def test_numeric_and_string_constraints_are_enforced() -> None:
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1},
                "s": {"type": "string", "minLength": 2, "pattern": "^a"},
            },
        },
    )
    assert validate_tool_arguments(tool, _tool_call(arguments={"n": 1, "s": "ab"})) == {
        "n": 1,
        "s": "ab",
    }
    with pytest.raises(ValueError):
        validate_tool_arguments(tool, _tool_call(arguments={"n": 0, "s": "ab"}))
    with pytest.raises(ValueError):
        validate_tool_arguments(tool, _tool_call(arguments={"n": 1, "s": "b"}))


def test_allof_applies_every_branch() -> None:
    tool = _plain_schema_tool({"allOf": [{"type": "integer"}, {"minimum": 5}]})
    assert validate_tool_arguments(tool, _tool_call(arguments={"value": 5})) == {"value": 5}
    with pytest.raises(ValueError):
        validate_tool_arguments(tool, _tool_call(arguments={"value": 4}))


def test_array_tuple_items_validate_positionally() -> None:
    tool = _plain_schema_tool({"type": "array", "items": [{"type": "integer"}, {"type": "string"}]})
    assert validate_tool_arguments(tool, _tool_call(arguments={"value": [1, "a"]})) == {
        "value": [1, "a"]
    }
    with pytest.raises(ValueError):
        validate_tool_arguments(tool, _tool_call(arguments={"value": ["a", "a"]}))


def test_unresolvable_ref_is_ignored_not_fatal() -> None:
    """``$ref`` 指向不存在的目标时不崩溃（本端返回原 schema，等价于不约束）。"""
    tool = _plain_schema_tool({"$ref": "#/$defs/missing"})
    assert validate_tool_arguments(tool, _tool_call(arguments={"value": "anything"})) == {
        "value": "anything"
    }


# ============================================================
# 不修改入参
# ============================================================


def test_does_not_mutate_the_incoming_tool_call() -> None:
    tool = _plain_schema_tool({"type": "number"})
    call = _tool_call(arguments={"value": "42"})
    result = validate_tool_arguments(tool, call)
    assert result == {"value": 42}
    assert call.arguments == {"value": "42"}, "入参被就地修改（上游用 structuredClone 隔离）"
    assert result is not call.arguments


def test_returns_a_fresh_object_each_call() -> None:
    tool = _plain_schema_tool({"type": "object", "properties": {"a": {"type": "string"}}})
    call = _tool_call(arguments={"value": {"a": "x"}})
    first = validate_tool_arguments(tool, call)
    second = validate_tool_arguments(tool, call)
    assert first == second
    assert first is not second


# ============================================================
# 错误消息形状
# ============================================================


def test_error_message_shape_matches_upstream() -> None:
    """注意：``12345`` 对 ``type: string`` **会**被转换成 ``"12345"`` 而非报错，
    故此处用一个无法转换为字符串的对象来触发校验失败。"""
    tool = _plain_schema_tool({"type": "string"})
    call = _tool_call(arguments={"value": {"nested": 1}})
    with pytest.raises(ValueError) as excinfo:
        validate_tool_arguments(tool, call)
    text = str(excinfo.value)
    lines = text.splitlines()
    assert lines[0] == 'Validation failed for tool "echo":'
    assert lines[1].startswith("  - value: ")
    assert "Received arguments:" in lines
    # 回显的是**模型原始**参数（不是转换后的）
    assert '"nested": 1' in text


def test_error_echo_keeps_non_ascii_unescaped() -> None:
    """``JSON.stringify`` 不转义非 ASCII；Python 默认会，故必须 ``ensure_ascii=False``。"""
    tool = _plain_schema_tool({"type": "string"})
    with pytest.raises(ValueError) as excinfo:
        validate_tool_arguments(tool, _tool_call(arguments={"value": {"路径": "不存在"}}))
    # value 是对象而 schema 要 string -> 失败；回显应保留中文原文
    assert "路径" in str(excinfo.value)
    assert "\\u8def" not in str(excinfo.value)


# ============================================================
# validate_tool_call
# ============================================================


def test_validate_tool_call_finds_the_tool_by_name() -> None:
    tools = [
        Tool(name="other", description="d", parameters={"type": "object"}),
        _plain_schema_tool({"type": "number"}),
    ]
    assert validate_tool_call(tools, _tool_call(arguments={"value": "1"})) == {"value": 1}


def test_validate_tool_call_raises_for_unknown_tool() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_tool_call([], _tool_call(name="ghost"))
    assert str(excinfo.value) == 'Tool "ghost" not found'


def test_validate_tool_call_propagates_validation_failure() -> None:
    tools = [_plain_schema_tool({"type": "integer"})]
    with pytest.raises(ValueError, match="Validation failed"):
        validate_tool_call(tools, _tool_call(arguments={"value": "abc"}))


# ============================================================
# 导入路径 / 导出面
# ============================================================


def test_both_import_paths_resolve_to_the_same_objects() -> None:
    from pi_ai import validate_tool_arguments as top_a
    from pi_ai import validate_tool_call as top_b
    from pi_ai.utils.validation import validate_tool_arguments as alias_a
    from pi_ai.utils.validation import validate_tool_call as alias_b

    assert validate_tool_arguments is top_a is alias_a
    assert validate_tool_call is top_b is alias_b


def test_export_surface_matches_upstream() -> None:
    from pi_ai import validation as mod

    assert sorted(mod.__all__) == ["validate_tool_arguments", "validate_tool_call"]


def test_module_has_no_mutable_module_level_state() -> None:
    """上游有透明缓存 ``validatorCache``（WeakMap）；本端刻意不复刻，故无模块级可变状态。"""
    from pi_ai import validation as mod

    assert not hasattr(mod, "validatorCache")
    for name in dir(mod):
        if name.startswith("__"):
            continue
        value = getattr(mod, name)
        if isinstance(value, (dict, list, set)):
            assert isinstance(value, (frozenset,)), f"模块级可变容器：{name}"
