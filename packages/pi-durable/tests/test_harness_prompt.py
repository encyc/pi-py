"""``harness/prompt.py`` 的测试 —— 移植 ``harness/prompt.ts``（158 行）。

三段：**重放**（顺序语义是重点）、**渲染**（抛出 / 取消 / 标签）、**规划**（三种情形）。
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from pi_ai import SystemMessage, Tool
from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_chord.context import AbortController, with_abort_signal
from pi_durable.harness.prompt import (
    plan_system_entries,
    render_sections,
    replay_sections,
)


def system(sections: dict[str, Any]) -> SystemMessage:
    """⚠️ 用 **pydantic** ``SystemMessage``，不是 dict。

    ``pi_ai.transcript.get_current_tools()`` 按**属性**读 ``tools_added`` / ``tools_removed``,
    给它一个 camelCase 的 dict 会**静默返回空列表**（见 R26 同一族问题）。
    """
    return SystemMessage(content="", sections=cast(Any, sections), timestamp=1)


def tool(name: str, description: str = "d") -> Tool:
    return Tool(name=name, description=description, parameters={"type": "object"})


def view(
    entries: list[Any] | None = None, messages: list[Any] | None = None, head: Any = None
) -> Any:
    return {
        "head": head,
        "entries": entries or [],
        "contributions": [],
        "messages": messages or [],
    }


# --------------------------------------------------------------------------------------
# replay_sections
# --------------------------------------------------------------------------------------


def test_replay_sets_deletes_and_re_adds() -> None:
    """**就地设置、``None`` 删除、再添加则追加到末尾**。"""
    assert replay_sections([system({"a": "1"}), system({"b": "2"})]) == {"a": "1", "b": "2"}
    assert replay_sections([system({"a": "1"}), system({"a": None})]) == {}
    assert replay_sections([system({"a": "1"}), system({"a": None}), system({"a": "2"})]) == {
        "a": "2"
    }


def test_replay_updating_an_existing_key_keeps_its_position() -> None:
    """⚠️ JS 的 ``Map.set`` 更新值但**不改位置**；Python 的 ``d[k] = v`` 对已存在的键同样如此。

    这里**不能**「先删再赋」——那会把它挪到末尾。

    **为什么这是真 bug 而不只是差异**：这条顺序会**改变 ``plan_sections`` 的判断**——
    重放出来的顺序与期望不同时，它会走「全删 + 按序重加」那条路
    （见 ``test_remove_all_then_re_add_all_when_the_order_would_differ``）。
    也就是说，一个错的位置会让本该是「最小 patch」的写入退化成「清空重写一遍」。
    """
    shown = replay_sections([system({"a": "1", "b": "2"}), system({"a": "9"})])
    assert list(shown) == ["a", "b"]  # 位置没动
    assert shown["a"] == "9"


def test_replay_delete_then_re_add_moves_to_the_end() -> None:
    """只有「先删后加」才真的追加到末尾——顺序是有意义的（见规划的第三种情形）。"""
    shown = replay_sections([system({"a": "1", "b": "2"}), system({"a": None}), system({"a": "9"})])
    assert list(shown) == ["b", "a"]


def test_replay_ignores_non_system_messages_and_missing_sections() -> None:
    assert (
        replay_sections([{"role": "user", "content": "x"}, {"role": "system", "content": ""}]) == {}
    )


# --------------------------------------------------------------------------------------
# render_sections
# --------------------------------------------------------------------------------------


async def test_render_wraps_tagged_sections() -> None:
    sections = [{"key": "rules", "render": lambda *_: "be nice"}]
    desired = await render_sections(sections, {}, {}, lambda e: None, CTX)  # type: ignore[arg-type]
    assert desired == {"rules": "<rules>\nbe nice\n</rules>"}


async def test_render_honours_tag_false() -> None:
    sections = [{"key": "rules", "render": lambda *_: "raw", "tag": False}]
    desired = await render_sections(sections, {}, {}, lambda e: None, CTX)  # type: ignore[arg-type]
    assert desired == {"rules": "raw"}


async def test_render_omits_an_undefined_section() -> None:
    sections = [{"key": "a", "render": lambda *_: None}]
    assert await render_sections(sections, {}, {}, lambda e: None, CTX) == {}  # type: ignore[arg-type]


async def test_render_awaits_an_async_renderer() -> None:
    async def render(*_: Any) -> str:
        return "async"

    sections = [{"key": "a", "render": render}]
    desired = await render_sections(sections, {}, {}, lambda e: None, CTX)  # type: ignore[arg-type]
    assert desired == {"a": "<a>\nasync\n</a>"}


async def test_a_throwing_section_keeps_its_shown_text_and_is_reported() -> None:
    reported: list[Any] = []

    def boom(*_: Any) -> Any:
        raise RuntimeError("render exploded")

    sections = [{"key": "a", "render": boom}]
    desired = await render_sections(sections, {}, {"a": "old"}, reported.append, CTX)  # type: ignore[arg-type]

    assert desired == {"a": "old"}  # 保留已显示的文本
    assert len(reported) == 1
    assert "render exploded" in str(reported[0])


async def test_a_throwing_section_without_shown_text_contributes_nothing() -> None:
    sections = [{"key": "a", "render": lambda *_: (_ for _ in ()).throw(RuntimeError("x"))}]
    desired = await render_sections(sections, {}, {}, lambda e: None, CTX)  # type: ignore[arg-type]
    assert desired == {}


async def test_errors_after_cancellation_propagate() -> None:
    """⚠️ ``context`` 已被取消之后**再**抛出的错误**向上传播**——不再被吞成「报告」。"""
    from pi_chord.context import AbortError

    reported: list[Any] = []
    controller = AbortController()
    controller.abort()

    def boom(*_: Any) -> Any:
        raise AbortError("cancelled")

    sections = [{"key": "a", "render": boom}]
    with pytest.raises(AbortError):
        await render_sections(
            sections,
            {},
            {},
            reported.append,
            with_abort_signal(controller.signal, CTX),  # type: ignore[arg-type]
        )
    assert reported == []


# --------------------------------------------------------------------------------------
# plan_system_entries —— 情形一：完整基线
# --------------------------------------------------------------------------------------


def test_baseline_when_a_head_marker_has_no_later_system_entry() -> None:
    """有 head 标记、且它之后没有 ``pi.system`` 条目 → **写一个完整基线**，
    并 ``omit`` 掉所有被保留的更早 ``pi.system`` 条目。"""
    head = {"id": 10, "conversationId": 1, "kind": "pi.reset", "head": 10}
    entries = [
        {"id": 5, "conversationId": 1, "kind": "pi.system"},
        head,
    ]
    planned = plan_system_entries(view(entries=entries, head=head), {"a": "1"}, [tool("t")], 7.0)

    assert len(planned) == 1
    message = planned[0]["model"][0]
    assert message["sections"] == {"a": "1"}
    # ⚠️ 空的可选键**整个不存在**（上游 omitUndefinedProperties；本包按 R23 在构造处不写）
    assert "toolsRemoved" not in message
    assert [t.name for t in message["toolsAdded"]] == ["t"]
    # 被保留的更早 pi.system 条目要被 omit
    assert planned[0]["edits"] == [{"target": 5, "action": "omit"}]


def test_baseline_without_retained_system_entries_has_no_edits() -> None:
    head = {"id": 10, "conversationId": 1, "kind": "pi.reset", "head": 10}
    planned = plan_system_entries(view(entries=[head], head=head), {}, [], 1.0)
    assert len(planned) == 1
    # 没有被保留的更早条目 → **没有** `edits` 键
    assert "edits" not in planned[0]
    assert planned[0]["model"][0]["sections"] == {}


# --------------------------------------------------------------------------------------
# plan_system_entries —— 情形二/三：最小 patch 与「全删重加」
# --------------------------------------------------------------------------------------


def test_minimal_patch_of_changed_values() -> None:
    messages = [system({"a": "1", "b": "2"})]
    planned = plan_system_entries(view(messages=messages), {"a": "9", "b": "2"}, [], 1.0)

    assert len(planned) == 1
    assert planned[0]["model"][0]["sections"] == {"a": "9"}  # 只写改过的


def test_minimal_patch_with_a_null_removal() -> None:
    messages = [system({"a": "1", "b": "2"})]
    planned = plan_system_entries(view(messages=messages), {"a": "1"}, [], 1.0)
    assert planned[0]["model"][0]["sections"] == {"b": None}  # `None` 表示删除


def test_nothing_planned_when_everything_matches() -> None:
    messages = [system({"a": "1"})]
    planned = plan_system_entries(view(messages=messages), {"a": "1"}, [], 1.0)
    assert [p for p in planned if "sections" in p["model"][0]] == []


def test_remove_all_then_re_add_all_when_the_order_would_differ() -> None:
    """**顺序不同**时：先删掉全部已显示的，再按序重加全部期望的。"""
    messages = [system({"a": "1", "b": "2"})]
    planned = plan_system_entries(view(messages=messages), {"b": "2", "a": "1"}, [], 1.0)

    patches = [p["model"][0]["sections"] for p in planned if "sections" in p["model"][0]]
    assert patches == [{"a": None, "b": None}, {"b": "2", "a": "1"}]


def test_tool_changes_ride_on_the_last_planned_entry() -> None:
    """工具变更**搭在最后一条**规划出来的条目上。"""
    messages = [system({"a": "1"})]
    planned = plan_system_entries(view(messages=messages), {"a": "9"}, [tool("t")], 1.0)

    assert len(planned) == 1
    message = planned[0]["model"][0]
    assert message["sections"] == {"a": "9"}
    assert [t.name for t in message["toolsAdded"]] == ["t"]


def test_tool_changes_get_their_own_entry_without_section_patches() -> None:
    messages = [system({"a": "1"})]
    planned = plan_system_entries(view(messages=messages), {"a": "1"}, [tool("t")], 1.0)

    assert len(planned) == 1
    message = planned[0]["model"][0]
    assert "sections" not in message
    assert [t.name for t in message["toolsAdded"]] == ["t"]


def test_declaration_change_removes_and_re_adds_the_tool() -> None:
    """**声明变了**的工具会被移除再添加（重放没法原地改声明）。"""
    from pi_ai.transcript import to_tool_declaration

    original = tool("t", "old")
    messages = [
        system({}),
        # ⚠️ 必须是 pydantic 对象：`get_current_tools()` 按**属性**读 `tools_added`
        SystemMessage(content="", tools_added=[to_tool_declaration(original)], timestamp=1),
    ]
    planned = plan_system_entries(view(messages=messages), {}, [tool("t", "new")], 1.0)

    message = planned[0]["model"][0]
    assert [t["name"] for t in message.get("toolsRemoved", [])] == ["t"]
    assert [t.name for t in message["toolsAdded"]] == ["t"]


def test_unchanged_tools_are_left_alone() -> None:
    """⚠️ 期望的列表必须与**已存声明**同形。

    ``to_tool_declaration()`` 会剥掉 ``Tool`` 上不属于声明的字段，
    所以拿原始 ``Tool`` 去比会被判成「声明变了」。真实调用方传的是
    ``agent.tools``（本身就是声明），这里照此构造。
    """
    from pi_ai.transcript import to_tool_declaration

    same = to_tool_declaration(tool("t"))
    messages = [SystemMessage(content="", tools_added=[same], timestamp=1)]
    planned = plan_system_entries(view(messages=messages), {}, [same], 1.0)

    assert [
        p for p in planned if "toolsAdded" in p["model"][0] or "toolsRemoved" in p["model"][0]
    ] == []
