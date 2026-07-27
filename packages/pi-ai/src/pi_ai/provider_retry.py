"""Abortable retries for provider SDK requests.

The OpenAI and Anthropic clients are configured with ``max_retries=0`` and
wrapped here so retry backoff can be cancelled by the agent.
"""

from __future__ import annotations

import asyncio
import email.utils
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


def _retry_delay_ms(error: BaseException, retry_index: int, maximum: int | None) -> float:
    headers = _headers(error)
    retry_after_ms = _header(headers, "retry-after-ms")
    if retry_after_ms is not None:
        try:
            return _validate_delay(float(retry_after_ms), maximum, str(error))
        except ValueError:
            pass
    retry_after = _header(headers, "retry-after")
    if retry_after is not None:
        try:
            delay = float(retry_after) * 1000
        except ValueError:
            parsed = email.utils.parsedate_to_datetime(retry_after)
            delay = float(parsed.timestamp()) * 1000 - time.time() * 1000
        return _validate_delay(delay, maximum, str(error))
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
) -> T:
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
            retry_index = max_retries - retries_remaining
            retries_remaining -= 1
            await _abortable_sleep(
                _retry_delay_ms(error, retry_index, max_retry_delay_ms), cancel_event
            )


__all__ = ["retry_provider_request"]
