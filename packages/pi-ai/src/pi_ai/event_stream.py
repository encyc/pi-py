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
from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

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
        self._result_future: asyncio.Future[TResult] = asyncio.get_event_loop().create_future()
        self._events: list[TEvent] = []

    # ---- 生产者接口 ----

    def push(self, event: TEvent) -> None:
        """推送一个事件。在流已结束后调用会被忽略。"""
        if self._ended:
            return
        self._events.append(event)
        self._queue.put_nowait(event)

    def end(self, result: TResult) -> None:
        """标记流成功结束，并附带最终结果。"""
        if self._ended:
            return
        self._ended = True
        if not self._result_future.done():
            self._result_future.set_result(result)
        self._queue.put_nowait(_SENTINEL)

    def error(self, error: BaseException) -> None:
        """标记流因异常结束。"""
        if self._ended:
            return
        self._ended = True
        if not self._result_future.done():
            self._result_future.set_exception(error)
        self._queue.put_nowait(_SENTINEL)

    # ---- 消费者接口 ----

    async def result(self) -> TResult:
        """等待并返回终止事件携带的最终结果。"""
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


__all__ = ["EventStream"]
