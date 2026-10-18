"""headers 归一化（M-7）与上下文估算（M-10）测试。"""

from __future__ import annotations

import math

import pytest

from pi_ai.estimate import (
    CHARS_PER_TOKEN,
    ContextUsageEstimate,
    calculate_context_tokens,
    estimate_context_tokens,
    estimate_message_tokens,
    estimate_text_tokens,
)
from pi_ai.headers import headers_to_record, provider_headers_to_record
from pi_ai.transcript import normalize_context
from pi_ai.types import (
    AssistantMessage,
    Context,
    ImageContent,
    SystemMessage,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)

# ============================================================
# M-7：providerHeadersToRecord
# ============================================================


def test_headers_to_record_handles_mappings_and_none():
    assert headers_to_record(None) == {}
    assert headers_to_record({"A": "1"}) == {"A": "1"}
    assert headers_to_record({"A": 1}) == {"A": "1"}
    assert headers_to_record(object()) == {}


def test_provider_headers_to_record_returns_none_when_empty():
    assert provider_headers_to_record() is None
    assert provider_headers_to_record(None, None) is None
    assert provider_headers_to_record({}) is None


def test_provider_headers_to_record_merges_varargs_with_last_wins():
    """v1.3.0 签名由单参数改为**变参**，后者覆盖前者（issue #10429）。"""
    merged = provider_headers_to_record(
        {"User-Agent": "pi", "originator": "pi"},
        {"originator": "custom"},
    )
    assert merged == {"User-Agent": "pi", "originator": "custom"}


def test_provider_headers_to_record_folds_case_insensitively():
    """按小写名折叠，避免 ``User-Agent``/``user-agent`` 同时出现在请求里。"""
    merged = provider_headers_to_record({"User-Agent": "pi"}, {"user-agent": "custom"})
    assert merged is not None
    assert list(merged) == ["user-agent"]
    assert merged["user-agent"] == "custom"


def test_provider_headers_to_record_none_deletes_a_key():
    merged = provider_headers_to_record({"originator": "pi", "keep": "1"}, {"originator": None})
    assert merged == {"keep": "1"}
    assert provider_headers_to_record({"a": "1"}, {"a": None}) is None


# ============================================================
# M-10：上下文估算
# ============================================================


def test_chars_per_token_is_three_point_five():
    """v1.3.0 由 4 改为 3.5，使输出上限估算更保守（issue #10497）。"""
    assert CHARS_PER_TOKEN == 3.5


def test_estimate_text_tokens_uses_the_new_ratio():
    # 7 字符 / 3.5 = 2 token（若仍是 4 则 ceil(7/4)=2，用 8 字符区分）
    assert estimate_text_tokens("1234567") == 2
    assert estimate_text_tokens("12345678") == 3  # ceil(8/3.5)=3；旧比例下会是 2


def test_estimate_text_tokens_avoids_float_boundary_errors():
    # 3.5 * 2 == 7 在浮点下是 7.000000000000001，整数实现必须仍得 2
    assert estimate_text_tokens("a" * 7) == 2
    assert estimate_text_tokens("a" * 14) == 4


def test_estimate_message_tokens_for_each_role():
    assert estimate_message_tokens(UserMessage(content="12345678")) == 3
    assert (
        estimate_message_tokens(
            ToolResultMessage(toolCallId="c", toolName="t", content=[TextContent(text="12345678")])
        )
        == 3
    )
    assert (
        estimate_message_tokens(
            AssistantMessage(content=[TextContent(text="12345678")], stop_reason="stop")
        )
        == 3
    )
    # thinking 计入
    assert (
        estimate_message_tokens(
            AssistantMessage(content=[ThinkingContent(thinking="12345678")], stop_reason="stop")
        )
        == 3
    )
    # tool call：name + 序列化 arguments
    tokens = estimate_message_tokens(
        AssistantMessage(content=[ToolCall(id="c", name="t", arguments={})])
    )
    assert tokens >= 1


def test_estimate_message_tokens_counts_images():
    msg = UserMessage(content=[ImageContent(data="x", mime_type="image/png")])
    # 4800 字符 / 3.5 ≈ 1372
    assert estimate_message_tokens(msg) == math.ceil(4800 / 3.5)


def test_system_message_tokens_include_prompt_and_tools():
    plain = SystemMessage(content="12345678")
    assert estimate_message_tokens(plain) == 3
    with_tools = SystemMessage(
        content="12345678",
        tools_added=[Tool(name="t", description="d", parameters={"type": "object"})],
    )
    assert estimate_message_tokens(with_tools) > estimate_message_tokens(plain)


def test_calculate_context_tokens_prefers_total():
    assert calculate_context_tokens(Usage(input=1, output=2, total_tokens=10)) == 10
    assert calculate_context_tokens(Usage(input=1, output=2, cache_read=3, cache_write=4)) == 10


def test_estimate_context_tokens_uses_last_assistant_usage_as_baseline():
    messages = [
        UserMessage(content="a" * 100),
        AssistantMessage(
            content=[TextContent(text="ok")],
            stop_reason="stop",
            timestamp=10,
            usage=Usage(input=100, output=10, total_tokens=110),
        ),
        UserMessage(content="b" * 70),  # 70/3.5 = 20
    ]
    estimate = estimate_context_tokens(messages)
    assert isinstance(estimate, ContextUsageEstimate)
    assert estimate.usage_tokens == 110
    assert estimate.trailing_tokens == 20
    assert estimate.tokens == 130
    assert estimate.last_usage_index == 1


def test_estimate_context_tokens_ignores_error_and_aborted_usage():
    messages = [
        AssistantMessage(
            content=[],
            stop_reason="error",
            timestamp=10,
            usage=Usage(input=100, output=10, total_tokens=110),
        ),
        UserMessage(content="b" * 70),
    ]
    estimate = estimate_context_tokens(messages)
    assert estimate.last_usage_index is None
    assert estimate.usage_tokens == 0


def test_estimate_context_tokens_ignores_usage_predating_a_newer_prefix_message():
    """更晚的 prefix 消息（如压缩摘要）插在 assistant **之前**时，其 usage 不再适用。

    上游语义（``estimate.ts:71-95``）：遍历时维护"已见消息的最大 timestamp"，
    assistant 的 usage 只有在其 ``timestamp >= 该最大值`` 时才被采信——即当有
    比它更新的消息出现在它**前面**时（压缩摘要被插到历史之前），视为失效。
    """
    messages = [
        UserMessage(content="summary", timestamp=20),  # 更新的前缀消息在 assistant 之前
        AssistantMessage(
            content=[],
            stop_reason="stop",
            timestamp=10,
            usage=Usage(input=100, output=10, total_tokens=110),
        ),
        UserMessage(content="x" * 35),
    ]
    estimate = estimate_context_tokens(messages)
    assert estimate.last_usage_index is None
    assert estimate.usage_tokens == 0


def test_estimate_context_tokens_keeps_usage_when_no_newer_prefix_exists():
    """对照组：assistant 之前的消息都更旧时，usage 正常采信。"""
    messages = [
        UserMessage(content="hi", timestamp=1),
        AssistantMessage(
            content=[],
            stop_reason="stop",
            timestamp=10,
            usage=Usage(input=100, output=10, total_tokens=110),
        ),
        UserMessage(content="x" * 35),
    ]
    estimate = estimate_context_tokens(messages)
    assert estimate.last_usage_index == 1
    assert estimate.usage_tokens == 110


def test_estimate_context_tokens_accepts_transcript_context():
    ctx = normalize_context(Context(system_prompt="be brief", messages=[UserMessage(content="hi")]))
    from_transcript = estimate_context_tokens(ctx)
    from_messages = estimate_context_tokens(ctx.messages)
    assert from_transcript.tokens == from_messages.tokens
    assert from_transcript.tokens > 0


@pytest.mark.parametrize("value", ["", "a"])
def test_estimate_tokens_is_stable_for_tiny_inputs(value: str) -> None:
    expected = 0 if value == "" else 1
    assert estimate_text_tokens(value) == expected
