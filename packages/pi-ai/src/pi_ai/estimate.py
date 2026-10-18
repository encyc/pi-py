"""上下文 token 用量估算。

对应上游 ``utils/estimate.ts``（v1.3.0，117 行）。纯计算，不发请求。

v1.3.0 两处关键变更（本模块据此实现）：
1. ``CHARS_PER_TOKEN`` **4 → 3.5**（上游 :15）。降低每 token 字符数会**高估**输入
   token，从而在按"上下文窗口 - 已用 - 安全余量"计算输出上限时更保守，减少
   "context length exceeded" 类失败（上游 CHANGELOG 1.1.0 Fixed, issue #10497）。
2. system message 参与估算（上游 :49-55，M-1 的配套）。system message 的
   ``tools_added`` / ``tools_removed`` 也计入，因为工具声明确实占用上下文。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .text import get_system_message_text
from .types import AssistantMessage, ImageContent, Message, TextContent, TranscriptContext, Usage

__all__ = [
    "CHARS_PER_TOKEN",
    "ESTIMATED_IMAGE_CHARS",
    "ContextUsageEstimate",
    "calculate_context_tokens",
    "estimate_text_tokens",
    "estimate_text_and_image_content_tokens",
    "estimate_message_tokens",
    "estimate_context_tokens",
]

#: 每 token 平均字符数。v1.3.0 由 4 改为 3.5（见模块 docstring）。
CHARS_PER_TOKEN = 3.5

#: 一张图的等效字符数（上游同值）。
ESTIMATED_IMAGE_CHARS = 4800


@dataclass
class ContextUsageEstimate:
    """上下文用量估算结果。对应上游 ``ContextUsageEstimate``。"""

    #: 估算的总上下文 token。
    tokens: int
    #: 最近一个可用 assistant usage 块报告的 token。
    usage_tokens: int
    #: 该 usage 块**之后**新增内容的估算 token。
    trailing_tokens: int
    #: 提供 usage 的消息下标；没有则为 None。
    last_usage_index: int | None


def calculate_context_tokens(usage: Usage) -> int:
    """由 usage 计算上下文 token 总量（回退到四项之和）。"""
    return usage.total_tokens or (usage.input + usage.output + usage.cache_read + usage.cache_write)


def _safe_json_dumps(value: Any) -> str:
    try:
        dumped = json.dumps(value, default=str)
    except Exception:  # noqa: BLE001 - 估算绝不允许因不可序列化而抛出
        return "[unserializable]"
    return dumped if dumped is not None else "undefined"


def _estimate_text_and_image_chars(content: str | list[Any]) -> int:
    if isinstance(content, str):
        return len(content)
    chars = 0
    for block in content:
        if isinstance(block, TextContent) or getattr(block, "type", None) == "text":
            chars += len(getattr(block, "text", "") or "")
        elif isinstance(block, ImageContent) or getattr(block, "type", None) == "image":
            chars += ESTIMATED_IMAGE_CHARS
    return chars


def estimate_text_tokens(text: str) -> int:
    """粗估文本 token 数（向上取整）。"""
    return _ceil_div(len(text), CHARS_PER_TOKEN)


def estimate_text_and_image_content_tokens(content: str | list[Any]) -> int:
    return _ceil_div(_estimate_text_and_image_chars(content), CHARS_PER_TOKEN)


def _ceil_div(chars: int, per_token: float) -> int:
    # 用整数运算避免浮点边界（如 3.5 * 2 == 7.000000000000001）。
    # ceil(chars / per_token) == ceil(2*chars / 7) 对 3.5 成立。
    numerator = chars * 2 if per_token == 3.5 else None
    if numerator is not None:
        return -(-numerator // 7)
    import math

    return math.ceil(chars / per_token)


def estimate_message_tokens(message: Message) -> int:
    """估算单条消息的 token 数（system 消息含其工具声明）。"""
    role = getattr(message, "role", None)

    if role == "system":
        from .types import SystemMessage

        assert isinstance(message, SystemMessage)
        return (
            estimate_text_tokens(get_system_message_text(message))
            + _estimate_tools_tokens(message.tools_added)
            + _estimate_tools_tokens(message.tools_removed)
        )
    if role == "user":
        return estimate_text_and_image_content_tokens(message.content)
    if role == "toolResult":
        return estimate_text_and_image_content_tokens(message.content)

    chars = 0
    for block in getattr(message, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            chars += len(getattr(block, "text", "") or "")
        elif block_type == "thinking":
            chars += len(getattr(block, "thinking", "") or "")
        else:
            chars += len(getattr(block, "name", "") or "") + len(
                _safe_json_dumps(getattr(block, "arguments", {}))
            )
    return _ceil_div(chars, CHARS_PER_TOKEN)


def _estimate_tools_tokens(tools: list[Any] | None) -> int:
    if not tools:
        return 0
    return estimate_text_tokens(_safe_json_dumps([t.model_dump(by_alias=True) for t in tools]))


def _get_last_assistant_usage_info(messages: list[Message]) -> tuple[Usage, int] | None:
    latest_prefix_timestamp = float("-inf")
    usage_info: tuple[Usage, int] | None = None

    for index, message in enumerate(messages):
        if getattr(message, "role", None) == "assistant":
            assert isinstance(message, AssistantMessage)
            # 该响应之后又插入了更新的前缀消息（例如压缩摘要），故其 usage 无法
            # 描述当前前缀。
            usage_applies = message.timestamp >= latest_prefix_timestamp
            if (
                usage_applies
                and message.stop_reason not in ("aborted", "error")
                and calculate_context_tokens(message.usage) > 0
            ):
                usage_info = (message.usage, index)
        latest_prefix_timestamp = max(latest_prefix_timestamp, message.timestamp)

    return usage_info


def estimate_context_tokens(
    context: TranscriptContext | list[Message],
) -> ContextUsageEstimate:
    """估算整个上下文的 token 用量。

    对应上游 ``estimateContextTokens``：优先用最近一次 assistant usage 作为
    前缀基线，只累加其后的新增内容；没有可用 usage 时逐条累加。
    接受 :class:`TranscriptContext` 或裸消息列表（上游同签名）。
    """
    messages = context.messages if isinstance(context, TranscriptContext) else context
    usage_info = _get_last_assistant_usage_info(messages)
    if usage_info is not None:
        usage, index = usage_info
        usage_tokens = calculate_context_tokens(usage)
        trailing = sum(estimate_message_tokens(m) for m in messages[index + 1 :])
        return ContextUsageEstimate(
            tokens=usage_tokens + trailing,
            usage_tokens=usage_tokens,
            trailing_tokens=trailing,
            last_usage_index=index,
        )

    tokens = sum(estimate_message_tokens(m) for m in messages)
    return ContextUsageEstimate(
        tokens=tokens, usage_tokens=0, trailing_tokens=tokens, last_usage_index=None
    )
