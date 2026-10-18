"""``is_context_overflow`` / ``is_recoverable_length`` 测试。

**逐条移植上游** ``packages/ai/test/overflow.test.ts``（v1.3.0，197 行）的全部用例。
"""

from __future__ import annotations

from typing import Any

import pytest

from pi_ai.overflow import (
    OVERFLOW_PATTERNS,
    get_overflow_patterns,
    is_context_overflow,
    is_recoverable_length,
)
from pi_ai.types import AssistantMessage, Usage


def _error_message(error_message: str, provider: str = "ollama") -> AssistantMessage:
    """上游 ``createErrorMessage`` 的等价物。"""
    return AssistantMessage(
        content=[],
        api="openai-completions",
        provider=provider,
        model="qwen3.5:35b",
        usage=Usage(),
        stop_reason="error",
        error_message=error_message,
        timestamp=1,
    )


def _length_stop(
    *,
    input: int,  # noqa: A002 - 与上游用例字段名一致，便于逐条对照
    cache_read: int,
    output: int,
    cache_write: int = 0,
    api: str = "openai-completions",
    provider: str = "test-provider",
    model: str = "test-model",
) -> AssistantMessage:
    """上游 ``createLengthStopMessage`` 的等价物。"""
    return AssistantMessage(
        content=[],
        api=api,
        provider=provider,
        model=model,
        usage=Usage(
            input=input,
            output=output,
            cacheRead=cache_read,
            cacheWrite=cache_write,
            totalTokens=input + cache_read + cache_write + output,
        ),
        stop_reason="length",
        timestamp=1,
    )


# ============================================================
# 形态 1：显式溢出报错
# ============================================================


def _error_overflows(error_message: str, provider: str, context_window: int) -> bool:
    return is_context_overflow(_error_message(error_message, provider), context_window)


def test_detects_explicit_ollama_prompt_too_long_errors() -> None:
    assert _error_overflows(
        "400 `prompt too long; exceeded max context length by 100918 tokens`", "ollama", 32768
    )


def test_detects_zai_prompt_too_long_errors() -> None:
    """Regression for #9805."""
    assert _error_overflows('400 {"code":"1261","message":"Prompt too long"}', "zai", 1048576)


def test_detects_zai_cn_endpoint_prompt_exceeds_max_length_errors() -> None:
    """Regression for #10208."""
    assert _error_overflows(
        '400 {"code":"1261","message":"Prompt exceeds max length"}', "zai", 1048576
    )


def test_detects_together_ai_context_length_errors() -> None:
    assert _error_overflows(
        "400 The input (516368 tokens) is longer than the model's context length (262144 tokens).",
        "together",
        262144,
    )


def test_detects_litellm_wrapped_maximum_context_length_errors() -> None:
    assert _error_overflows(
        "Error: 503 litellm.ServiceUnavailableError: litellm.MidStreamFallbackError: "
        "litellm.APIConnectionError: APIConnectionError: OpenAIException - Requested token "
        "count exceeds the model's maximum context length of 131072 tokens.",
        "litellm",
        131072,
    )


def test_detects_openai_compatible_parenthesized_maximum_context_length_errors() -> None:
    assert _error_overflows(
        "Error: 400 Input length (265330) exceeds model's maximum context length (262144).",
        "openai",
        262144,
    )


def test_detects_openrouter_poolside_maximum_allowed_input_length_errors() -> None:
    assert _error_overflows(
        "Provider returned error: Input length 131393 exceeds the maximum allowed input "
        "length of 131040 tokens.",
        "openrouter",
        131072,
    )


def test_detects_ds4_configured_context_size_errors() -> None:
    assert _error_overflows(
        "400 Prompt has 256468 tokens, but the configured context size is 256000 tokens",
        "ds4",
        256000,
    )
    # 带千分位逗号的变体（模式里的 [\d,]+）
    assert _error_overflows(
        "Prompt has 5,958,968 tokens, but the configured context size is 256,000 tokens",
        "ds4",
        256000,
    )


def test_does_not_treat_generic_non_overflow_ollama_errors_as_overflow() -> None:
    assert not _error_overflows("500 `model runner crashed unexpectedly`", "ollama", 32768)


@pytest.mark.parametrize(
    "error_message", ["400 status code (no body)", "413 status code (no body)"]
)
def test_only_treats_bodyless_400_and_413_as_overflow_for_cerebras(error_message: str) -> None:
    """Regression for #9482：无 body 的状态码只对 cerebras provider 视为溢出。"""
    assert is_context_overflow(_error_message(error_message, "cerebras"), 131072) is True
    assert is_context_overflow(_error_message(error_message, "opencode-go"), 1000000) is False


def test_does_not_treat_bedrock_throttling_too_many_tokens_as_overflow() -> None:
    """Bedrock 的 HTTP 429 限流措辞会命中 ``too many tokens``，须由 NON_OVERFLOW 排除。"""
    assert not _error_overflows(
        "Throttling error: Too many tokens, please wait before trying again.",
        "amazon-bedrock",
        200000,
    )


def test_does_not_treat_bedrock_service_unavailable_as_overflow() -> None:
    assert not _error_overflows(
        "Service unavailable: The service is temporarily unavailable.", "amazon-bedrock", 200000
    )


def test_does_not_treat_generic_rate_limit_errors_as_overflow() -> None:
    assert not _error_overflows("Rate limit exceeded, please retry after 30 seconds.", "x", 200000)


def test_does_not_treat_http_429_style_errors_as_overflow() -> None:
    assert not _error_overflows("Too many requests. Please slow down.", "x", 200000)


# ============================================================
# 形态 2 / 3：静默溢出与 length 截断
# ============================================================


def test_detects_xiaomi_style_overflow() -> None:
    message = _length_stop(
        input=58, cache_read=1048512, output=0, provider="xiaomi", model="mimo-v2.5-pro"
    )
    assert is_context_overflow(message, 1048576) is True


def test_does_not_treat_normal_length_stops_with_output_as_overflow() -> None:
    assert is_context_overflow(_length_stop(input=1000, cache_read=0, output=4096), 200000) is False


def test_does_not_treat_zero_output_length_stops_far_below_context_as_overflow() -> None:
    assert is_context_overflow(_length_stop(input=100, cache_read=0, output=0), 200000) is False


def test_silent_overflow_when_successful_usage_exceeds_the_window() -> None:
    message = AssistantMessage(
        api="openai-completions",
        provider="zai",
        model="glm",
        usage=Usage(input=200000, cacheRead=0, totalTokens=200000),
        stop_reason="stop",
        timestamp=1,
    )
    assert is_context_overflow(message, 100000) is True
    assert is_context_overflow(message, 300000) is False


def test_silent_overflow_counts_cache_read_toward_input() -> None:
    message = AssistantMessage(
        api="openai-completions",
        provider="zai",
        model="glm",
        usage=Usage(input=1, cacheRead=99_999, totalTokens=100_000),
        stop_reason="stop",
        timestamp=1,
    )
    assert is_context_overflow(message, 100_000) is False  # 1 + 99999 == 100000，非严格大于
    assert is_context_overflow(message, 99_999) is True


def test_context_window_zero_is_treated_as_absent() -> None:
    """``context_window`` 用**真值**判断（与上游 JS 一致）：0 等同不传。"""
    message = AssistantMessage(
        api="a",
        provider="p",
        model="m",
        usage=Usage(input=10**9, totalTokens=10**9),
        stop_reason="stop",
        timestamp=1,
    )
    assert is_context_overflow(message, 0) is False
    assert is_context_overflow(message, None) is False
    assert is_context_overflow(message, 100) is True


def test_provider_is_not_required_for_pattern_detection() -> None:
    """形态 1 与 provider 无关（除 cerebras 的专用分支）。"""
    assert is_context_overflow(_error_message("prompt is too long", ""), None) is True


def test_missing_error_message_is_not_overflow() -> None:
    """``stop_reason == "error"`` 但没有 errorMessage 时不判定为溢出。"""
    message = AssistantMessage(api="a", provider="p", model="m", stop_reason="error", timestamp=1)
    assert is_context_overflow(message, 100) is False


def test_usage_fields_default_to_zero_and_do_not_break_detection() -> None:
    message = AssistantMessage(api="a", provider="p", model="m", stop_reason="length", timestamp=1)
    assert is_context_overflow(message, 1000) is False


# ============================================================
# is_recoverable_length
# ============================================================


def test_treats_a_length_stop_below_the_desired_output_limit_as_recoverable() -> None:
    message = _length_stop(
        input=3,
        cache_read=253584,
        cache_write=25554,
        output=16,
        api="openai-responses",
        provider="openai",
        model="gpt-5.6-sol",
    )
    assert is_recoverable_length(message, 128000) is True


def test_does_not_recover_a_length_stop_that_reached_the_desired_output_limit() -> None:
    assert is_recoverable_length(_length_stop(input=4062, cache_read=0, output=1024), 1024) is False


def test_treats_zero_output_length_stops_as_recoverable_without_context_metadata() -> None:
    assert is_recoverable_length(_length_stop(input=100, cache_read=0, output=0), 128000) is True


@pytest.mark.parametrize("desired", [0, -1])
def test_not_recoverable_when_desired_max_output_is_not_positive(desired: int) -> None:
    assert is_recoverable_length(_length_stop(input=1, cache_read=0, output=0), desired) is False


def test_not_recoverable_for_non_length_stops() -> None:
    message = _length_stop(input=1, cache_read=0, output=0)
    message.stop_reason = "stop"
    assert is_recoverable_length(message, 128000) is False


# ============================================================
# get_overflow_patterns
# ============================================================


def test_get_overflow_patterns_returns_a_copy() -> None:
    patterns = get_overflow_patterns()
    assert patterns == OVERFLOW_PATTERNS
    assert patterns is not OVERFLOW_PATTERNS, "必须返回副本，避免调用方篡改模块常量"
    patterns.clear()
    assert len(OVERFLOW_PATTERNS) > 0, "模块常量被就地篡改"


def test_get_overflow_patterns_are_compiled_and_case_insensitive() -> None:
    patterns = get_overflow_patterns()
    assert patterns
    assert all(hasattr(p, "search") for p in patterns)
    # 抽查大小写不敏感（上游用 /i）
    assert any(p.search("PROMPT IS TOO LONG") for p in patterns)


# ============================================================
# 导入路径 / 导出面
# ============================================================


def test_both_import_paths_resolve_to_the_same_objects() -> None:
    from pi_ai import get_overflow_patterns as top_c
    from pi_ai import is_context_overflow as top_a
    from pi_ai import is_recoverable_length as top_b
    from pi_ai.utils.overflow import get_overflow_patterns as alias_c
    from pi_ai.utils.overflow import is_context_overflow as alias_a
    from pi_ai.utils.overflow import is_recoverable_length as alias_b

    assert is_context_overflow is top_a is alias_a
    assert is_recoverable_length is top_b is alias_b
    assert get_overflow_patterns is top_c is alias_c


def test_export_surface_matches_upstream() -> None:
    from pi_ai import overflow as mod

    assert sorted(mod.__all__) == [
        "get_overflow_patterns",
        "is_context_overflow",
        "is_recoverable_length",
    ]


def test_module_level_tables_are_immutable_constants() -> None:
    """上游三个模式表都是 ``const``（运行时只读）；转发层因此无"状态被复制"风险。"""
    from pi_ai import overflow as mod

    assert isinstance(mod.OVERFLOW_PATTERNS, list)
    assert isinstance(mod.NON_OVERFLOW_PATTERNS, list)
    assert hasattr(mod.CEREBRAS_BODYLESS_OVERFLOW_PATTERN, "search")
    assert "_last_ordinary_timestamp" not in dir(mod)  # 与 uuid 不同：无生成器状态


def test_overflow_does_not_mutate_the_message() -> None:
    message = _error_message("prompt is too long")
    snapshot: dict[str, Any] = message.model_dump(by_alias=True)
    is_context_overflow(message, 1000)
    assert message.model_dump(by_alias=True) == snapshot
