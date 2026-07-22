"""pi-ai: Python port of @earendil-works/pi-ai.

统一 LLM API：多 provider 适配、流式调用、模型注册。

对应上游 ``packages/ai``（TypeScript）。

快速上手：
    from pi_ai import stream, Context, UserMessage, Model

    model = get_model("openai", "gpt-4o")
    ctx = Context(messages=[UserMessage(content="Hello")])
    event_stream = stream(model, ctx)
    async for event in event_stream:
        ...
"""

from __future__ import annotations

__version__ = "0.81.1"
__upstream_ref__ = "earendil-works/pi@v0.81.1"

# ---- 类型 ----
# ---- 事件流 ----
from .event_stream import EventStream

# ---- 事件 ----
from .events import (
    AssistantMessageEvent,
    DoneEvent,
    ErrorEvent,
    StartEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)

# ---- 异常 ----
from .exceptions import (
    LLMAuthenticationError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMStreamError,
    LLMTimeoutError,
    MaxRetriesExceededError,
    PiAIError,
    RetryableError,
)

# ---- 模型注册 ----
from .models import (
    ProviderStreams,
    clear_api_providers,
    clear_models,
    get_api_provider,
    get_model,
    list_models,
    register_api_provider,
    register_builtins,
    register_model,
)

# ---- 重试 ----
from .retry import RetryCallbacks, RetryPolicy, is_retryable_assistant_error, retry_assistant_call

# ---- 流式入口 ----
from .stream import complete, complete_simple, stream, stream_simple
from .types import (
    Api,
    AssistantContentBlock,
    AssistantMessage,
    CacheRetention,
    Context,
    ImageContent,
    KnownApi,
    KnownProvider,
    Message,
    Model,
    ModelCost,
    ModelCostRates,
    ModelCostTier,
    ModelThinkingLevel,
    ProviderId,
    SessionAffinityFormat,
    SimpleStreamOptions,
    StopReason,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ThinkingLevel,
    Tool,
    ToolCall,
    ToolResultContentBlock,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserContentBlock,
    UserMessage,
)

# 注册内置 provider（副作用，幂等）
register_builtins()


__all__ = [
    "__version__",
    "__upstream_ref__",
    # 类型
    "Api",
    "AssistantContentBlock",
    "AssistantMessage",
    "CacheRetention",
    "Context",
    "ImageContent",
    "KnownApi",
    "KnownProvider",
    "Message",
    "Model",
    "ModelCost",
    "ModelCostRates",
    "ModelCostTier",
    "ModelThinkingLevel",
    "ProviderId",
    "SessionAffinityFormat",
    "SimpleStreamOptions",
    "StopReason",
    "StreamOptions",
    "TextContent",
    "ThinkingContent",
    "ThinkingLevel",
    "Tool",
    "ToolCall",
    "ToolResultContentBlock",
    "ToolResultMessage",
    "Usage",
    "UsageCost",
    "UserContentBlock",
    "UserMessage",
    # 事件
    "AssistantMessageEvent",
    "DoneEvent",
    "ErrorEvent",
    "StartEvent",
    "TextDeltaEvent",
    "TextEndEvent",
    "TextStartEvent",
    "ThinkingDeltaEvent",
    "ThinkingEndEvent",
    "ThinkingStartEvent",
    "ToolCallDeltaEvent",
    "ToolCallEndEvent",
    "ToolCallStartEvent",
    # 事件流
    "EventStream",
    # 异常
    "LLMAuthenticationError",
    "LLMConnectionError",
    "LLMError",
    "LLMRateLimitError",
    "LLMResponseError",
    "LLMStreamError",
    "LLMTimeoutError",
    "MaxRetriesExceededError",
    "PiAIError",
    "RetryableError",
    # 模型注册
    "ProviderStreams",
    "clear_api_providers",
    "clear_models",
    "get_api_provider",
    "get_model",
    "list_models",
    "register_api_provider",
    "register_builtins",
    "register_model",
    # 重试
    "RetryCallbacks",
    "RetryPolicy",
    "is_retryable_assistant_error",
    "retry_assistant_call",
    # 流式入口
    "complete",
    "complete_simple",
    "stream",
    "stream_simple",
]
