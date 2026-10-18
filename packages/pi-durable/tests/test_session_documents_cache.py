"""阶段 3 第 4 步的测试：文档缓存逐出 + ``snapshot`` / ``document_state`` / ``watch_doc``。

逐出用**假时钟**做确定性测试；观察者用内存与真实 SQLite 各跑一遍。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.documents import define_doc
from pi_durable.session import SessionImpl
from pi_durable.session.session import (
    document_retention,
    set_default_document_retention,
)
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage

pytestmark = pytest.mark.asyncio

TOKEN = define_doc(
    {
        "kind": "pi.notes",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",
        "initial": lambda seed: {"n": 0},
    }
)


async def make_session(backend: str, tmp_path: Path, **kwargs: Any) -> Any:
    storage = (
        MemoryStorage()
        if backend == "memory"
        else await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    )
    return SessionImpl(storage, **kwargs)


async def create_doc(session: SessionImpl, n: int = 1) -> None:
    async def change(tx: Any) -> None:
        await tx.create_root_conversation()
        draft = await tx.doc(TOKEN, 1)
        draft["n"] = n

    await session.commit(change, CTX)


async def bump(session: SessionImpl, n: int) -> None:
    async def change(tx: Any) -> None:
        draft = await tx.doc(TOKEN, 1)
        draft["n"] = n

    await session.commit(change, CTX)


# --------------------------------------------------------------------------------------
# 保留期的合法化
# --------------------------------------------------------------------------------------


async def test_document_retention_normalizes() -> None:
    """``0`` 是有效值（立刻逐出），``inf`` 表示永不舍弃；负数 / 非数字 / ``None`` 用默认。"""
    assert document_retention(0) == 0
    assert document_retention(1500) == 1500
    assert document_retention(float("inf")) == float("inf")
    assert document_retention(-1) == 600_000  # 默认
    assert document_retention(None) == 600_000
    assert document_retention("100") == 600_000  # type: ignore[arg-type]


async def test_default_retention_is_process_wide_and_restorable() -> None:
    """⚠️ ``set_default_document_retention`` 是**进程级**状态（与上游模块级 ``let`` 一致）。

    所以用它做测试隔离时**必须还原**。本包自己的测试优先用每实例的
    ``document_retention_ms=``，只有这条测试碰全局开关。
    """
    original = document_retention(None)
    try:
        set_default_document_retention(1)
        assert document_retention(None) == 1
    finally:
        set_default_document_retention(original)
    assert document_retention(None) == original


async def test_per_instance_retention_does_not_touch_the_global() -> None:
    original = document_retention(None)
    session = SessionImpl(MemoryStorage(), document_retention_ms=5)
    assert session._document_retention_ms() == 5  # noqa: SLF001
    assert document_retention(None) == original


# --------------------------------------------------------------------------------------
# 逐出
# --------------------------------------------------------------------------------------


async def test_retention_zero_evicts_at_the_end_of_each_commit() -> None:
    """保留期为 ``0``：逐出排在写线上立刻执行，提交一结束就落。"""
    session = SessionImpl(MemoryStorage(), document_retention_ms=0)
    await create_doc(session)
    # 提交结束后再跑一次写线，让排上的逐出有机会执行
    await session.read_on_line(lambda: _noop())

    stats = session.cache_stats()
    assert stats["cached"] == 0
    assert stats["idle"] == 0
    assert stats["sweeps"] >= 1


async def test_idle_document_is_evicted_once_retention_passes() -> None:
    """假时钟：把时钟推过保留期，再触发一次扫除。"""
    now = [0.0]
    session = SessionImpl(MemoryStorage(), clock=lambda: now[0], document_retention_ms=100.0)
    await create_doc(session)
    assert session.cache_stats()["cached"] == 1

    now[0] = 50.0
    session._sweep_documents()  # noqa: SLF001
    assert session.cache_stats()["cached"] == 1  # 还没到期

    now[0] = 200.0
    session._sweep_documents()  # noqa: SLF001
    assert session.cache_stats()["cached"] == 0  # 到期了


async def test_an_attached_document_is_never_evicted() -> None:
    """有观察者附着的缓存化身**永不被逐出**——即使保留期早就过了。"""
    now = [0.0]
    session = SessionImpl(MemoryStorage(), clock=lambda: now[0], document_retention_ms=100.0)
    await create_doc(session)
    state = await session.document_state(TOKEN, 1, CTX)
    assert state is not None

    now[0] = 100_000.0
    session._sweep_documents()  # noqa: SLF001

    stats = session.cache_stats()
    assert stats["cached"] == 1  # 还在
    assert stats["attached"] == 1
    assert stats["idle"] == 0  # 附着期间**不在**空闲表里


async def test_detaching_makes_the_document_idle_again() -> None:
    now = [0.0]
    session = SessionImpl(MemoryStorage(), clock=lambda: now[0], document_retention_ms=100.0)
    await create_doc(session)
    state = await session.document_state(TOKEN, 1, CTX)
    assert state is not None

    state.dispose()
    stats = session.cache_stats()
    assert stats["attached"] == 0
    assert stats["idle"] == 1  # 重新变空闲

    now[0] = 100_000.0
    session._sweep_documents()  # noqa: SLF001
    assert session.cache_stats()["cached"] == 0


async def test_pinning_is_reference_counted() -> None:
    session = SessionImpl(MemoryStorage(), document_retention_ms=100.0)
    await create_doc(session)
    first = await session.document_state(TOKEN, 1, CTX)
    second = await session.document_state(TOKEN, 1, CTX)
    assert session.cache_stats()["attached"] == 1  # 同一个化身，计数 2

    first.dispose()  # type: ignore[union-attr]
    assert session.cache_stats()["attached"] == 1  # 还有一个

    second.dispose()  # type: ignore[union-attr]
    assert session.cache_stats()["attached"] == 0
    assert session.cache_stats()["idle"] == 1


async def test_infinite_retention_never_evicts() -> None:
    now = [0.0]
    session = SessionImpl(MemoryStorage(), clock=lambda: now[0], document_retention_ms=float("inf"))
    await create_doc(session)
    now[0] = 10**12
    session._sweep_documents()  # noqa: SLF001
    assert session.cache_stats()["cached"] == 1


async def test_hot_document_moves_back_at_most_once_per_granule() -> None:
    """粒度 = min(1s, 保留期/10)：粒度内反复使用**不**重排空闲表（``session.ts:604-609``）。"""
    now = [0.0]
    session = SessionImpl(MemoryStorage(), clock=lambda: now[0], document_retention_ms=10_000.0)
    await create_doc(session)
    first_touch = session._idle_documents['["pi.notes","conversation",1,null]']  # noqa: SLF001

    now[0] = 500.0  # 粒度是 1000ms，还在同一粒度内
    await bump(session, 2)
    assert (
        session._idle_documents['["pi.notes","conversation",1,null]']  # noqa: SLF001
        == first_touch
    )

    now[0] = 5_000.0  # 换了一个粒度
    await bump(session, 3)
    assert (
        session._idle_documents['["pi.notes","conversation",1,null]']  # noqa: SLF001
        == 5_000.0
    )


async def test_timers_degrade_when_there_is_no_running_loop() -> None:
    """**上游降级路径的 Python 对应**（``session.ts:614-658``）。

     上游的判据是「``setTimeout`` 的句柄没有 ``unref``」（Cloudflare Workers）。
     Python 的 ``asyncio.TimerHandle`` 没有 ``unref``，**但也
    不需要**——``asyncio.run()`` 在主协程结束后就关循环，挂起的定时器不会吊住进程。
     所以本包把判据换成 Python 侧**真实存在**的那一个：**当前没有运行中的事件循环**。

     后果：没有事件循环时，过期文档改由**每次提交结束**时的扫除回收，而不是按时回收。
     状态始终正确，只是内存回收晚一步。
    """
    session = SessionImpl(MemoryStorage(), clock=lambda: 0.0, document_retention_ms=100.0)
    assert session._document_timers_unavailable is False  # noqa: SLF001

    # 造一个「空闲文档」，然后在**事件循环之外**排一次逐出。
    # 用假时钟把 delay 顶成正数，走的才是「定时器」那条路而不是「立刻排」。
    session._idle_documents['["pi.notes","conversation",1,null]'] = 0.0  # noqa: SLF001

    def outside_a_loop() -> None:
        session._schedule_document_sweep()  # noqa: SLF001

    await __import__("asyncio").to_thread(outside_a_loop)
    assert session._document_timers_unavailable is True  # noqa: SLF001
    # 降级之后不再保留定时器
    assert session._document_sweep is None  # noqa: SLF001

    # 但逐出仍然会发生——由提交结束时的扫除兜底
    session._idle_documents['["pi.notes","conversation",1,null]'] = 0.0  # noqa: SLF001
    session._clock = lambda: 10_000.0  # noqa: SLF001
    session._sweep_documents()  # noqa: SLF001
    assert session.cache_stats()["cached"] == 0


async def _noop() -> None:
    return None


# --------------------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_snapshot_reads_a_committed_document(backend: str, tmp_path: Path) -> None:
    session = await make_session(backend, tmp_path)
    try:
        await create_doc(session, n=7)
        assert await session.snapshot(TOKEN, 1, CTX) == {"n": 7}
    finally:
        await session.close(CTX)


async def test_snapshot_returns_none_for_an_absent_document() -> None:
    session = SessionImpl(MemoryStorage())
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    assert await session.snapshot(TOKEN, 1, CTX) is None


async def test_snapshot_hits_the_cache_without_the_line() -> None:
    """命中缓存时 ``snapshot`` **不排队**（``session.ts:298-306``）。

    用一个占住写线的作业验证：如果 ``snapshot`` 排队，它会一直等下去。
    """
    import asyncio

    session = SessionImpl(MemoryStorage())
    await create_doc(session)
    gate = asyncio.Event()

    async def hold() -> None:
        await gate.wait()

    holder = session.read_on_line(hold)
    await asyncio.sleep(0)  # 让 holder 占住写线

    result = await asyncio.wait_for(session.snapshot(TOKEN, 1, CTX), 1)
    assert result == {"n": 1}

    gate.set()
    await holder


# --------------------------------------------------------------------------------------
# document_state
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_document_state_hydrates_and_follows_commits(backend: str, tmp_path: Path) -> None:
    session = await make_session(backend, tmp_path)
    try:
        await create_doc(session, n=1)
        state = await session.document_state(TOKEN, 1, CTX)
        assert state is not None
        assert state.value == {"n": 1}

        await bump(session, n=2)
        assert state.value == {"n": 2}
    finally:
        await session.close(CTX)


async def test_document_state_returns_none_for_an_absent_document() -> None:
    session = SessionImpl(MemoryStorage())
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    assert await session.document_state(TOKEN, 1, CTX) is None


# --------------------------------------------------------------------------------------
# watch_doc
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_watch_doc_delivers_deltas(backend: str, tmp_path: Path) -> None:
    import asyncio

    session = await make_session(backend, tmp_path)
    try:
        await create_doc(session, n=1)
        watch = await session.watch_doc(TOKEN, 1, CTX)
        assert watch is not None
        assert watch.value == {"n": 1}

        deliveries: list[Any] = []

        async def listener(value: Any, ops: Any, context: Any) -> None:
            deliveries.append((value, ops))

        watch.start(listener)
        await bump(session, n=2)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert deliveries
        assert deliveries[-1][0] == {"n": 2}
        assert watch.value == {"n": 2}
        await watch.stop()
    finally:
        await session.close(CTX)


async def test_watch_doc_returns_none_for_an_absent_document() -> None:
    session = SessionImpl(MemoryStorage())
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    assert await session.watch_doc(TOKEN, 1, CTX) is None


async def test_watch_doc_ends_on_session_close() -> None:
    import asyncio

    session = SessionImpl(MemoryStorage())
    await create_doc(session)
    watch = await session.watch_doc(TOKEN, 1, CTX)
    assert watch is not None

    await session.close(CTX)
    end = await asyncio.wait_for(watch.closed, 2)
    assert end["reason"] == "session_closed"


async def test_watch_doc_ends_with_session_failed() -> None:
    import asyncio

    session = SessionImpl(MemoryStorage())
    await create_doc(session)
    watch = await session.watch_doc(TOKEN, 1, CTX)
    assert watch is not None

    cause = RuntimeError("died")
    session.fail(cause)
    end = await asyncio.wait_for(watch.closed, 2)
    assert end["reason"] == "session_failed"
    # 「同一错误」：拿到的是**根因本身**，不是包裹它的失败记录
    assert end["error"] is cause


async def test_watch_doc_ends_when_the_document_retires() -> None:
    import asyncio

    session = SessionImpl(MemoryStorage())
    await create_doc(session)
    watch = await session.watch_doc(TOKEN, 1, CTX)
    assert watch is not None

    # ⚠️ **必须 start**：观察只有在被投递时才排空，而「退休」这个终态是由
    # **投递 null 之后**才产生的（``observation.ts:266-269``）。没 start 的话
    # 帧一直躺在待投递队列里，``closed`` 不会落定——上游同理。
    async def listener(value: Any, ops: Any, context: Any) -> None:
        return None

    watch.start(listener)

    async def retire(tx: Any) -> None:
        await tx.retire_doc(TOKEN, 1)

    await session.commit(retire, CTX)
    end = await asyncio.wait_for(watch.closed, 2)
    assert end["reason"] == "retired"
    assert watch.value is None
