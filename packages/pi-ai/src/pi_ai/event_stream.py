"""异步事件流。

对应上游 ``utils/event-stream.ts`` 的 ``EventStream`` / ``AssistantMessageEventStream``。

设计（移植自旧版优秀实现）：
- 基于 ``asyncio.Queue`` 的生产者/消费者缓冲。
- ``push`` 由生产者调用；``async for`` / ``result`` 由消费者调用。
- 哨兵对象标记流结束，避免 None 歧义。
- ``result()`` 返回终止事件携带的最终结果（done 的 message 或 error 的 error）。

与上游契约一致：生产者函数（如 ``stream()``）**同步返回** EventStream，
内部用 asyncio task 驱动事件推送；所有失败编码为 error 事件，而非抛异常。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

from .events import AssistantMessageEvent
from .types import AssistantMessage

TEvent = TypeVar("TEvent")
TResult = TypeVar("TResult")

_SENTINEL = object()


class EventStream(Generic[TEvent, TResult], AsyncIterator[TEvent]):
    """异步事件流。

    泛型参数：
    - ``TEvent``：流中事件的类型。
    - ``TResult``：终止事件携带的最终结果类型（如 ``AssistantMessage``）。
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._ended = False
        # 结果 Future **惰性创建**：``asyncio.get_event_loop()`` 在 Python 3.12+ 于
        # 无运行事件循环时抛 RuntimeError，而流必须能在同步上下文里构造（provider
        # 的 ``stream()`` 就是同步返回 EventStream，上游
        # ``createAssistantMessageEventStream()`` 同样可在任意处调用）。
        self._result_future: asyncio.Future[TResult] | None = None
        self._result: TResult | None = None
        self._result_error: BaseException | None = None
        self._events: list[TEvent] = []

    # ---- 生产者接口 ----

    def push(self, event: TEvent) -> None:
        """推送一个事件。在流已结束后调用会被忽略。"""
        if self._ended:
            return
        self._events.append(event)
        self._queue.put_nowait(event)

    def _settle(self) -> None:
        """把已记录的结果/异常落到 Future 上（若消费者已经在等）。"""
        future = self._result_future
        if future is None or future.done():
            return
        if self._result_error is not None:
            future.set_exception(self._result_error)
        else:
            future.set_result(self._result)  # type: ignore[arg-type]

    def end(self, result: TResult) -> None:
        """标记流成功结束，并附带最终结果。"""
        if self._ended:
            return
        self._ended = True
        self._result = result
        self._settle()
        self._queue.put_nowait(_SENTINEL)

    def error(self, error: BaseException) -> None:
        """标记流因异常结束。"""
        if self._ended:
            return
        self._ended = True
        self._result_error = error
        self._settle()
        self._queue.put_nowait(_SENTINEL)

    # ---- 消费者接口 ----

    async def result(self) -> TResult:
        """等待并返回终止事件携带的最终结果。

        首次调用时在当前运行循环上创建 Future；若流已结束（同步生产者），
        立即以已记录的结果/异常兑现。
        """
        if self._result_future is None:
            self._result_future = asyncio.get_running_loop().create_future()
            if self._ended:
                self._settle()
        return await self._result_future

    @property
    def events(self) -> list[TEvent]:
        """已推送的全部事件（用于重放）。"""
        return list(self._events)

    async def __anext__(self) -> TEvent:
        item = await self._queue.get()
        if item is _SENTINEL:
            raise StopAsyncIteration
        return item  # type: ignore[no-any-return]

    def __aiter__(self) -> AsyncIterator[TEvent]:
        return self


class AssistantMessageEventStream(EventStream[AssistantMessageEvent, AssistantMessage]):
    """一次 assistant 响应的事件流，并**为该响应计时**。

    对应上游 v1.3.0 ``AssistantMessageEventStream``（utils/event-stream.ts:97-131，
    issue #10549）。最终消息（``done`` / ``error`` 事件，或传给 :meth:`end` 的结果）
    会带上 ``duration_ms``，用单调时钟从流创建时刻起算，除非：

    - 该消息已有 ``duration_ms``；
    - 该消息的 ``timestamp`` **早于**本流的创建时刻——说明这是"别处早已开始"的响应
      被转发进来（例如稍后才取回的 deferred 结果），此时不应计时。

    上游用 ``performance.now()``；Python 对应 ``time.monotonic()``（不受系统
    时钟调整影响）。
    """

    def __init__(self) -> None:
        super().__init__()
        self._started_at_ms = int(time.time() * 1000)
        self._started_at_monotonic = time.monotonic()

    def push(self, event: AssistantMessageEvent) -> None:
        """推送事件；``done`` / ``error`` 事件携带的最终消息在此被计时。"""
        if event.type == "done":
            self._time(event.message)
        elif event.type == "error":
            self._time(event.error)
        super().push(event)

    def end(self, result: AssistantMessage) -> None:
        """结束流；结果消息在此被计时。"""
        if result is not None:
            self._time(result)
        super().end(result)

    def _time(self, message: AssistantMessage | None) -> None:
        if message is None or self._ended:
            return
        if getattr(message, "duration_ms", None) is not None:
            return
        if getattr(message, "timestamp", 0) < self._started_at_ms:
            # 该响应在别处开始（deferred 结果转发），不计时。
            return
        elapsed = (time.monotonic() - self._started_at_monotonic) * 1000
        message.duration_ms = max(0, round(elapsed))


def create_assistant_message_event_stream() -> AssistantMessageEventStream:
    """构造 :class:`AssistantMessageEventStream` 的工厂（对应上游同名工厂）。"""
    return AssistantMessageEventStream()


__all__ = [
    "EventStream",
    "AssistantMessageEventStream",
    "create_assistant_message_event_stream",
]
