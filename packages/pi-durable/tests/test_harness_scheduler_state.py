"""``TaskScheduler`` 的**状态模型**与已交付方法的测试。

⚠️ **`TaskScheduler` 是「整类一次做」的交付单位**（63 方法 / 约 1640 行）——
本文件覆盖**已落地的那部分**：核心类型 + 40 个字段的状态模型 + `__init__` + `join`。
**未落地的方法根本不存在**（不留桩）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from typing import Any

import pytest

from pi_chord.context import AbortController
from pi_durable.harness.scheduler import (
    Invocation,
    Phase,
    Reservation,
    TaskScheduler,
    TaskSchedulerOptions,
)


def options(**over: Any) -> TaskSchedulerOptions:
    fields = {f.name: None for f in dataclasses.fields(TaskSchedulerOptions)}
    # `join` 要用到 controller.abort()，所以给个真的
    fields["now"] = lambda: 0.0
    fields["report"] = lambda error: None
    fields["context"] = None
    fields.update(over)
    return TaskSchedulerOptions(**fields)


# --------------------------------------------------------------------------------------
# 核心类型
# --------------------------------------------------------------------------------------


def test_invocation_carries_its_mode_and_ends_mutably() -> None:
    """⚠️ ``ended`` 是**可变的**：调用结束那一刻置位，之后任何操作都拒绝它。

    其余字段在构造时就定了——一次调用**不会换任务、换模式、换信号**。
    """
    invocation = Invocation(
        task_id=7,
        conversation_id=1,
        mode="run",
        controller=AbortController(),
        context=None,
    )
    assert invocation.ended is False
    invocation.ended = True
    assert invocation.ended is True
    assert invocation.watches == set()  # 默认空集合，不是共享的


def test_invocation_watch_sets_are_not_shared() -> None:
    """⚠️ 默认值必须是**每个实例一份**——共享一个 set 会让两个调用的观察互相干扰。"""
    first = Invocation(
        task_id=1, conversation_id=1, mode="run", controller=AbortController(), context=None
    )
    second = Invocation(
        task_id=2, conversation_id=1, mode="run", controller=AbortController(), context=None
    )
    first.watches.add("w")
    assert second.watches == set()


def test_phase_agent_starts_unresolved() -> None:
    """``agent`` 是**懒解析**的：每个阶段**至多一次**、在首次使用时解析。"""
    phase = Phase(snapshot=lambda: None, task=lambda: None)
    assert phase.agent is None


def test_reservation_is_frozen() -> None:
    reservation = Reservation(invocation=None, task=None, snapshot=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        reservation.task = 1  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# 状态模型
# --------------------------------------------------------------------------------------


def test_the_state_model_has_forty_fields() -> None:
    """**40 个字段**：13 个来自 options + 19 个状态容器 + 8 个标志。

    这个形状本身就是设计——它是**不可切**的根源（见 PORTING.md〈不可切的具体成因〉）。
    """
    scheduler = TaskScheduler(options())
    assert len(vars(scheduler)) == 40


def test_every_container_starts_empty_and_independent() -> None:
    """每个容器都是**独立的空实例**——共享可变默认值会让两个调度器互相污染。"""
    first = TaskScheduler(options())
    second = TaskScheduler(options())

    for name in ("_live", "_contexts", "_invocations", "_edges", "_settled", "_below", "_waiters"):
        assert getattr(first, name) == {}, name
    for name in (
        "_drop_queue",
        "_roots",
        "_unloaded",
        "_intent",
        "_runnable",
        "_finalize_checks",
        "_abandoned",
    ):
        assert getattr(first, name) == set(), name

    first._live[1] = {"id": 1}
    first._runnable.add(1)
    assert second._live == {}
    assert second._runnable == set()


def test_the_eight_flags_start_lowered() -> None:
    """调度器**一开始是不使能的**（``#enabled = false``）——``resume()`` 才打开它。

    这个默认很重要：开档本身**不派发任何东西**（``open`` 的文档明说），
    所以一个刚构造好的调度器不能开始跑任务。
    """
    scheduler = TaskScheduler(options())
    assert scheduler._enabled is False
    assert scheduler._closing is False
    assert scheduler._dirty is False
    assert scheduler._draining is False
    assert scheduler._reconcile_scheduled is False
    assert scheduler._sweep_scheduled is False
    assert scheduler._cascade_pending is False
    assert scheduler._unsubscribe_registry() is None  # 默认是个 no-op


def test_the_thirteen_capabilities_come_from_the_options() -> None:
    """调度器**不认识 Harness**：13 个能力全部以回调/对象的形式递进来。

    **这正是它能被单独构造与测试的前提。**
    """
    sentinel = object()
    scheduler = TaskScheduler(options(storage=sentinel, models=sentinel))
    assert scheduler._storage is sentinel
    assert scheduler._models is sentinel


# --------------------------------------------------------------------------------------
# join
# --------------------------------------------------------------------------------------


async def test_join_aborts_every_invocation_and_waits() -> None:
    """⚠️ Close 在**每一个关闭监听器都跑完之后**才调它——这样任务持有的等待、观察与流
    是以 **Session 的原因**结束的，而不是被它自己的信号当成取消。

    **这个顺序很重要**：反过来会让「Harness 关闭」在 transcript 里显示成「用户取消了任务」。
    """
    scheduler = TaskScheduler(options())
    controller = AbortController()
    done: asyncio.Future = asyncio.get_running_loop().create_future()
    invocation = Invocation(
        task_id=1,
        conversation_id=1,
        mode="run",
        controller=controller,
        context=None,
        done=done,
    )
    scheduler._invocations[1] = invocation

    waiting = asyncio.ensure_future(scheduler.join())
    await asyncio.sleep(0)
    assert controller.signal.aborted is True  # 信号发了
    assert not waiting.done()  # 还在等

    done.set_result(None)
    await asyncio.wait_for(waiting, 2)


async def test_join_without_invocations_returns_at_once() -> None:
    await asyncio.wait_for(TaskScheduler(options()).join(), 2)


async def test_join_is_not_a_noop_when_a_done_future_is_missing() -> None:
    """``done`` 缺失时 ``join`` **不能挂住**——它至少要把信号发出去。"""
    scheduler = TaskScheduler(options())
    invocation = Invocation(
        task_id=1,
        conversation_id=1,
        mode="run",
        controller=AbortController(),
        context=None,
    )
    invocation.done = None
    scheduler._invocations[1] = invocation
    await asyncio.wait_for(scheduler.join(), 2)
    assert invocation.controller.signal.aborted is True


# --------------------------------------------------------------------------------------
# 未落地的方法**根本不存在**
# --------------------------------------------------------------------------------------


def test_unimplemented_methods_do_not_exist_yet() -> None:
    """⚠️ 本轮的交付单位是**整类**，但只落地了一部分。

    按准则：**未做的方法根本不存在**（不留桩——不存在优于存在但抛）。
    所以这里断言「还不存在」，等它们落地时**这条测试会失败**，
    提醒把它改成「存在且可用」。
    """
    for name in ("open", "resume", "abort", "wait_for_task", "wait_for_idle", "abort_conversation"):
        assert not hasattr(TaskScheduler, name), f"{name} 尚未落地，不该存在"

    # 已落地的那些**都是真的**
    assert inspect.iscoroutinefunction(TaskScheduler.join)
    assert TaskScheduler.__init__ is not object.__init__
