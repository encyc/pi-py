from __future__ import annotations

import asyncio

import pytest

from pi_ai.provider_retry import retry_provider_request


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code = status
        self.headers = headers or {}


async def test_provider_retry_retries_transient_status():
    calls = 0

    async def request():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError("busy", 503, {"retry-after-ms": "1"})
        return "ok"

    assert await retry_provider_request(request, max_retries=1) == "ok"
    assert calls == 2


async def test_provider_retry_honors_should_retry_false():
    async def request():
        raise ProviderError("no", 503, {"x-should-retry": "false"})

    with pytest.raises(ProviderError):
        await retry_provider_request(request, max_retries=3)


async def test_provider_retry_delay_is_abortable():
    cancel_event = asyncio.Event()

    async def request():
        cancel_event.set()
        raise ProviderError("busy", 503, {"retry-after-ms": "1000"})

    with pytest.raises(asyncio.CancelledError):
        await retry_provider_request(request, max_retries=1, cancel_event=cancel_event)


# ============================================================
# v1.3.0 对齐（M-9）：不可解析 Retry-After 必须回退指数退避
# ============================================================
#
# 本端 v0.85.1 有四处真实缺陷（上游 issue #9571 同类）：
#   1. retry-after-ms: nan -> max(0, nan) == 0 -> 立即重试（热循环）
#   2. retry-after: nan    -> 同上
#   3. retry-after-ms: inf -> inf > max -> 抛 RuntimeError 打断重试循环
#   4. retry-after: <不可解析日期> -> parsedate_to_datetime 抛 ValueError，
#      穿透 _retry_delay_ms 逃出 retry_provider_request


def _exponential_range(retry_index: int = 0) -> tuple[float, float]:
    """指数退避（含 0-25% jitter）的取值区间。"""
    base = min(0.5 * (2**retry_index), 8) * 1000
    return base * 0.75, base


@pytest.mark.parametrize(
    "headers",
    [
        {"retry-after-ms": "nan"},
        {"retry-after": "nan"},
        {"retry-after-ms": "inf"},
        {"retry-after": "-inf"},
        {"retry-after": "not-a-date"},
        {"retry-after": "Thu, 99 Zzz 9999 99:99:99 GMT"},
    ],
)
def test_unparseable_retry_after_falls_back_to_exponential(headers: dict[str, str]) -> None:
    from pi_ai.provider_retry import _retry_delay_ms

    delay = _retry_delay_ms(ProviderError("busy", 503, headers), 0, None)
    low, high = _exponential_range(0)
    assert low <= delay <= high, f"{headers} -> {delay}ms 未回退到指数退避"


def test_retry_after_nan_does_not_retry_immediately() -> None:
    """核心回归：``nan`` 不得被算成 0 延迟（那会变成热循环）。"""
    from pi_ai.provider_retry import _retry_delay_ms

    assert _retry_delay_ms(ProviderError("busy", 503, {"retry-after-ms": "nan"}), 0, None) > 0


def test_retry_after_inf_does_not_raise() -> None:
    """核心回归：``inf`` 不得抛 RuntimeError 打断重试循环。"""
    from pi_ai.provider_retry import _retry_delay_ms

    assert _retry_delay_ms(ProviderError("busy", 503, {"retry-after-ms": "inf"}), 0, None) > 0


def test_garbage_retry_after_date_does_not_escape_the_retry_loop() -> None:
    """核心回归：不可解析日期不得让 ValueError 逃出重试循环。"""
    calls = 0

    async def request():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError("busy", 503, {"retry-after": "definitely not a date"})
        return "ok"

    assert asyncio.run(retry_provider_request(request, max_retries=1)) == "ok"
    assert calls == 2


def test_valid_retry_after_is_still_honored() -> None:
    from pi_ai.provider_retry import _retry_delay_ms

    assert _retry_delay_ms(ProviderError("busy", 503, {"retry-after-ms": "1500"}), 0, None) == 1500
    assert _retry_delay_ms(ProviderError("busy", 503, {"retry-after": "2"}), 0, None) == 2000


def test_no_retry_statuses_fails_immediately() -> None:
    """v1.3.0 新增 ``noRetryStatuses``：这些状态码立即失败。"""
    calls = 0

    async def request():
        nonlocal calls
        calls += 1
        raise ProviderError("busy", 503, {"retry-after-ms": "1"})

    with pytest.raises(ProviderError):
        asyncio.run(retry_provider_request(request, max_retries=3, no_retry_statuses=(503,)))
    assert calls == 1


def test_no_retry_statuses_leaves_other_statuses_retryable() -> None:
    calls = 0

    async def request():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError("busy", 503, {"retry-after-ms": "1"})
        return "ok"

    assert (
        asyncio.run(retry_provider_request(request, max_retries=1, no_retry_statuses=(429,)))
        == "ok"
    )
    assert calls == 2
