"""retry 工具测试：错误分类、重试循环、退避、取消标准化。"""

from __future__ import annotations

import asyncio

import pytest

from pi_ai import AssistantMessage
from pi_ai.retry import (
    RetryCallbacks,
    RetryPolicy,
    is_retryable_assistant_error,
    retry_assistant_call,
)


def _err_msg(msg: str) -> AssistantMessage:
    return AssistantMessage(stop_reason="error", error_message=msg)


def _ok_msg() -> AssistantMessage:
    return AssistantMessage(stop_reason="stop")


# ============================================================
# 错误分类
# ============================================================


@pytest.mark.parametrize(
    "msg",
    [
        "429: Too Many Requests",
        "Rate limit exceeded",
        "503 Service Unavailable",
        "The engine is currently overloaded",
        "fetch failed: connection refused",
        "socket hang up",
        "stream ended before message_stop",
        "ResourceExhausted",
        "timed out after 30000ms",
        "you can retry your request",
    ],
)
def test_retryable_errors(msg):
    """可重试：瞬时错误。"""
    assert is_retryable_assistant_error(_err_msg(msg)) is True


@pytest.mark.parametrize(
    "msg",
    [
        "insufficient_quota",
        "You are out of budget",
        "quota exceeded for this month",
        "billing issue",
        "FreeUsageLimitError",
        "Monthly usage limit reached",
    ],
)
def test_non_retryable_errors(msg):
    """不可重试：配额/计费错误（即使伴随 429 文本也不重试）。"""
    assert is_retryable_assistant_error(_err_msg(msg)) is False


def test_non_error_not_retryable():
    """非 error 的消息不重试。"""
    assert is_retryable_assistant_error(_ok_msg()) is False


def test_quota_with_429_text_not_retryable():
    """配额错误伴随 429 文本：不可重试模式优先。"""
    assert is_retryable_assistant_error(_err_msg("429 insufficient_quota")) is False


def test_empty_error_message_not_retryable():
    """空 error_message 不重试。"""
    assert is_retryable_assistant_error(AssistantMessage(stop_reason="error")) is False


# ============================================================
# 重试循环
# ============================================================


async def test_no_retry_policy_returns_first():
    """无 policy：直接返回首次结果。"""
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        return _err_msg("429 rate limit")

    result = await retry_assistant_call(produce, None)
    assert calls == 1
    assert result.stop_reason == "error"


async def test_success_first_try():
    """首次成功不重试。"""
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        return _ok_msg()

    result = await retry_assistant_call(produce, RetryPolicy(enabled=True, max_retries=3))
    assert calls == 1
    assert result.stop_reason == "stop"


async def test_retries_then_succeeds():
    """重试后成功。"""
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        if calls < 3:
            return _err_msg("429 rate limit")
        return _ok_msg()

    result = await retry_assistant_call(
        produce, RetryPolicy(enabled=True, max_retries=5, base_delay_ms=1)
    )
    assert calls == 3
    assert result.stop_reason == "stop"


async def test_max_retries_exhausted():
    """重试耗尽后返回最终错误。"""
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        return _err_msg("429 rate limit")

    result = await retry_assistant_call(
        produce, RetryPolicy(enabled=True, max_retries=2, base_delay_ms=1)
    )
    assert calls == 3  # 1 initial + 2 retries
    assert result.stop_reason == "error"


async def test_non_retryable_returns_immediately():
    """不可重试错误：首次即返回，不消耗重试次数。"""
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        return _err_msg("insufficient_quota")

    result = await retry_assistant_call(
        produce, RetryPolicy(enabled=True, max_retries=5, base_delay_ms=1)
    )
    assert calls == 1
    assert result.stop_reason == "error"


async def test_aborted_never_retried():
    """aborted 永不重试。"""
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        return AssistantMessage(stop_reason="aborted")

    result = await retry_assistant_call(
        produce, RetryPolicy(enabled=True, max_retries=5, base_delay_ms=1)
    )
    assert calls == 1
    assert result.stop_reason == "aborted"


async def test_callbacks_fire():
    """重试回调正确触发。"""
    scheduled = []
    started = []
    finished = []
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        if calls < 2:
            return _err_msg("429")
        return _ok_msg()

    async def on_scheduled(attempt, max_att, delay, err):
        scheduled.append((attempt, max_att, delay, err))

    async def on_start():
        started.append(True)

    async def on_finished(success, attempt, err):
        finished.append((success, attempt, err))

    cb = RetryCallbacks(
        on_retry_scheduled=on_scheduled,
        on_retry_attempt_start=on_start,
        on_retry_finished=on_finished,
    )
    await retry_assistant_call(
        produce, RetryPolicy(enabled=True, max_retries=3, base_delay_ms=1), callbacks=cb
    )
    assert len(scheduled) == 1
    assert scheduled[0][0] == 1  # attempt 1
    assert len(started) == 1
    assert len(finished) == 1
    assert finished[0][0] is True  # success


async def test_backoff_exponential():
    """退避指数增长：base * 2**(attempt-1)。"""
    delays = []
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        return _err_msg("429")

    async def on_scheduled(attempt, max_att, delay, err):
        delays.append(delay)

    cb = RetryCallbacks(on_retry_scheduled=on_scheduled)
    await retry_assistant_call(
        produce, RetryPolicy(enabled=True, max_retries=3, base_delay_ms=10), callbacks=cb
    )
    # attempt 1: 10*1=10, attempt 2: 10*2=20, attempt 3: 10*4=40
    assert delays == [10, 20, 40]


async def test_cancel_during_backoff_normalizes_to_aborted():
    """退避期间取消：标准化为 aborted。"""
    calls = 0
    cancel_event = asyncio.Event()

    async def produce():
        nonlocal calls
        calls += 1
        if calls == 1:
            cancel_event.set()  # 第一次 error 后触发取消
        return _err_msg("429")

    result = await retry_assistant_call(
        produce,
        RetryPolicy(enabled=True, max_retries=5, base_delay_ms=1000),
        cancel_event=cancel_event,
    )
    assert calls == 1
    assert result.stop_reason == "aborted"
    assert result.error_message is None
