"""``harness/context.py`` 的测试 —— 移植 ``harness/context.ts``（333 行）。

三段（与规划一致）：

1. **纯函数**：``order_tool_results`` 的三种情形、``lead_with_system``、``find_baseline``；
2. ``capture_context_bounds`` 的两个 O(1) 读；
3. **增量读 ``read_context_from``** 的三条分支 —— 写错不会崩，只会让增量**悄悄退化成全量**，
   所以它最值钱。

内存与真实 SQLite 各跑一遍。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_ai import AssistantMessage, TextContent, ToolCall, ToolResultMessage, UserMessage
from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.harness.context import (
    MISSING_RESULT_TEXT,
    active_entries,
    capture_context_bounds,
    derive_context,
    freeze_json,
    lead_with_system,
    order_tool_results,
    read_context,
    read_context_from,
)
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage

# 上面的 `find_baseline` 不直接导出（内部函数），用 derive_context 的行为覆盖它。


def _role(message: Any) -> Any:
    """条目里的消息是 **JSON dict**（从存储读回来），合成的那条也是 dict。

    只有本地直接构造的 pydantic 对象才有属性——所以助手要两种都认。
    """
    return message.get("role") if isinstance(message, dict) else message.role


def _get(message: Any, name: str) -> Any:
    return message.get(name) if isinstance(message, dict) else getattr(message, name)


def user(text: str, timestamp: int = 1) -> UserMessage:
    return UserMessage(content=[TextContent(type="text", text=text)], timestamp=timestamp)


def assistant(
    text: str = "", *, calls: list[ToolCall] | None = None, stop: str = "stop", ts: int = 2
) -> Any:
    content: list[Any] = []
    if text:
        content.append(TextContent(type="text", text=text))
    if calls:
        content.extend(calls)
    return AssistantMessage(content=content, stopReason=stop, timestamp=ts)


def tool_result(call_id: str, name: str = "read", ts: int = 3) -> ToolResultMessage:
    return ToolResultMessage(
        toolCallId=call_id,
        toolName=name,
        content=[TextContent(type="text", text="ok")],
        isError=False,
        timestamp=ts,
    )


def call(id: str, name: str = "read") -> ToolCall:
    return ToolCall(id=id, name=name, arguments={})


async def make_session(backend: str, tmp_path: Path) -> SessionImpl:
    storage = (
        MemoryStorage()
        if backend == "memory"
        else await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    )
    session = SessionImpl(storage)
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    return session


# --------------------------------------------------------------------------------------
# 1a. order_tool_results
# --------------------------------------------------------------------------------------


def test_order_tool_results_places_results_after_their_assistant() -> None:
    messages = [
        user("hi"),
        assistant(calls=[call("c1"), call("c2")]),
        tool_result("c2"),
        tool_result("c1"),
    ]
    ordered = order_tool_results(messages)
    # 结果按**调用序**排（c1 在 c2 之前），而不是它们的到达序
    assert [_role(m) for m in ordered] == ["user", "assistant", "toolResult", "toolResult"]
    assert [_get(m, "tool_call_id") for m in ordered[2:]] == ["c1", "c2"]


def test_order_tool_results_synthesizes_a_missing_result() -> None:
    """缺失的结果**合成一条**——而且它是 ``isError``，带固定文本与 ``missing_result`` 原因。"""
    messages = [assistant(calls=[call("c1"), call("c2")]), tool_result("c1")]
    ordered = order_tool_results(messages)

    assert [_role(m) for m in ordered] == ["assistant", "toolResult", "toolResult"]
    assert _get(ordered[1], "tool_call_id") == "c1"
    # ⚠️ 合成的那条是 **dict**（与从 JSON 读回来的消息同形状），不是 pydantic 对象
    missing = ordered[2]
    assert isinstance(missing, dict)
    assert missing["tool_call_id"] == "c2"
    assert missing["is_error"] is True
    assert missing["details"] == {"reason": "missing_result"}
    assert missing["content"][0]["text"] == MISSING_RESULT_TEXT
    assert missing["tool_name"] == "read"
    assert missing["timestamp"] == 2  # 助手消息的时间戳


def test_order_tool_results_drops_unmatched_results() -> None:
    """没匹配上的结果**被丢弃**。"""
    messages = [assistant(calls=[call("c1")]), tool_result("c1"), tool_result("orphan")]
    ordered = order_tool_results(messages)
    assert [_get(m, "tool_call_id") for m in ordered if _role(m) == "toolResult"] == ["c1"]


def test_order_tool_results_only_looks_before_the_next_assistant() -> None:
    """结果只取自「下一条助手消息之前」。"""
    messages = [
        assistant(calls=[call("c1")], ts=1),
        assistant(text="later", ts=2),
        tool_result("c1"),
    ]
    ordered = order_tool_results(messages)
    # c1 的结果在**第二条助手之后**，所以对它来说算缺失 → 合成
    assert ordered[1]["is_error"] is True
    assert ordered[1]["content"][0]["text"] == MISSING_RESULT_TEXT
    # 那条真实结果没被任何**助手**看到（它在第二条助手之后），于是被当作孤儿**丢掉**
    assert [_role(m) for m in ordered] == ["assistant", "toolResult", "assistant"]
    assert _get(ordered[1], "tool_call_id") == "c1"


def test_order_tool_results_keeps_messages_without_calls() -> None:
    messages = [user("hi"), assistant("no calls")]
    assert order_tool_results(messages) == messages


def test_order_tool_results_passes_through_non_assistant_non_result() -> None:
    messages = [user("a"), user("b")]
    assert [_role(m) for m in order_tool_results(messages)] == ["user", "user"]


# --------------------------------------------------------------------------------------
# 1b. lead_with_system
# --------------------------------------------------------------------------------------


def system(text: str = "sys") -> Any:
    from pi_ai import SystemMessage

    return SystemMessage(content=[TextContent(type="text", text=text)], timestamp=0)


def test_lead_with_system_moves_it_after_only_user_messages() -> None:
    """输入先提交、system 提示后渲染，所以要把它提上来。"""
    messages = [user("hi"), system("s"), user("next")]
    led = lead_with_system(messages, baseline=None)
    assert led[0].role == "system"
    assert [_role(m) for m in led[1:]] == ["user", "user"]


def test_lead_with_system_moves_the_baseline_even_after_an_assistant() -> None:
    """**基线**即使前面有助手消息也要提上来。"""
    baseline = system("baseline")
    messages = [assistant("a"), baseline, user("u")]
    led = lead_with_system(messages, baseline=baseline)
    assert led[0] is baseline


def test_lead_with_system_leaves_a_non_baseline_after_an_assistant() -> None:
    """**非基线**的 system 消息前面有助手消息时**留在原地**——挪它会改变它对模型的含义。"""
    messages = [assistant("a"), system("s"), user("u")]
    led = lead_with_system(messages, baseline=None)
    assert [_role(m) for m in led] == ["assistant", "system", "user"]


def test_lead_with_system_is_a_noop_when_already_first() -> None:
    messages = [system("s"), user("u")]
    assert lead_with_system(messages, baseline=None) == messages


def test_lead_with_system_is_a_noop_without_a_system_message() -> None:
    messages = [user("u"), assistant("a")]
    assert lead_with_system(messages, baseline=None) == messages


# --------------------------------------------------------------------------------------
# 2. capture_context_bounds
# --------------------------------------------------------------------------------------


async def test_capture_context_bounds_returns_none_for_an_empty_conversation() -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]

    async def change(tx: Any) -> None:
        bounds = await capture_context_bounds(tx._host.storage, 1, CTX)  # noqa: SLF001
        assert bounds is None

    await session.commit(change, CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_capture_context_bounds_uses_the_newest_entry_as_tail(
    backend: str, tmp_path: Path
) -> None:
    session = await make_session(backend, tmp_path)
    try:
        first = await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.user"}), CTX)
        second = await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.user"}), CTX)

        captured: list[Any] = []

        async def change(tx: Any) -> None:
            captured.append(await capture_context_bounds(tx._host.storage, 1, CTX))  # noqa: SLF001

        await session.commit(change, CTX)
        bounds = captured[0]
        assert bounds.tail == second["id"]  # 最新可见条目
        assert first["id"] < second["id"]
    finally:
        await session.close(CTX)


async def test_capture_context_bounds_honours_an_explicit_at() -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    first = await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.user"}), CTX)
    await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.user"}), CTX)

    captured: list[Any] = []

    async def change(tx: Any) -> None:
        captured.append(await capture_context_bounds(tx._host.storage, 1, CTX, first["id"]))  # noqa: SLF001

    await session.commit(change, CTX)
    assert captured[0].tail == first["id"]


async def test_capture_context_bounds_rejects_an_invisible_entry() -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]

    async def change(tx: Any) -> None:
        with pytest.raises(RuntimeError, match="is not visible from conversation"):
            await capture_context_bounds(tx._host.storage, 1, CTX, 999_999)  # noqa: SLF001

    await session.commit(change, CTX)


async def test_capture_context_bounds_finds_the_head_marker() -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.user"}), CTX)
    marker = await session.commit(
        lambda tx: tx.append_entry(1, {"kind": "pi.reset", "head": "self"}), CTX
    )

    captured: list[Any] = []

    async def change(tx: Any) -> None:
        captured.append(await capture_context_bounds(tx._host.storage, 1, CTX))  # noqa: SLF001

    await session.commit(change, CTX)
    assert captured[0].head["id"] == marker["id"]


# --------------------------------------------------------------------------------------
# 3. 增量读 read_context_from —— 三条分支
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_added_tail_extends_the_range(backend: str, tmp_path: Path) -> None:
    """``tail`` **变大**：只扫 tail 之后的新条目，范围被**扩展**（不是重算）。"""
    session = await make_session(backend, tmp_path)
    try:
        await session.commit(
            lambda tx: tx.append_entry(1, {"kind": "pi.user", "model": [user("1").model_dump()]}),
            CTX,
        )

        view1, range1 = await read_context_from(session, session._storage, 1, CTX, None, None)  # noqa: SLF001
        assert len(view1["entries"]) == 1
        assert range1 is not None

        await session.commit(
            lambda tx: tx.append_entry(1, {"kind": "pi.user", "model": [user("2").model_dump()]}),
            CTX,
        )
        view2, range2 = await read_context_from(session, session._storage, 1, CTX, None, range1)  # noqa: SLF001

        assert len(view2["entries"]) == 2
        assert range2 is not None
        # 扩展分支保留**同一个**已结算前缀对象（没有整段重算）
        assert range2.settled == range1.settled
        assert len(range2.open) >= len(range1.open)
    finally:
        await session.close(CTX)


async def test_unchanged_tail_reuses_the_previous_range() -> None:
    """``tail`` **不变**：直接复用 ``previous``（连视图都不重算）。"""
    session = await make_session("memory", None)  # type: ignore[arg-type]
    await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.user"}), CTX)

    _, range1 = await read_context_from(session, session._storage, 1, CTX, None, None)  # noqa: SLF001
    view2, range2 = await read_context_from(session, session._storage, 1, CTX, None, range1)  # noqa: SLF001

    assert range2 is range1  # **同一个对象**
    # 但视图的数组是**自己的副本**（范围会活过这次读）
    assert view2["entries"] is not range1.view["entries"]


async def test_smaller_tail_truncates_without_rescanning() -> None:
    """``tail`` **变小**（``at`` 指向更早的条目）：从缓存的条目里**截断**，不重扫。"""
    session = await make_session("memory", None)  # type: ignore[arg-type]
    first = await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.user"}), CTX)
    await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.user"}), CTX)

    _, range2 = await read_context_from(session, session._storage, 1, CTX, None, None)  # noqa: SLF001
    assert len(range2.entries) == 2  # type: ignore[union-attr]

    view1, range1 = await read_context_from(
        session,
        session._storage,
        1,
        CTX,
        first["id"],
        range2,  # noqa: SLF001
    )
    assert len(view1["entries"]) == 1
    assert range1 is not range2
    assert range1.bounds.tail == first["id"]  # type: ignore[union-attr]


async def test_a_new_head_marker_derives_the_whole_range_again() -> None:
    """head 标记一变，就必须**整段重算**——head 会改变哪些条目是活动的。"""
    session = await make_session("memory", None)  # type: ignore[arg-type]
    await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.user"}), CTX)
    _, range1 = await read_context_from(session, session._storage, 1, CTX, None, None)  # noqa: SLF001
    assert range1 is not None and range1.bounds.head is None

    await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.reset", "head": "self"}), CTX)
    view2, range2 = await read_context_from(session, session._storage, 1, CTX, None, range1)  # noqa: SLF001

    assert range2 is not range1
    assert range2.bounds.head is not None  # type: ignore[union-attr]
    # head 标记本身排在活动条目**最前**
    assert view2["entries"][0]["id"] == range2.bounds.head["id"]  # type: ignore[union-attr]


async def test_an_edit_in_an_added_entry_forces_a_full_rederive() -> None:
    """新条目带了编辑 → 可能改到**更早**的条目 → 必须整段重算（``extend_range`` 的例外分支）。"""
    session = await make_session("memory", None)  # type: ignore[arg-type]
    first = await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.user"}), CTX)
    _, range1 = await read_context_from(session, session._storage, 1, CTX, None, None)  # noqa: SLF001

    await session.commit(
        lambda tx: tx.append_entry(
            1,
            {"kind": "pi.user", "edits": [{"action": "omit", "target": first["id"]}]},
        ),
        CTX,
    )
    view2, range2 = await read_context_from(session, session._storage, 1, CTX, None, range1)  # noqa: SLF001

    assert range2 is not range1
    # 那条被 omit 的条目贡献为空
    assert view2["contributions"][0] == []


async def test_read_context_from_without_bounds_returns_an_empty_view() -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    view, range_ = await read_context_from(session, session._storage, 1, CTX, None, None)  # noqa: SLF001
    assert view == {"head": None, "entries": [], "contributions": [], "messages": []}
    assert range_ is None


# --------------------------------------------------------------------------------------
# 派生语义
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_derive_context_orders_tool_results(backend: str, tmp_path: Path) -> None:
    session = await make_session(backend, tmp_path)
    try:
        await session.commit(
            lambda tx: tx.append_entry(1, {"kind": "pi.user", "model": [user("hi").model_dump()]}),
            CTX,
        )
        await session.commit(
            lambda tx: tx.append_entry(
                1,
                {
                    "kind": "pi.assistant",
                    "model": [assistant(calls=[call("c1")]).model_dump()],
                },
            ),
            CTX,
        )
        await session.commit(
            lambda tx: tx.append_entry(
                1, {"kind": "pi.tool-result", "model": [tool_result("c1").model_dump()]}
            ),
            CTX,
        )

        view = await read_context(session, session._storage, 1, CTX)  # noqa: SLF001
        roles = [_role(m) for m in view["messages"]]
        assert roles == ["user", "assistant", "toolResult"]
        assert _get(view["messages"][2], "tool_call_id") == "c1"
    finally:
        await session.close(CTX)


async def test_excluded_stop_reasons_do_not_contribute() -> None:
    """``aborted`` / ``error`` 的助手消息不进上下文。"""
    session = await make_session("memory", None)  # type: ignore[arg-type]
    await session.commit(
        lambda tx: tx.append_entry(
            1, {"kind": "pi.assistant", "model": [assistant("gone", stop="aborted").model_dump()]}
        ),
        CTX,
    )
    view = await read_context(session, session._storage, 1, CTX)  # noqa: SLF001
    assert view["messages"] == []
    assert view["contributions"] == [[]]


async def test_an_omit_edit_contributes_nothing() -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    first = await session.commit(
        lambda tx: tx.append_entry(1, {"kind": "pi.user", "model": [user("hi").model_dump()]}), CTX
    )
    await session.commit(
        lambda tx: tx.append_entry(
            1, {"kind": "pi.user", "edits": [{"action": "omit", "target": first["id"]}]}
        ),
        CTX,
    )
    view = await read_context(session, session._storage, 1, CTX)  # noqa: SLF001
    assert view["contributions"][0] == []


async def test_the_newest_edit_wins() -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    target = await session.commit(
        lambda tx: tx.append_entry(1, {"kind": "pi.user", "model": [user("orig").model_dump()]}),
        CTX,
    )
    await session.commit(
        lambda tx: tx.append_entry(
            1,
            {
                "kind": "pi.user",
                "edits": [
                    {
                        "action": "replace",
                        "target": target["id"],
                        "messages": [user("first").model_dump()],
                    }
                ],
            },
        ),
        CTX,
    )
    await session.commit(
        lambda tx: tx.append_entry(
            1,
            {
                "kind": "pi.user",
                "edits": [
                    {
                        "action": "replace",
                        "target": target["id"],
                        "messages": [user("second").model_dump()],
                    }
                ],
            },
        ),
        CTX,
    )
    view = await read_context(session, session._storage, 1, CTX)  # noqa: SLF001
    assert view["contributions"][0][0]["content"][0]["text"] == "second"


async def test_active_entries_returns_raw_entries() -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.user"}), CTX)
    marker = await session.commit(
        lambda tx: tx.append_entry(1, {"kind": "pi.reset", "head": "self"}), CTX
    )

    async def change(tx: Any) -> None:
        bounds = await capture_context_bounds(tx._host.storage, 1, CTX)  # noqa: SLF001
        entries = await active_entries(tx._host.storage, 1, bounds, CTX)  # noqa: SLF001
        # head 标记排最前，且只出现一次
        assert entries[0]["id"] == marker["id"]
        assert [e["id"] for e in entries].count(marker["id"]) == 1

    await session.commit(change, CTX)


async def test_derive_context_without_bounds_is_empty() -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    view = await derive_context(session._storage, 1, None, CTX)  # noqa: SLF001
    assert view["messages"] == []


def test_freeze_json_is_a_documented_passthrough() -> None:
    """偏离 #51：Python 没有 ``Object.freeze`` 的对等物，只保证解除关联。

    这不是桩——它是有意的语义偏离，理由见函式文档。
    """
    value = {"a": [1, 2]}
    assert freeze_json(value) is value
