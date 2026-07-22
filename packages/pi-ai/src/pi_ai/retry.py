"""重试工具。

对应上游 ``utils/retry.ts``。基于 ``AssistantMessage.stop_reason`` 判定是否重试，
纯指数退避（``base_delay * 2**(attempt-1)``），无 jitter、无最大延迟上限。

关键设计（与上游一致）：
- 接收 ``produce()`` 回调而非流本身；每次重试重新调一次 produce。
- 基于 ``stop_reason``：``aborted`` 直接返回不重试；非 error 直接返回成功；
  error 时按错误分类决定重试。
- **不可重试模式优先**（配额/计费错误常伴随 429 文本，须先排除）。
- 退避 sleep 期间被取消，标准化为 ``stop_reason="aborted"``，与流内取消一致。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .types import AssistantMessage

# ============================================================
# 错误分类正则（大小写不敏感，子串匹配）
# ============================================================

#: 不可重试：配额/计费/订阅耗尽（确定性错误，优先匹配）。
#: 对应上游 ``NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN``。
_NON_RETRYABLE_PATTERNS = [
    "GoUsageLimitError",
    "FreeUsageLimitError",
    "Monthly usage limit reached",
    "available balance",
    "insufficient_quota",
    "out of budget",
    "quota exceeded",
    "billing",
]
_NON_RETRYABLE_RE = re.compile("|".join(_NON_RETRYABLE_PATTERNS), re.IGNORECASE)

#: 可重试：过载/限流/5xx/网络/连接/超时/stream 早断等瞬时错误。
#: 对应上游 ``RETRYABLE_PROVIDER_ERROR_PATTERN``。
#: 注意 ``.?`` 是正则元字符（前一个字符可选），且 "timed? out" 中间有空格。
_RETRYABLE_PATTERNS = [
    # 通用负载/HTTP 状态/服务端瞬时故障
    "overloaded",
    "rate.?limit",
    "too many requests",
    "429",
    "500",
    "502",
    "503",
    "504",
    "524",
    "service.?unavailable",
    "server.?error",
    "internal.?error",
    # wrapper/provider 瞬时上游故障
    "provider.?returned.?error",
    # 网络/代理/fetch 传输故障
    "network.?error",
    "connection.?error",
    "connection.?refused",
    "connection.?lost",
    "other side closed",
    "fetch failed",
    "upstream.?connect",
    "reset before headers",
    "socket hang up",
    "socket connection was closed",
    "timed? out",  # 注意：源码此处 "time" + 可选 "d" + 空格 + "out"
    "timeout",
    "terminated",
    # WebSocket 传输
    "websocket.?closed",
    "websocket.?error",
    # 流过早结束
    "ended without",
    "stream ended before message_stop",
    "stream ended before a terminal response event",
    "http2 request did not get a response",
    # provider 请求的重试
    "retry delay",
    "you can retry your request",
    "try your request again",
    "please retry your request",
    # gRPC（如 NVIDIA NIM）
    "ResourceExhausted",
]
_RETRYABLE_RE = re.compile("|".join(_RETRYABLE_PATTERNS), re.IGNORECASE)


def is_retryable_assistant_error(message: AssistantMessage) -> bool:
    """判断一个 error 消息是否可重试。

    顺序关键：先查不可重试（配额/计费），命中即 False；否则查可重试。
    """
    if message.stop_reason != "error" or not message.error_message:
        return False
    msg = message.error_message
    if _NON_RETRYABLE_RE.search(msg):
        return False
    return bool(_RETRYABLE_RE.search(msg))


# ============================================================
# RetryPolicy / 回调
# ============================================================


@dataclass
class RetryPolicy:
    """重试策略。"""

    enabled: bool = False
    #: 最大重试次数（0 = 不重试）。首次调用不计为重试。总尝试上限 = 1 + max_retries。
    max_retries: int = 0
    #: 基础延迟（毫秒）。每次重试延迟 = base_delay_ms * 2**(attempt-1)。
    base_delay_ms: int = 1000


@dataclass
class RetryCallbacks:
    """重试过程回调（均可选）。均为 async。"""

    on_retry_scheduled: Callable[[int, int, int, str], Awaitable[None]] | None = None
    on_retry_attempt_start: Callable[[], Awaitable[None]] | None = None
    on_retry_finished: Callable[[bool, int, str | None], Awaitable[None]] | None = None


class _RetrySleepCancelledError(BaseException):
    """退避 sleep 期间被取消的内部哨兵异常。"""


async def _sleep(ms: int, cancel_event: asyncio.Event | None) -> None:
    """可被 cancel_event 取消的 sleep。"""
    if cancel_event is not None and cancel_event.is_set():
        raise _RetrySleepCancelledError
    try:
        await asyncio.wait_for(asyncio.sleep(ms / 1000), timeout=None)
        if cancel_event is not None and cancel_event.is_set():
            raise _RetrySleepCancelledError
    except _RetrySleepCancelledError:
        raise
    except asyncio.CancelledError as exc:
        raise _RetrySleepCancelledError from exc


async def retry_assistant_call(
    produce: Callable[[], Awaitable[AssistantMessage]],
    policy: RetryPolicy | None,
    cancel_event: asyncio.Event | None = None,
    callbacks: RetryCallbacks | None = None,
) -> AssistantMessage:
    """带重试的 assistant 调用。

    每次调用 ``produce()`` 产出一条 AssistantMessage；若 stop_reason 为 error 且
    可重试，按指数退避等待后重试。aborted 与成功直接返回。
    """
    max_attempts = policy.max_retries if (policy and policy.enabled) else 0

    attempt = 0
    last_retry: tuple[int, str] | None = None
    response: AssistantMessage | None = None

    while True:
        response = await produce()

        # aborted：终结性，永不重试
        if response.stop_reason == "aborted":
            if last_retry and callbacks and callbacks.on_retry_finished:
                await callbacks.on_retry_finished(False, last_retry[0], None)
            return response

        # 成功：非 error 直接返回
        if response.stop_reason != "error":
            if last_retry and callbacks and callbacks.on_retry_finished:
                await callbacks.on_retry_finished(True, last_retry[0], None)
            return response

        # error：预算耗尽或不可重试 → 返回最终错误
        if attempt >= max_attempts or not is_retryable_assistant_error(response):
            if last_retry and callbacks and callbacks.on_retry_finished:
                await callbacks.on_retry_finished(False, last_retry[0], response.error_message)
            return response

        # 进入重试
        attempt += 1
        err_msg = response.error_message or "Unknown error"
        last_retry = (attempt, err_msg)
        delay_ms = (policy.base_delay_ms if policy else 1000) * (2 ** (attempt - 1))

        if callbacks and callbacks.on_retry_scheduled:
            await callbacks.on_retry_scheduled(attempt, max_attempts, delay_ms, err_msg)

        try:
            await _sleep(delay_ms, cancel_event)
        except _RetrySleepCancelledError:
            # 退避期间被取消 → 标准化为 aborted
            if callbacks and callbacks.on_retry_finished:
                await callbacks.on_retry_finished(False, attempt, err_msg)
            response.stop_reason = "aborted"
            response.error_message = None
            return response

        if callbacks and callbacks.on_retry_attempt_start:
            await callbacks.on_retry_attempt_start()


__all__ = [
    "RetryPolicy",
    "RetryCallbacks",
    "is_retryable_assistant_error",
    "retry_assistant_call",
]
