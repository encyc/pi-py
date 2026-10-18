"""``harness/tool.py`` 的 **Slice A**（叶子层）测试 —— 移植 ``harness/tool.ts`` 的叶子部分。

⚠️ **本片是按「可测性」切的**：``ToolTask`` 的三个 phase 都收 ``TaskRuntime``，
而它的实现在 `scheduler.py`（关键路径上靠后）。所以本片**不碰 `TaskRuntime`**，
这里测的全是**纯函数 + 一个用真实 `tx` 的导出**。

**Slice B**（`ToolTask` 本体）在 `scheduler.py` 到位后做——那时才用它**真实的**实现测。
"""

from __future__ import annotations

from typing import Any

import pytest

from pi_ai import TextContent, ToolCall
from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_chord.output import OutputLimits
from pi_durable.harness.tool import (
    COMPLETED,
    MADE_NESTED_CALLS,
    MAX_SUMMARY_ERROR_CHARS,
    NestedCallDoc,
    append_tool_result,
    bound_content,
    check_key,
    fallback_result,
    from_slot,
    harness_error,
    json_equal,
    nested_result,
    output_value,
    render_diagnostics,
    structured_output_error,
    summary_of,
    tool_diagnostic,
    truncated,
)
from pi_durable.harness.types import ToolDiagnostic, ToolExecutionResult, ToolRegistration
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage


def limits(retain: str = "head", max_bytes: int = 1000, max_lines: int = 100) -> OutputLimits:
    """⚠️ chord 的 ``OutputLimits`` 是 **snake_case**（不是上游的 camelCase）。"""
    return OutputLimits(max_bytes=max_bytes, max_lines=max_lines, retain=retain)


def text(value: str) -> TextContent:
    return TextContent(type="text", text=value)


def tool(name: str = "read", **kwargs: Any) -> ToolRegistration[Any]:
    fields: dict[str, Any] = {
        "name": name,
        "description": name,
        "parameters": {"type": "object"},
        "execute": lambda *_: None,
    }
    fields.update(kwargs)
    return ToolRegistration(**fields)


def call(id: str = "c1", name: str = "read") -> ToolCall:
    return ToolCall(id=id, name=name, arguments={})


# --------------------------------------------------------------------------------------
# 常量与文档
# --------------------------------------------------------------------------------------


def test_nested_call_doc_is_a_task_scoped_family() -> None:
    """一次嵌套调用一份文档：任何一次提交都不重写别的；**随调用者退役**。"""
    definition = NestedCallDoc.definition
    assert definition["kind"] == "pi.tool.nested-call"
    assert definition["scope"] == "task"
    assert definition["family"] is True
    assert definition["version"] == 1
    assert definition["initial"](None) == {}


def test_made_nested_calls_memo_key() -> None:
    assert MADE_NESTED_CALLS == "pi.tool.madeNestedCalls"


def test_completed_ending() -> None:
    assert COMPLETED == {"status": "completed"}


# --------------------------------------------------------------------------------------
# harness_error / tool_diagnostic / invalid
# --------------------------------------------------------------------------------------


def test_harness_error_has_no_content_and_one_error_diagnostic() -> None:
    result = harness_error("tool_unavailable", "Tool x is not available")
    assert result.output == ()
    assert result.is_error is True
    assert len(result.diagnostics or ()) == 1
    diagnostic = (result.diagnostics or ())[0]
    assert diagnostic.severity == "error"
    assert diagnostic.code == "tool_unavailable"
    assert diagnostic.message == "Tool x is not available"


def test_tool_diagnostic_is_always_error_severity() -> None:
    assert tool_diagnostic("code", "message").severity == "error"


# --------------------------------------------------------------------------------------
# truncated / render_diagnostics
# --------------------------------------------------------------------------------------


def test_truncated_names_the_retained_end() -> None:
    head = truncated({"dropped_lines": 3, "dropped_bytes": 40}, "head")
    assert head.severity == "warn"
    assert head.code == "truncated"
    assert "to its beginning" in head.message
    assert "3 lines, 40 bytes dropped" in head.message

    tail = truncated({"dropped_lines": 1, "dropped_bytes": 2}, "tail")
    assert "to its end" in tail.message


def test_truncated_without_a_known_retain_omits_the_phrase() -> None:
    """⚠️ 从槽位重建时 ``retain`` **未知**——那时不能瞎猜，只能省略那句。"""
    rebuilt = truncated({"dropped_lines": 1, "dropped_bytes": 2})
    assert "to its" not in rebuilt.message
    assert rebuilt.message.startswith("Output truncated:")


def test_render_diagnostics_shape() -> None:
    rendered = render_diagnostics(
        [tool_diagnostic("a", "first"), ToolDiagnostic(severity="warn", message="second")]
    )
    assert rendered == "<harness>\n[error] first\n[warn] second\n</harness>"


# --------------------------------------------------------------------------------------
# output_value / bound_content
# --------------------------------------------------------------------------------------


def test_output_value_for_a_schemaless_tool() -> None:
    assert output_value([]) == ""
    assert output_value([text("only")]) == "only"
    both = [text("a"), text("b")]
    assert output_value(both) == both  # 多个 → 内容列表


def test_bound_content_leaves_short_content_alone() -> None:
    content = [text("short")]
    bounded = bound_content(content, limits())
    assert bounded["content"] == content
    assert bounded["dropped_bytes"] == 0


def test_bound_content_replaces_text_items_at_the_kept_position() -> None:
    content = [text("x" * 500), text("y" * 500)]
    bounded = bound_content(content, limits(max_bytes=100))
    assert bounded["dropped_bytes"] > 0
    # head：只剩下第一个文本项，且它是**定界后**的
    assert len(bounded["content"]) == 1
    assert len(bounded["content"][0].text) < 1000


def test_bound_content_keeps_non_text_items() -> None:
    from pi_ai import ImageContent

    image = ImageContent(type="image", data="", mimeType="image/png")
    content = [text("x" * 500), image]
    bounded = bound_content(content, limits(max_bytes=10))
    assert any(getattr(item, "type", None) == "image" for item in bounded["content"])


# --------------------------------------------------------------------------------------
# check_key
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["", "a/b", "__proto__", "1", "42"])
def test_check_key_rejects_bad_keys(key: str) -> None:
    """键是调用 ID 的**路径段**，所以不能含 ``/``；默认键是正整数，所以显式键不能是正整数。"""
    with pytest.raises(ValueError):
        check_key(key)


@pytest.mark.parametrize("key", ["a", "0", "has space", "UPPER", "-1", "01"])
def test_check_key_accepts_good_keys(key: str) -> None:
    check_key(key)  # 不抛


# --------------------------------------------------------------------------------------
# json_equal
# --------------------------------------------------------------------------------------


def test_json_equal_is_structural() -> None:
    assert json_equal({"a": 1, "b": [1, 2]}, {"b": [1, 2], "a": 1})  # 键序无关
    assert not json_equal({"a": 1}, {"a": 1, "b": 2})
    assert not json_equal([1, 2], [2, 1])
    assert json_equal(None, None)


def test_json_equal_distinguishes_true_from_one() -> None:
    """⚠️ Python 的 ``1 == True`` 成立，**JSON 里它们是不同的值**。

    不区分会让「同一把键、不同参数」的嵌套调用被**误判成同一个**。
    """
    assert not json_equal(1, True)
    assert not json_equal(True, 1)
    assert json_equal(1, 1)
    assert json_equal(True, True)
    assert not json_equal("1", 1)


# --------------------------------------------------------------------------------------
# summary_of / error_text_of
# --------------------------------------------------------------------------------------


def test_summary_omits_empty_fields_rather_than_writing_empty_strings() -> None:
    """⚠️ ``error == ""`` 表示「没有错误文本」，此时**不写那个键**（R23：在构造处省略）。"""
    summary = summary_of({"taskId": 1, "isError": False}, [])
    assert summary == {"isError": False}
    assert "error" not in summary
    assert "durationMs" not in summary
    assert "usage" not in summary


def test_summary_takes_error_text_from_diagnostics_first() -> None:
    summary = summary_of(
        {
            "taskId": 1,
            "isError": True,
            "diagnostics": [tool_diagnostic("c", "boom")],
            "durationMs": 5,
        },
        [text("fallback text")],
    )
    assert summary["error"] == "boom"  # 说明优先于输出文本
    assert summary["isError"] is True
    assert summary["durationMs"] == 5


def test_summary_takes_error_text_from_output_when_no_error_diagnostics() -> None:
    summary = summary_of({"taskId": 1, "isError": True, "diagnostics": []}, [text("a"), text("b")])
    assert summary["error"] == "a\nb"


def test_summary_bounds_the_error_text() -> None:
    long = "x" * 900
    summary = summary_of(
        {"taskId": 1, "isError": True, "diagnostics": [tool_diagnostic("c", long)]}, []
    )
    assert len(summary["error"]) == MAX_SUMMARY_ERROR_CHARS


# --------------------------------------------------------------------------------------
# nested_result / fallback_result / from_slot
# --------------------------------------------------------------------------------------


def test_nested_result_is_strict_json_without_output_or_control() -> None:
    """⚠️ 不带 ``output``（只有模型读它）与 ``control``（只有模型发起的调用用）。"""
    result = ToolExecutionResult[Any](
        output=[text("for the model")],
        structured_output={"a": 1},
        details={"d": 1},
        diagnostics=(tool_diagnostic("c", "m"),),
    )
    nested = nested_result(7, result, 12.0)

    assert nested["taskId"] == 7
    assert nested["structuredOutput"] == {"a": 1}
    assert nested["durationMs"] == 12.0
    assert "output" not in nested
    assert "control" not in nested
    assert nested["isError"] is False


def test_nested_result_omits_none_fields() -> None:
    """上游是 ``omitUndefinedProperties``——本包**在构造处省略**（R23）。"""
    nested = nested_result(1, ToolExecutionResult[Any](), None)
    for key in ("details", "usage", "durationMs", "structuredOutput"):
        assert key not in nested, key


@pytest.mark.parametrize(
    ("status", "phrase"),
    [
        ("faulted", "failed:"),
        ("orphaned", "could not resume:"),
        ("completed", "ended without a result"),
    ],
)
def test_fallback_result_messages(status: str, phrase: str) -> None:
    outcome: dict[str, Any] = {"status": status}
    if status == "faulted":
        outcome["error"] = {"message": "boom"}
    if status == "orphaned":
        outcome["reason"] = "owner ended"
    fallback = fallback_result(3, "read", outcome)
    assert fallback["taskId"] == 3
    assert fallback["isError"] is True
    assert phrase in fallback["diagnostics"][0]["message"]


def test_from_slot_rebuilds_from_durable_partial_output() -> None:
    """⚠️ 这是「中断 / 丢弃」时**唯一**的持久证据来源（它在 ``pi.live`` 里）。"""
    slot = {
        "output": "partial",
        "droppedBytes": 10,
        "droppedLines": 2,
        "details": {"d": 1},
        "diagnostics": [tool_diagnostic("old", "earlier")],
    }
    result = from_slot(slot, "aborted", "Tool read was aborted")

    assert result.is_error is True
    assert result.output == [text("partial")]
    assert result.details == {"d": 1}
    codes = [d.code for d in result.diagnostics or ()]
    assert codes == ["old", "truncated", "aborted"]  # 截断说明夹在中间


def test_from_slot_without_output_has_empty_content() -> None:
    result = from_slot(None, "abandoned", "not started")
    assert result.output == []
    assert [d.code for d in result.diagnostics or ()] == ["abandoned"]


# --------------------------------------------------------------------------------------
# structured_output_error（jsonschema）
# --------------------------------------------------------------------------------------


def schema_tool(schema: Any) -> ToolRegistration[Any]:
    return tool(structured_output_schema=schema)


def test_a_schemaless_tool_must_not_return_structured_output() -> None:
    assert structured_output_error(tool(), ToolExecutionResult[Any]()) is None
    broken = structured_output_error(tool(), ToolExecutionResult[Any](structured_output={"a": 1}))
    assert broken is not None and "declares no structuredOutputSchema" in broken


def test_a_schema_tool_must_return_structured_output_unless_it_errored() -> None:
    noted = schema_tool({"type": "object"})
    assert structured_output_error(noted, ToolExecutionResult[Any]()) is not None
    # 出错时可以不带（错误结果本来就允许没有值）
    assert structured_output_error(noted, ToolExecutionResult[Any](is_error=True)) is None


def test_structured_output_validation_accepts_and_rejects() -> None:
    noted = schema_tool(
        {"type": "object", "properties": {"count": {"type": "number"}}, "required": ["count"]}
    )
    ok = ToolExecutionResult[Any](structured_output={"count": 3})
    assert structured_output_error(noted, ok) is None

    bad = ToolExecutionResult[Any](structured_output={"count": "three"})
    broken = structured_output_error(noted, bad)
    assert broken is not None
    assert "does not match its schema" in broken
    assert "count" in broken  # 路径里带上出错的字段


def test_structured_output_error_reports_the_root_for_a_top_level_mismatch() -> None:
    noted = schema_tool({"type": "object"})
    broken = structured_output_error(noted, ToolExecutionResult[Any](structured_output=[1, 2]))
    assert broken is not None and "root:" in broken


def test_structured_output_uses_the_full_jsonschema_keyword_set() -> None:
    """⚠️ **两条校验路径别混**：``parameters`` 走 pi-ai 的校验器（6 个关键字被忽略），
    而 ``structured_output_schema`` 走**本包的 ``jsonschema``**（全关键字）。

    这里用 ``pattern``（pi-ai 的 24 个里**没有**它）证明这一路是真的全支持。
    """
    noted = schema_tool(
        {"type": "object", "properties": {"id": {"type": "string", "pattern": "^a+$"}}}
    )
    assert (
        structured_output_error(noted, ToolExecutionResult[Any](structured_output={"id": "aaa"}))
        is None
    )
    assert (
        structured_output_error(noted, ToolExecutionResult[Any](structured_output={"id": "bbb"}))
        is not None
    )


# --------------------------------------------------------------------------------------
# append_tool_result（真实 tx，内存 + SQLite）
# --------------------------------------------------------------------------------------


async def make_session(backend: str, tmp_path: Any) -> SessionImpl:
    storage = (
        MemoryStorage()
        if backend == "memory"
        else await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    )
    session = SessionImpl(storage)
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    return session


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_append_tool_result_puts_diagnostics_at_the_end_of_the_content(
    backend: str, tmp_path: Any
) -> None:
    """⚠️ 内容**以渲染后的说明结尾**，所以存下来的消息**正是模型看到的那条**；
    ``data`` 保留结构化那份列表。"""
    session = await make_session(backend, tmp_path)
    try:
        result = ToolExecutionResult[Any](
            output=[text("the answer")],
            diagnostics=(tool_diagnostic("c", "careful"),),
        )
        captured: list[Any] = []

        async def change(tx: Any) -> None:
            captured.append(await append_tool_result(tx, 1, call(), result, {"timestamp": 5}))

        await session.commit(change, CTX)
        entry = captured[0]

        assert entry["kind"] == "pi.tool-result"
        # ⚠️ 消息是**严格 JSON 的 dict**（不是 pydantic 对象）——存储要序列化它。
        message = entry["model"][0]
        assert isinstance(message, dict)
        assert message["tool_call_id"] == "c1"
        # 最后一项是渲染后的说明
        assert message["content"][-1]["text"].startswith("<harness>")
        assert "[error] careful" in message["content"][-1]["text"]
        # `data` 保留结构化那份
        assert entry["data"]["diagnostics"][0]["message"] == "careful"
        assert message["is_error"] is False
    finally:
        await session.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_append_tool_result_records_usage_in_the_same_commit(
    backend: str, tmp_path: Any
) -> None:
    from pi_ai import Usage

    session = await make_session(backend, tmp_path)
    try:
        result = ToolExecutionResult[Any](usage=Usage(input=7, output=3))

        async def change(tx: Any) -> None:
            await append_tool_result(tx, 1, call(), result, {"timestamp": 1})

        await session.commit(change, CTX)
        from pi_durable.harness.usage import UsageDoc

        usage = await session.snapshot(UsageDoc, 1, CTX)
        assert usage is not None
        assert usage["tools"]["read"]["input"] == 7
    finally:
        await session.close(CTX)


async def test_append_tool_result_omits_absent_fields() -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    captured: list[Any] = []

    async def change(tx: Any) -> None:
        captured.append(
            await append_tool_result(
                tx, 1, call(), ToolExecutionResult[Any](output=[text("x")]), {"timestamp": 1}
            )
        )

    await session.commit(change, CTX)
    message = captured[0]["model"][0]
    # ⚠️ 省略的键**整个不存在**（上游 omitUndefinedProperties；本包在构造处省略，R23）
    assert "details" not in message
    assert "duration_ms" not in message
    assert captured[0]["data"] == {"diagnostics": []}
