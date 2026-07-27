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
