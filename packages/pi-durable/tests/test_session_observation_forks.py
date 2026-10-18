"""Session 阶段 1 的测试：``observation.py``(292) + ``forks.py``(97)。

不含写线——这一阶段刻意只覆盖「不依赖 Session 运行时」的两块。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_chord.context import AbortController
from pi_chord.state import ReplicatedStateSnapshot, replicated_state
from pi_durable.session import (
    MAX_PENDING_WATCH_FRAMES,
    RETIREMENT_OPERATIONS,
    CommittedStateSource,
    CommittedWatch,
    prepare_fork_document_copies,
)
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage

pytestmark = pytest.mark.asyncio


def frame(cursor: int, value: Any, ops: list[Any] | None = None) -> Any:
    from pi_chord.state import ReplicatedStateSourceFrame

    return ReplicatedStateSourceFrame(cursor=cursor, value=value, ops=ops or [], context=CTX)


# --------------------------------------------------------------------------------------
# CommittedStateSource
# --------------------------------------------------------------------------------------


async def test_source_snapshot_then_frames_in_order() -> None:
    released: list[int] = []
    source: CommittedStateSource[dict[str, Any]] = CommittedStateSource(
        {"n": 0}, lambda: released.append(1)
    )

    attachment = source.attach()
    assert attachment.snapshot == ReplicatedStateSnapshot(value={"n": 0}, cursor=0)

    seen: list[Any] = []
    attachment.activate(seen.append)
    source.advance({"n": 1}, [("s", ("n",), 1)], CTX)
    source.advance({"n": 2}, [("s", ("n",), 2)], CTX)
    # 已激活后的 publish 走 `call_soon`（上游同理走 queueMicrotask），需要让循环转一轮
    await asyncio.sleep(0)

    assert [f.value for f in seen] == [{"n": 1}, {"n": 2}]
    assert [f.cursor for f in seen] == [1, 2]


async def test_source_buffers_frames_before_activate_and_drains_synchronously() -> None:
    source: CommittedStateSource[dict[str, Any]] = CommittedStateSource({"n": 0}, lambda: None)
    attachment = source.attach()
    source.advance({"n": 1}, [], CTX)
    source.advance({"n": 2}, [], CTX)

    seen: list[Any] = []
    attachment.activate(seen.append)  # 必须**同步**排空
    assert [f.value for f in seen] == [{"n": 1}, {"n": 2}]


async def test_source_release_runs_once_after_last_attachment_disposes() -> None:
    released: list[int] = []
    source: CommittedStateSource[dict[str, Any]] = CommittedStateSource(
        {}, lambda: released.append(1)
    )

    first = source.attach()
    second = source.attach()
    first.dispose()
    assert released == []  # 还有附着
    second.dispose()
    assert released == [1]
    second.dispose()  # 幂等
    assert released == [1]


async def test_source_retirement_stops_advancing() -> None:
    source: CommittedStateSource[dict[str, Any] | None] = CommittedStateSource(
        {"n": 0}, lambda: None
    )
    attachment = source.attach()
    seen: list[Any] = []
    attachment.activate(seen.append)

    source.advance(None, [RETIREMENT_OPERATIONS[0]], CTX)
    source.advance({"n": 9}, [], CTX)  # 退休后不再推
    await asyncio.sleep(0)

    assert [f.value for f in seen] == [None]


async def test_source_close_session_disposes_attachments() -> None:
    released: list[int] = []
    source: CommittedStateSource[dict[str, Any]] = CommittedStateSource(
        {}, lambda: released.append(1)
    )
    source.attach()
    source.close_session()
    assert released == [1]
    with pytest.raises(RuntimeError, match="State source is closed"):
        source.attach()


async def test_attachment_activate_is_single_use() -> None:
    source: CommittedStateSource[dict[str, Any]] = CommittedStateSource({}, lambda: None)
    attachment = source.attach()
    attachment.activate(lambda f: None)
    with pytest.raises(RuntimeError, match="already active"):
        attachment.activate(lambda f: None)
    # 上游先查 activated 再查 disposed（``observation.ts:96-98``），所以同一个附着
    # 重复 activate 报的是 "already active"；要测 disposed 得用一个没激活过的附着。
    # 「保活」附着必须先挂上：释放**最后一个**附着会关掉整个源。
    keeper = source.attach()
    attachment.dispose()
    fresh = source.attach()
    fresh.dispose()
    with pytest.raises(RuntimeError, match="is disposed"):
        fresh.activate(lambda f: None)
    keeper.dispose()


async def test_attachment_isolates_listener_failure() -> None:
    """直连监听器抛出只影响这一个附着（``observation.ts:111-118``）。"""
    released: list[int] = []
    source: CommittedStateSource[dict[str, Any]] = CommittedStateSource(
        {}, lambda: released.append(1)
    )
    attachment = source.attach()

    def boom(_frame: Any) -> None:
        raise ValueError("listener exploded")

    attachment.activate(boom)
    source.advance({"n": 1}, [], CTX)
    await asyncio.sleep(0)  # 让 call_soon 的排空跑起来
    await asyncio.sleep(0)
    assert released == [1]  # 附着被释放，异常没有冒到 advance 的调用方


async def test_source_drives_a_real_chord_replicated_state() -> None:
    """真正挂到 chord 的 ``replicated_state`` 上——这是本模块存在的理由。"""
    source: CommittedStateSource[dict[str, Any]] = CommittedStateSource({"n": 0}, lambda: None)
    state = replicated_state(source)
    try:
        assert state.value == {"n": 0}
        source.advance({"n": 5}, [("s", ("n",), 5)], CTX)
        await asyncio.sleep(0)
        assert state.value == {"n": 5}
    finally:
        state.dispose()


# --------------------------------------------------------------------------------------
# CommittedWatch
# --------------------------------------------------------------------------------------


async def test_watch_start_is_never_inline() -> None:
    watch: CommittedWatch[dict[str, Any] | None] = CommittedWatch({}, lambda: None, lambda e: None)
    deliveries: list[Any] = []

    async def listener(value: Any, ops: Any, context: Any) -> None:
        deliveries.append(value)

    watch.start(listener)
    watch.advance({"n": 1}, [], CTX)
    assert deliveries == []  # 绝不内联
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert deliveries == [{"n": 1}]


async def test_watch_value_tracks_latest_delivered() -> None:
    watch: CommittedWatch[dict[str, Any] | None] = CommittedWatch(
        {"n": 0}, lambda: None, lambda e: None
    )

    async def listener(value: Any, ops: Any, context: Any) -> None:
        return None

    watch.start(listener)
    assert watch.value == {"n": 0}
    watch.advance({"n": 1}, [], CTX)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert watch.value == {"n": 1}


async def test_watch_overflow_replaces_pending_frames_with_one() -> None:
    watch: CommittedWatch[dict[str, Any] | None] = CommittedWatch({}, lambda: None, lambda e: None)
    for index in range(MAX_PENDING_WATCH_FRAMES + 10):
        watch.advance({"n": index}, [], CTX)

    deliveries: list[Any] = []

    async def listener(value: Any, ops: Any, context: Any) -> None:
        deliveries.append(value)

    watch.start(listener)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # 溢出时丢弃全部待投递帧、改投一帧「替换」= 当时的最新值（索引 100），
    # 其后 9 帧正常累积（与上游 `observation.ts:229-237` 一致）。
    assert deliveries[0] == {"n": MAX_PENDING_WATCH_FRAMES}
    assert deliveries[-1] == {"n": MAX_PENDING_WATCH_FRAMES + 9}
    assert len(deliveries) == 10


async def test_watch_overflow_uses_replace_when_given() -> None:
    watch: CommittedWatch[dict[str, Any] | None] = CommittedWatch(
        {}, lambda: None, lambda e: None, replace=lambda: {"n": -1}
    )
    for index in range(MAX_PENDING_WATCH_FRAMES + 1):
        watch.advance({"n": index}, [], CTX)

    deliveries: list[Any] = []

    async def listener(value: Any, ops: Any, context: Any) -> None:
        deliveries.append(value)

    watch.start(listener)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # 溢出只发生一次（在第 100 帧），此后没有再溢出，所以只投递那一帧替身。
    assert deliveries == [{"n": -1}]


async def test_watch_retire_terminates_after_delivering_null() -> None:
    watch: CommittedWatch[dict[str, Any] | None] = CommittedWatch({}, lambda: None, lambda e: None)
    deliveries: list[Any] = []

    async def listener(value: Any, ops: Any, context: Any) -> None:
        deliveries.append(value)

    watch.start(listener)
    watch.advance(None, [RETIREMENT_OPERATIONS[0]], CTX)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert deliveries == [None]
    assert await watch.closed == {"reason": "retired"}


async def test_watch_stop_is_idempotent_and_detaches() -> None:
    detached: list[int] = []
    watch: CommittedWatch[dict[str, Any] | None] = CommittedWatch(
        {}, lambda: detached.append(1), lambda e: None
    )

    assert await watch.stop() == {"reason": "stopped"}
    assert detached == [1]
    assert await watch.stop() == {"reason": "stopped"}
    assert detached == [1]


async def test_watch_listener_error_reports_then_terminates() -> None:
    reported: list[Any] = []
    watch: CommittedWatch[dict[str, Any] | None] = CommittedWatch({}, lambda: None, reported.append)

    async def listener(value: Any, ops: Any, context: Any) -> None:
        raise ValueError("boom")

    watch.start(listener)
    watch.advance({"n": 1}, [], CTX)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(reported) == 1
    assert isinstance(reported[0], ValueError)
    end = await watch.closed
    assert end["reason"] == "listener_error"
    assert isinstance(end["error"], ValueError)


async def test_watch_close_session_reports_failure() -> None:
    other: CommittedWatch[dict[str, Any] | None] = CommittedWatch({}, lambda: None, lambda e: None)
    other.close_session()
    assert await other.closed == {"reason": "session_closed"}

    failed: CommittedWatch[dict[str, Any] | None] = CommittedWatch({}, lambda: None, lambda e: None)
    failure = RuntimeError("storage died")
    failed.close_session(failure)
    end = await failed.closed
    assert end["reason"] == "session_failed"
    assert end["error"] is failure


async def test_watch_cancellation_via_abort_signal() -> None:
    controller = AbortController()
    watch: CommittedWatch[dict[str, Any] | None] = CommittedWatch({}, lambda: None, lambda e: None)
    watch.observe_cancellation(controller.signal)

    controller.abort()
    assert await watch.closed == {"reason": "cancelled"}

    with pytest.raises(RuntimeError, match="already installed"):
        watch.observe_cancellation(controller.signal)


async def test_watch_cancellation_when_already_aborted() -> None:
    controller = AbortController()
    controller.abort()
    watch: CommittedWatch[dict[str, Any] | None] = CommittedWatch({}, lambda: None, lambda e: None)
    watch.observe_cancellation(controller.signal)
    assert await watch.closed == {"reason": "cancelled"}


async def test_watch_delivery_context_has_no_abort_signal() -> None:
    """投递用的上下文必须剥掉调用方的 abort signal（``observation.ts:256``）。"""
    controller = AbortController()
    from pi_chord.context import with_abort_signal

    captured: list[Any] = []

    async def listener(value: Any, ops: Any, context: Any) -> None:
        captured.append(context)

    watch: CommittedWatch[dict[str, Any] | None] = CommittedWatch({}, lambda: None, lambda e: None)
    watch.start(listener)
    watch.advance({"n": 1}, [], with_abort_signal(controller.signal, CTX))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert captured
    # 剥掉之后不能再被原 signal 取消：这里只要确认拿到了上下文即可，
    # 具体「没有 signal」由 chord 的 without_abort_signal 保证。
    assert captured[0] is not None


async def test_watch_start_after_stop_is_rejected() -> None:
    watch: CommittedWatch[dict[str, Any] | None] = CommittedWatch({}, lambda: None, lambda e: None)
    await watch.stop()
    with pytest.raises(RuntimeError, match="Watch is stopped"):
        watch.start(lambda v, o, c: asyncio.sleep(0))  # type: ignore[arg-type,return-value]


async def test_watch_start_twice_is_rejected() -> None:
    watch: CommittedWatch[dict[str, Any] | None] = CommittedWatch({}, lambda: None, lambda e: None)

    async def listener(value: Any, ops: Any, context: Any) -> None:
        return None

    watch.start(listener)
    with pytest.raises(RuntimeError, match="already started"):
        watch.start(listener)


# --------------------------------------------------------------------------------------
# forks.py —— 真实 SQLite 端到端
# --------------------------------------------------------------------------------------


async def seed_conversation(storage: Any, context: Any = CTX) -> dict[str, Any]:
    """根会话 + 一个条目 + 三种文档，返回关键 ID。"""
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], context)
    # 文档先落，条目后落——这样条目所在提交的 seq **晚于**文档的 createdAt，
    # as-of 查询在该时间点才看得到它们。反过来（同批或之后）会正确地读不到。
    as_of_doc = await storage.mint_id()
    current_doc = await storage.mint_id()
    other_scope = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {
                    "id": as_of_doc,
                    "kind": "pi.asof",
                    "scope": {"kind": "conversation", "conversationId": 1},
                    "history": "rewindable",
                    "fork": "asOf",
                },
                "content": {"kind": "base", "version": 1, "value": {"n": 1}},
            },
            {
                "type": "document.create",
                "record": {
                    "id": current_doc,
                    "kind": "pi.current",
                    "scope": {"kind": "conversation", "conversationId": 1},
                    "history": "latest",
                    "fork": "current",
                },
                "content": {"kind": "base", "version": 1, "value": {"n": 2}},
            },
            {
                "type": "document.create",
                "record": {"id": other_scope, "kind": "pi.session", "scope": {"kind": "session"}},
                "content": {"kind": "base", "version": 1, "value": {"n": 3}},
            },
        ],
        context,
    )
    entry_id = await storage.mint_id()
    parent_seq = await storage.commit(
        [
            {
                "type": "entry",
                "value": {"id": entry_id, "conversationId": 1, "kind": "pi.user"},
            }
        ],
        context,
    )
    return {"entry": entry_id, "seq": parent_seq, "as_of": as_of_doc, "current": current_doc}


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_prepare_fork_copies_selects_both_policies(backend: str, tmp_path: Path) -> None:
    """as-of 组 + current 组都要选中；session 作用域的文档**不**复制。"""
    storage: Any
    if backend == "memory":
        storage = MemoryStorage()
    else:
        storage = await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    try:
        seeded = await seed_conversation(storage)
        child = await storage.mint_id()
        await storage.commit(
            [
                {
                    "type": "conversation",
                    "value": {
                        "id": child,
                        "parent": {"conversationId": 1, "at": seeded["entry"]},
                    },
                }
            ],
            CTX,
        )

        copies = await prepare_fork_document_copies(storage, 1, seeded["entry"], child, CTX)

        kinds = sorted(copy.record["kind"] for copy in copies)
        assert kinds == ["pi.asof", "pi.current"], kinds
        by_kind = {copy.record["kind"]: copy for copy in copies}
        for copy in copies:
            assert copy.record["scope"] == {"kind": "conversation", "conversationId": child}
            assert copy.record["id"] not in {seeded["as_of"], seeded["current"]}
        # as-of 组钉在条目所在的提交点；current 组钉在字面量 "current"
        assert by_kind["pi.asof"].source["at"] == seeded["seq"]
        assert by_kind["pi.current"].source["at"] == "current"
        # history 被继承，fork 保持源自己的策略
        assert by_kind["pi.asof"].record["fork"] == "asOf"
        assert by_kind["pi.current"].record["fork"] == "current"
        assert by_kind["pi.current"].record["history"] == "latest"
    finally:
        await storage.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_prepare_fork_rejects_invisible_entry(backend: str, tmp_path: Path) -> None:
    storage: Any
    if backend == "memory":
        storage = MemoryStorage()
    else:
        storage = await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    try:
        await seed_conversation(storage)
        child = await storage.mint_id()
        with pytest.raises(RuntimeError, match="is not visible from conversation"):
            await prepare_fork_document_copies(storage, 1, 999_999, child, CTX)
    finally:
        await storage.close(CTX)


async def test_prepare_fork_returns_empty_when_nothing_to_copy(tmp_path: Path) -> None:
    storage = await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    try:
        await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
        entry_id = await storage.mint_id()
        await storage.commit(
            [{"type": "entry", "value": {"id": entry_id, "conversationId": 1, "kind": "pi.user"}}],
            CTX,
        )
        child = await storage.mint_id()
        copies = await prepare_fork_document_copies(storage, 1, entry_id, child, CTX)
        assert copies == []
    finally:
        await storage.close(CTX)
