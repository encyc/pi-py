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
    # Sign in with ChatGPT：订阅的**共享额度**上限，按小时而非秒重置。
    # 上游 v1.3.0 新增（utils/retry.ts:21）。本端未实现该登录方式，
    # 但为保持分类与上游逐字一致而保留（无可重试文本时该模式无副作用）。
    "subscription_sharing_usage_limit_exceeded",
]
_NON_RETRYABLE_RE = re.compile("|".join(_NON_RETRYABLE_PATTERNS), re.IGNORECASE)

#: 可重试：过载/限流/5xx/网络/连接/超时/stream 早断等瞬时错误。
#: 对应上游 ``RETRYABLE_PROVIDER_ERROR_PATTERN``。
#: 注意 ``.?`` 是正则元字符（前一个字符可选），且 "timed? out" 中间有空格。
_RETRYABLE_PATTERNS = [
    # 通用负载/HTTP 状态/服务端瞬时故障
    "overloaded",
    # v1.3.0 新增：网关/服务端繁忙与容量措辞（上游 utils/retry.ts:30-33）。
    # 对应 issue #10543（server_busy 不再终止本轮）与 #10278（model is at capacity）。
    "server_busy",
    "servers are currently busy",
    "currently experiencing high demand",
    "model is at capacity",
    "rate.?limit",
    "too many requests",
    "429",
    "500",
    "502",
    "503",
    "504",
    # v1.3.0 新增：Cloudflare 520（上游 issue #9627）。
    "520",
    "524",
    # v1.3.0 改写：同时匹配 "The service is temporarily unavailable"
    # （上游 issue #10965；旧模式 ``service.?unavailable`` 匹配不到该文本）。
    "service.?(?:is.?)?(?:temporarily.?)?unavailable",
    "server.?error",
    "internal.?error",
    # wrapper/provider 瞬时上游故障
    # v1.3.0 改写：同时匹配 "The upstream provider returned an error"
    # （上游 issue #10842；旧模式匹配不到中间的 "an"）。
    "provider.?returned.?(?:an.?)?error",
    # 上游请求缓冲区超限（provider 转发层重试上游时的瞬时失败）
    "exceeded request buffer limit while retrying upstream",
    # v1.3.0 新增：网关转发上游响应流失败，以 Responses ``response.failed``
    # 事件到达且无错误码（上游 issue #10920）。
    "unable to forward upstream",
    # 网络/代理/fetch 传输故障
    "network.?error",
    "connection.?error",
    "connection.?refused",
    "connection.?lost",
    "other side closed",
    "fetch failed",
    "getaddrinfo",
    "ENOTFOUND",
    "EAI_AGAIN",
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
    # v1.3.0 新增：Node ERR_HTTP2_STREAM_CANCEL——HTTP/2 会话在请求发出前死亡，
    # 例如 Bedrock SDK 5 分钟会话超时之后（上游 issue #10379）。
    "pending stream has been canceled",
    # provider 请求的重试
    "retry delay",
    "you can retry your request",
    "try your request again",
    "please retry your request",
    # gRPC（如 NVIDIA NIM）
    "ResourceExhausted",
    # Sign in with ChatGPT：用量/用户数据暂时不可用。用量失败可能在流中途到达，
    # 消息里没有 HTTP 503（上游 utils/retry.ts:107-109）。同上，仅为分类一致。
    "subscription_sharing_usage_unavailable",
    "subscription_sharing_user_unavailable",
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
    """重试策略。对应上游 ``RetryPolicy``。

    ``max_agent_delay_ms`` 是 v1.3.0 新增：**每次**计算出的延迟都被它封顶，
    默认 60 秒。注意这是**默认行为变更**——即便调用方不设置该字段，退避也不再
    是无上限的指数增长（上游 CHANGELOG 0.86.0 Added, issue #8826）。
    """

    enabled: bool = False
    #: 最大重试次数（0 = 不重试）。首次调用不计为重试。总尝试上限 = 1 + max_retries。
    max_retries: int = 0
    #: 基础延迟（毫秒）。每次重试延迟 = base_delay_ms * 2**(attempt-1)，再被
    #: ``max_agent_delay_ms`` 封顶。
    base_delay_ms: int = 1000
    #: agent 级重试延迟上限（毫秒）。缺省 60 秒。
    max_agent_delay_ms: int | None = None


#: agent 级重试延迟的默认上限（毫秒）。对应上游 ``DEFAULT_MAX_AGENT_RETRY_DELAY_MS``。
DEFAULT_MAX_AGENT_RETRY_DELAY_MS = 60_000


def retry_delay_ms(
    policy: RetryPolicy,
    attempt: int,
) -> int:
    """计算第 ``attempt`` 次重试的退避延迟（毫秒）。

    对应上游 ``retryDelayMs``（utils/retry.ts:133）。上游用
    ``Number.isSafeInteger`` 防御 JS 的浮点溢出；Python 整数无溢出，
    但 ``2 ** (attempt - 1)`` 在 attempt 极大时会产生巨型整数，故仍先与上限
    比较再求幂之外的裁剪（此处直接取 min，Python 可承受）。
    """
    delay: int = policy.base_delay_ms * (2 ** max(0, attempt - 1))
    cap: int = (
        policy.max_agent_delay_ms
        if policy.max_agent_delay_ms is not None
        else DEFAULT_MAX_AGENT_RETRY_DELAY_MS
    )
    return min(delay, cap)


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
        delay_ms = retry_delay_ms(policy or RetryPolicy(), attempt)

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
    "DEFAULT_MAX_AGENT_RETRY_DELAY_MS",
    "RetryPolicy",
    "RetryCallbacks",
    "is_retryable_assistant_error",
    "retry_assistant_call",
    "retry_delay_ms",
]
