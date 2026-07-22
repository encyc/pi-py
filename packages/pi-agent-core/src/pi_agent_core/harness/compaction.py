"""上下文压缩（Compaction）。

对应上游 ``packages/agent/src/harness/compaction/compaction.ts``。

核心功能：
- ``estimate_tokens``：粗估消息的 token 数（字符数 / 4 的启发式）。
- ``calculate_context_tokens``：从 usage 提取已用 token。
- ``should_compact``：判断是否需要压缩。
- ``find_cut_point``：找到压缩的切割点（保留最近 N token，之前的总结）。
- ``compact``：执行压缩（调用 LLM 生成 summary）。

token 估算用启发式（chars/4），上游有更精确的 tokenizer 但 Python 端
保持轻量；需要精确计数的场景可覆盖 estimate_tokens。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pi_ai import (
    Message,
    Model,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    complete_simple,
)

#: 启发式：约 4 字符 ≈ 1 token（英文）。中文约 1.5 字符/token，这里取折中。
_CHARS_PER_TOKEN = 4

#: 默认压缩设置。
DEFAULT_COMPACTION_SETTINGS = {
    "enabled": True,
    "reserve_tokens": 30000,  # 压缩后保留的目标 token 数
    "keep_recent_tokens": 8000,  # 压缩点之后保留的最近 token 数
}


@dataclass
class CompactionSettings:
    """压缩设置。"""

    enabled: bool = True
    #: 压缩后保留的目标 token 数（触发阈值通常为 context_window 的比例）。
    reserve_tokens: int = 30000
    #: 压缩点之后保留的最近 token 数（不压缩）。
    keep_recent_tokens: int = 8000


@dataclass
class CompactionResult:
    """压缩结果。"""

    summary: str
    retained_tail: list[Message] = field(default_factory=list)
    removed_count: int = 0


# ============================================================
# token 估算
# ============================================================


def estimate_tokens(message: Message) -> int:
    """粗估单条消息的 token 数。

    启发式：提取所有文本内容，字符数 / 4。工具调用的参数 JSON 也计入。
    """
    total_chars = 0
    content = getattr(message, "content", None)
    if isinstance(content, str):
        total_chars += len(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, TextContent):
                total_chars += len(block.text)
            elif isinstance(block, ThinkingContent):
                total_chars += len(block.thinking)
            elif isinstance(block, ToolCall):
                total_chars += len(json.dumps(block.arguments, ensure_ascii=False))
                total_chars += len(block.name)
    # role 等元数据开销
    total_chars += 20
    return max(1, total_chars // _CHARS_PER_TOKEN)


def estimate_context_tokens(messages: list[Message]) -> int:
    """估算整个消息列表的 token 数。"""
    return sum(estimate_tokens(m) for m in messages)


def calculate_context_tokens(usage: Any) -> int:
    """从 AssistantMessage.usage 提取已用 token（input + cache）。

    对应上游 ``calculateContextTokens``。
    """
    if usage is None:
        return 0
    input_t = getattr(usage, "input", 0) or 0
    cache_read = getattr(usage, "cache_read", 0) or getattr(usage, "cacheRead", 0) or 0
    cache_write = getattr(usage, "cache_write", 0) or getattr(usage, "cacheWrite", 0) or 0
    return input_t + cache_read + cache_write


# ============================================================
# should_compact
# ============================================================


def should_compact(
    context_tokens: int,
    context_window: int,
    settings: CompactionSettings | None = None,
) -> bool:
    """判断是否需要压缩。

    当 context_tokens 超过 context_window 的 80% 时触发。
    """
    if not settings or not settings.enabled:
        return False
    if context_window <= 0:
        return False
    threshold = int(context_window * 0.8)
    return context_tokens >= threshold


# ============================================================
# find_cut_point
# ============================================================


def find_cut_point(
    messages: list[Message],
    keep_recent_tokens: int,
) -> int:
    """找到压缩切割点。

    从末尾向前累加 token，直到达到 keep_recent_tokens。返回切割点索引：
    ``messages[:cut]`` 将被总结，``messages[cut:]`` 保留。

    保证切割点不落在 toolResult（它必须跟在 assistant(toolCall) 之后）。
    """
    if not messages:
        return 0
    acc = 0
    cut = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        msg_tokens = estimate_tokens(messages[i])
        if acc + msg_tokens > keep_recent_tokens and i < len(messages) - 1:
            cut = i + 1
            break
        acc += msg_tokens
        cut = i

    # 避免切割点落在 toolResult（需要配对的前序 assistant）
    while cut < len(messages) and isinstance(messages[cut], ToolResultMessage):
        cut -= 1
    return max(0, cut)


# ============================================================
# compact（调用 LLM 生成 summary）
# ============================================================

#: 压缩用的 system prompt。
SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a conversation summarizer. Summarize the following conversation "
    "concisely, preserving key decisions, context, and any pending tasks. "
    "Write in the same language as the conversation."
)


async def generate_summary(
    model: Model,
    messages: list[Message],
    **options: Any,
) -> str:
    """调用 LLM 生成对话摘要。"""
    # 序列化消息为文本
    serialized = _serialize_conversation(messages)
    ctx_msg = UserMessage(content=f"Summarize this conversation:\n\n{serialized}")
    from pi_ai import Context, SimpleStreamOptions

    ctx = Context(system_prompt=SUMMARIZATION_SYSTEM_PROMPT, messages=[ctx_msg])
    opts = (
        SimpleStreamOptions(max_tokens=2000, **options)
        if options
        else SimpleStreamOptions(max_tokens=2000)
    )
    result = await complete_simple(model, ctx, opts)
    # 提取文本
    if result.content and isinstance(result.content[0], TextContent):
        return result.content[0].text
    return ""


async def compact(
    model: Model,
    messages: list[Message],
    settings: CompactionSettings | None = None,
    **options: Any,
) -> CompactionResult:
    """执行上下文压缩。

    1. 找切割点（保留最近 keep_recent_tokens）。
    2. 对切割点之前的消息调用 LLM 生成 summary。
    3. 返回 CompactionResult（summary + retained_tail）。
    """
    settings = settings or CompactionSettings()
    cut = find_cut_point(messages, settings.keep_recent_tokens)
    if cut == 0:
        # 全部保留，无需压缩
        return CompactionResult(summary="", retained_tail=list(messages), removed_count=0)

    to_summarize = messages[:cut]
    retained = messages[cut:]

    summary = await generate_summary(model, to_summarize, **options)
    return CompactionResult(summary=summary, retained_tail=retained, removed_count=cut)


def _serialize_conversation(messages: list[Message]) -> str:
    """把消息列表序列化为可读文本（供摘要）。"""
    lines: list[str] = []
    for msg in messages:
        role = getattr(msg, "role", "unknown")
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, TextContent):
                    parts.append(block.text)
                elif isinstance(block, ToolCall):
                    parts.append(f"[tool call: {block.name}({json.dumps(block.arguments)})]")
            text = "\n".join(parts)
        else:
            text = str(content)
        lines.append(f"[{role}] {text}")
    return "\n\n".join(lines)


__all__ = [
    "DEFAULT_COMPACTION_SETTINGS",
    "CompactionSettings",
    "CompactionResult",
    "estimate_tokens",
    "estimate_context_tokens",
    "calculate_context_tokens",
    "should_compact",
    "find_cut_point",
    "generate_summary",
    "compact",
    "SUMMARIZATION_SYSTEM_PROMPT",
]
