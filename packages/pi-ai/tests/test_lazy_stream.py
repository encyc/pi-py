"""``lazy_stream`` 行为测试（task-33）。

对应上游 ``api/lazy.ts:41-62``。要点：
- **同步**返回流；异步 setup 在其背后运行
- setup 成功 → 内层事件被转发，并以**内层结果**收尾
- setup 失败 → 转成 ``error`` 事件 + error 消息收尾（**不向调用方抛**）
- 计时：``durationMs`` 由外层 ``AssistantMessageEventStream`` 写入
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import AsyncIterator

import pytest

from pi_ai.event_stream import AssistantMessageEventStream
from pi_ai.events import DoneEvent, ErrorEvent, StartEvent, TextDeltaEvent
from pi_ai.lazy import lazy_stream
from pi_ai.types import AssistantMessage, Model, TextContent


def _model() -> Model:
    return Model(
        id="gpt-4o", name="GPT-4o", api="openai-completions", provider="openai", baseUrl="https://x"
    )


def _stream_with(text: str, *, delay: float = 0.0) -> AssistantMessageEventStream:
    """构造一个已就绪的内层流。"""
    es = AssistantMessageEventStream()
    message = AssistantMessage(
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=int(time.time() * 1000),
    )
    es.push(StartEvent(partial=message))
    block = TextContent(text=text)
    message.content.append(block)
    es.push(TextDeltaEvent(content_index=0, delta=text, partial=message))
    es.push(DoneEvent(reason="stop", message=message))
    es.end(message)
    return es


async def _drain(stream: AsyncIterator[object]) -> list[str]:
    return [getattr(e, "type", None) or "" for e in [x async for x in stream]]  # type: ignore[union-attr]


def test_lazy_stream_returns_synchronously() -> None:
    """核心契约：setup 尚未完成时就已经拿到流对象（不阻塞调用方）。"""
    started = asyncio.Event()

    async def setup() -> AssistantMessageEventStream:
        started.set()
        await asyncio.sleep(0.01)
        return _stream_with("hi")

    async def run() -> None:
        outer = lazy_stream(_model(), setup)  # 立即返回，未 await
        assert isinstance(outer, AssistantMessageEventStream)
        assert not started.is_set()  # setup 还没跑（在同一 tick 内）
        events = await _drain(outer)
        assert events == ["start", "text_delta", "done"]
        result = await outer.result()
        assert result.stop_reason == "stop"
        assert result.content[0].text == "hi"  # type: ignore[union-attr]

    asyncio.run(run())


def test_lazy_stream_converts_setup_failure_into_error_event() -> None:
    """setup 抛异常 → error 事件 + error 消息；调用方**不会**收到异常。"""

    async def setup() -> AssistantMessageEventStream:
        raise RuntimeError("no api key for openai")

    async def run() -> None:
        outer = lazy_stream(_model(), setup)
        events = await _drain(outer)
        assert events == ["error"]
        result = await outer.result()
        assert result.stop_reason == "error"
        assert "no api key for openai" in (result.error_message or "")
        # 错误消息带上 model 三元组，便于定位
        assert result.api == "openai-completions"
        assert result.provider == "openai"
        assert result.model == "gpt-4o"
        assert result.content == []

    asyncio.run(run())


def test_setup_failure_error_event_carries_the_message() -> None:
    async def setup() -> AssistantMessageEventStream:
        raise ValueError("boom")

    async def run() -> None:
        outer = lazy_stream(_model(), setup)
        events = [e async for e in outer]
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, ErrorEvent)
        assert event.reason == "error"
        assert event.error.stop_reason == "error"
        assert "boom" in (event.error.error_message or "")

    asyncio.run(run())


def test_lazy_stream_error_message_timestamp_is_request_start() -> None:
    """``timestamp`` 取请求开始时刻（不是失败时刻），使 durationMs 能计入。"""

    async def setup() -> AssistantMessageEventStream:
        await asyncio.sleep(0.02)
        raise RuntimeError("late failure")

    async def run() -> None:
        before = int(time.time() * 1000)
        outer = lazy_stream(_model(), setup)
        result = await outer.result()
        after = int(time.time() * 1000)
        assert before <= result.timestamp <= after
        # 外层流据此写入耗时（M-11 的 durationMs 语义）
        assert result.duration_ms is not None
        assert result.duration_ms >= 15

    asyncio.run(run())


def test_lazy_stream_forwards_inner_error_stream() -> None:
    """内层自身以 error 收尾时，错误被原样转发（外层不覆盖）。"""

    async def setup() -> AssistantMessageEventStream:
        es = AssistantMessageEventStream()
        message = AssistantMessage(
            api="openai-completions",
            provider="openai",
            model="gpt-4o",
            stop_reason="error",
            error_message="upstream 503",
            timestamp=int(time.time() * 1000),
        )
        es.push(ErrorEvent(reason="error", error=message))
        es.end(message)
        return es

    async def run() -> None:
        outer = lazy_stream(_model(), setup)
        events = await _drain(outer)
        assert events == ["error"]
        result = await outer.result()
        assert result.error_message == "upstream 503"

    asyncio.run(run())


def test_lazy_stream_surfaces_setup_await_failure_of_a_bare_iterable() -> None:
    """内层是无 ``result()`` 的裸异步可迭代时，退化为最后一个终止事件的消息。"""

    async def setup() -> AsyncIterator[object]:
        message = AssistantMessage(
            api="openai-completions",
            provider="openai",
            model="gpt-4o",
            stop_reason="stop",
            timestamp=int(time.time() * 1000),
        )
        yield StartEvent(partial=message)
        yield DoneEvent(reason="stop", message=message)

    async def run() -> None:
        outer = lazy_stream(_model(), setup)  # type: ignore[arg-type]
        events = await _drain(outer)
        assert events == ["start", "done"]
        result = await outer.result()
        assert result.stop_reason == "stop"

    asyncio.run(run())


def test_lazy_stream_result_never_hangs_on_empty_source() -> None:
    """内层不发任何事件且无 result() 时，仍必须终结（result() 不悬挂）。"""

    async def setup() -> AsyncIterator[object]:
        return
        yield  # pragma: no cover - 使其成为异步生成器

    async def run() -> None:
        outer = lazy_stream(_model(), setup)  # type: ignore[arg-type]
        result = await asyncio.wait_for(outer.result(), timeout=5)
        assert result.stop_reason == "error"
        assert "no result" in (result.error_message or "").lower()

    asyncio.run(run())


#: 真实单调钟。必须在类体外取：类体内的 `time` 会先命中类命名空间里刚定义的
#: `time` 方法（而不是模块 `time`），从而 `time.monotonic` 报 AttributeError。
_REAL_MONOTONIC = time.monotonic


class _TickingClock:
    """每次读 ``time()`` 前进 1ms 的假时钟。

    用途：把 ``lazy_stream`` 的 **1ms 竞态**变成**确定性**断言。真实时钟下该竞态
    命中率约 0.1%/次（只在毫秒刻度恰好落在两次读钟之间时发生），单文件跑几乎不
    出现、全仓跑偶发——正是它此前以 flaky 形式暴露的原因。
    """

    def __init__(self, start_ms: int) -> None:
        self._next = itertools.count(start_ms)

    def time(self) -> float:
        return next(self._next) / 1000.0

    #: 耗时测算用真实单调钟（不受本假时钟影响）
    monotonic = staticmethod(_REAL_MONOTONIC)


def test_setup_error_duration_survives_the_1ms_tick_boundary(monkeypatch) -> None:
    """**确定性**证明：毫秒刻度落在两次读钟之间时，setup 失败消息仍被计入 duration_ms。

    假时钟每次读都前进 1ms，于是"先取时刻再建流"的旧顺序必然满足
    ``message.timestamp < stream._started_at_ms``（差 1），从而被 ``_time()``
    的"早于流创建时刻"守卫跳过。本端把取时刻挪到建流**之后**，该守卫不再命中。

    本用例自带**判别性证明**（见下方断言）：同一假时钟下按旧顺序构造的消息确实
    ``duration_ms is None``——所以这不是一条恒真的空断言。
    """
    import pi_ai.event_stream as event_stream_mod
    import pi_ai.lazy as lazy_mod

    fake = _TickingClock(1_700_000_000_000)
    monkeypatch.setattr(lazy_mod, "time", fake)
    monkeypatch.setattr(event_stream_mod, "time", fake)

    async def failing_setup() -> AssistantMessageEventStream:
        raise RuntimeError("no api key")

    async def run() -> AssistantMessage:
        return await lazy_stream(_model(), failing_setup).result()

    result = asyncio.run(run())
    assert result.stop_reason == "error"
    assert result.duration_ms is not None, "setup 失败消息未被计时（1ms 竞态复现）"

    # ---- 判别性证明：同一假时钟下，旧顺序（先取时刻、再建流）必然丢计时 ----
    stale = _create_setup_error_message_for_test(_model(), int(fake.time() * 1000))
    later_stream = event_stream_mod.AssistantMessageEventStream()  # 起点比 stale 晚 1ms
    later_stream.end(stale)
    assert stale.duration_ms is None, "假时钟没能复现竞态，该用例已失去判别力"


def _create_setup_error_message_for_test(model, timestamp: int):
    from pi_ai.lazy import _create_setup_error_message

    return _create_setup_error_message(model, RuntimeError("stale"), timestamp)


def test_lazy_stream_uses_the_stream_start_not_a_pre_stream_clock_read() -> None:
    """静态回归守卫：``lazy_stream`` 里建流必须在取时刻**之前**。

    直接断言源码顺序，防止有人"照上游对齐"把两行换回去而让竞态复活
    （上游 ``api/lazy.ts:50-51`` 就是这个顺序）。
    """
    import inspect

    import pi_ai.lazy as lazy_mod

    source = inspect.getsource(lazy_mod.lazy_stream)
    create_at = source.index("outer = AssistantMessageEventStream()")
    clock_at = source.index("started_at = int(time.time() * 1000)")
    assert create_at < clock_at, "建流必须在取时刻之前（否则 1ms 竞态复活）"


def test_lazy_stream_duration_is_not_double_stamped() -> None:
    """内层已写 durationMs 时外层不覆盖（``AssistantMessageEventStream`` 语义）。"""

    async def setup() -> AssistantMessageEventStream:
        inner = _stream_with("x")
        message = await inner.result()
        message.duration_ms = 999  # 模拟"别处已计时"
        return inner

    async def run() -> None:
        outer = lazy_stream(_model(), setup)
        result = await outer.result()
        assert result.duration_ms == 999

    asyncio.run(run())


def test_lazy_module_export_surface() -> None:
    """导出面：``lazy_stream`` + ``LazyStreamSetup``（类型别名，便于下游标注 setup）。

    上游同文件另有 ``lazyApi`` 与 ``LazyApiCapabilities``——**未移植**：本端 provider
    是及时导入的，没有"动态 import 的 API 实现模块"需要包装。
    """
    from pi_ai import lazy as lazy_mod

    assert lazy_mod.__all__ == ["lazy_stream", "LazyStreamSetup"]
    assert not hasattr(lazy_mod, "lazy_api")
    assert not hasattr(lazy_mod, "LazyApiCapabilities")


@pytest.mark.parametrize("exc", [RuntimeError, ValueError, KeyError])
def test_setup_failure_of_any_exception_type_is_contained(exc: type[Exception]) -> None:
    async def setup() -> AssistantMessageEventStream:
        raise exc("nope")

    async def run() -> None:
        result = await lazy_stream(_model(), setup).result()
        assert result.stop_reason == "error"

    asyncio.run(run())
