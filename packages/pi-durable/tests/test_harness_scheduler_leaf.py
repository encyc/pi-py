"""``harness/scheduler.py`` 的 **Slice A**（模块级层）测试。

⚠️ 按**可测性**切：``TaskScheduler`` 是**63 个方法、约 1640 行**的一个类，
全部共享 ``#`` 私有状态——**按函数粒度切不开**（抽走任何一个都要连带抽走那套状态，
那是结构偏离）。所以本片只收**不碰那套状态**的模块级类型与纯函数。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pi_chord.context import AbortController, AbortError
from pi_durable.harness.scheduler import (
    LIVE_STATUSES,
    MAX_TIMER_DELAY,
    SCAN_PAGE_SIZE,
    TaskSchedulerOptions,
    add_to,
    can_reserve,
    cancellation_intent,
    conversation_node,
    conversation_of,
    delay,
    delete_from,
    ended_error,
    failed_outcome,
    memo_of,
    missing_migration,
    node_of,
    parent_node,
    parent_of,
    with_abort_mark,
    with_state,
)
from pi_durable.harness.types import RegistryReader

# --------------------------------------------------------------------------------------
# 常量与类型
# --------------------------------------------------------------------------------------


def test_constants() -> None:
    assert SCAN_PAGE_SIZE == 256
    assert MAX_TIMER_DELAY == 2_147_483_647
    assert LIVE_STATUSES == ("pending", "running", "waiting", "completing")


def test_registry_reader_has_both_methods() -> None:
    """⚠️ **按完整形状定义**（R28 情形二）：调度器与 ``TaskRuntime`` 都要它。"""
    assert {n for n in dir(RegistryReader) if not n.startswith("_")} == {"snapshot", "subscribe"}
    with pytest.raises(TypeError):
        RegistryReader()


def test_task_scheduler_options_is_frozen() -> None:
    import dataclasses

    options = TaskSchedulerOptions(
        **{f.name: None for f in dataclasses.fields(TaskSchedulerOptions)}
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        options.session = 1  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# 节点
# --------------------------------------------------------------------------------------


def test_conversation_node_round_trips() -> None:
    assert conversation_node(7) == "c7"
    assert conversation_of(conversation_node(7)) == 7
    assert conversation_of("c123") == 123


def test_parent_node_is_the_owner_or_the_conversation() -> None:
    assert parent_node({"conversationId": 3, "owner": 9, "background": False}) == 9
    assert parent_node({"conversationId": 3, "background": False}) == "c3"


def test_node_of_omits_an_absent_owner() -> None:
    """⚠️ 省略的键**整个不存在**（上游用条件展开；本包在构造处省略，R23）。"""
    assert node_of({"conversationId": 1, "background": True}) == {
        "conversationId": 1,
        "background": True,
    }
    assert node_of({"conversationId": 1, "background": False, "owner": 5})["owner"] == 5


def test_parent_of_mirrors_parent_node() -> None:
    assert parent_of({"conversationId": 3, "owner": 9, "background": False}) == {"task": 9}
    assert parent_of({"conversationId": 3, "background": False}) == {"conversation": 3}


# --------------------------------------------------------------------------------------
# 中止标记与取消意图
# --------------------------------------------------------------------------------------


def test_with_abort_mark_drops_a_stale_reason() -> None:
    """⚠️ 先把旧的 ``abortReason`` **整个删掉**再写新的。

    不删的话，一次「**不带原因**的重启标记」会**留着上一次的原因**——
    于是「为什么被抛弃」的显示会是过期的。
    """
    record = {"id": 1, "abortRequested": False, "abortReason": "old"}
    marked = with_abort_mark(record)
    assert marked["abortRequested"] is True
    assert "abortReason" not in marked  # 旧的被删掉了


def test_with_abort_mark_sets_the_new_reason() -> None:
    marked = with_abort_mark({"id": 1}, "restart")
    assert marked["abortReason"] == "restart"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"status": "completing", "outcome": {"status": "completed"}}, False),
        ({"status": "completing", "outcome": {"status": "failed"}}, True),
        ({"status": "terminal", "outcome": {"status": "aborted"}}, True),
        ({"status": "terminal", "outcome": {"status": "completed"}}, False),
        ({"status": "running"}, False),
    ],
)
def test_failed_outcome(state: dict[str, Any], expected: bool) -> None:
    assert failed_outcome({"state": state}) is expected


def test_cancellation_intent_covers_a_mark_or_a_failed_outcome() -> None:
    assert cancellation_intent({"state": {"status": "running"}, "abortRequested": True}) is True
    assert (
        cancellation_intent({"state": {"status": "completing", "outcome": {"status": "failed"}}})
        is True
    )
    assert cancellation_intent({"state": {"status": "running"}, "abortRequested": False}) is False


def test_a_terminal_task_is_never_a_cancellation_intent() -> None:
    """⚠️ ``status !== "terminal"`` 是**必要的**：终局任务本身不算取消意图。

    否则一个**正常失败的历史任务**会一直把它的子树当取消处理——
    那会让后来重新挂上去的工作被立刻中止。
    """
    record = {
        "state": {"status": "terminal", "outcome": {"status": "failed"}},
        "abortRequested": True,
    }
    assert cancellation_intent(record) is False


# --------------------------------------------------------------------------------------
# 状态与预留
# --------------------------------------------------------------------------------------


def test_with_state_keeps_memos_while_live() -> None:
    record = {"id": 1, "memos": {"a": 1}, "state": {"status": "pending"}}
    updated = with_state(record, {"status": "running"})
    assert updated["memos"] == {"a": 1}


def test_with_state_drops_memos_once_an_outcome_is_decided() -> None:
    """⚠️ **结果一旦定了，备忘就消失**——它们只在任务活着的时候有意义。"""
    record = {"id": 1, "memos": {"a": 1}, "state": {"status": "running"}}
    for status in ("completing", "terminal"):
        updated = with_state(record, {"status": status})
        assert "memos" not in updated, status


@pytest.mark.parametrize(
    ("version", "migrate", "current", "expected"),
    [
        (2, None, 2, True),  # 同版本
        (3, object(), 2, True),  # 更新 + 有迁移
        (3, None, 2, False),  # 更新但没迁移
        (1, None, 2, False),  # 更旧
    ],
)
def test_can_reserve(version: int, migrate: Any, current: int, expected: bool) -> None:
    task = type("T", (), {"definition": {"version": version, "migrate": migrate}})()
    assert can_reserve(task, {"version": current}) is expected


# --------------------------------------------------------------------------------------
# 备忘
# --------------------------------------------------------------------------------------


def test_memo_of_reads_only_own_entries() -> None:
    """⚠️ 上游注释明说：像 ``toString`` 这样的名字**不能**解析到继承来的属性。

    Python 的对应风险是魔术名——所以这里用 ``in`` 而**不是** ``getattr``。
    """
    assert memo_of({"memos": {"a": 1}}, "a") == 1
    assert memo_of({"memos": {}}, "a") is None
    assert memo_of(None, "a") is None
    assert memo_of({}, "a") is None


def test_memo_of_does_not_resolve_dunder_names() -> None:
    assert memo_of({}, "__class__") is None
    assert memo_of({"memos": {}}, "__len__") is None


# --------------------------------------------------------------------------------------
# 错误
# --------------------------------------------------------------------------------------


def test_missing_migration_names_both_versions() -> None:
    error = missing_migration({"kind": "demo", "version": 1}, {"version": 2})
    assert "demo" in str(error)
    assert "version 2" in str(error)
    assert "from 1" in str(error)


def test_ended_error_names_the_task() -> None:
    assert "Task 7 invocation has ended" in str(ended_error({"taskId": 7}))


# --------------------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------------------


def test_add_to_creates_the_set_on_first_use() -> None:
    index: dict[Any, set[Any]] = {}
    add_to(index, "a", 1)
    add_to(index, "a", 2)
    assert index == {"a": {1, 2}}


def test_delete_from_drops_an_empty_key() -> None:
    """⚠️ **集合空了就把键也删掉**——留着空集合会让「这个节点还有没有下层活」
    一直为真，于是它**永远不会被回收**。"""
    index: dict[Any, set[Any]] = {"a": {1}}
    delete_from(index, "a", 1)
    assert index == {}


def test_delete_from_keeps_other_members_and_missing_keys() -> None:
    index: dict[Any, set[Any]] = {"a": {1, 2}}
    delete_from(index, "a", 1)
    assert index == {"a": {2}}
    delete_from(index, "missing", 1)  # 不抛
    assert index == {"a": {2}}


# --------------------------------------------------------------------------------------
# delay
# --------------------------------------------------------------------------------------


async def test_delay_returns_after_the_wait() -> None:
    controller = AbortController()
    await asyncio.wait_for(delay(10, controller.signal), 2)


async def test_delay_rejects_with_the_signals_reason() -> None:
    """⚠️ 中止时**用信号自己的原因**拒绝——不是新造一个错误。"""
    controller = AbortController()
    controller.abort(AbortError("stop now"))

    with pytest.raises(AbortError, match="stop now"):
        await asyncio.wait_for(delay(5000, controller.signal), 2)


async def test_delay_rejects_at_once_for_an_already_aborted_signal() -> None:
    controller = AbortController()
    controller.abort()
    with pytest.raises(AbortError):
        await asyncio.wait_for(delay(5000, controller.signal), 2)


async def test_delay_removes_its_listener_after_the_wait() -> None:
    """⚠️ 超时要**移除监听器**、中止要**清掉定时器**——两边都做，否则长等待会攒监听器。"""
    controller = AbortController()
    signal = controller.signal
    before = len(getattr(signal, "_listeners", []) or [])
    await asyncio.wait_for(delay(5, signal), 2)
    after = len(getattr(signal, "_listeners", []) or [])
    assert after <= before


async def test_delay_of_zero_still_checks_the_signal() -> None:
    controller = AbortController()
    controller.abort()
    with pytest.raises(AbortError):
        await asyncio.wait_for(delay(0, controller.signal), 2)
