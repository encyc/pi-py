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
6. **终止判定**：``finish_reason`` 缺失默认视为异常；但对声明
   ``compat.supportsFinishReason=False`` 的兼容端点（不发 finish_reason），
   流结束时按内容推断 ``stop``/``toolUse``。
7. **reasoning_details 是回放元数据**（v0.85.1）：流式期间只收集不发事件，
   块结束时一次性序列化进 thinking 签名，多轮回放为 assistant 消息字段。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from ..async_utils import maybe_await
from ..constrained_sampling import (
    GrammarToolInputJsonBuffer,
    append_grammar_tool_input_json_delta,
    create_grammar_tool_input_properties,
    get_grammar_tool_input,
    get_json_schema_tool_parameters,
    resolve_grammar_constrained_sampling,
    resolve_json_schema_strict_sampling,
)
from ..event_stream import AssistantMessageEventStream, EventStream
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
from ..json_parse import parse_streaming_json, parse_tool_call_arguments
from ..provider_retry import retry_provider_request
from ..transcript import (
    ensure_transcript,
    get_current_system_prompt,
    get_current_tools,
    resolve_transcript,
)
from ..transform_messages import transform_assistant_content
from ..types import (
    AssistantMessage,
    Context,
    Model,
    SimpleStreamOptions,
    StopReason,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ThinkingTokenBudgetField,
    ToolCall,
    ToolResultMessage,
    TranscriptContext,
    Usage,
    UsageCost,
    UserMessage,
)
from ..user_agent import get_pi_user_agent

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

#: 当 thinking 预算与答案共享响应上限时，始终为答案保留的 token 数。
#: 对应上游 ``simple-options.ts`` 的 ``MIN_ANSWER_TOKENS``。
_MIN_ANSWER_TOKENS = 1024

#: 默认 thinking token 预算（按 reasoning level）。对应上游
#: ``support thinking_token_budget`` 的内置预算。
_DEFAULT_THINKING_BUDGETS: dict[str, int] = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
}


def _clamp_reasoning(level: str) -> str:
    """把 ``xhigh``/``max`` 折叠为 ``high``（预算表只覆盖到 high）。对应上游 ``clampReasoning``。"""
    return "high" if level in ("xhigh", "max") else level


def _thinking_budget_for_level(reasoning_level: str, custom_budgets: dict[str, int] | None) -> int:
    """按级别取 thinking token 预算（自定义覆盖默认）。对应上游 ``thinkingBudgetForLevel``。"""
    budgets = {**_DEFAULT_THINKING_BUDGETS, **(custom_budgets or {})}
    return budgets.get(_clamp_reasoning(reasoning_level), 0)


def _clamp_thinking_budget_to_answer_room(thinking_budget: int, ceiling: int) -> int:
    """封顶 thinking 预算，保证答案至少剩 ``_MIN_ANSWER_TOKENS``。

    对应上游 ``clampThinkingBudgetToAnswerRoom``。
    """
    return min(thinking_budget, max(0, ceiling - _MIN_ANSWER_TOKENS))


_THINKING_BUDGET_FIELDS: tuple[ThinkingTokenBudgetField, ...] = (
    "thinking_token_budget",
    "thinking_budget",
    "thinking_budget_tokens",
)


def _resolve_thinking_token_budget_field(compat: dict[str, Any]) -> ThinkingTokenBudgetField | None:
    """解析封顶 reasoning 的顶层字段名。

    对应上游 ``resolveThinkingTokenBudgetField``：
    ``compat.thinkingTokenBudgetField`` 优先，遗留布尔
    ``compat.supportsThinkingTokenBudget`` 等价于 ``"thinking_token_budget"``（vLLM）。
    """
    field = compat.get("thinkingTokenBudgetField")
    if isinstance(field, str) and field in _THINKING_BUDGET_FIELDS:
        return field
    if compat.get("supportsThinkingTokenBudget"):
        return "thinking_token_budget"
    return None


# ============================================================
# reasoning_details（OpenRouter 等的结构化 reasoning 回放）
# ============================================================

#: reasoning detail 的合法 type。对应上游 ``OpenAIReasoningDetail``。
_REASONING_DETAIL_TYPES = ("reasoning.summary", "reasoning.encrypted", "reasoning.text")


def _is_openai_reasoning_detail(detail: Any) -> bool:
    """校验单个 reasoning detail 结构。"""
    if not isinstance(detail, dict):
        return False
    dtype = detail.get("type")
    if dtype not in _REASONING_DETAIL_TYPES:
        return False
    index = detail.get("index")
    if not (
        (detail.get("id") is None or isinstance(detail.get("id"), str))
        and (detail.get("format") is None or isinstance(detail.get("format"), str))
        and (index is None or (isinstance(index, (int, float)) and not isinstance(index, bool)))
    ):
        return False
    if dtype == "reasoning.summary":
        return isinstance(detail.get("summary"), str)
    if dtype == "reasoning.encrypted":
        return isinstance(detail.get("data"), str)
    return isinstance(detail.get("text"), str) and (
        detail.get("signature") is None or isinstance(detail.get("signature"), str)
    )


def parse_openai_reasoning_details(signature: str | None) -> list[dict[str, Any]] | None:
    """从 thinking 块签名解析结构化 reasoning details。失败返回 None。"""
    if not signature:
        return None
    try:
        parsed = json.loads(signature)
    except (ValueError, TypeError):
        return None
    if (
        isinstance(parsed, list)
        and len(parsed) > 0
        and all(_is_openai_reasoning_detail(d) for d in parsed)
    ):
        return parsed
    return None


def parse_legacy_encrypted_reasoning_detail(signature: str | None) -> dict[str, Any] | None:
    """从工具调用的 thought_signature 解析遗留的加密 reasoning detail。"""
    if not signature:
        return None
    try:
        parsed = json.loads(signature)
    except (ValueError, TypeError):
        return None
    if (
        isinstance(parsed, dict)
        and parsed.get("type") == "reasoning.encrypted"
        and isinstance(parsed.get("id"), str)
        and len(parsed["id"]) > 0
        and isinstance(parsed.get("data"), str)
        and len(parsed["data"]) > 0
    ):
        return parsed
    return None


def _append_reasoning_detail(details: list[dict[str, Any]], detail: dict[str, Any]) -> None:
    """合并连续的同类 reasoning 增量；加密条目保持独立。

    公共字段仅在来源有值时回填（对应上游 ``??=``/``||=``，避免把 absent
    键序列化为 null）。
    """

    def fill_missing(target: dict[str, Any], source: dict[str, Any]) -> None:
        for key in ("id", "format", "index"):
            if target.get(key) is None and source.get(key) is not None:
                target[key] = source[key]

    last = details[-1] if details else None
    if detail["type"] == "reasoning.text" and last is not None and last["type"] == "reasoning.text":
        last["text"] += detail["text"]
        if detail.get("signature") and not last.get("signature"):
            last["signature"] = detail["signature"]
        fill_missing(last, detail)
        return
    if (
        detail["type"] == "reasoning.summary"
        and last is not None
        and last["type"] == "reasoning.summary"
    ):
        last["summary"] += detail["summary"]
        fill_missing(last, detail)
        return
    details.append(dict(detail))


# ============================================================
# 流式增量解析的块状态
# ============================================================


class _ToolCallBlock:
    """工具调用的流式累加状态。

    保留一个放进 ``output.content`` 的真实 ``ToolCall`` 实例（保证 content 始终
    持有合法模型对象），累加状态（``partial_args``/``stream_index``/``content_index``）
    仅解析期使用。
    """

    __slots__ = (
        "tool_call",
        "partial_args",
        "stream_index",
        "content_index",
        "custom_input_property",
        "custom_input_buffer",
    )

    def __init__(
        self,
        content_index: int,
        id: str = "",
        name: str = "",
        custom_input_property: str | None = None,
    ) -> None:
        self.tool_call = ToolCall(id=id, name=name, arguments={})
        self.partial_args: str = ""
        self.stream_index: int | None = None
        self.content_index = content_index
        self.custom_input_property = custom_input_property
        self.custom_input_buffer = (
            GrammarToolInputJsonBuffer() if custom_input_property is not None else None
        )
        if custom_input_property is not None:
            self.tool_call.arguments = {custom_input_property: ""}


# ============================================================
# usage 解析
# ============================================================


def _parse_chunk_usage(raw: Any, model: Model) -> Usage:
    """从 OpenAI chunk usage 提取 token 统计。对应上游 ``parseChunkUsage``。

    注意：SDK 对象的属性可能存在但值为 None（如 DeepSeek 的 cache_write_tokens=None），
    故所有字段统一 ``or 0`` 兜底，避免 ``int - None`` 崩溃。
    """
    prompt_tokens = getattr(raw, "prompt_tokens", 0) or 0
    ptd = getattr(raw, "prompt_tokens_details", None)
    cached = getattr(ptd, "cached_tokens", None) if ptd else None
    # 缓存读取命中的三种摆放：OpenAI/OpenRouter 用 prompt_tokens_details.cached_tokens，
    # DeepSeek 用 prompt_cache_hit_tokens，Kimi 在最终 usage chunk 用顶层 cached_tokens。
    cache_read = (
        cached or getattr(raw, "prompt_cache_hit_tokens", 0) or getattr(raw, "cached_tokens", 0)
    ) or 0
    cache_write = (getattr(ptd, "cache_write_tokens", 0) if ptd else 0) or 0

    # cached_tokens 是缓存"读取"命中，不减去；input 扣除缓存部分使恒等式成立
    input_tokens = max(0, prompt_tokens - cache_read - cache_write)
    output_tokens = getattr(raw, "completion_tokens", 0) or 0
    ctd = getattr(raw, "completion_tokens_details", None)
    reasoning = (getattr(ctd, "reasoning_tokens", 0) if ctd else 0) or 0

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
    model: Model,
    api_key: str,
    options_headers: dict[str, str | None] | None,
    http_client: Any = None,
) -> AsyncOpenAI:
    # 统一携带 pi User-Agent（对应上游 getPiUserAgent），可被 model/请求级 headers 覆盖
    headers: dict[str, Any] = {"User-Agent": get_pi_user_agent(), **(model.headers or {})}
    if options_headers:
        headers.update(options_headers)
    return AsyncOpenAI(
        api_key=api_key,
        base_url=model.base_url,
        default_headers=headers or None,
        max_retries=0,
        http_client=http_client,
    )


def _convert_tools(
    tools: list[Any],
    *,
    supports_strict_mode: bool = True,
    supports_openai_grammar_tools: bool = False,
) -> list[dict[str, Any]]:
    """ToolDef 列表 -> OpenAI tools 格式。"""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        grammar = resolve_grammar_constrained_sampling(tool, supports_openai_grammar_tools)
        if grammar is not None:
            converted.append(
                {
                    "type": "custom",
                    "custom": {
                        "name": tool.name,
                        "description": tool.description,
                        "format": {
                            "type": "grammar",
                            "grammar": {
                                "syntax": grammar.format,
                                "definition": grammar.definition,
                            },
                        },
                    },
                }
            )
            continue
        strict = resolve_json_schema_strict_sampling(tool, supports_strict_mode)
        function = {
            "name": tool.name,
            "description": tool.description,
            # strict 工具发送 strict 化后的 schema（v0.85.1）
            "parameters": get_json_schema_tool_parameters(tool, strict),
        }
        if supports_strict_mode:
            function["strict"] = strict if strict is not None else False
        converted.append({"type": "function", "function": function})
    return converted


def _convert_messages(
    context: TranscriptContext,
    model: Model,
    grammar_tool_input_properties: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    """TranscriptContext -> OpenAI messages。返回 (messages, dev_headers)。

    v1.3.0 变更：
    - **M-1**：系统提示词取自 transcript 的 system message（``systemPrompt`` 已不在
      provider 契约里）；不支持会话中途 system 消息的端点先折叠（``resolve_transcript``）。
    - **M-6**：assistant 消息的思考块交由
      :func:`~pi_ai.transform_messages.transform_assistant_content` 处理——
      **跨模型丢弃**（v0.85.1 是降级为文本，v1.3.0 反转为丢弃）。
    - **M-13.5**：用户多模态内容过滤掉空 text 块，避免纯图消息被部分兼容端点拒绝
      （上游 issue #9797）。
    """
    normalized = resolve_transcript(context, False)
    system_prompt = get_current_system_prompt(normalized.messages)
    out: list[dict[str, Any]] = []
    # 系统提示
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})

    for msg in normalized.messages:
        if isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            else:
                # 内容块数组 -> OpenAI 多模态格式（过滤空 text 块，M-13.5）
                parts: list[dict[str, Any]] = []
                for block in content:
                    if block.type == "text":
                        if not block.text:
                            continue
                        parts.append({"type": "text", "text": block.text})
                    elif block.type == "image":
                        img: dict[str, Any] = {"url": f"data:{block.mime_type};base64,{block.data}"}
                        parts.append({"type": "image_url", "image_url": img})
                if not parts:
                    continue
                out.append({"role": "user", "content": parts})
        elif isinstance(msg, AssistantMessage):
            # 文本 + thinking -> content；tool_calls 单独。
            # 注：pydantic-mypy 插件对 Annotated 判别联合的 content 字段解析有偏差，
            # 故用 __dict__ 直取绕过，运行时类型仍由 isinstance 保证。
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            tool_call_blocks: list[ToolCall] = []
            thinking_blocks: list[ThinkingContent] = []
            # M-6：跨模型思考丢弃 + 清除跨模型 thoughtSignature
            blocks = transform_assistant_content(list(msg.__dict__["content"]), msg, model)
            for block in blocks:
                if isinstance(block, ToolCall):
                    tool_call_blocks.append(block)
                elif isinstance(block, ThinkingContent):
                    thinking_blocks.append(block)
                elif isinstance(block, TextContent):
                    text_parts.append(block.text)
            # reasoning 回放数据：优先 thinking 块签名里的结构化 details，
            # 回退到遗留的工具调用 thoughtSignature 加密条目（v0.85.1）。
            preserved_reasoning_details: list[dict[str, Any]] | None = None
            for tb in thinking_blocks:
                details = parse_openai_reasoning_details(tb.thinking_signature)
                if details is not None:
                    preserved_reasoning_details = details
                    break
            if preserved_reasoning_details is None:
                legacy = [
                    d
                    for d in (
                        parse_legacy_encrypted_reasoning_detail(tc.thought_signature)
                        for tc in tool_call_blocks
                    )
                    if d is not None
                ]
                if legacy:
                    preserved_reasoning_details = legacy
            for tc in tool_call_blocks:
                input_property = (grammar_tool_input_properties or {}).get(tc.name)
                if input_property is not None:
                    tool_calls.append(
                        {
                            "id": tc.id,
                            "type": "custom",
                            "custom": {
                                "name": tc.name,
                                "input": get_grammar_tool_input(
                                    tc.name, tc.arguments, input_property
                                ),
                            },
                        }
                    )
                else:
                    tool_calls.append(
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                    )
            entry: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                entry["content"] = "\n".join(text_parts)
            if tool_calls:
                entry["tool_calls"] = tool_calls
            if preserved_reasoning_details:
                entry["reasoning_details"] = preserved_reasoning_details
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
    """容错解析不完整的 JSON 字符串。

    保留本名以兼容既有调用（provider 内部与测试）；实现已迁移到
    :mod:`pi_ai.json_parse`，与 Anthropic provider 共用一份。
    """
    return parse_streaming_json(s)


def _resolve_sampling_params(model: Model, options: StreamOptions | None) -> dict[str, Any] | None:
    """合并模型级 / 思考级别 / 请求级采样参数。

    对应上游 v1.3.0 ``resolveSamplingParams``（api/simple-options.ts:24）：

    1. ``model.sampling_params``（模型默认）
    2. ``model.sampling_params_by_thinking_level[生效思考级别]``
    3. ``options.sampling_params``（请求级）

    后者按 key 覆盖前者。v0.85.1 本端缺第 2 层，且**直连 ``stream()`` 时连第 1 层
    也会因为模型级只在 ``stream_simple`` 路径被读取而失效**（上游 issue #9506）。
    三者皆空时返回 None。
    """
    request_params = options.sampling_params if options else None
    if (
        not model.sampling_params
        and not model.sampling_params_by_thinking_level
        and not request_params
    ):
        return None
    merged: dict[str, Any] = dict(model.sampling_params or {})
    level = getattr(options, "reasoning", None) if options else None
    if level and model.sampling_params_by_thinking_level:
        effective = _clamp_reasoning(level)
        merged.update(model.sampling_params_by_thinking_level.get(effective) or {})
    if request_params:
        merged.update(request_params)
    return merged


def _run_openai_stream(
    model: Model,
    context: Context | TranscriptContext,
    options: StreamOptions | None,
) -> EventStream[AssistantMessageEvent, AssistantMessage]:
    # 裸 Context 直接调用 provider 时也要归一化，否则 system_prompt/tools 会被
    # 静默丢弃（它们不在 context.messages 里）。见 transcript.ensure_transcript。
    context = ensure_transcript(context)
    es: AssistantMessageEventStream = AssistantMessageEventStream()
    api_key = (options.api_key if options else None) or _resolve_api_key()

    async def drive() -> None:
        output = AssistantMessage(
            api=model.api,
            provider=model.provider,
            model=model.id,
            stop_reason="pending",
            timestamp=int(time.time() * 1000),
        )
        es.push(StartEvent(partial=output.model_copy(deep=True)))
        client = _create_client(
            model,
            api_key,
            options.headers if options else None,
            options.http_client if options else None,
        )

        # 构建请求参数
        compat = model.compat or {}
        normalized_context = resolve_transcript(context, False)
        current_tools = get_current_tools(normalized_context.messages)
        grammar_tool_input_properties = create_grammar_tool_input_properties(
            current_tools,
            compat.get("supportsOpenAIGrammarTools", False),
        )
        messages, _ = _convert_messages(normalized_context, model, grammar_tool_input_properties)
        params: dict[str, Any] = {
            "model": model.id,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if current_tools:
            params["tools"] = _convert_tools(
                current_tools,
                # M-13.2：v1.3.0 起未知 OpenAI-compatible 端点**默认不发** strict schema
                # （上游 api/openai-completions.ts:1677 `supportsStrictMode: false`，
                # issue #9816）。v0.85.1 本端默认 True，会让不支持的端点收到 strict 字段。
                supports_strict_mode=compat.get("supportsStrictMode", False),
                supports_openai_grammar_tools=compat.get("supportsOpenAIGrammarTools", False),
            )
        # provider 中性的工具选择（v0.85.1）：OpenAI 直接透传字符串
        tool_choice = getattr(options, "tool_choice", None) if options else None
        if tool_choice is not None:
            params["tool_choice"] = tool_choice
        if options and options.temperature is not None:
            params["temperature"] = options.temperature
        if options and options.max_tokens is not None:
            # 优先 max_completion_tokens，回退 max_tokens
            params["max_tokens"] = options.max_tokens
        if options and options.timeout_ms is not None:
            params["timeout"] = options.timeout_ms / 1000

        # vLLM 调度优先级（v0.85.1）：顶层 priority 字段，仅在 compat 声明时发送
        if compat.get("vllmPriority") is not None:
            params["priority"] = compat["vllmPriority"]

        # 封顶 reasoning 的顶层预算字段（vLLM/Qwen/SGLang/llama.cpp 等）：推理与
        # 答案共享 max_tokens，不设上限时一次推理密集的轮次可能耗尽整个响应、
        # 既无答案也无工具调用。与 thinkingFormat 无关（同一台服务器可同时服务
        # zai/qwen/chat-template 模型）。
        budget_field = _resolve_thinking_token_budget_field(compat)
        reasoning_effort = getattr(options, "reasoning", None) if options else None
        thinking_budget: int | None = None
        if reasoning_effort and model.reasoning:
            ceiling = params.get("max_tokens") or model.max_tokens
            budget = _clamp_thinking_budget_to_answer_room(
                _thinking_budget_for_level(
                    reasoning_effort,
                    getattr(options, "thinking_budgets", None) if options else None,
                ),
                ceiling,
            )
            thinking_budget = budget if budget > 0 else None
        if budget_field and thinking_budget is not None:
            params[budget_field] = thinking_budget

        # 泛型采样参数：放在具名字段之后，使自定义键覆盖它们（如 llama.cpp/vLLM/
        # SGLang 的 top_p/top_k/min_p/repetition_penalty）。
        # M-13.4：合并顺序为 模型级 → 思考级别级 → 请求级（上游 issue #9506/#9776）。
        sampling_params = _resolve_sampling_params(model, options)
        if sampling_params:
            params.update(sampling_params)

        # 流式块状态：用真实模型实例累加，保证 output.content 始终持合法对象
        text_block: TextContent | None = None
        text_content_idx: int | None = None
        thinking_block: ThinkingContent | None = None
        thinking_content_idx: int | None = None
        has_finish_reason = False
        tool_blocks_by_index: dict[int, _ToolCallBlock] = {}
        tool_blocks_by_id: dict[str, _ToolCallBlock] = {}

        # reasoning_details 是回放元数据而非用户可见的流式增量（v0.85.1）：
        # 流式期间保存在内存，块结束时一次性序列化进 thinking 签名。
        streamed_reasoning_details: list[dict[str, Any]] | None = None

        def ensure_thinking_block(initial: str = "") -> ThinkingContent:
            """确保存在 thinking 块（reasoning_details 也需要一个宿主块）。"""
            nonlocal thinking_block, thinking_content_idx
            if thinking_block is None:
                thinking_block = ThinkingContent(thinking=initial)
                thinking_content_idx = len(output.content)
                output.content.append(thinking_block)
                es.push(ThinkingStartEvent(content_index=thinking_content_idx, partial=output))
            return thinking_block

        def apply_streamed_reasoning_details() -> None:
            """把流式期间收集的 reasoning details 序列化进 thinking 签名。"""
            if streamed_reasoning_details is not None and thinking_block is not None:
                thinking_block.thinking_signature = json.dumps(streamed_reasoning_details)

        def ensure_tool_block(
            index: int | None,
            tool_id: str | None,
            func_name: str | None,
            custom_input_property: str | None = None,
        ) -> _ToolCallBlock:
            block = tool_blocks_by_index.get(index) if index is not None else None
            if block is None and tool_id:
                block = tool_blocks_by_id.get(tool_id)
            if block is None:
                # 新建：把 ToolCall 实例直接放进 content，记录其索引
                cidx = len(output.content)
                block = _ToolCallBlock(
                    content_index=cidx,
                    id=tool_id or "",
                    name=func_name or "",
                    custom_input_property=custom_input_property,
                )
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
            stream_obj = await retry_provider_request(
                lambda: client.chat.completions.create(**params),
                max_retries=(options.max_retries or 0) if options else 0,
                max_retry_delay_ms=options.max_retry_delay_ms if options else None,
                cancel_event=options.cancel_event if options else None,
            )
            async for chunk in stream_obj:
                # M-22：在 Pi 归一化之前观察原始流事件（上游 api/openai-completions.ts:559）。
                on_stream_event = options.on_provider_stream_event if options else None
                if on_stream_event is not None:
                    await maybe_await(on_stream_event(chunk, model))

                # usage（chunk 级）
                if chunk.usage:
                    output.usage = _parse_chunk_usage(chunk.usage, model)

                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                # finish_reason
                if choice.finish_reason:
                    output.raw_stop_reason = choice.finish_reason
                    mapped: StopReason = _STOP_REASON_MAP.get(choice.finish_reason, "error")
                    output.stop_reason = mapped
                    if mapped == "error":
                        output.error_message = f"Unhandled finish_reason: {choice.finish_reason}"
                    has_finish_reason = True

                # M-13.1：**先**处理 reasoning，再处理 content。
                # 部分代理（如 LiteLLM）把 reasoning 与答案聚合进同一个 delta，
                # thinking 块必须先于 text 块建立，否则思考会显示在答案之后
                # （上游 api/openai-completions.ts:592，issue #10926）。
                #
                # 思考增量（三字段名，单槽位）
                for field in _THINKING_FIELDS:
                    thinking_val = getattr(delta, field, None)
                    if thinking_val:
                        tb = ensure_thinking_block()
                        tb.thinking += thinking_val
                        assert thinking_content_idx is not None
                        es.push(
                            ThinkingDeltaEvent(
                                content_index=thinking_content_idx,
                                delta=thinking_val,
                                partial=output,
                            )
                        )
                        break

                # 结构化 reasoning_details（OpenRouter 等按增量下发）：仅收集回放
                # 数据，不发流式事件；连续同类增量合并，加密条目保持独立。
                raw_details = getattr(delta, "reasoning_details", None)
                if isinstance(raw_details, list):
                    for detail in raw_details:
                        if not _is_openai_reasoning_detail(detail):
                            continue
                        ensure_thinking_block()
                        if streamed_reasoning_details is None:
                            streamed_reasoning_details = []
                        _append_reasoning_detail(streamed_reasoning_details, detail)

                # 文本增量（单槽位）——位于 reasoning 之后（M-13.1）
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

                # 工具调用增量（双索引 + 字符串累加 + 每增量重新解析）
                tool_calls_delta = getattr(delta, "tool_calls", None)
                if tool_calls_delta:
                    for tc_delta in tool_calls_delta:
                        idx = getattr(tc_delta, "index", None)
                        tc_id = getattr(tc_delta, "id", None)
                        func = getattr(tc_delta, "function", None)
                        func_name = getattr(func, "name", None) if func else None
                        args_chunk = getattr(func, "arguments", None) if func else None
                        custom = getattr(tc_delta, "custom", None)
                        custom_name = getattr(custom, "name", None) if custom else None
                        custom_chunk = getattr(custom, "input", None) if custom else None
                        tool_name = func_name or custom_name
                        custom_property = (
                            grammar_tool_input_properties.get(tool_name or "")
                            if custom is not None and func is None
                            else None
                        )

                        block = ensure_tool_block(
                            idx, tc_id, tool_name, custom_input_property=custom_property
                        )
                        delta_str = ""
                        if args_chunk:
                            block.partial_args += args_chunk
                            block.tool_call.arguments = _parse_streaming_json(block.partial_args)
                            delta_str = args_chunk
                        elif custom_chunk and func is None and block.custom_input_property:
                            current = block.tool_call.arguments[block.custom_input_property]
                            next_input = str(current) + custom_chunk
                            buffer = block.custom_input_buffer
                            assert buffer is not None
                            delta_str = (
                                append_grammar_tool_input_json_delta(
                                    buffer,
                                    block.custom_input_property,
                                    next_input,
                                    close=False,
                                )
                                or ""
                            )
                            block.tool_call.arguments = {block.custom_input_property: next_input}
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
                apply_streamed_reasoning_details()
                es.push(
                    ThinkingEndEvent(
                        content_index=thinking_content_idx,
                        content=thinking_block.thinking,
                        partial=output,
                    )
                )
            for block in tool_blocks_by_index.values():
                # M-4：收尾时改用 parse_tool_call_arguments —— 不再保留"部分解析前缀"。
                # 模型给出的完整参数若不是合法 JSON 对象，arguments 置 {} 并写
                # argumentsError，由工具参数校验把原文报回模型（上游 issue #10897）。
                # 流式期间的增量解析仍用 parse_streaming_json（见上）。
                if block.partial_args:
                    parsed_args, args_error = parse_tool_call_arguments(block.partial_args)
                    block.tool_call.arguments = parsed_args
                    block.tool_call.arguments_error = args_error
                if block.custom_input_property and block.custom_input_buffer:
                    input_value = str(block.tool_call.arguments[block.custom_input_property])
                    closing_delta = append_grammar_tool_input_json_delta(
                        block.custom_input_buffer,
                        block.custom_input_property,
                        input_value,
                        close=True,
                    )
                    if closing_delta:
                        es.push(
                            ToolCallDeltaEvent(
                                content_index=block.content_index,
                                delta=closing_delta,
                                partial=output,
                            )
                        )
                es.push(
                    ToolCallEndEvent(
                        content_index=block.content_index,
                        tool_call=block.tool_call,
                        partial=output,
                    )
                )

            # 终止判定（对齐 v0.84.1 supportsFinishReason 语义）
            supports_finish_reason = compat.get("supportsFinishReason", True)
            if output.stop_reason == "aborted":
                raise RuntimeError("Request was aborted")
            # 声明不发 finish_reason 的兼容端点：按内容推断 stop/toolUse
            if not has_finish_reason and not supports_finish_reason:
                output.stop_reason = (
                    "toolUse" if any(isinstance(b, ToolCall) for b in output.content) else "stop"
                )
            if output.stop_reason == "error":
                raise RuntimeError(output.error_message or "provider error")
            # 仅当端点本应发 finish_reason 却没发、或仍停在 pending 时才视为异常
            if (
                supports_finish_reason and not has_finish_reason
            ) or output.stop_reason == "pending":
                raise RuntimeError("Stream ended without finish_reason")

            es.push(DoneEvent(reason=output.stop_reason, message=output))
            es.end(output)

        except BaseException as exc:  # noqa: BLE001
            apply_streamed_reasoning_details()
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
        context: Context | TranscriptContext,
        options: StreamOptions | None = None,
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        return _run_openai_stream(model, context, options)

    def stream_simple(
        self,
        model: Model,
        context: Context | TranscriptContext,
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
