"""流式调用入口。

对应上游 ``stream.ts``。薄壳：按 ``model.api`` 取 provider 实现，委托调用。

与上游契约一致：
- 函数**同步返回** ``EventStream``，内部异步驱动事件。
- provider 实现内部不抛异常，失败编码为 error 事件。
"""

from __future__ import annotations

from .event_stream import EventStream
from .events import AssistantMessageEvent, ErrorEvent
from .models import get_api_provider
from .types import AssistantMessage, Context, Model, SimpleStreamOptions, StreamOptions


def stream(
    model: Model,
    context: Context,
    options: StreamOptions | None = None,
) -> EventStream[AssistantMessageEvent, AssistantMessage]:
    """流式调用模型。返回事件流。"""
    impl = get_api_provider(model.api)
    return impl.stream(model, context, options)


def stream_simple(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> EventStream[AssistantMessageEvent, AssistantMessage]:
    """流式调用模型（带思考级别的简化版）。返回事件流。"""
    impl = get_api_provider(model.api)
    return impl.stream_simple(model, context, options)


async def complete(
    model: Model,
    context: Context,
    options: StreamOptions | None = None,
) -> AssistantMessage:
    """非流式：聚合整个事件流得到最终 AssistantMessage。"""
    event_stream = stream(model, context, options)
    return await event_stream.result()


async def complete_simple(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessage:
    """非流式（带思考级别）。"""
    event_stream = stream_simple(model, context, options)
    return await event_stream.result()


def _make_error_stream(
    message: AssistantMessage, reason: str
) -> EventStream[AssistantMessageEvent, AssistantMessage]:
    """构造一个只发单个 error 事件就结束的流（用于 provider 缺失等同步失败）。"""
    es: EventStream[AssistantMessageEvent, AssistantMessage] = EventStream()
    ev = ErrorEvent(reason=reason, error=message)
    es.push(ev)
    es.end(message)
    return es


__all__ = ["stream", "stream_simple", "complete", "complete_simple"]
