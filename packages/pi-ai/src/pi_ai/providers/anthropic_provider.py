"""Anthropic Messages provider。

对应上游 ``api/anthropic-messages.ts``（42KB）。使用 anthropic Python SDK 的
类型化流事件（``RawMessageStreamEvent``），无需手动解析 SSE（上游手动解析约 190 行，
Python SDK 原生暴露类型化事件）。

核心机制（务必与上游一致）：
1. **system 是独立参数**（非 message），支持 cache_control。
2. **思考支持**：两种模式 —— adaptive（新模型，effort）与 budget（旧模型，budget_tokens）。
   thinking 块的 signature 增量累加（signature_delta），回放时必须带 signature。
3. **工具结果合并**：连续 toolResult 消息合并进一个 user 轮次（Anthropic 要求
   tool_result 块在 user 角色消息里）。
4. **block 索引映射**：Anthropic 的 ``event.index`` ≠ pi 的 contentIndex，需按 index 查找。
5. **usage 双点提取**：message_start 拿初始值，message_delta 仅在 ``!= null`` 时更新。
6. **错误编码为事件**，``max_retries=0``。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from anthropic import AsyncAnthropic

from ..event_stream import EventStream
from ..events import (
    AssistantMessageEvent,
    DoneEvent,
    ErrorEvent,
    StartEvent,
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
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
    UsageCost,
)

#: Anthropic stop_reason -> pi StopReason 映射。
_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "toolUse",
    "pause_turn": "stop",
    "stop_sequence": "stop",
    "refusal": "error",
    "sensitive": "error",
}


# ============================================================
# 客户端创建
# ============================================================


def _create_client(
    model: Model, api_key: str, options_headers: dict[str, str | None] | None
) -> AsyncAnthropic:
    headers: dict[str, str] = dict(model.headers or {})
    if options_headers:
        for k, v in options_headers.items():
            if v is not None:
                headers[k] = v
    return AsyncAnthropic(
        api_key=api_key,
        base_url=model.base_url,
        default_headers=headers or None,
        max_retries=0,
    )


def _resolve_api_key() -> str:
    import os

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError(
            "未找到 API key：请在 options.api_key 传入，或设置 ANTHROPIC_API_KEY 环境变量"
        )
    return key


# ============================================================
# 消息转换
# ============================================================


def _convert_tools(tools: list[Any]) -> list[dict[str, Any]]:
    """ToolDef -> Anthropic tools 格式。input_schema 始终含 type/properties/required。"""
    out: list[dict[str, Any]] = []
    for tool in tools:
        schema = tool.to_json_schema() if hasattr(tool, "to_json_schema") else tool.parameters
        out.append(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": {
                    "type": "object",
                    "properties": schema.get("properties", {}),
                    "required": schema.get("required", []),
                },
            }
        )
    return out


def _convert_messages(context: Context) -> tuple[list[dict[str, Any]], Any]:
    """Context -> (anthropic messages, system)。

    system 为 list[{type:text, text, cache_control?}] 或 None。
    连续 toolResult 消息合并进一个 user 轮次。
    """
    from ..types import AssistantMessage, ToolResultMessage, UserMessage

    system: list[dict[str, Any]] | None = None
    if context.system_prompt:
        system = [{"type": "text", "text": context.system_prompt}]

    messages: list[dict[str, Any]] = []
    msgs = context.messages
    i = 0
    while i < len(msgs):
        msg = msgs[i]
        if isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, str):
                messages.append({"role": "user", "content": content})
            else:
                parts: list[dict[str, Any]] = []
                for block in content:
                    if block.type == "text" and block.text:
                        parts.append({"type": "text", "text": block.text})
                    elif block.type == "image":
                        parts.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": block.mime_type,
                                    "data": block.data,
                                },
                            }
                        )
                if parts:
                    messages.append({"role": "user", "content": parts})
        elif isinstance(msg, AssistantMessage):
            # text + thinking（带 signature 回放）+ tool_use
            parts = []
            for block in msg.__dict__["content"]:
                if isinstance(block, TextContent) and block.text:
                    parts.append({"type": "text", "text": block.text})
                elif isinstance(block, ThinkingContent):
                    if block.redacted:
                        # 脱敏思考：用 redacted_thinking 回放
                        parts.append(
                            {"type": "redacted_thinking", "data": block.thinking_signature or ""}
                        )
                    elif block.thinking_signature:
                        # 正常思考：signature 必须带回
                        parts.append(
                            {
                                "type": "thinking",
                                "thinking": block.thinking,
                                "signature": block.thinking_signature,
                            }
                        )
                    # 缺 signature 的思考块跳过（Anthropic 会拒绝无 signature 的思考块）
                elif isinstance(block, ToolCall):
                    parts.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.arguments,
                        }
                    )
            if parts:
                messages.append({"role": "assistant", "content": parts})
        elif isinstance(msg, ToolResultMessage):
            # 连续 toolResult 合并进一个 user 轮次
            tool_results: list[dict[str, Any]] = []
            while i < len(msgs):
                tr = msgs[i]
                if not isinstance(tr, ToolResultMessage):
                    break
                tr_content: list[dict[str, Any]] = []
                for block in tr.__dict__["content"]:
                    if block.type == "text":
                        tr_content.append({"type": "text", "text": block.text})
                    elif block.type == "image":
                        tr_content.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": block.mime_type,
                                    "data": block.data,
                                },
                            }
                        )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tr.tool_call_id,
                        "content": tr_content or "",
                        "is_error": tr.is_error,
                    }
                )
                i += 1
            messages.append({"role": "user", "content": tool_results})
            continue  # i 已在内部推进
        i += 1

    return messages, system


# ============================================================
# thinking 配置
# ============================================================


def _build_thinking_config(model: Model, options: StreamOptions | None) -> dict[str, Any] | None:
    """构建 thinking 请求参数。返回 None 表示不启用。"""
    compat = model.compat or {}
    force_adaptive = compat.get("forceAdaptiveThinking", False)

    # 从 options 的 extra 字段取 anthropic 私有选项（StreamOptions 允许 extra）
    thinking_enabled = None
    thinking_budget = None
    effort = None
    if options is not None:
        thinking_enabled = getattr(options, "thinking_enabled", None)
        thinking_budget = getattr(options, "thinking_budget_tokens", None)
        effort = getattr(options, "effort", None)

    if thinking_enabled is True:
        if force_adaptive:
            cfg: dict[str, Any] = {"type": "adaptive", "display": "summarized"}
            if effort:
                cfg["effort"] = effort
            return cfg
        else:
            return {
                "type": "enabled",
                "budget_tokens": thinking_budget or 1024,
                "display": "summarized",
            }
    if (
        thinking_enabled is False
        and model.thinking_level_map
        and model.thinking_level_map.get("off") is not None
    ):
        return {"type": "disabled"}
    return None


# ============================================================
# usage / cost
# ============================================================


def _apply_cost(usage: Usage, model: Model) -> None:
    rates = model.cost
    per_million = 1_000_000
    c = UsageCost(
        input=usage.input * rates.input / per_million,
        output=usage.output * rates.output / per_million,
        cache_read=usage.cache_read * rates.cache_read / per_million,
        cache_write=usage.cache_write * rates.cache_write / per_million,
    )
    c.total = c.input + c.output + c.cache_read + c.cache_write
    usage.cost = c


def _update_usage_from_start(usage_obj: Any, model: Model) -> Usage:
    """从 message_start 的 usage 提取初始值。"""
    u = Usage(
        input=getattr(usage_obj, "input_tokens", 0) or 0,
        output=getattr(usage_obj, "output_tokens", 0) or 0,
        cache_read=getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
        cache_write=getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
    )
    # cache_write_1h（仅 message_start 有）
    cc = getattr(usage_obj, "cache_creation", None)
    if cc is not None:
        u.cache_write_1h = getattr(cc, "ephemeral_1h_input_tokens", 0) or 0
    u.total_tokens = u.input + u.output + u.cache_read + u.cache_write
    _apply_cost(u, model)
    return u


def _update_usage_from_delta(usage: Usage, delta_usage: Any, model: Model) -> None:
    """message_delta 的 usage：仅在字段 != null 时更新（保留 message_start 的值）。"""
    if getattr(delta_usage, "input_tokens", None) is not None:
        usage.input = delta_usage.input_tokens
    if getattr(delta_usage, "output_tokens", None) is not None:
        usage.output = delta_usage.output_tokens
    if getattr(delta_usage, "cache_read_input_tokens", None) is not None:
        usage.cache_read = delta_usage.cache_read_input_tokens
    if getattr(delta_usage, "cache_creation_input_tokens", None) is not None:
        usage.cache_write = delta_usage.cache_creation_input_tokens
    # thinking_tokens 是 output 的子集
    otd = getattr(delta_usage, "output_tokens_details", None)
    if otd is not None:
        thinking = getattr(otd, "thinking_tokens", None)
        if thinking is not None:
            usage.reasoning = thinking
    usage.total_tokens = usage.input + usage.output + usage.cache_read + usage.cache_write
    _apply_cost(usage, model)


# ============================================================
# 流式块状态
# ============================================================


class _Block:
    """流式累加块。记录 anthropic index -> pi content_index 映射与临时状态。"""

    __slots__ = ("anthropic_index", "content_index", "partial_json", "tool_call")

    def __init__(self, anthropic_index: int, content_index: int) -> None:
        self.anthropic_index = anthropic_index
        self.content_index = content_index
        self.partial_json: str = ""
        self.tool_call: ToolCall | None = None


def _parse_streaming_json(s: str) -> dict[str, Any]:
    """容错解析不完整 JSON。"""
    if not s:
        return {}
    import json

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


# ============================================================
# 主流式函数
# ============================================================


def _run_anthropic_stream(
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

        messages, system = _convert_messages(context)
        params: dict[str, Any] = {
            "model": model.id,
            "messages": messages,
            "max_tokens": (
                options.max_tokens if options and options.max_tokens else model.max_tokens
            )
            or 4096,
            "stream": True,
        }
        if system:
            params["system"] = system
        if context.tools:
            params["tools"] = _convert_tools(context.tools)
        if options and options.temperature is not None:
            params["temperature"] = options.temperature
        if options and options.timeout_ms is not None:
            params["timeout"] = options.timeout_ms / 1000

        thinking_cfg = _build_thinking_config(model, options)
        if thinking_cfg is not None:
            params["thinking"] = thinking_cfg

        # block 状态：anthropic_index -> _Block
        blocks: dict[int, _Block] = {}

        def find_block_by_anthropic_idx(idx: int) -> _Block | None:
            return blocks.get(idx)

        try:
            response = await client.messages.create(**params)
            # 遍历类型化流事件
            async for event in response:
                etype = event.type

                if etype == "message_start":
                    msg_obj = getattr(event, "message", None)
                    if msg_obj is not None:
                        output.response_id = getattr(msg_obj, "id", None)
                        usage_obj = getattr(msg_obj, "usage", None)
                        if usage_obj is not None:
                            output.usage = _update_usage_from_start(usage_obj, model)
                    es.push(StartEvent(partial=output))

                elif etype == "content_block_start":
                    cb = getattr(event, "content_block", None)
                    aidx = getattr(event, "index", 0)
                    cidx = len(output.content)
                    bt = getattr(cb, "type", None) if cb else None
                    # blk 在各 elif 分支复用；声明为 Optional 以兼容后续分支的查找赋值
                    blk: _Block | None = None
                    if bt == "text":
                        output.content.append(TextContent(text=""))
                        blk = _Block(aidx, cidx)
                        blocks[aidx] = blk
                        es.push(TextStartEvent(content_index=cidx, partial=output))
                    elif bt == "thinking":
                        output.content.append(ThinkingContent(thinking="", thinking_signature=""))
                        blk = _Block(aidx, cidx)
                        blocks[aidx] = blk
                        es.push(ThinkingStartEvent(content_index=cidx, partial=output))
                    elif bt == "redacted_thinking":
                        data = getattr(cb, "data", "") or ""
                        output.content.append(
                            ThinkingContent(
                                thinking="[Reasoning redacted]",
                                thinking_signature=data,
                                redacted=True,
                            )
                        )
                        blk = _Block(aidx, cidx)
                        blocks[aidx] = blk
                        es.push(ThinkingStartEvent(content_index=cidx, partial=output))
                    elif bt == "tool_use":
                        tc = ToolCall(
                            id=getattr(cb, "id", "") or "",
                            name=getattr(cb, "name", "") or "",
                            arguments=getattr(cb, "input", {}) or {},
                        )
                        output.content.append(tc)
                        blk = _Block(aidx, cidx)
                        blk.tool_call = tc
                        blocks[aidx] = blk
                        es.push(ToolCallStartEvent(content_index=cidx, partial=output))

                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    aidx = getattr(event, "index", 0)
                    blk = find_block_by_anthropic_idx(aidx)
                    if blk is None or delta is None:
                        continue
                    dtype = getattr(delta, "type", None)
                    cidx = blk.content_index
                    if dtype == "text_delta":
                        text_val = getattr(delta, "text", "")
                        block = output.content[cidx]
                        if isinstance(block, TextContent):
                            block.text += text_val
                        es.push(TextDeltaEvent(content_index=cidx, delta=text_val, partial=output))
                    elif dtype == "thinking_delta":
                        thinking_val = getattr(delta, "thinking", "")
                        block = output.content[cidx]
                        if isinstance(block, ThinkingContent):
                            block.thinking += thinking_val
                        es.push(
                            ThinkingDeltaEvent(
                                content_index=cidx, delta=thinking_val, partial=output
                            )
                        )
                    elif dtype == "input_json_delta":
                        pj = getattr(delta, "partial_json", "")
                        if blk.tool_call is not None:
                            blk.partial_json += pj
                            blk.tool_call.arguments = _parse_streaming_json(blk.partial_json)
                        es.push(ToolCallDeltaEvent(content_index=cidx, delta=pj, partial=output))
                    elif dtype == "signature_delta":
                        # 签名增量累加，不发事件（仅更新块状态，content_block_stop 时完整携带）
                        sig = getattr(delta, "signature", "")
                        block = output.content[cidx]
                        if isinstance(block, ThinkingContent):
                            existing = block.thinking_signature or ""
                            block.thinking_signature = existing + sig

                elif etype == "content_block_stop":
                    aidx = getattr(event, "index", 0)
                    blk = find_block_by_anthropic_idx(aidx)
                    if blk is None:
                        continue
                    cidx = blk.content_index
                    block = output.content[cidx]
                    if isinstance(block, TextContent):
                        es.push(
                            TextEndEvent(content_index=cidx, content=block.text, partial=output)
                        )
                    elif isinstance(block, ThinkingContent):
                        es.push(
                            ThinkingEndEvent(
                                content_index=cidx,
                                content=block.thinking,
                                partial=output,
                            )
                        )
                    elif isinstance(block, ToolCall):
                        # 最终解析参数
                        if blk.partial_json:
                            block.arguments = _parse_streaming_json(blk.partial_json)
                        es.push(
                            ToolCallEndEvent(content_index=cidx, tool_call=block, partial=output)
                        )

                elif etype == "message_delta":
                    delta = getattr(event, "delta", None)
                    usage_delta = getattr(event, "usage", None)
                    if delta is not None:
                        stop_reason = getattr(delta, "stop_reason", None)
                        if stop_reason:
                            mapped = _STOP_REASON_MAP.get(stop_reason, "error")
                            output.stop_reason = mapped  # type: ignore[assignment]
                            if mapped == "error":
                                output.error_message = f"stop_reason: {stop_reason}"
                    if usage_delta is not None:
                        _update_usage_from_delta(output.usage, usage_delta, model)

                elif etype == "message_stop":
                    pass  # 流结束，在循环外终止

            # 终止判定
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


def _format_error(exc: BaseException) -> str:
    """格式化 provider 错误。"""
    from anthropic import APIStatusError

    if isinstance(exc, APIStatusError):
        status = exc.status_code
        try:
            body = str(exc.body)[:4000]
        except Exception:  # noqa: BLE001
            body = str(exc)
        return f"{status}: {body}"
    return str(exc)


# ============================================================
# provider 句柄
# ============================================================


class _AnthropicProvider:
    def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None = None,
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        return _run_anthropic_stream(model, context, options)

    def stream_simple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        return _run_anthropic_stream(model, context, options)


anthropic_api_provider: Any = _AnthropicProvider()


# ============================================================
# 内置 Claude 模型（精简）
# ============================================================

from ..types import ModelCost  # noqa: E402

ANTHROPIC_MODELS: list[Model] = [
    Model(
        id="claude-sonnet-4-5",
        name="Claude Sonnet 4.5",
        api="anthropic-messages",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        reasoning=True,
        input=["text", "image"],
        cost=ModelCost(input=3, output=15, cache_read=0.3, cache_write=3.75),
        context_window=200000,
        max_tokens=16000,
        compat={"forceAdaptiveThinking": True, "supportsToolReferences": True},
    ),
    Model(
        id="claude-haiku-4-5",
        name="Claude Haiku 4.5",
        api="anthropic-messages",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        reasoning=True,
        input=["text", "image"],
        cost=ModelCost(input=1, output=5, cache_read=0.1, cache_write=1.25),
        context_window=200000,
        max_tokens=16000,
        compat={"forceAdaptiveThinking": True},
    ),
]


__all__ = ["anthropic_api_provider", "ANTHROPIC_MODELS"]
