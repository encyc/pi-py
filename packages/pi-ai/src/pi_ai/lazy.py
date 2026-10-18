"""惰性流：先同步返回流，异步 setup 在其背后运行。

对应上游 ``api/lazy.ts``（v1.3.0，99 行）。

**这是流生命周期工具，不是被排除的 OAuth / classifier / images 能力。** 上游用它
把「同步返回 ``AssistantMessageEventStream``」与「异步 setup（鉴权解析、惰性模块
加载、虚拟模型路由）」解耦，setup 失败时以 error 事件终结流
（``api/lazy.ts:41-62``）。下游 ``coding-agent/src/core/model-runtime.ts:705`` 等处
正是这样用的：每次 ``stream()`` 都把 ``assertChatModel`` + ``prepareRequest``
（异步鉴权/请求准备）放进 setup。

本端只移植 :func:`lazy_stream`；上游同文件的 :func:`lazyApi`（把动态 import 的
API 实现模块包成 ``ProviderStreams``）**未移植**——本端 provider 是及时导入的，
没有动态模块加载需求。见 ``PORTING.md``。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterable, Awaitable, Callable
from typing import TypeAlias

from .event_stream import AssistantMessageEventStream
from .events import AssistantMessageEvent, ErrorEvent
from .types import AssistantMessage, Model

__all__ = ["lazy_stream", "LazyStreamSetup"]

#: setup 可调用对象：返回（或可等待出）一个 ``AssistantMessageEvent`` 异步可迭代。
#:
#: **允许两种形态**，因为 JS 的 ``await`` 对普通值与 Promise 一视同仁——上游
#: ``setup: () => Promise<AsyncIterable<...>>`` 在 TS 里其实也接受直接返回值的
#: （非 async）函数。Python 需显式区分，故两种都收：
#: ``async def setup() -> Stream``（返回可等待）与 ``def setup() -> Stream``（直接返回）。
LazyStreamSetup: TypeAlias = Callable[
    [], Awaitable[AsyncIterable[AssistantMessageEvent]] | AsyncIterable[AssistantMessageEvent]
]


def _create_setup_error_message(
    model: Model, error: BaseException, timestamp: int
) -> AssistantMessage:
    """构造 setup 失败的 error 消息。对应上游 ``createSetupErrorMessage``。

    ``timestamp`` 取**请求开始**时刻（调用 :func:`lazy_stream` 时），而非失败时刻
    ——这样 ``AssistantMessageEventStream`` 才会为它计入 ``duration_ms``
    （后者要求消息时间戳不早于流创建时刻）。上游同样传 ``startedAt``。
    """
    return AssistantMessage(
        api=model.api,
        provider=model.provider,
        model=model.id,
        stop_reason="error",
        error_message=str(error),
        timestamp=timestamp,
    )


async def _maybe_result(source: object) -> AssistantMessage | None:
    """取内层流的 ``result()``（若有且为可等待对象）。"""
    getter = getattr(source, "result", None)
    if getter is None or not callable(getter):
        return None
    value = getter()
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        resolved = await value
        return resolved if isinstance(resolved, AssistantMessage) else None
    return value if isinstance(value, AssistantMessage) else None


async def _forward_stream(
    target: AssistantMessageEventStream,
    source: AsyncIterable[AssistantMessageEvent],
) -> None:
    """把 ``source`` 的事件转发进 ``target``，并以 inner 的结果收尾。

    对应上游 ``forwardStream``（``api/lazy.ts:31-39``）。上游在 source 无 ``result()``
    时会 ``end(undefined)``；Python 的 ``EventStream.end()`` 需要一个结果对象，
    故退化为"取最后一个终止事件携带的消息"（``done.message`` / ``error.error``），
    仍取不到时构造一个 error 消息——保证 ``result()`` 永不悬挂。
    """
    last_message: AssistantMessage | None = None
    async for event in source:
        target.push(event)
        message = getattr(event, "message", None)
        if message is None:
            message = getattr(event, "error", None)
        if isinstance(message, AssistantMessage):
            last_message = message

    result = await _maybe_result(source)
    if result is None:
        result = last_message
    if result is None:
        result = AssistantMessage(
            stop_reason="error", error_message="Lazy stream produced no result"
        )
    target.end(result)


async def _resolve_setup(source: LazyStreamSetup) -> AsyncIterable[AssistantMessageEvent]:
    """调用 ``setup`` 并归一化为异步可迭代。

    对齐 JS 的 ``await`` 语义（对普通值与 Promise 一视同仁）：先看返回值本身是否
    已是异步可迭代（``EventStream`` / 异步生成器都满足），是则直接用；否则当作
    可等待对象 ``await``。这样 ``async def setup()`` 与 ``def setup()`` 两种写法都能用，
    与上游 TS 里 ``setup: () => Promise<...>`` 实际也接受直接返回值一致。

    先判 ``AsyncIterable`` 而非先判 coroutine，是为了让类型收窄干净
    （``else`` 分支即 ``Awaitable[...]``），无需 ``cast``。
    """
    produced = source()
    if isinstance(produced, AsyncIterable):
        return produced
    return await produced


def lazy_stream(
    model: Model,
    setup: LazyStreamSetup,
) -> AssistantMessageEventStream:
    """同步返回一个流，异步 ``setup`` 在其背后运行。

    对应上游 ``lazyStream``（``api/lazy.ts:41-62``）。签名与语义：

    :param model: 仅用于在 setup 失败时构造 error 消息的 api/provider/model 字段
    :param setup: **无参**可调用对象，返回（或可等待出）一个 ``AssistantMessageEvent``
        异步可迭代——通常是 provider 的 ``stream()`` / ``stream_simple()`` 返回值。
        可写成 ``async def``（内含异步鉴权/请求准备）或普通 ``def``（对齐 JS 里
        ``await`` 接受普通值的语义）。
    :returns: **立即**返回的 :class:`~pi_ai.event_stream.AssistantMessageEventStream`。
        调用方可马上 ``async for`` 消费；setup 期间不会有事件。setup 抛出的任何异常
        都会转成一个 ``error`` 事件 + 以 error 消息收尾（**不会**向调用方抛）。

    典型用法（与下游 ``model-runtime.ts`` 一致）：

    .. code-block:: python

        return lazy_stream(model, lambda: self._prepare_and_stream(model, context, options))
    """
    started_at = int(time.time() * 1000)
    outer = AssistantMessageEventStream()

    async def drive() -> None:
        try:
            inner = await _resolve_setup(setup)
        except BaseException as error:  # noqa: BLE001 - setup 失败必须变成 error 事件
            message = _create_setup_error_message(model, error, started_at)
            outer.push(ErrorEvent(reason="error", error=message))
            outer.end(message)
            return
        try:
            await _forward_stream(outer, inner)
        except BaseException as error:  # noqa: BLE001 - 转发期失败同样必须终结流
            # 走到这里说明是转发/取结果的意外异常（内层流自身的失败已由它自己
            # 编码成 error 事件）。``push``/``end`` 在流已结束后是 no-op，故这里
            # 无条件补发是安全的，不会产生第二个终止事件。
            message = _create_setup_error_message(model, error, started_at)
            outer.push(ErrorEvent(reason="error", error=message))
            outer.end(message)

    asyncio.ensure_future(drive())
    return outer
