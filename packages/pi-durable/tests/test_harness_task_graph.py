"""``harness/task_graph.py`` 的测试 —— 移植 ``harness/task-graph.ts``（236 行）。

任务图是 Session 里**每个活任务**的只读视图（spec §9.5）：状态、阶段、等待的依赖、拥有的对话。
它建在 Session 线上、靠**提交发布**推进。

内存与真实 SQLite 各跑一遍。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.harness.task_graph import (
    LIVE_STATUSES,
    TaskGraphView,
    advance,
)
from pi_durable.harness.task_graph import (
    _Mount as Mount,
)
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage
from pi_durable.tasks import define_task


@dataclass
class DemoDefinition:
    name: str = "demo"
    version: int = 1
    phase: str = "ready"

    def initial(self, input: Any) -> dict[str, Any]:
        return {"phase": self.phase}


async def make_session(backend: str, tmp_path: Path) -> SessionImpl:
    storage = (
        MemoryStorage()
        if backend == "memory"
        else await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    )
    session = SessionImpl(storage)
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    return session


async def add_task(session: SessionImpl, **options: Any) -> int:
    task = define_task(
        DemoDefinition(**{k: v for k, v in options.items() if k in ("name", "phase")})
    )
    opts: dict[str, Any] = {"ownership": {"kind": "conversation"}, "conversationId": 1}
    opts.update({k: v for k, v in options.items() if k not in ("name", "phase")})
    return await session.commit(lambda tx: tx.create_task(task, {}, opts), CTX)


# --------------------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_empty_graph(backend: str, tmp_path: Path) -> None:
    session = await make_session(backend, tmp_path)
    try:
        view = TaskGraphView(session, session._storage)  # noqa: SLF001
        state = await view.state(CTX)
        assert state.value == {"tasks": {}}
        state.dispose()
    finally:
        await session.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_graph_lists_live_tasks_by_decimal_id(backend: str, tmp_path: Path) -> None:
    session = await make_session(backend, tmp_path)
    try:
        first = await add_task(session, name="a")
        second = await add_task(session, name="b")

        view = TaskGraphView(session, session._storage)  # noqa: SLF001
        state = await view.state(CTX)
        tasks = state.value["tasks"]

        assert set(tasks) == {str(first), str(second)}
        assert tasks[str(first)]["kind"] == "a"
        assert tasks[str(first)]["state"] == {"status": "pending", "phase": "ready"}
        state.dispose()
    finally:
        await session.close(CTX)


async def test_graph_excludes_terminal_tasks() -> None:
    """⚠️ **终局任务离开图**——``live_statuses`` 不含 ``terminal``。"""
    assert "terminal" not in LIVE_STATUSES
    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]
    id = await add_task(session)

    async def finish(tx: Any) -> None:
        tx.set_task(
            {
                "id": id,
                "conversationId": 1,
                "kind": "demo",
                "version": 1,
                "input": {},
                "state": {"status": "terminal", "outcome": {"status": "completed", "result": {}}},
                "background": False,
                "abortRequested": False,
            }
        )

    await session.commit(finish, CTX)

    view = TaskGraphView(session, session._storage)  # noqa: SLF001
    state = await view.state(CTX)
    assert state.value["tasks"] == {}


async def test_graph_records_waiting_state_without_payloads() -> None:
    """``waiting`` 保留阶段、依赖与策略——**但不含检查点负载与结果**。"""
    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]
    owner = await add_task(session, name="owner")
    waiter = await add_task(session, name="waiter")

    async def wait(tx: Any) -> None:
        tx.set_task(
            {
                "id": waiter,
                "conversationId": 1,
                "kind": "waiter",
                "version": 1,
                "input": {},
                "owner": owner,
                "state": {
                    "status": "waiting",
                    "checkpoint": {"phase": "next"},
                    "on": [owner],
                    "policy": "allSettled",
                },
                "background": False,
                "abortRequested": False,
            }
        )

    await session.commit(wait, CTX)

    view = TaskGraphView(session, session._storage)  # noqa: SLF001
    state = await view.state(CTX)
    node = state.value["tasks"][str(waiter)]

    assert node["state"] == {
        "status": "waiting",
        "phase": "next",
        "on": [owner],
        "policy": "allSettled",
    }
    assert node["owner"] == owner
    # 负载不进图
    assert "checkpoint" not in node["state"]
    assert "outcome" not in node["state"]


async def test_graph_records_completing_outcome_status_only() -> None:
    """``completing`` 只带**结果状态**，不带结果本身。"""
    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]
    id = await add_task(session)

    async def completing(tx: Any) -> None:
        tx.set_task(
            {
                "id": id,
                "conversationId": 1,
                "kind": "demo",
                "version": 1,
                "input": {},
                "state": {
                    "status": "completing",
                    "outcome": {"status": "failed", "error": {"message": "held"}},
                },
                "background": False,
                "abortRequested": False,
            }
        )

    await session.commit(completing, CTX)

    view = TaskGraphView(session, session._storage)  # noqa: SLF001
    state = await view.state(CTX)
    assert state.value["tasks"][str(id)]["state"] == {"status": "completing", "outcome": "failed"}


# --------------------------------------------------------------------------------------
# 推进
# --------------------------------------------------------------------------------------


async def test_advance_tracks_a_task_through_its_lifecycle() -> None:
    """图靠**提交发布**推进：pending → running →（离开图）。"""
    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]
    view = TaskGraphView(session, session._storage)  # noqa: SLF001
    state = await view.state(CTX)

    id = await add_task(session)
    node = state.value["tasks"][str(id)]
    assert node["state"] == {"status": "pending", "phase": "ready"}

    async def running(tx: Any) -> None:
        tx.set_task(
            {
                "id": id,
                "conversationId": 1,
                "kind": "demo",
                "version": 1,
                "input": {},
                "state": {"status": "running", "checkpoint": {"phase": "effect"}},
                "background": False,
                "abortRequested": False,
            }
        )

    await session.commit(running, CTX)
    assert state.value["tasks"][str(id)]["state"] == {"status": "running", "phase": "effect"}

    async def done(tx: Any) -> None:
        tx.set_task(
            {
                "id": id,
                "conversationId": 1,
                "kind": "demo",
                "version": 1,
                "input": {},
                "state": {"status": "terminal", "outcome": {"status": "completed", "result": {}}},
                "background": False,
                "abortRequested": False,
            }
        )

    await session.commit(done, CTX)
    assert str(id) not in state.value["tasks"]  # 终局 → 离开图
    state.dispose()


async def test_advance_records_the_conversations_a_task_owns() -> None:
    """任务**拥有的对话**按 ID 序记进节点；同一次提交里创建的也能找到拥有者。"""
    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]
    view = TaskGraphView(session, session._storage)  # noqa: SLF001
    state = await view.state(CTX)

    owner = await add_task(session)

    async def owned(tx: Any) -> None:
        await tx.create_conversation({"kind": "task", "taskId": owner})
        await tx.create_conversation({"kind": "task", "taskId": owner})

    await session.commit(owned, CTX)
    conversations = state.value["tasks"][str(owner)]["conversations"]
    assert len(conversations) == 2
    assert conversations == sorted(conversations)  # 按 ID 序
    state.dispose()


def test_advance_ignores_non_task_changes() -> None:
    """只有 ``task`` 变更进图；对话变更只在**有拥有者**时更新 ``conversations``。"""
    mount = Mount({"tasks": {}})
    advance(
        mount,
        {
            "seq": 1,
            "changes": [
                {"type": "conversation", "value": {"id": 5}},  # 无拥有者 → 忽略
                {"type": "entry", "value": {"id": 6, "conversationId": 1}},
            ],
        },
        CTX,
        lambda error: None,
    )
    assert mount.value == {"tasks": {}}


def test_advance_reports_a_throwing_observer_and_still_delivers_to_others() -> None:
    """一个观察者抛出**只被报告**，其余照常拿到修订。"""
    mount = Mount({"tasks": {}})
    seen: list[Any] = []
    reported: list[Any] = []

    class Boom:
        def advance(self, value: Any, ops: Any, context: Any) -> None:
            raise RuntimeError("observer exploded")

    class Good:
        def advance(self, value: Any, ops: Any, context: Any) -> None:
            seen.append(value)

    mount.observers.add(Boom())
    mount.observers.add(Good())

    advance(
        mount,
        {
            "seq": 1,
            "changes": [
                {
                    "type": "task",
                    "value": {
                        "id": 3,
                        "conversationId": 1,
                        "kind": "demo",
                        "state": {"status": "pending", "checkpoint": {"phase": "ready"}},
                        "background": False,
                        "abortRequested": False,
                    },
                }
            ],
        },
        CTX,
        reported.append,
    )

    assert len(reported) == 1
    assert seen and "3" in seen[0]["tasks"]


def test_advance_skips_an_unchanged_node() -> None:
    """节点没变就**不产生操作**——所以也不会打扰观察者。"""
    record = {
        "id": 3,
        "conversationId": 1,
        "kind": "demo",
        "state": {"status": "pending", "checkpoint": {"phase": "ready"}},
        "background": False,
        "abortRequested": False,
    }
    mount = Mount({"tasks": {}})
    advance(mount, {"seq": 1, "changes": [{"type": "task", "value": record}]}, CTX, lambda e: None)
    before = mount.value

    delivered: list[Any] = []

    class Observer:
        def advance(self, value: Any, ops: Any, context: Any) -> None:
            delivered.append(ops)

    mount.observers.add(Observer())
    advance(mount, {"seq": 2, "changes": [{"type": "task", "value": record}]}, CTX, lambda e: None)

    assert mount.value is before  # 没换值
    assert delivered == []  # 没投递


# --------------------------------------------------------------------------------------
# state / watch
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_watch_follows_the_graph(backend: str, tmp_path: Path) -> None:
    import asyncio

    session = await make_session(backend, tmp_path)
    try:
        view = TaskGraphView(session, session._storage)  # noqa: SLF001
        watch = await view.watch(CTX)
        assert watch.value == {"tasks": {}}

        deliveries: list[Any] = []

        async def listener(value: Any, ops: Any, context: Any) -> None:
            deliveries.append((value, ops))

        watch.start(listener)
        id = await add_task(session)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert deliveries
        assert str(id) in watch.value["tasks"]
        await watch.stop()
    finally:
        await session.close(CTX)


async def test_watch_cancels_with_an_aborted_context() -> None:
    from pi_chord.context import AbortController, AbortError, with_abort_signal

    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]
    view = TaskGraphView(session, session._storage)  # noqa: SLF001
    controller = AbortController()
    controller.abort()

    with pytest.raises(AbortError):
        await view.watch(with_abort_signal(controller.signal, CTX))


async def test_state_is_disposable_and_the_mount_drops_with_the_last_observer() -> None:
    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]
    view = TaskGraphView(session, session._storage)  # noqa: SLF001

    first = await view.state(CTX)
    second = await view.state(CTX)
    assert view._mount is not None  # noqa: SLF001

    first.dispose()
    assert view._mount is not None  # noqa: SLF001 - 还有一个观察者

    second.dispose()
    assert view._mount is None  # noqa: SLF001 - 最后一个离开，挂载被丢弃


async def test_closing_the_session_closes_every_observer() -> None:
    import asyncio

    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]
    view = TaskGraphView(session, session._storage)  # noqa: SLF001
    watch = await view.watch(CTX)
    await add_task(session)

    await session.close(CTX)
    end = await asyncio.wait_for(watch.closed, 2)
    assert end["reason"] == "session_closed"


async def test_attaching_after_close_is_rejected() -> None:
    """⚠️ 已关闭的 Session 上附着，报的是 **``Session is closed``**（来自 ``read_on_line`` 的准入），
    **不是** ``Harness is closed``——上游也是这个顺序：``readOnLine`` 先 ``assertUsable()``。

    ``_attach`` 里那个 ``closed_error`` 是给**另一个**竞态用的：
    关闭在本挂载**构建期间**才开始。那时准入已经过了，所以要自己再查一次。
    """
    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]
    view = TaskGraphView(session, session._storage)  # noqa: SLF001
    await session.close(CTX)

    with pytest.raises(RuntimeError, match="Session is closed"):
        await view.state(CTX)
