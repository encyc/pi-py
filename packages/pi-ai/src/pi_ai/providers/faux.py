"""Faux provider —— 测试用 mock provider。

对应上游 ``providers/faux.ts``。不发真实请求，按预设脚本产出事件，
专供测试与本地开发。通过 ``FauxScript`` 控制输出。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..event_stream import AssistantMessageEventStream, EventStream
from ..events import (
    AssistantMessageEvent,
    DoneEvent,
    ErrorEvent,
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
    StopReason,
    StreamOptions,
    ToolCall,
    TranscriptContext,
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
        stop_reason: StopReason | None = None,
    ) -> None:
        self.text = text
        self.tool_calls = tool_calls or []
        self.error = error
        self.usage = usage
        self.stop_reason = stop_reason


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
    context: Context | TranscriptContext,
    options: StreamOptions | None,
) -> EventStream[AssistantMessageEvent, AssistantMessage]:
    # context 未使用：faux 按脚本产出，不回放历史。仍归一化以保持入参契约一致。
    del context
    script = _pop_script()
    es: AssistantMessageEventStream = AssistantMessageEventStream()

    async def drive() -> None:
        output = AssistantMessage(
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=script.usage or Usage(),
            stop_reason="pending",
            timestamp=int(time.time() * 1000),
        )

        # error 路径
        if script.error is not None:
            output.stop_reason = "error"
            output.error_message = script.error
            es.push(ErrorEvent(reason="error", error=output))
            es.end(output)
            return

        es.push(StartEvent(partial=output.model_copy(deep=True)))

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
        final_reason: StopReason = script.stop_reason or (
            "toolUse" if script.tool_calls else "stop"
        )
        # ``pending`` 是"还没结束"的中间态，脚本不该以它收尾。
        # 上游 faux.ts:441 直接 throw，由外层 catch 转成 error 事件。
        if final_reason == "pending":
            output.stop_reason = "error"
            output.error_message = "Faux response ended without a stop reason"
            es.push(ErrorEvent(reason="error", error=output))
            es.end(output)
            return
        # 上游 faux.ts:444-449：``error`` 与 ``aborted`` 必须发 **ErrorEvent**。
        # 历史 bug：此前一律发 ``DoneEvent``，而 ``DoneEvent.reason`` 只接受
        # ``stop|length|toolUse``，于是 ``FauxScript(stop_reason="aborted")`` 会在
        # push 时抛 ValidationError；该异常发生在 asyncio task 内、无人 await，
        # 流因此**永不结束**，消费者（含 agent-core 测试）永久挂起。
        if final_reason in ("error", "aborted"):
            output.stop_reason = final_reason
            if final_reason == "error" and not output.error_message:
                output.error_message = "Faux response ended with an error"
            es.push(ErrorEvent(reason=final_reason, error=output))
            es.end(output)
            return
        output.stop_reason = final_reason
        es.push(DoneEvent(reason=output.stop_reason, message=output))
        es.end(output)

    async def guarded_drive() -> None:
        """驱动流，并**保证**任何异常都会终结流。

        没有这层保护时，``drive()`` 里的任何异常（如构造事件时的校验失败）都只会
        留在无人 await 的 asyncio task 里：流既不 done 也不 error，消费者
        ``async for`` 永久挂起。真实 provider 都有等价的 ``except BaseException``
        兜底，faux 此前缺这一层，是上面那个坑能"挂死测试"的根因。
        """
        try:
            await drive()
        except BaseException as exc:  # noqa: BLE001 - 绝不允许异常逃逸出驱动任务
            error_message = AssistantMessage(
                api=model.api,
                provider=model.provider,
                model=model.id,
                stop_reason="error",
                error_message=f"Faux driver failed: {exc!r}",
                timestamp=int(time.time() * 1000),
            )
            es.push(ErrorEvent(reason="error", error=error_message))
            es.end(error_message)

    asyncio.ensure_future(guarded_drive())
    return es


class _FauxProvider:
    """实现 ProviderStreams 协议。"""

    def stream(
        self,
        model: Model,
        context: TranscriptContext,
        options: StreamOptions | None = None,
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        return _run_faux(model, context, options)

    def stream_simple(
        self,
        model: Model,
        context: TranscriptContext,
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
