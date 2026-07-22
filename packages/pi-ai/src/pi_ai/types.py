"""pi-ai 核心类型系统。

对应上游 ``packages/ai/src/types.ts``。用 Pydantic v2 BaseModel 替代上游的
typebox/interface —— 既做运行时校验又做序列化。

设计要点：
- 内容块用 ``type`` Literal 字段做 discriminator（与上游一致）。
- 消息用 ``role`` Literal 字段做 discriminator。
- BaseModel 默认允许属性原地修改（streaming 累加需要，见 provider 实现），
  校验仅在构造时发生。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ============================================================
# 基础标量/字面量类型
# ============================================================

#: 已知的 LLM API 协议名。对应上游 ``KnownApi``。
KnownApi = Literal[
    "openai-completions",
    "mistral-conversations",
    "openai-responses",
    "azure-openai-responses",
    "openai-codex-responses",
    "anthropic-messages",
    "bedrock-converse-stream",
    "google-generative-ai",
    "google-vertex",
    "pi-messages",
]

#: API 名：已知值或任意字符串。上游用 ``(string & {})`` 技巧保持自动补全，
#: Python 无法等价表达，退化为 ``str``。
Api = str

#: 已知的 provider 名（部分；上游 KnownProvider 是长列表）。完整复刻时按需补齐。
KnownProvider = Literal[
    "amazon-bedrock",
    "anthropic",
    "google",
    "google-vertex",
    "openai",
    "azure-openai-responses",
    "openai-codex",
    "deepseek",
    "github-copilot",
    "xai",
    "groq",
    "cerebras",
    "openrouter",
    "vercel-ai-gateway",
    "zai",
    "zai-coding-cn",
    "mistral",
    "moonshotai",
    "moonshotai-cn",
    "together",
    "fireworks",
    "huggingface",
    "nvidia",
    "minimax",
    "minimax-cn",
    "qwen-token-plan",
    "qwen-token-plan-cn",
    "xiaomi",
    "kimi-coding",
    "opencode",
    "opencode-go",
    "cloudflare-workers-ai",
    "cloudflare-ai-gateway",
    "radius",
    "ant-ling",
]

ProviderId = str

#: 思考级别（不含 off）。对应上游 ``ThinkingLevel``。
ThinkingLevel = Literal["minimal", "low", "medium", "high", "xhigh", "max"]

#: 思考级别（含 off）。对应上游 ``ModelThinkingLevel``。
ModelThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]

#: 缓存保留期。
CacheRetention = Literal["none", "short", "long"]

#: 传输方式。
Transport = Literal["sse", "websocket", "websocket-cached", "auto"]

#: 会话亲和性格式。
SessionAffinityFormat = Literal["openai", "openai-nosession", "openrouter"]

#: 停止原因。对应上游 ``StopReason``。
StopReason = Literal["stop", "length", "toolUse", "error", "aborted"]


# ============================================================
# 内容块（discriminated union on ``type``）
# ============================================================


class TextContent(BaseModel):
    """文本内容块。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["text"] = "text"
    text: str
    #: 遗留签名或 TextSignatureV1 JSON。
    text_signature: str | None = Field(default=None, alias="textSignature")


class ThinkingContent(BaseModel):
    """思考（reasoning）内容块。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["thinking"] = "thinking"
    thinking: str
    #: provider 私有签名（如 OpenAI reasoning item ID）。
    thinking_signature: str | None = Field(default=None, alias="thinkingSignature")
    #: 为 True 时真实载荷在 thinking_signature 里。
    redacted: bool = False


class ImageContent(BaseModel):
    """图像内容块。``data`` 为 base64 编码。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["image"] = "image"
    data: str
    mime_type: str = Field(alias="mimeType")


class ToolCall(BaseModel):
    """工具调用块。注意 discriminator 是 ``"toolCall"``（camelCase，与上游一致）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: Google 特有的思考签名。
    thought_signature: str | None = Field(default=None, alias="thoughtSignature")


#: 助手消息允许的内容块联合（无图像）。
AssistantContentBlock = Annotated[
    TextContent | ThinkingContent | ToolCall,
    Field(discriminator="type"),
]

#: 用户消息允许的内容块联合（无思考、无工具调用）。
UserContentBlock = Annotated[
    TextContent | ImageContent,
    Field(discriminator="type"),
]

#: 工具结果消息允许的内容块联合。
ToolResultContentBlock = Annotated[
    TextContent | ImageContent,
    Field(discriminator="type"),
]


# ============================================================
# Usage / 计费
# ============================================================


class UsageCost(BaseModel):
    """单次调用的费用拆解（美元）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    input: float = 0.0
    output: float = 0.0
    cache_read: float = Field(default=0.0, alias="cacheRead")
    cache_write: float = Field(default=0.0, alias="cacheWrite")
    total: float = 0.0


class Usage(BaseModel):
    """token 用量与费用。对应上游 ``Usage``。

    不变量：``input + cache_read + cache_write + output == total_tokens``。
    ``reasoning`` 是 ``output`` 的子集（非累加）。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    input: int = 0
    output: int = 0
    cache_read: int = Field(default=0, alias="cacheRead")
    cache_write: int = Field(default=0, alias="cacheWrite")
    #: 仅 Anthropic 的 1h 缓存写入子集。
    cache_write_1h: int | None = Field(default=None, alias="cacheWrite1h")
    #: output 的子集；provider 不报告时为 None。
    reasoning: int | None = None
    total_tokens: int = Field(default=0, alias="totalTokens")
    cost: UsageCost = Field(default_factory=UsageCost)


# ============================================================
# 消息（discriminated union on ``role``）
# ============================================================


class UserMessage(BaseModel):
    """用户消息。``content`` 可为纯字符串或内容块数组。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    role: Literal["user"] = "user"
    content: str | list[UserContentBlock]
    #: Unix 毫秒时间戳。
    timestamp: int = 0


class AssistantMessage(BaseModel):
    """助手消息。``content`` 始终为数组，含工具调用。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    role: Literal["assistant"] = "assistant"
    content: list[AssistantContentBlock] = Field(default_factory=list)
    api: Api = ""
    provider: ProviderId = ""
    model: str = ""
    response_model: str | None = Field(default=None, alias="responseModel")
    response_id: str | None = Field(default=None, alias="responseId")
    diagnostics: list[dict[str, Any]] | None = None
    usage: Usage = Field(default_factory=Usage)
    stop_reason: StopReason = Field(default="stop", alias="stopReason")
    error_message: str | None = Field(default=None, alias="errorMessage")
    timestamp: int = 0


class ToolResultMessage(BaseModel):
    """工具结果消息。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str = Field(alias="toolCallId")
    tool_name: str = Field(alias="toolName")
    content: list[ToolResultContentBlock] = Field(default_factory=list)
    details: Any = None
    usage: Usage | None = None
    added_tool_names: list[str] | None = Field(default=None, alias="addedToolNames")
    is_error: bool = Field(default=False, alias="isError")
    timestamp: int = 0


#: 消息联合（按 role 判别）。对应上游 ``Message``。
Message = Annotated[
    UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="role"),
]


# ============================================================
# Tool / Context
# ============================================================


class Tool(BaseModel):
    """工具定义。``parameters`` 是 JSON Schema dict（对应上游 typebox TSchema）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)

    def to_json_schema(self) -> dict[str, Any]:
        """返回给 LLM 的 JSON Schema。"""
        schema = dict(self.parameters)
        schema.pop("title", None)
        return schema


class Context(BaseModel):
    """LLM 调用上下文。system prompt 是普通字符串字段（无 SystemMessage）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    system_prompt: str | None = Field(default=None, alias="systemPrompt")
    messages: list[Message] = Field(default_factory=list)
    tools: list[Tool] | None = None


# ============================================================
# Model
# ============================================================


class ModelCostRates(BaseModel):
    """每百万 token 的费率（美元）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    input: float = 0.0
    output: float = 0.0
    cache_read: float = Field(default=0.0, alias="cacheRead")
    cache_write: float = Field(default=0.0, alias="cacheWrite")


class ModelCostTier(ModelCostRates):
    """阶梯费率：超过 ``input_tokens_above`` 后适用。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    input_tokens_above: int = Field(alias="inputTokensAbove")


class ModelCost(ModelCostRates):
    """模型费率（可含阶梯）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tiers: list[ModelCostTier] | None = None


class Model(BaseModel):
    """LLM 模型描述。对应上游 ``Model<TApi>``（泛型在 Python 退化为 ``api: str``）。

    ``compat`` 按 ``api`` 值条件存在；此处用宽松 dict 承载，具体校验在 provider 侧。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    name: str
    api: Api
    provider: ProviderId
    base_url: str = Field(alias="baseUrl")
    reasoning: bool = False
    thinking_level_map: dict[str, str | None] | None = Field(default=None, alias="thinkingLevelMap")
    input: list[Literal["text", "image"]] = Field(default_factory=list)
    cost: ModelCost = Field(default_factory=ModelCost)
    context_window: int = Field(default=0, alias="contextWindow")
    max_tokens: int = Field(default=0, alias="maxTokens")
    headers: dict[str, str] | None = None
    compat: dict[str, Any] | None = None


# ============================================================
# 流式选项
# ============================================================


class StreamOptions(BaseModel):
    """流式调用选项。函数类型的字段（onPayload/onResponse/signal）不放进模型，
    由 provider 实现按需从 options 取用。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True, arbitrary_types_allowed=True)

    temperature: float | None = None
    max_tokens: int | None = Field(default=None, alias="maxTokens")
    api_key: str | None = Field(default=None, alias="apiKey")
    transport: Transport | None = None
    cache_retention: CacheRetention | None = Field(default=None, alias="cacheRetention")
    session_id: str | None = Field(default=None, alias="sessionId")
    headers: dict[str, str | None] | None = None
    timeout_ms: int | None = Field(default=None, alias="timeoutMs")
    max_retries: int | None = Field(default=None, alias="maxRetries")
    max_retry_delay_ms: int | None = Field(default=None, alias="maxRetryDelayMs")
    metadata: dict[str, Any] | None = None
    env: dict[str, str] | None = None


class SimpleStreamOptions(StreamOptions):
    """带思考级别的简化流式选项。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True, arbitrary_types_allowed=True)

    reasoning: ThinkingLevel | None = None
    thinking_budgets: dict[str, int] | None = Field(default=None, alias="thinkingBudgets")


__all__ = [
    # 字面量
    "Api",
    "KnownApi",
    "KnownProvider",
    "ProviderId",
    "ThinkingLevel",
    "ModelThinkingLevel",
    "CacheRetention",
    "Transport",
    "SessionAffinityFormat",
    "StopReason",
    # 内容块
    "TextContent",
    "ThinkingContent",
    "ImageContent",
    "ToolCall",
    "AssistantContentBlock",
    "UserContentBlock",
    "ToolResultContentBlock",
    # usage
    "UsageCost",
    "Usage",
    # 消息
    "UserMessage",
    "AssistantMessage",
    "ToolResultMessage",
    "Message",
    # tool / context
    "Tool",
    "Context",
    # model
    "ModelCostRates",
    "ModelCostTier",
    "ModelCost",
    "Model",
    # 选项
    "StreamOptions",
    "SimpleStreamOptions",
]
