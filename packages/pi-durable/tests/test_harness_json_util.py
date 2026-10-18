"""批次 D 第 a 步的测试：``harness/json.py``(31) + ``harness/util.py``(61)。

这两个文件**没有任何运行时依赖**（只依赖 chord 与批次 B 的 types），所以是批次 D 里
最独立的一块，先做。
"""

from __future__ import annotations

import asyncio

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_chord.context import AbortController, with_abort_signal
from pi_durable.errors import SessionFailed
from pi_durable.harness.json import assign_json
from pi_durable.harness.util import Waiters, closed_error, scan_all

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------------------
# assign_json：逐叶写入
# --------------------------------------------------------------------------------------


async def test_assign_json_writes_a_scalar_leaf() -> None:
    target: dict[str, object] = {"a": 1}
    assign_json(target, "a", 2)
    assert target == {"a": 2}


async def test_assign_json_skips_an_unchanged_leaf() -> None:
    """值相同就**不写**——这是这个函数存在的理由：少写一次就少一次 set 操作。"""
    target: dict[str, object] = {"a": 1}
    assign_json(target, "a", 1)
    assert target == {"a": 1}


async def test_assign_json_recurses_into_objects_and_deletes_missing_keys() -> None:
    """逐叶写入是**完整替换**的语义：``value`` 里没有的键要删掉。"""
    target = {"m": {"keep": 1, "drop": 2, "also": 3}}
    assign_json(target, "m", {"keep": 9, "added": 4})
    assert target == {"m": {"keep": 9, "added": 4}}


async def test_assign_json_keeps_object_identity_when_recursing() -> None:
    """递归时**原地改**内层对象，不换引用——否则又会退化成一次整体 set。"""
    inner = {"n": 1}
    target = {"m": inner}
    assign_json(target, "m", {"n": 2})
    assert target["m"] is inner
    assert inner == {"n": 2}


async def test_assign_json_updates_arrays_in_place_and_appends_extras() -> None:
    """``len(current) <= len(value)``：按下标递归，多出来的 **append**（不是整体替换）。"""
    items: list[object] = [{"v": 1}, {"v": 2}]
    target = {"rows": items}
    assign_json(target, "rows", [{"v": 10}, {"v": 20}, {"v": 30}])

    assert target["rows"] is items  # 同一个数组
    assert items == [{"v": 10}, {"v": 20}, {"v": 30}]


async def test_assign_json_replaces_a_shorter_array_wholesale() -> None:
    """``len(current) > len(value)``：不能原地缩，整体替换。"""
    target: dict[str, object] = {"rows": [1, 2, 3]}
    assign_json(target, "rows", [9])
    assert target == {"rows": [9]}


async def test_assign_json_treats_a_type_change_as_a_write() -> None:
    target: dict[str, object] = {"m": {"n": 1}}
    assign_json(target, "m", [1, 2])
    assert target == {"m": [1, 2]}

    other: dict[str, object] = {"m": [1]}
    assign_json(other, "m", {"n": 1})
    assert other == {"m": {"n": 1}}


async def test_assign_json_writes_list_slots_by_index() -> None:
    target: list[object] = [{"v": 1}]
    assign_json(target, 0, {"v": 2})
    assert target == [{"v": 2}]


async def test_assign_json_handles_a_tool_named_proto() -> None:
    """``__proto__`` 这类键在 JS 上是原型陷阱；Python 的 dict 里就是普通键。"""
    target: dict[str, object] = {}
    assign_json(target, "__proto__", {"polluted": False})
    assert target == {"__proto__": {"polluted": False}}
    assert "__proto__" in target


async def test_assign_json_nested_append_keeps_the_inner_identity() -> None:
    """嵌套：外层对象原地改，内层数组也原地改并 append。"""
    inner_rows: list[object] = [{"v": 1}]
    target: dict[str, object] = {"msg": {"rows": inner_rows, "role": "assistant"}}
    assign_json(target, "msg", {"rows": [{"v": 1}, {"v": 2}]})

    msg = target["msg"]
    assert isinstance(msg, dict)
    assert msg["rows"] is inner_rows
    assert inner_rows == [{"v": 1}, {"v": 2}]


# --------------------------------------------------------------------------------------
# Waiters
# --------------------------------------------------------------------------------------


async def test_waiters_resolve_delivers_to_every_waiter() -> None:
    waiters: Waiters[str, int] = Waiters()
    first = waiters.add("k", CTX)
    second = waiters.add("k", CTX)
    assert waiters.keys() == ["k"]

    waiters.resolve("k", 42)
    assert await first == 42
    assert await second == 42
    assert waiters.keys() == []  # 这一组消失了


async def test_waiters_resolve_settles_each_waiter_once() -> None:
    waiters: Waiters[str, int] = Waiters()
    pending = waiters.add("k", CTX)
    waiters.resolve("k", 1)
    waiters.resolve("k", 2)  # 第二次没有等待者，是空操作
    assert await pending == 1


async def test_waiters_reject_all() -> None:
    waiters: Waiters[str, int] = Waiters()
    first = waiters.add("a", CTX)
    second = waiters.add("b", CTX)

    error = RuntimeError("harness closed")
    waiters.reject_all(error)
    for pending in (first, second):
        with pytest.raises(RuntimeError, match="harness closed"):
            await pending
    assert waiters.keys() == []


async def test_waiters_are_grouped_by_key() -> None:
    waiters: Waiters[str, int] = Waiters()
    a = waiters.add("a", CTX)
    b = waiters.add("b", CTX)

    waiters.resolve("a", 1)
    assert await a == 1
    assert not b.done()
    waiters.resolve("b", 2)
    assert await b == 2


async def test_waiters_reject_immediately_on_an_aborted_context() -> None:
    """已取消的上下文一进来就拒绝（``util.ts:11``）。"""
    from pi_chord.context import AbortError

    waiters: Waiters[str, int] = Waiters()
    controller = AbortController()
    controller.abort()

    pending = waiters.add("k", with_abort_signal(controller.signal, CTX))
    with pytest.raises(AbortError):
        await pending
    assert waiters.keys() == []  # 没有登记


async def test_waiters_drop_on_cancellation_while_pending() -> None:
    from pi_chord.context import AbortError

    waiters: Waiters[str, int] = Waiters()
    controller = AbortController()
    pending = waiters.add("k", with_abort_signal(controller.signal, CTX))
    assert waiters.keys() == ["k"]

    controller.abort()
    with pytest.raises(AbortError):
        await pending
    assert waiters.keys() == []  # 取消时从组里摘掉，空组也删掉


async def test_waiters_cancellation_keeps_other_keys() -> None:
    from pi_chord.context import AbortError

    waiters: Waiters[str, int] = Waiters()
    controller = AbortController()
    keep = waiters.add("keep", CTX)
    drop = waiters.add("drop", with_abort_signal(controller.signal, CTX))

    controller.abort()
    with pytest.raises(AbortError):
        await drop
    assert waiters.keys() == ["keep"]
    waiters.resolve("keep", 7)
    assert await keep == 7


# --------------------------------------------------------------------------------------
# scan_all / closed_error
# --------------------------------------------------------------------------------------


async def test_scan_all_follows_every_cursor() -> None:
    pages = [
        {"items": [1, 2], "next": {"after": 2}},
        {"items": [3], "next": {"after": 3}},
        {"items": [4]},
    ]
    seen: list[object] = []

    async def scan(cursor: object) -> object:
        seen.append(cursor)
        return pages[len(seen) - 1]

    assert await scan_all(scan) == [1, 2, 3, 4]
    assert seen == [None, {"after": 2}, {"after": 3}]


async def test_scan_all_returns_empty_for_an_empty_first_page() -> None:
    async def scan(cursor: object) -> object:
        return {"items": []}

    assert await scan_all(scan) == []


async def test_closed_error_reports_plain_close() -> None:
    class Plain:
        failure = None

    error = closed_error(Plain())
    assert isinstance(error, RuntimeError)
    assert str(error) == "Harness is closed"


async def test_closed_error_carries_the_session_cause() -> None:
    """Session 因失败而关闭时，Harness 以**带根因的** ``SessionFailed`` 拒绝。"""
    cause = RuntimeError("storage died")

    class Failed:
        failure = {"error": cause}

    error = closed_error(Failed())
    assert isinstance(error, SessionFailed)
    # 根因在 `__cause__` 上（上游是 `new Error(msg, { cause })`），不在消息里——
    # 消息是固定的「close and reopen it」。
    assert error.__cause__ is cause


async def test_waiters_work_under_concurrent_waiters() -> None:
    """多个 waiter 并发等待同一个 key，全部拿到同一个值。"""
    waiters: Waiters[str, int] = Waiters()
    pendings = [waiters.add("k", CTX) for _ in range(5)]
    await asyncio.sleep(0)
    waiters.resolve("k", 99)
    assert await asyncio.gather(*pendings) == [99] * 5
