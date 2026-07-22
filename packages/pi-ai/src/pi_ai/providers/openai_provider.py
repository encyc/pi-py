"""OpenAI Chat Completions provider。

对应上游 ``api/openai-completions.ts``（48KB 的核心文件）。Python 版聚焦
Chat Completions 协议（覆盖 OpenAI 及大量兼容厂商）。

核心机制（务必与上游一致）：
1. ``AsyncOpenAI`` 客户端，``max_retries=0``（重试是外层关注点）。
2. **工具调用累加**：按 ``delta.index`` 和 ``delta.id`` 双索引，原始参数 JSON
   字符串拼接进 ``partial_args``，**每个增量重新解析**（用 ``json-repair``
   容忍不完整 JSON）。
3. **文本/思考是单槽位**：同一时间只有一个激活的 text 块和一个 thinking 块。
4. **usage 在 chunk 级别**提取（``stream_options={"include_usage": True}``）。
5. **错误不内联重试**：编码为 error 事件（``stopReason="error"`` + ``errorMessage``）。
6. **finish_reason 缺失 = 异常**。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from ..event_stream import EventStream
from ..events import (
    AssistantMessageEvent,
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
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
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)

#: 思考内容可能的字段名（不同厂商差异），第一个非空者胜出。
_THINKING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")

#: OpenAI finish_reason -> pi StopReason
_STOP_REASON_MAP: dict[str, StopReason] = {
    "stop": "stop",
    "end_turn": "stop",
    "length": "length",
    "tool_calls": "toolUse",
    "function_call": "toolUse",
    "content_filter": "error",
    "network_error": "error",
}


# ============================================================
# 流式增量解析的块状态
# ============================================================


class _ToolCallBlock:
    """工具调用的流式累加状态。

    保留一个放进 ``output.content`` 的真实 ``ToolCall`` 实例（保证 content 始终
    持有合法模型对象），累加状态（``partial_args``/``stream_index``/``content_index``）
    仅解析期使用。
    """

    __slots__ = ("tool_call", "partial_args", "stream_index", "content_index")

    def __init__(self, content_index: int, id: str = "", name: str = "") -> None:
        self.tool_call = ToolCall(id=id, name=name, arguments={})
        self.partial_args: str = ""
        self.stream_index: int | None = None
        self.content_index = content_index


# ============================================================
# usage 解析
# ============================================================


def _parse_chunk_usage(raw: Any, model: Model) -> Usage:
    """从 OpenAI chunk usage 提取 token 统计。对应上游 ``parseChunkUsage``。"""
    prompt_tokens = getattr(raw, "prompt_tokens", 0) or 0
    ptd = getattr(raw, "prompt_tokens_details", None)
    cached = getattr(ptd, "cached_tokens", None) if ptd else None
    cache_read = cached or getattr(raw, "prompt_cache_hit_tokens", 0) or 0
    cache_write = getattr(ptd, "cache_write_tokens", 0) if ptd else 0

    # cached_tokens 是缓存"读取"命中，不减去；input 扣除缓存部分使恒等式成立
    input_tokens = max(0, prompt_tokens - cache_read - cache_write)
    output_tokens = getattr(raw, "completion_tokens", 0) or 0
    ctd = getattr(raw, "completion_tokens_details", None)
    reasoning = getattr(ctd, "reasoning_tokens", 0) if ctd else 0

    total = input_tokens + output_tokens + cache_read + cache_write
    usage = Usage(
        input=input_tokens,
        output=output_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=reasoning or None,
        total_tokens=total,
    )
    _apply_cost(usage, model)
    return usage


def _apply_cost(usage: Usage, model: Model) -> None:
    """按 model.cost 费率计算 cost。"""
    rates = model.cost
    # 每百万 token -> 每 token
    per_million = 1_000_000
    c = UsageCost(
        input=usage.input * rates.input / per_million,
        output=usage.output * rates.output / per_million,
        cache_read=usage.cache_read * rates.cache_read / per_million,
        cache_write=usage.cache_write * rates.cache_write / per_million,
    )
    c.total = c.input + c.output + c.cache_read + c.cache_write
    usage.cost = c


# ============================================================
# 主流式函数
# ============================================================


def _create_client(
    model: Model, api_key: str, options_headers: dict[str, str | None] | None
) -> AsyncOpenAI:
    headers: dict[str, Any] = dict(model.headers or {})
    if options_headers:
        headers.update(options_headers)
    return AsyncOpenAI(
        api_key=api_key,
        base_url=model.base_url,
        default_headers=headers or None,
        max_retries=0,
    )


def _convert_tools(tools: list[Any]) -> list[dict[str, Any]]:
    """ToolDef 列表 -> OpenAI tools 格式。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.to_json_schema() if hasattr(t, "to_json_schema") else t.parameters,
                "strict": False,
            },
        }
        for t in tools
    ]


def _convert_messages(
    context: Context,
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    """Context.messages -> OpenAI messages。返回 (messages, dev_headers)。"""
    out: list[dict[str, Any]] = []
    # 系统提示
    if context.system_prompt:
        out.append({"role": "system", "content": context.system_prompt})

    for msg in context.messages:
        if isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            else:
                # 内容块数组 -> OpenAI 多模态格式
                parts: list[dict[str, Any]] = []
                for block in content:
                    if block.type == "text":
                        parts.append({"type": "text", "text": block.text})
                    elif block.type == "image":
                        img: dict[str, Any] = {"url": f"data:{block.mime_type};base64,{block.data}"}
                        parts.append({"type": "image_url", "image_url": img})
                out.append({"role": "user", "content": parts})
        elif isinstance(msg, AssistantMessage):
            # 文本 + thinking -> content；tool_calls 单独。
            # 注：pydantic-mypy 插件对 Annotated 判别联合的 content 字段解析有偏差，
            # 故用 __dict__ 直取绕过，运行时类型仍由 isinstance 保证。
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in msg.__dict__["content"]:
                if isinstance(block, ToolCall):
                    tool_calls.append(
                        {
                            "id": block.id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": json.dumps(block.arguments),
                            },
                        }
                    )
            entry: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                entry["content"] = "\n".join(text_parts)
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
        elif isinstance(msg, ToolResultMessage):
            text = "\n".join(b.text for b in msg.content if b.type == "text")
            out.append(
                {
                    "role": "tool",
                    "content": text or "",
                    "tool_call_id": msg.tool_call_id,
                }
            )
    return out, None


def _parse_streaming_json(s: str) -> dict[str, Any]:
    """容错解析不完整的 JSON 字符串。对应上游 ``parseStreamingJson``。"""
    if not s:
        return {}
    try:
        result: dict[str, Any] = json.loads(s)
        return result
    except Exception:
        pass
    try:
        from json_repair import repair_json

        repaired = repair_json(s, return_objects=True)
        return repaired if isinstance(repaired, dict) else {}
    except Exception:
        return {}


def _run_openai_stream(
    model: Model,
    context: Context,
    options: StreamOptions | None,
) -> EventStream[AssistantMessageEvent, AssistantMessage]:
    es: EventStream[AssistantMessageEvent, AssistantMessage] = EventStream()
    api_key = (options.api_key if options else None) or _resolve_api_key()

    async def drive() -> None:
        output = AssistantMessage(
            api=model.api,
            provider=model.provider,
            model=model.id,
            timestamp=int(time.time() * 1000),
        )
        client = _create_client(model, api_key, options.headers if options else None)

        # 构建请求参数
        messages, _ = _convert_messages(context)
        params: dict[str, Any] = {
            "model": model.id,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if context.tools:
            params["tools"] = _convert_tools(context.tools)
        if options and options.temperature is not None:
            params["temperature"] = options.temperature
        if options and options.max_tokens is not None:
            # 优先 max_completion_tokens，回退 max_tokens
            params["max_tokens"] = options.max_tokens
        if options and options.timeout_ms is not None:
            params["timeout"] = options.timeout_ms / 1000

        # 流式块状态：用真实模型实例累加，保证 output.content 始终持合法对象
        text_block: TextContent | None = None
        text_content_idx: int | None = None
        thinking_block: ThinkingContent | None = None
        thinking_content_idx: int | None = None
        has_finish_reason = False
        tool_blocks_by_index: dict[int, _ToolCallBlock] = {}
        tool_blocks_by_id: dict[str, _ToolCallBlock] = {}

        def ensure_tool_block(
            index: int | None, tool_id: str | None, func_name: str | None
        ) -> _ToolCallBlock:
            block = tool_blocks_by_index.get(index) if index is not None else None
            if block is None and tool_id:
                block = tool_blocks_by_id.get(tool_id)
            if block is None:
                # 新建：把 ToolCall 实例直接放进 content，记录其索引
                cidx = len(output.content)
                block = _ToolCallBlock(content_index=cidx, id=tool_id or "", name=func_name or "")
                output.content.append(block.tool_call)
                if index is not None:
                    block.stream_index = index
                    tool_blocks_by_index[index] = block
                if tool_id:
                    tool_blocks_by_id[tool_id] = block
                es.push(ToolCallStartEvent(content_index=cidx, partial=output))
            if block.stream_index is None and index is not None:
                block.stream_index = index
                tool_blocks_by_index[index] = block
            if not block.tool_call.id and tool_id:
                block.tool_call.id = tool_id
                tool_blocks_by_id[tool_id] = block
            if not block.tool_call.name and func_name:
                block.tool_call.name = func_name
            return block

        try:
            stream_obj = await client.chat.completions.create(**params)
            async for chunk in stream_obj:
                # usage（chunk 级）
                if chunk.usage:
                    output.usage = _parse_chunk_usage(chunk.usage, model)

                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                # finish_reason
                if choice.finish_reason:
                    mapped: StopReason = _STOP_REASON_MAP.get(choice.finish_reason, "error")
                    output.stop_reason = mapped
                    if mapped == "error":
                        output.error_message = f"finish_reason: {choice.finish_reason}"
                    has_finish_reason = True

                # 文本增量（单槽位）
                delta_content = getattr(delta, "content", None)
                if delta_content:
                    if text_block is None:
                        text_block = TextContent(text="")
                        text_content_idx = len(output.content)
                        output.content.append(text_block)
                        es.push(TextStartEvent(content_index=text_content_idx, partial=output))
                    text_block.text += delta_content
                    es.push(
                        TextDeltaEvent(
                            content_index=text_content_idx,
                            delta=delta_content,
                            partial=output,
                        )
                    )

                # 思考增量（三字段名，单槽位）
                for field in _THINKING_FIELDS:
                    thinking_val = getattr(delta, field, None)
                    if thinking_val:
                        if thinking_block is None:
                            thinking_block = ThinkingContent(thinking="")
                            thinking_content_idx = len(output.content)
                            output.content.append(thinking_block)
                            es.push(
                                ThinkingStartEvent(
                                    content_index=thinking_content_idx, partial=output
                                )
                            )
                        thinking_block.thinking += thinking_val
                        es.push(
                            ThinkingDeltaEvent(
                                content_index=thinking_content_idx,
                                delta=thinking_val,
                                partial=output,
                            )
                        )
                        break

                # 工具调用增量（双索引 + 字符串累加 + 每增量重新解析）
                tool_calls_delta = getattr(delta, "tool_calls", None)
                if tool_calls_delta:
                    for tc_delta in tool_calls_delta:
                        idx = getattr(tc_delta, "index", None)
                        tc_id = getattr(tc_delta, "id", None)
                        func = getattr(tc_delta, "function", None)
                        func_name = getattr(func, "name", None) if func else None
                        args_chunk = getattr(func, "arguments", None) if func else None

                        block = ensure_tool_block(idx, tc_id, func_name)
                        delta_str = ""
                        if args_chunk:
                            block.partial_args += args_chunk
                            block.tool_call.arguments = _parse_streaming_json(block.partial_args)
                            delta_str = args_chunk
                        es.push(
                            ToolCallDeltaEvent(
                                content_index=block.content_index,
                                delta=delta_str,
                                partial=output,
                            )
                        )

            # ---- 流结束，发各块的 *_end 事件 ----
            if text_block is not None and text_content_idx is not None:
                es.push(
                    TextEndEvent(
                        content_index=text_content_idx,
                        content=text_block.text,
                        partial=output,
                    )
                )
            if thinking_block is not None and thinking_content_idx is not None:
                es.push(
                    ThinkingEndEvent(
                        content_index=thinking_content_idx,
                        content=thinking_block.thinking,
                        partial=output,
                    )
                )
            for block in tool_blocks_by_index.values():
                es.push(
                    ToolCallEndEvent(
                        content_index=block.content_index,
                        tool_call=block.tool_call,
                        partial=output,
                    )
                )

            # 终止判定（finish_reason 缺失 = 异常，与上游一致）
            if not has_finish_reason:
                raise RuntimeError("Stream ended without finish_reason")
            if output.stop_reason == "aborted":
                raise RuntimeError("Request was aborted")
            if output.stop_reason == "error":
                raise RuntimeError(output.error_message or "provider error")

            es.push(DoneEvent(reason=output.stop_reason, message=output))
            es.end(output)

        except BaseException as exc:  # noqa: BLE001
            output.stop_reason = "error"
            output.error_message = _format_error(exc)
            es.push(ErrorEvent(reason="error", error=output))
            es.end(output)

    asyncio.ensure_future(drive())
    return es


def _resolve_api_key() -> str:
    import os

    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "未找到 API key：请在 options.api_key 传入，或设置 OPENAI_API_KEY 环境变量"
        )
    return key


def _format_error(exc: BaseException) -> str:
    """格式化 provider 错误。对应上游 ``formatProviderError``。"""
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        try:
            body = exc.response.text[:4000]
        except Exception:  # noqa: BLE001
            body = str(exc)
        return f"{status}: {body}"
    if isinstance(exc, APITimeoutError):
        return f"timeout: {exc}"
    if isinstance(exc, APIConnectionError):
        return f"connection error: {exc}"
    return str(exc)


# ============================================================
# provider 句柄
# ============================================================


class _OpenAIProvider:
    def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None = None,
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        return _run_openai_stream(model, context, options)

    def stream_simple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        return _run_openai_stream(model, context, options)


openai_api_provider: Any = _OpenAIProvider()


# ============================================================
# 内置 OpenAI 模型（精简，后续由配置/生成补充）
# ============================================================

from ..types import ModelCost  # noqa: E402

OPENAI_MODELS: list[Model] = [
    Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        reasoning=False,
        input=["text", "image"],
        cost=ModelCost(input=2.5, output=10, cache_read=1.25),
        context_window=128000,
        max_tokens=16384,
    ),
    Model(
        id="gpt-4o-mini",
        name="GPT-4o mini",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        reasoning=False,
        input=["text", "image"],
        cost=ModelCost(input=0.15, output=0.6, cache_read=0.075),
        context_window=128000,
        max_tokens=16384,
    ),
]


__all__ = ["openai_api_provider", "OPENAI_MODELS"]
