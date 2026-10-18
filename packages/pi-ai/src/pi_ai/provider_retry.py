"""Abortable retries for provider SDK requests.

The OpenAI and Anthropic clients are configured with ``max_retries=0`` and
wrapped here so retry backoff can be cancelled by the agent.
"""

from __future__ import annotations

import asyncio
import email.utils
import math
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TypeVar

T = TypeVar("T")
DEFAULT_MAX_RETRY_DELAY_MS = 60_000


def _headers(error: BaseException) -> Mapping[str, str]:
    headers = getattr(error, "headers", None)
    if headers is None:
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def _status(error: BaseException) -> int | None:
    value = getattr(error, "status_code", getattr(error, "status", None))
    return value if isinstance(value, int) else None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return str(value)
    return None


def _is_retryable(error: BaseException) -> bool:
    headers = _headers(error)
    should_retry = _header(headers, "x-should-retry")
    if should_retry == "true":
        return True
    if should_retry == "false":
        return False
    status = _status(error)
    if status is None:
        return hasattr(error, "status_code") or hasattr(error, "status")
    return status in {408, 409, 429} or status >= 500


def _validate_delay(delay_ms: float, maximum: int | None, message: str) -> float:
    max_delay = DEFAULT_MAX_RETRY_DELAY_MS if maximum is None else maximum
    if max_delay > 0 and delay_ms > max_delay:
        raise RuntimeError(
            f"Server requested {int((delay_ms + 999) // 1000)}s retry delay "
            f"(max: {int((max_delay + 999) // 1000)}s). {message}"
        )
    return max(0, delay_ms)


def _parse_retry_after_ms_header(value: str | None) -> float | None:
    """解析 ``retry-after-ms`` 头（**值已是毫秒**），不可解析时返回 None。

    对应上游 ``Number.isFinite`` 守卫（utils/provider-retry.ts:55-58，issue #9571）。

    v0.85.1 本端缺陷：``float("nan")`` 不抛异常，``nan > max`` 为 False，经
    ``max(0, nan)`` 得到 **0** → "立即重试"形成热循环；``inf`` 则 ``inf > max``
    为 True → 抛 RuntimeError 打断重试循环。两者都应视为"不可解析"而回退指数退避。
    """
    if value is None:
        return None
    try:
        milliseconds = float(value)
    except ValueError:
        return None
    return milliseconds if math.isfinite(milliseconds) else None


def _parse_retry_after_header(value: str | None) -> float | None:
    """解析 ``retry-after`` 头（**秒数**或 HTTP 日期），返回毫秒，失败返回 None。

    对应上游 utils/provider-retry.ts:60-65：先按秒数解析，失败再按 HTTP 日期
    （``Date.parse``）解析，两者都得不到有限值则返回 None。

    v0.85.1 本端缺陷：``nan`` 同上得 0；日期不可解析时
    ``email.utils.parsedate_to_datetime`` 在 Python 3.10+ **抛 ValueError**，
    该异常会穿透 ``_retry_delay_ms`` 逃出 ``retry_provider_request``，用无关异常
    取代原有 provider 错误。
    """
    if value is None:
        return None
    try:
        milliseconds = float(value) * 1000
        if math.isfinite(milliseconds):
            return milliseconds
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        return None
    try:
        milliseconds = parsed.timestamp() * 1000 - time.time() * 1000
    except (OverflowError, OSError, ValueError):
        return None
    return milliseconds if math.isfinite(milliseconds) else None


def _retry_delay_ms(error: BaseException, retry_index: int, maximum: int | None) -> float:
    headers = _headers(error)
    retry_after_ms = _parse_retry_after_ms_header(_header(headers, "retry-after-ms"))
    if retry_after_ms is not None:
        return _validate_delay(retry_after_ms, maximum, str(error))
    retry_after = _parse_retry_after_header(_header(headers, "retry-after"))
    if retry_after is not None:
        return _validate_delay(retry_after, maximum, str(error))
    exponential = min(0.5 * (2**retry_index), 8) * 1000
    return float(exponential * (1 - random.random() * 0.25))


async def _abortable_sleep(ms: float, cancel_event: asyncio.Event | None) -> None:
    if cancel_event is None:
        await asyncio.sleep(ms / 1000)
        return
    if cancel_event.is_set():
        raise asyncio.CancelledError
    sleeper = asyncio.create_task(asyncio.sleep(ms / 1000))
    cancelled = asyncio.create_task(cancel_event.wait())
    done, pending = await asyncio.wait({sleeper, cancelled}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if cancelled in done:
        raise asyncio.CancelledError


async def retry_provider_request(
    request: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 0,
    max_retry_delay_ms: int | None = None,
    cancel_event: asyncio.Event | None = None,
    no_retry_statuses: tuple[int, ...] | None = None,
) -> T:
    """带可取消退避的 provider 请求重试。

    ``no_retry_statuses`` 是 v1.3.0 新增（上游 utils/provider-retry.ts:8）：这些
    HTTP 状态码**立即失败**，即使默认策略本会重试它们（如 503 携带
    ``x-should-retry: true``）。
    """
    retries_remaining = max_retries
    while True:
        try:
            return await request()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError from error
            if retries_remaining <= 0 or not _is_retryable(error):
                raise
            status = _status(error)
            if no_retry_statuses and status is not None and status in no_retry_statuses:
                raise
            retry_index = max_retries - retries_remaining
            retries_remaining -= 1
            await _abortable_sleep(
                _retry_delay_ms(error, retry_index, max_retry_delay_ms), cancel_event
            )


__all__ = ["retry_provider_request"]
