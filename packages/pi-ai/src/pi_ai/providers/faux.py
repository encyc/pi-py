"""Faux provider —— 测试用 mock provider。

对应上游 ``providers/faux.ts``。不发真实请求，按预设脚本产出事件，
专供测试与本地开发。通过 ``FauxScript`` 控制输出。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..event_stream import EventStream
from ..events import (
    AssistantMessageEvent,
    DoneEvent,
    StartEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from ..types import (
    AssistantMessage,
    Context,
    Model,
    SimpleStreamOptions,
    StreamOptions,
    ToolCall,
    Usage,
)


class FauxScript:
    """预设的 faux 输出脚本。

    - ``text``：要产出的文本（会按字符切片成 text_delta）。
    - ``tool_calls``：要产出的工具调用列表。
    - ``error``：若设置，流以 error 终止（用于测试错误路径）。
    - ``usage``：覆盖默认 usage。
    """

    def __init__(
        self,
        text: str = "",
        tool_calls: list[ToolCall] | None = None,
        error: str | None = None,
        usage: Usage | None = None,
    ) -> None:
        self.text = text
        self.tool_calls = tool_calls or []
        self.error = error
        self.usage = usage


#: 全局脚本栈。测试 push 一个脚本，provider 取栈顶（或最后一个）。
_scripts: list[FauxScript] = []


def push_script(script: FauxScript) -> None:
    """压入一个 faux 脚本（provider 会取最后一个）。"""
    _scripts.append(script)


def clear_scripts() -> None:
    _scripts.clear()


def _pop_script() -> FauxScript:
    if not _scripts:
        return FauxScript(text="faux response")
    # FIFO：先 push 的先消费（便于多轮脚本按顺序匹配多个 turn）
    return _scripts.pop(0)


def _run_faux(
    model: Model,
    context: Context,
    options: StreamOptions | None,
) -> EventStream[AssistantMessageEvent, AssistantMessage]:
    script = _pop_script()
    es: EventStream[AssistantMessageEvent, AssistantMessage] = EventStream()

    async def drive() -> None:
        output = AssistantMessage(
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=script.usage or Usage(),
            timestamp=int(time.time() * 1000),
        )

        # error 路径
        if script.error is not None:
            output.stop_reason = "error"
            output.error_message = script.error
            from ..events import ErrorEvent as _ErrEv

            es.push(_ErrEv(reason="error", error=output))
            es.end(output)
            return

        es.push(StartEvent(partial=output))

        content_index = 0
        # 文本块
        if script.text:
            from ..types import TextContent

            tc = TextContent(text="")
            output.content.append(tc)
            es.push(TextStartEvent(content_index=content_index, partial=output))
            # 按字符切片，原地累加到模型实例
            for ch in script.text:
                tc.text += ch
                es.push(TextDeltaEvent(content_index=content_index, delta=ch, partial=output))
                await asyncio.sleep(0)
            es.push(TextEndEvent(content_index=content_index, content=tc.text, partial=output))
            content_index += 1

        # 工具调用块
        for call in script.tool_calls:
            output.content.append(call)
            es.push(ToolCallStartEvent(content_index=content_index, partial=output))
            es.push(ToolCallEndEvent(content_index=content_index, tool_call=call, partial=output))
            content_index += 1

        # 终止
        if script.tool_calls:
            output.stop_reason = "toolUse"
        else:
            output.stop_reason = "stop"
        es.push(DoneEvent(reason=output.stop_reason, message=output))
        es.end(output)

    asyncio.ensure_future(drive())
    return es


class _FauxProvider:
    """实现 ProviderStreams 协议。"""

    def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None = None,
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        return _run_faux(model, context, options)

    def stream_simple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        return _run_faux(model, context, options)


faux_api_provider: Any = _FauxProvider()

FAUX_MODEL = Model(
    id="faux",
    name="Faux (test)",
    api="faux",
    provider="faux",
    base_url="",
    reasoning=False,
    input=["text"],
    context_window=1000000,
    max_tokens=4096,
)


__all__ = ["FauxScript", "push_script", "clear_scripts", "faux_api_provider", "FAUX_MODEL"]
