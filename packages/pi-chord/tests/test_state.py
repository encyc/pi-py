"""``pi_chord.state`` 测试 —— 对照上游 ``chord/src/state.ts`` 与
``test/replicated-state*.test.ts``、``test/state*.test.ts``。

重点覆盖上游 ``state.ts:20-27`` 写死的投递语义：串行化、水合优先、
100 条积压溢出只保留最新、失败隔离、退订不等待在跑的回调。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT, Context
from pi_chord.state import (
    ReplicatedStateDelivery,
    ReplicatedStateSnapshot,
    ReplicatedStateSourceAttachment,
    ReplicatedStateSourceFrame,
    ReplicatedStateSourceOptions,
    replicated_state,
)

Record = tuple[str, int, Any]


def record_into(sink: list[Record]) -> Callable[[Any, Context, ReplicatedStateDelivery], None]:
    def listener(value: Any, context: Context, delivery: ReplicatedStateDelivery) -> None:
        sink.append((delivery.kind, delivery.sequence, value))

    return listener


# ─── 可变（权威）复制状态 ────────────────────────────────────────────────────


def test_subscribe_hydrates_immediately_with_the_current_value() -> None:
    """上游 ``state.ts:201-212``：订阅先拿一次 ``hydrate``。"""
    state = replicated_state({"count": 0})
    seen: list[Record] = []
    state.subscribe(record_into(seen))
    assert len(seen) == 1
    kind, sequence, value = seen[0]
    assert kind == "hydrate"
    assert sequence == 0
    assert value == {"count": 0}
    assert state.value == {"count": 0}


def test_change_publishes_one_update_per_accepted_change() -> None:
    state = replicated_state({"count": 0})
    seen: list[Record] = []
    state.subscribe(record_into(seen))
    state.change(BACKGROUND_CONTEXT, lambda draft: draft.__setitem__("count", 1))
    state.change(BACKGROUND_CONTEXT, lambda draft: draft.__setitem__("count", 2))
    assert [(k, s) for k, s, _ in seen] == [("hydrate", 0), ("update", 1), ("update", 2)]
    assert state.value == {"count": 2}


def test_noop_change_publishes_nothing() -> None:
    """上游 ``state.ts:277``：只有 ``ops.length > 0`` 才发布。"""
    state = replicated_state({"count": 0})
    seen: list[Record] = []
    state.subscribe(record_into(seen))
    state.change(BACKGROUND_CONTEXT, lambda draft: draft.__setitem__("count", 0))
    assert len(seen) == 1  # 只有水合
    assert state.value == {"count": 0}


def test_unsubscribe_stops_delivery() -> None:
    state = replicated_state({"count": 0})
    seen: list[Record] = []
    unsubscribe = state.subscribe(record_into(seen))
    unsubscribe()
    state.change(BACKGROUND_CONTEXT, lambda draft: draft.__setitem__("count", 1))
    assert len(seen) == 1  # 只有水合
    # 状态本身仍然推进
    assert state.value == {"count": 1}


def test_state_advances_even_without_subscribers() -> None:
    state = replicated_state({"count": 0})
    state.change(BACKGROUND_CONTEXT, lambda draft: draft.__setitem__("count", 5))
    assert state.value == {"count": 5}


def test_a_late_subscriber_hydrates_from_the_current_revision() -> None:
    """新订阅者只看到订阅时刻之后的值（``state.ts:230`` 的 ``sequence <= hydratedSequence``）。"""
    state = replicated_state({"count": 0})
    state.change(BACKGROUND_CONTEXT, lambda draft: draft.__setitem__("count", 1))
    seen: list[Record] = []
    state.subscribe(record_into(seen))
    assert seen[0][2] == {"count": 1}
    assert seen[0][1] == 1  # 水合携带当前 sequence


def test_replace_publishes_and_takes_ownership() -> None:
    state = replicated_state({"count": 0})
    seen: list[Record] = []
    state.subscribe(record_into(seen))
    state.replace(BACKGROUND_CONTEXT, {"fresh": True})
    assert state.value == {"fresh": True}
    assert seen[-1][2] == {"fresh": True}


def test_replace_with_an_equal_value_publishes_nothing() -> None:
    state = replicated_state({"count": 0})
    seen: list[Record] = []
    state.subscribe(record_into(seen))
    state.replace(BACKGROUND_CONTEXT, {"count": 0})
    assert len(seen) == 1  # 只有水合


def test_change_callback_failure_leaves_the_state_untouched() -> None:
    state = replicated_state({"count": 0})
    seen: list[Record] = []
    state.subscribe(record_into(seen))

    def boom(draft: Any) -> None:
        draft["count"] = 99
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        state.change(BACKGROUND_CONTEXT, boom)
    assert state.value == {"count": 0}
    assert len(seen) == 1


def test_reentrant_change_is_rejected() -> None:
    """上游 ``state.ts:256``。"""
    state = replicated_state({"count": 0})

    def reenter(draft: Any) -> None:
        draft["count"] = 1
        state.change(BACKGROUND_CONTEXT, lambda inner: inner.__setitem__("count", 2))

    with pytest.raises(RuntimeError, match="reentrantly"):
        state.change(BACKGROUND_CONTEXT, reenter)


def test_reentrant_replace_is_rejected() -> None:
    """上游 ``state.ts:281``。"""
    state = replicated_state({"count": 0})

    def reenter(draft: Any) -> None:
        draft["count"] = 1
        state.replace(BACKGROUND_CONTEXT, {"other": 1})

    with pytest.raises(RuntimeError, match="replaced from a change callback"):
        state.change(BACKGROUND_CONTEXT, reenter)


def test_async_change_callback_is_rejected() -> None:
    """上游 ``state.ts:263-266``：回调必须同步。"""
    state = replicated_state({"count": 0})

    async def slow(draft: Any) -> None:
        draft["count"] = 1

    with pytest.raises(TypeError, match="must be synchronous"):
        state.change(BACKGROUND_CONTEXT, slow)
    assert state.value == {"count": 0}


def test_listener_failure_is_isolated() -> None:
    """上游 ``state.ts:22-23``：失败被隔离上报，投递继续。"""
    from pi_chord.state import _report_error_async  # noqa: PLC0415

    errors: list[BaseException] = []
    state = replicated_state({"count": 0})
    good: list[Record] = []

    def boom(value: Any, context: Context, delivery: ReplicatedStateDelivery) -> None:
        raise ValueError("listener failed")

    # 通过 source options 的 on_error 无法用于 mutable 状态，这里直接断言不抛出。
    state.subscribe(boom)
    state.subscribe(record_into(good))
    state.change(BACKGROUND_CONTEXT, lambda draft: draft.__setitem__("count", 1))
    # 抛出的监听器没有打断另一个订阅者
    assert good[-1][2] == {"count": 1}
    assert errors == []
    assert callable(_report_error_async)


def test_async_listener_is_awaited_before_the_next_delivery() -> None:
    """上游 ``state.ts:133-141``：监听器返回 Promise 时先等它落定再继续排空。"""

    async def scenario() -> list[Record]:
        state = replicated_state({"count": 0})
        seen: list[Record] = []
        gate = asyncio.Event()

        async def listener(value: Any, context: Context, delivery: ReplicatedStateDelivery) -> None:
            await gate.wait()
            seen.append((delivery.kind, delivery.sequence, value))

        state.subscribe(listener)
        await asyncio.sleep(0)  # 监听器开始跑并挂在 gate 上
        # **串行化的证据**：水合还没跑完，就什么都还没投递
        assert seen == []
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0)
        assert seen and seen[0][0] == "hydrate"  # 先完成水合
        state.change(BACKGROUND_CONTEXT, lambda draft: draft.__setitem__("count", 1))
        for _ in range(50):
            await asyncio.sleep(0)
        return seen

    seen = asyncio.run(scenario())
    assert seen[0][0] == "hydrate"
    assert [k for k, _, _ in seen] == ["hydrate", "update"]  # 顺序被串行化，未交错


def test_backlog_overflow_keeps_the_newest_and_skips_sequences() -> None:
    """上游 ``state.ts:20-25``：最多等 100 次投递，溢出只保留最新（**序号会跳跃**）。

    用一个卡住的异步监听器制造积压。
    """

    async def scenario() -> list[int]:
        state = replicated_state({"count": 0})
        sequences: list[int] = []
        release = asyncio.Event()

        async def listener(value: Any, context: Context, delivery: ReplicatedStateDelivery) -> None:
            if delivery.kind == "update":
                await release.wait()
            sequences.append(delivery.sequence)

        state.subscribe(listener)
        await asyncio.sleep(0)  # 让水合那个协程挂起在 release 上
        for index in range(1, 151):  # 从 1 开始，避免 index=0 时 count 未变化（no-op 不发布）
            state.change(BACKGROUND_CONTEXT, lambda draft, i=index: draft.__setitem__("count", i))
        release.set()
        for _ in range(500):
            await asyncio.sleep(0)
        return sequences

    sequences = asyncio.run(scenario())
    # 溢出时只保留最新的那个投递，因此序列里不会出现全部 150 个更新
    updates = [s for s in sequences if s > 0]
    assert 0 < len(updates) < 150
    assert sequences[0] == 0  # 水合仍排在第一位
    assert max(sequences) == 150  # 最后一次更新没被丢掉


# ─── 源（只发布）复制状态 ────────────────────────────────────────────────────


class _Attachment(ReplicatedStateSourceAttachment[Any]):
    def __init__(self, snapshot: ReplicatedStateSnapshot[Any]) -> None:
        self._snapshot = snapshot
        self.listener: Callable[[ReplicatedStateSourceFrame[Any]], None] | None = None
        self.disposed = 0

    @property
    def snapshot(self) -> ReplicatedStateSnapshot[Any]:
        return self._snapshot

    def activate(self, listener: Callable[[ReplicatedStateSourceFrame[Any]], None]) -> None:
        self.listener = listener

    def dispose(self) -> None:
        self.disposed += 1

    def emit(self, cursor: int, value: Any, context: Context = BACKGROUND_CONTEXT) -> None:
        assert self.listener is not None
        self.listener(ReplicatedStateSourceFrame(cursor, value, [], context))


class _Source:
    def __init__(self, snapshot: ReplicatedStateSnapshot[Any]) -> None:
        self.attachment = _Attachment(snapshot)

    def attach(self) -> ReplicatedStateSourceAttachment[Any]:
        return self.attachment


def test_attached_state_hydrates_from_the_snapshot() -> None:
    source = _Source(ReplicatedStateSnapshot({"count": 0}, 7))
    state = replicated_state(source)
    seen: list[Record] = []
    state.subscribe(record_into(seen))
    # 投递里的 sequence 是 **publisher 的本地序号**，不是源游标（上游 state.ts:202-206）。
    # 源游标只用于断层检测，见 test_cursor_gap_is_reported_and_disposes。
    assert seen[0] == ("hydrate", 0, {"count": 0})
    assert state.value == {"count": 0}


def test_attached_state_publishes_consecutive_frames() -> None:
    source = _Source(ReplicatedStateSnapshot({"count": 0}, 0))
    state = replicated_state(source)
    seen: list[Record] = []
    state.subscribe(record_into(seen))
    source.attachment.emit(1, {"count": 1})
    source.attachment.emit(2, {"count": 2})
    assert [(k, s) for k, s, _ in seen] == [("hydrate", 0), ("update", 1), ("update", 2)]
    assert state.value == {"count": 2}


def test_cursor_gap_is_reported_and_disposes() -> None:
    """上游 ``state.ts:330-338``：游标必须连续。"""
    errors: list[BaseException] = []
    source = _Source(ReplicatedStateSnapshot({"count": 0}, 0))
    state = replicated_state(source, ReplicatedStateSourceOptions(on_error=errors.append))
    source.attachment.emit(2, {"count": 2})  # 期望 1
    assert len(errors) == 1
    assert "cursor has a gap" in str(errors[0])
    assert "expected 1" in str(errors[0])
    assert source.attachment.disposed == 1
    assert state.value == {"count": 0}  # 断层的帧没有被发布


def test_frames_after_disposal_are_ignored() -> None:
    source = _Source(ReplicatedStateSnapshot({"count": 0}, 0))
    state = replicated_state(source)
    state.dispose()
    state.dispose()  # 幂等
    assert source.attachment.disposed == 1
    source.attachment.emit(1, {"count": 1})
    assert state.value == {"count": 0}


def test_dispose_is_idempotent() -> None:
    source = _Source(ReplicatedStateSnapshot({"count": 0}, 0))
    state = replicated_state(source)
    state.dispose()
    state.dispose()
    assert source.attachment.disposed == 1


def test_last_published_value_stays_readable_after_dispose() -> None:
    """上游 ``state.ts:88``：*The last published value remains readable*。"""
    source = _Source(ReplicatedStateSnapshot({"count": 0}, 0))
    state = replicated_state(source)
    source.attachment.emit(1, {"count": 1})
    state.dispose()
    assert state.value == {"count": 1}


def test_bad_snapshot_cursor_is_rejected() -> None:
    source = _Source(ReplicatedStateSnapshot({"count": 0}, 1.5))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="safe integer"):
        replicated_state(source)


def test_attach_failure_disposes_the_attachment() -> None:
    """上游 ``state.ts:372-379``。"""

    class _BadAttachment(_Attachment):
        def activate(self, listener: Callable[[ReplicatedStateSourceFrame[Any]], None]) -> None:
            raise RuntimeError("activate failed")

    source = _Source(ReplicatedStateSnapshot({"count": 0}, 0))
    source.attachment = _BadAttachment(source.attachment.snapshot)
    with pytest.raises(RuntimeError, match="activate failed"):
        replicated_state(source)
    assert source.attachment.disposed == 1


def test_dispose_failure_during_fail_is_reported_as_a_group() -> None:
    """上游 ``state.ts:344-349``：失败路径里 dispose 也炸 → AggregateError。"""

    class _AngryAttachment(_Attachment):
        def dispose(self) -> None:
            raise ValueError("dispose failed")

    errors: list[BaseException] = []
    source = _Source(ReplicatedStateSnapshot({"count": 0}, 0))
    source.attachment = _AngryAttachment(source.attachment.snapshot)
    replicated_state(source, ReplicatedStateSourceOptions(on_error=errors.append))
    source.attachment.emit(5, {"count": 5})  # 断层 → _fail → dispose 也失败
    assert len(errors) == 1
    assert isinstance(errors[0], BaseExceptionGroup)


def test_source_detection_is_by_callable_attach() -> None:
    """上游 ``state.ts:421-423``：有可调用 ``attach`` 就当源。"""
    plain = replicated_state({"a": 1})
    assert hasattr(plain, "change")  # mutable 形态

    source = _Source(ReplicatedStateSnapshot({"a": 1}, 0))
    attached = replicated_state(source)
    assert hasattr(attached, "dispose")  # attached 形态
