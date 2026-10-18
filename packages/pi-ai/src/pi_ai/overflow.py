"""上下文溢出（context overflow）判定。

对应上游 ``packages/ai/src/utils/overflow.ts``（v1.3.0，188 行）。

**模块级状态**：无。三个模式表（``OVERFLOW_PATTERNS`` / ``NON_OVERFLOW_PATTERNS`` /
``CEREBRAS_BODYLESS_OVERFLOW_PATTERN``）都是不可变的正则常量，运行时只读。
因此 ``pi_ai.utils.overflow`` 转发层不存在 ``uuid`` 那种"状态被复制成两份"的风险。

三种溢出形态（与上游一致）：

1. **显式报错**：``stop_reason == "error"`` 且 ``error_message`` 命中某个 provider 的
   溢出措辞（20 余种，见 :data:`OVERFLOW_PATTERNS`）；
2. **静默溢出**：请求成功（``stop_reason == "stop"``）但 ``input + cache_read``
   超过上下文窗口（z.ai 风格）；
3. **length 截断**：``stop_reason == "length"`` 且 ``output == 0`` 且
   ``input + cache_read`` 已填满窗口的 99%（Xiaomi MiMo 风格）。
"""

from __future__ import annotations

import re

from .types import AssistantMessage

__all__ = ["get_overflow_patterns", "is_context_overflow", "is_recoverable_length"]

#: 各 provider 的上下文溢出措辞。逐条对应上游 ``OVERFLOW_PATTERNS``（overflow.ts:37-63），
#: 顺序与注释保持一致。
OVERFLOW_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"prompt (?:is )?too long", re.IGNORECASE),  # Anthropic 与 z.ai
    re.compile(r"prompt exceeds max length", re.IGNORECASE),  # z.ai CN endpoint
    re.compile(r"request_too_large", re.IGNORECASE),  # Anthropic 字节数超限（HTTP 413）
    re.compile(r"input is too long for requested model", re.IGNORECASE),  # Amazon Bedrock
    re.compile(r"exceeds the context window", re.IGNORECASE),  # OpenAI
    re.compile(
        r"exceeds (?:the )?(?:model'?s )?maximum context length(?: of [\d,]+ tokens?|\s*\([\d,]+\))",
        re.IGNORECASE,
    ),  # OpenAI 兼容代理（LiteLLM）
    re.compile(r"input token count.*exceeds the maximum", re.IGNORECASE),  # Google Gemini
    re.compile(r"maximum prompt length is \d+", re.IGNORECASE),  # xAI Grok
    re.compile(r"reduce the length of the messages", re.IGNORECASE),  # Groq
    re.compile(r"maximum context length is \d+ tokens", re.IGNORECASE),  # OpenRouter
    re.compile(
        r"exceeds (?:the )?maximum allowed input length of [\d,]+ tokens?", re.IGNORECASE
    ),  # OpenRouter/Poolside
    re.compile(
        r"input \(\d+ tokens\) is longer than the model'?s context length \(\d+ tokens\)",
        re.IGNORECASE,
    ),  # Together AI
    re.compile(r"exceeds the limit of \d+", re.IGNORECASE),  # GitHub Copilot
    re.compile(r"exceeds the available context size", re.IGNORECASE),  # llama.cpp
    re.compile(r"greater than the context length", re.IGNORECASE),  # LM Studio
    re.compile(r"context window exceeds limit", re.IGNORECASE),  # MiniMax
    re.compile(r"exceeded model token limit", re.IGNORECASE),  # Kimi For Coding
    re.compile(r"too large for model with \d+ maximum context length", re.IGNORECASE),  # Mistral
    re.compile(
        r"prompt has [\d,]+ tokens?, but the configured context size is [\d,]+ tokens?",
        re.IGNORECASE,
    ),  # DS4
    re.compile(r"model_context_window_exceeded", re.IGNORECASE),  # z.ai finish_reason 文本
    re.compile(r"prompt too long; exceeded (?:max )?context length", re.IGNORECASE),  # Ollama
    re.compile(r"range of input length should be", re.IGNORECASE),  # DashScope / Qwen
    re.compile(r"context[_ ]length[_ ]exceeded", re.IGNORECASE),  # 通用兜底
    re.compile(r"too many tokens", re.IGNORECASE),  # 通用兜底
    re.compile(r"token limit exceeded", re.IGNORECASE),  # 通用兜底
]

#: Cerebras 对无 body 的 400/413 只回状态码，需单独识别（上游 overflow.ts:65）。
CEREBRAS_BODYLESS_OVERFLOW_PATTERN = re.compile(
    r"^4(?:00|13)\s*(?:status code)?\s*\(no body\)", re.IGNORECASE
)

#: **非**溢出错误（限流/服务不可用）。命中即跳过溢出模式匹配，即便同时命中某条溢出模式。
#: 例：Bedrock 把限流格式化成 "ThrottlingException: Too many tokens, please wait..."，
#: 裸匹配会命中 ``too many tokens`` 溢出模式（上游 overflow.ts:76-80）。
NON_OVERFLOW_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^(Throttling error|Service unavailable):", re.IGNORECASE),  # AWS Bedrock
    re.compile(r"rate limit", re.IGNORECASE),  # 通用限流
    re.compile(r"too many requests", re.IGNORECASE),  # 通用 HTTP 429
]


def is_context_overflow(message: AssistantMessage, context_window: int | None = None) -> bool:
    """判断一条 assistant 消息是否表示上下文溢出。

    对应上游 ``isContextOverflow``（``overflow.ts:137-171``）。

    :param message: 待判定的 assistant 消息
    :param context_window: 可选。传入后才能识别**静默溢出**（形态 2）与
        **length 截断**（形态 3）；不传则只做形态 1 的措辞匹配。
    :returns: 命中任一形态返回 True。

    .. note::
       ``context_window`` 用的是**真值**判断（与上游 JS 一致）：传 ``0`` 或 ``None``
       等同于"不传"，不会进入形态 2/3。
    """
    # 形态 1：错误措辞
    if message.stop_reason == "error" and message.error_message:
        is_non_overflow = any(p.search(message.error_message) for p in NON_OVERFLOW_PATTERNS)
        if not is_non_overflow:
            if any(p.search(message.error_message) for p in OVERFLOW_PATTERNS):
                return True
            if message.provider == "cerebras" and CEREBRAS_BODYLESS_OVERFLOW_PATTERN.search(
                message.error_message
            ):
                return True

    # 形态 2：静默溢出（z.ai 风格）——成功但用量已超窗口
    if context_window and message.stop_reason == "stop":
        input_tokens = message.usage.input + message.usage.cache_read
        if input_tokens > context_window:
            return True

    # 形态 3：length 截断（Xiaomi MiMo 风格）——服务端把超长输入截到刚好填满窗口，
    # 没给输出留空间：stop_reason "length" + output 0 + 输入填满窗口的 99%。
    if context_window and message.stop_reason == "length" and message.usage.output == 0:
        input_tokens = message.usage.input + message.usage.cache_read
        if input_tokens >= context_window * 0.99:
            return True

    return False


def is_recoverable_length(message: AssistantMessage, desired_max_output: int) -> bool:
    """length 停止是否**未达到**调用方或模型期望的输出上限。

    对应上游 ``isRecoverableLength``（``overflow.ts:179-181``）。这类响应可能是上下文
    压力或 provider 侧截断造成的，调用方可做**一次**有界的 compact-and-retry。

    :param desired_max_output: 必须是**未经上下文裁剪前**的原始上限。
    """
    return (
        message.stop_reason == "length"
        and desired_max_output > 0
        and message.usage.output < desired_max_output
    )


def get_overflow_patterns() -> list[re.Pattern[str]]:
    """返回溢出模式表的**副本**（上游注释：for testing purposes）。

    对应上游 ``getOverflowPatterns``（``overflow.ts:186-188``）。返回副本使调用方
    无法就地篡改模块常量。
    """
    return list(OVERFLOW_PATTERNS)
