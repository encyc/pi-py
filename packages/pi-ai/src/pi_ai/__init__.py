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

__version__ = "1.3.0"
__upstream_ref__ = "earendil-works/pi@v1.3.0"

# ---- 类型 ----
# ---- 事件流 ----
# ---- 文本 / transcript / 变换 ----
from .estimate import (
    CHARS_PER_TOKEN,
    ContextUsageEstimate,
    estimate_context_tokens,
    estimate_message_tokens,
    estimate_text_tokens,
)
from .event_stream import (
    AssistantMessageEventStream,
    EventStream,
    create_assistant_message_event_stream,
)

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
from .headers import headers_to_record, provider_headers_to_record

# ---- JSON 解析 ----
from .json_parse import (
    parse_json_with_repair,
    parse_streaming_json,
    parse_tool_call_arguments,
)
from .lazy import lazy_stream
from .model_operations import get_model_type, is_model_type

# ---- 模型注册 ----
from .models import (
    MODELS_OMITTED_MEMBERS,
    DeferredCancelOptions,
    DeferredFetchOptions,
    DeferredHandle,
    Models,
    ModelsDeferredCancelOptions,
    ModelsDeferredFetchOptions,
    ModelsRequestTransforms,
    ModelsSimpleStreamOptions,
    ProviderHeaders,
    ProviderRequestOptions,
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
from .oauth_page import oauth_error_html, oauth_success_html
from .overflow import get_overflow_patterns, is_context_overflow, is_recoverable_length

# ---- 重试 ----
from .retry import (
    DEFAULT_MAX_AGENT_RETRY_DELAY_MS,
    RetryCallbacks,
    RetryPolicy,
    is_retryable_assistant_error,
    retry_assistant_call,
    retry_delay_ms,
)

# ---- 流式入口 ----
from .stream import complete, complete_simple, stream, stream_simple
from .text import content_text, get_system_message_text, render_system_message_update
from .transcript import (
    collapse_system_messages,
    get_current_system_message,
    get_current_system_prompt,
    get_current_tools,
    normalize_context,
    resolve_transcript,
    resolve_transcript_tools,
)
from .transform_messages import (
    drop_cross_model_thinking,
    transform_assistant_content,
)
from .types import (
    AnyModel,
    Api,
    AssistantContentBlock,
    AssistantImages,
    AssistantMessage,
    CacheRetention,
    ClassifierAnswer,
    ClassifierApi,
    ClassifierBoolAnswer,
    ClassifierBoolQuestion,
    ClassifierChoiceAnswer,
    ClassifierChoiceQuestion,
    ClassifierContext,
    ClassifierModel,
    ClassifierQuestion,
    ClassifierResult,
    ClassifierScoreAnswer,
    ClassifierScoreQuestion,
    ClassifierStopReason,
    Context,
    ImageApi,
    ImageContent,
    ImageModel,
    ImagesContext,
    ImagesInputContent,
    ImagesOutputContent,
    ImagesStopReason,
    JsonObject,
    JsonValue,
    KnownApi,
    KnownClassifierApi,
    KnownImageApi,
    KnownProvider,
    Message,
    Model,
    ModelCost,
    ModelCostRates,
    ModelCostTier,
    ModelThinkingLevel,
    ModelType,
    ModelTypeMap,
    ProviderId,
    SessionAffinityFormat,
    SimpleStreamOptions,
    StopReason,
    StreamOptions,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ThinkingLevel,
    ThinkingTokenBudgetField,
    Tool,
    ToolCall,
    ToolChoice,
    ToolReference,
    ToolResultContentBlock,
    ToolResultMessage,
    TranscriptContext,
    Usage,
    UsageCost,
    UserContentBlock,
    UserMessage,
)

# ---- User-Agent ----
from .user_agent import get_pi_user_agent
from .uuid import uuidv7
from .validation import validate_tool_arguments, validate_tool_call

# 注册内置 provider（副作用，幂等）
register_builtins()


__all__ = [
    "__version__",
    "__upstream_ref__",
    # 类型
    "AnyModel",
    "Api",
    "AssistantContentBlock",
    "AssistantImages",
    "AssistantMessage",
    "ClassifierAnswer",
    "ClassifierApi",
    "ClassifierBoolAnswer",
    "ClassifierBoolQuestion",
    "ClassifierChoiceAnswer",
    "ClassifierChoiceQuestion",
    "ClassifierContext",
    "ClassifierModel",
    "ClassifierQuestion",
    "ClassifierResult",
    "ClassifierScoreAnswer",
    "ClassifierScoreQuestion",
    "ClassifierStopReason",
    "CacheRetention",
    "Context",
    "ImageApi",
    "ImageContent",
    "ImageModel",
    "ImagesContext",
    "ImagesInputContent",
    "ImagesOutputContent",
    "ImagesStopReason",
    "JsonObject",
    "JsonValue",
    "KnownApi",
    "KnownClassifierApi",
    "KnownImageApi",
    "KnownProvider",
    "Message",
    "Model",
    "ModelCost",
    "ModelCostRates",
    "ModelCostTier",
    "ModelThinkingLevel",
    "ModelType",
    "ModelTypeMap",
    "ProviderId",
    "SessionAffinityFormat",
    "SimpleStreamOptions",
    "StopReason",
    "StreamOptions",
    "SystemMessage",
    "TextContent",
    "ThinkingContent",
    "ThinkingLevel",
    "ThinkingTokenBudgetField",
    "Tool",
    "ToolCall",
    "ToolChoice",
    "ToolReference",
    "ToolResultContentBlock",
    "ToolResultMessage",
    "TranscriptContext",
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
    "AssistantMessageEventStream",
    "EventStream",
    "create_assistant_message_event_stream",
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
    # JSON / 文本 / transcript
    "CHARS_PER_TOKEN",
    "ContextUsageEstimate",
    "collapse_system_messages",
    "content_text",
    "drop_cross_model_thinking",
    "estimate_context_tokens",
    "estimate_message_tokens",
    "estimate_text_tokens",
    "get_current_system_message",
    "get_current_system_prompt",
    "get_current_tools",
    "get_model_type",
    "get_system_message_text",
    "headers_to_record",
    "lazy_stream",
    "normalize_context",
    "oauth_error_html",
    "oauth_success_html",
    "parse_json_with_repair",
    "parse_streaming_json",
    "parse_tool_call_arguments",
    "provider_headers_to_record",
    "render_system_message_update",
    "resolve_transcript",
    "resolve_transcript_tools",
    "transform_assistant_content",
    "get_overflow_patterns",
    "is_context_overflow",
    "is_recoverable_length",
    "validate_tool_arguments",
    "validate_tool_call",
    "uuidv7",
    # 模型注册
    "MODELS_OMITTED_MEMBERS",
    "DeferredCancelOptions",
    "DeferredFetchOptions",
    "DeferredHandle",
    "Models",
    "ModelsDeferredCancelOptions",
    "ModelsDeferredFetchOptions",
    "ModelsRequestTransforms",
    "ModelsSimpleStreamOptions",
    "ProviderHeaders",
    "ProviderRequestOptions",
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
    "DEFAULT_MAX_AGENT_RETRY_DELAY_MS",
    "RetryCallbacks",
    "RetryPolicy",
    "is_model_type",
    "is_retryable_assistant_error",
    "retry_assistant_call",
    "retry_delay_ms",
    # 流式入口
    "complete",
    "complete_simple",
    "stream",
    "stream_simple",
    # User-Agent
    "get_pi_user_agent",
]
