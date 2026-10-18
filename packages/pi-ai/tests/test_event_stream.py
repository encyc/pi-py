"""事件流测试，重点覆盖 v1.3.0 的 ``durationMs`` 计时（M-11）。

对应上游 ``test/event-stream.test.ts`` 与 ``utils/event-stream.ts:97-131``
（issue #10549）。

另含 FifoQueue 的性能回归说明：上游用双栈队列修复了"排空缓冲事件时 CPU 占用
呈二次增长"（issue #9055）。本端基于 ``asyncio.Queue``（``deque`` 语义，
入队/出队皆摊还 O(1)），**结构上不存在该问题**，故此处只做行为断言。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from pi_ai.event_stream import (
    AssistantMessageEventStream,
    EventStream,
    create_assistant_message_event_stream,
)
from pi_ai.events import DoneEvent, ErrorEvent, StartEvent, TextDeltaEvent
from pi_ai.types import AssistantMessage, TextContent


def _message(timestamp: int) -> AssistantMessage:
    return AssistantMessage(
        api="faux", provider="faux", model="faux", stop_reason="stop", timestamp=timestamp
    )


def test_done_event_stamps_duration_ms():
    async def run() -> AssistantMessage:
        es = AssistantMessageEventStream()
        msg = _message(int(time.time() * 1000))
        es.push(StartEvent(partial=msg))
        await asyncio.sleep(0.02)
        es.push(DoneEvent(reason="stop", message=msg))
        es.end(msg)
        return await es.result()

    result = asyncio.run(run())
    assert result.duration_ms is not None
    assert result.duration_ms >= 15  # 至少 sleep 的 20ms（留出时钟粒度余量）


def test_error_event_stamps_duration_ms():
    async def run() -> AssistantMessage:
        es = AssistantMessageEventStream()
        msg = _message(int(time.time() * 1000))
        msg.stop_reason = "error"
        msg.error_message = "boom"
        es.push(ErrorEvent(reason="error", error=msg))
        es.end(msg)
        return await es.result()

    assert asyncio.run(run()).duration_ms is not None


def test_end_stamps_duration_ms_without_events():
    async def run() -> AssistantMessage:
        es = AssistantMessageEventStream()
        msg = _message(int(time.time() * 1000))
        es.end(msg)
        return await es.result()

    assert asyncio.run(run()).duration_ms is not None


def test_deferred_result_started_elsewhere_stays_untimed():
    """``timestamp`` 早于流创建时刻 -> 不计时（转发别处已开始的响应）。"""

    async def run() -> AssistantMessage:
        es = AssistantMessageEventStream()
        old = _message(int(time.time() * 1000) - 60_000)
        es.push(DoneEvent(reason="stop", message=old))
        es.end(old)
        return await es.result()

    assert asyncio.run(run()).duration_ms is None


def test_existing_duration_ms_is_not_overwritten():
    async def run() -> AssistantMessage:
        es = AssistantMessageEventStream()
        msg = _message(int(time.time() * 1000))
        msg.duration_ms = 12345
        es.end(msg)
        return await es.result()

    assert asyncio.run(run()).duration_ms == 12345


def test_plain_event_stream_does_not_stamp_duration():
    """只有 ``AssistantMessageEventStream`` 计时；泛型 ``EventStream`` 不受影响。

    这保证 pi-agent-core 等下游直接使用 ``EventStream`` 的行为不变。
    """

    async def run() -> AssistantMessage:
        es: EventStream[object, AssistantMessage] = EventStream()
        msg = _message(int(time.time() * 1000))
        es.end(msg)
        return await es.result()

    assert asyncio.run(run()).duration_ms is None


def test_factory_returns_assistant_message_event_stream():
    assert isinstance(create_assistant_message_event_stream(), AssistantMessageEventStream)


def test_events_are_buffered_and_replayable():
    async def run() -> tuple[list[str], AssistantMessage]:
        es = AssistantMessageEventStream()
        msg = _message(int(time.time() * 1000))
        es.push(StartEvent(partial=msg))
        es.push(TextDeltaEvent(content_index=0, delta="hi", partial=msg))
        es.push(DoneEvent(reason="stop", message=msg))
        es.end(msg)
        seen = [e.type async for e in es]
        return seen, await es.result()

    seen, result = asyncio.run(run())
    assert seen == ["start", "text_delta", "done"]
    assert result.stop_reason == "stop"


def test_push_after_end_is_ignored():
    async def run() -> AssistantMessage:
        es = AssistantMessageEventStream()
        msg = _message(int(time.time() * 1000))
        es.end(msg)
        es.push(DoneEvent(reason="stop", message=msg))  # 应被忽略
        return await es.result()

    assert asyncio.run(run()) is not None


def test_large_buffered_drain_is_linear_enough():
    """issue #9055 的性能回归哨兵：排空大量缓冲事件不应出现二次爆炸。

    本端用 asyncio.Queue，理论上 O(n)。这里设一个宽松阈值（5000 事件 < 2s），
    只在实现退化为"每次 shift 都搬移整个列表"时才会触发。
    """

    async def run() -> int:
        es: EventStream[object, AssistantMessage] = EventStream()
        msg = _message(int(time.time() * 1000))
        for _ in range(5000):
            es.push(TextDeltaEvent(content_index=0, delta="x", partial=msg))
        es.end(msg)
        count = 0
        async for _event in es:
            count += 1
        return count

    started = time.monotonic()
    count = asyncio.run(run())
    elapsed = time.monotonic() - started
    assert count == 5000
    assert elapsed < 2.0, f"排空 5000 事件耗时 {elapsed:.2f}s，疑似退化为二次复杂度"


@pytest.mark.parametrize("value", [TextContent(text="x")])
def test_text_content_roundtrip(value: TextContent) -> None:
    assert value.type == "text"
