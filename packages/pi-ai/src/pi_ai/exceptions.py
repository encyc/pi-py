"""异常体系。

对应上游在 provider 实现中分散抛出的错误，这里集中定义层次清晰的异常。
移植自旧版优秀设计：基类带 message + details，每个异常带语义化属性。

provider 实现中，HTTP 错误不直接抛这些异常 —— 而是编码为 error 事件
（``stopReason="error"`` + ``errorMessage``）。这些异常用于同步路径
（如配置错误、校验错误）与上层捕获重试。
"""

from __future__ import annotations

from typing import Any


class PiAIError(Exception):
    """pi-ai 所有异常的基类。"""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} ({self.details})"
        return self.message


# ============================================================
# LLM 调用相关
# ============================================================


class LLMError(PiAIError):
    """LLM 调用错误的基类。"""


class LLMConnectionError(LLMError):
    """无法连接到 provider（网络层）。"""


class LLMRateLimitError(LLMError):
    """被限流（429）。"""

    def __init__(
        self, message: str, provider: str = "", retry_after: float | None = None, **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)
        self.provider = provider
        self.retry_after = retry_after


class LLMAuthenticationError(LLMError):
    """鉴权失败（401/403）。"""

    def __init__(self, message: str, provider: str = "", **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.provider = provider


class LLMResponseError(LLMError):
    """provider 返回了无法处理的响应。"""


class LLMStreamError(LLMError):
    """流式响应中断或格式错误。"""


class LLMTimeoutError(LLMError):
    """请求超时。"""


# ============================================================
# 重试相关
# ============================================================


class RetryableError(PiAIError):
    """可重试的错误（瞬时故障）。"""


class MaxRetriesExceededError(PiAIError):
    """重试次数用尽。"""

    def __init__(self, message: str, attempts: int = 0, last_error: BaseException | None = None):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


__all__ = [
    "PiAIError",
    "LLMError",
    "LLMConnectionError",
    "LLMRateLimitError",
    "LLMAuthenticationError",
    "LLMResponseError",
    "LLMStreamError",
    "LLMTimeoutError",
    "RetryableError",
    "MaxRetriesExceededError",
]
