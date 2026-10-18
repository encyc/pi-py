"""流式调用入口。

对应上游 ``stream.ts``。薄壳：把调用方的 ``Context`` 归一化为
``TranscriptContext``，按 ``model.api`` 取 provider 实现，委托调用。

与上游契约一致：
- 函数**同步返回** ``EventStream``，内部异步驱动事件。
- provider 实现内部不抛异常，失败编码为 error 事件。
- **归一化在此完成**（对应上游 ``compat.ts:254-263``）：``Context.system_prompt``
  / ``Context.tools`` 折进 transcript 首条 ``SystemMessage`` 后才交给 provider，
  因此 provider 面向的入参是 ``TranscriptContext``（v1.3.0 契约）。
"""

from __future__ import annotations

from .event_stream import EventStream
from .events import AssistantMessageEvent, ErrorEvent
from .models import get_api_provider
from .transcript import ensure_transcript
from .types import (
    AssistantMessage,
    Context,
    Model,
    SimpleStreamOptions,
    StreamOptions,
    TranscriptContext,
)


def normalize_context_for_request(context: Context | TranscriptContext) -> TranscriptContext:
    """公开入口的归一化别名（语义见 :func:`pi_ai.transcript.ensure_transcript`）。"""
    return ensure_transcript(context)


def stream(
    model: Model,
    context: Context | TranscriptContext,
    options: StreamOptions | None = None,
) -> EventStream[AssistantMessageEvent, AssistantMessage]:
    """流式调用模型。返回事件流。"""
    impl = get_api_provider(model.api)
    return impl.stream(model, normalize_context_for_request(context), options)


def stream_simple(
    model: Model,
    context: Context | TranscriptContext,
    options: SimpleStreamOptions | None = None,
) -> EventStream[AssistantMessageEvent, AssistantMessage]:
    """流式调用模型（带思考级别的简化版）。返回事件流。"""
    impl = get_api_provider(model.api)
    return impl.stream_simple(model, normalize_context_for_request(context), options)


async def complete(
    model: Model,
    context: Context | TranscriptContext,
    options: StreamOptions | None = None,
) -> AssistantMessage:
    """非流式：聚合整个事件流得到最终 AssistantMessage。"""
    event_stream = stream(model, context, options)
    return await event_stream.result()


async def complete_simple(
    model: Model,
    context: Context | TranscriptContext,
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


__all__ = [
    "stream",
    "stream_simple",
    "complete",
    "complete_simple",
    "normalize_context_for_request",
]
