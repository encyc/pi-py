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

from typing import Annotated, Any, Literal, TypeAlias, TypedDict

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
#:
#: v1.3.0：上游把 provider id ``azure-openai-responses`` 重命名为 ``azure``
#: （该 provider 现在同时提供 Responses 与 Chat Completions，见上游
#: v1.3.0/src/types.ts:94 与 CHANGELOG 1.0.3 Breaking）。**api id
#: ``azure-openai-responses`` 不变**（上游 :65 仍在 ``KnownApi`` 内），故两个名字
#: 同时存在于本文件的不同 Literal 中。新增 ``typesafe``（classifier-only）与
#: ``meta``（Muse 订阅 OAuth）两个 provider 名，仅类型对齐，本端未注册实现。
KnownProvider = Literal[
    "amazon-bedrock",
    "anthropic",
    "google",
    "google-vertex",
    "openai",
    "azure",
    "azure-openai-responses",
    "typesafe",
    "meta",
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
    "qwen-token-plan-individual",
    "baseten",
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

#: provider 中性的工具选择（简单请求）。对应上游 ``ToolChoice``。
#: 省略时各适配器使用 provider 私有行为。
ToolChoice = Literal["auto", "none"]

#: OpenAI-compatible 服务器上封顶 reasoning token 的顶层请求字段名。
#: 对应上游 ``ThinkingTokenBudgetField``：
#: ``thinking_token_budget``（vLLM）/ ``thinking_budget``（Qwen/DashScope/SGLang）/
#: ``thinking_budget_tokens``（llama.cpp）。
ThinkingTokenBudgetField = Literal[
    "thinking_token_budget", "thinking_budget", "thinking_budget_tokens"
]

#: 缓存保留期。
CacheRetention = Literal["none", "short", "long"]

#: 传输方式。
Transport = Literal["sse", "websocket", "websocket-cached", "auto"]

#: 会话亲和性格式。
SessionAffinityFormat = Literal["openai", "openai-nosession", "openrouter"]

#: 停止原因。对应上游 ``StopReason``。
StopReason = Literal["pending", "stop", "length", "toolUse", "error", "aborted"]


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
    #: provider 私有的不透明签名或序列化的 reasoning 回放数据
    #: （如 OpenAI reasoning item ID / reasoning_details JSON）。
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
    #: OpenAI Responses 的命名空间（动态加载/命名空间工具调用）。仅类型对齐，
    #: 本端未实现 Responses API。
    namespace: str | None = None
    #: 模型给出的完整参数文本**不是合法 JSON 对象**时设置；此时 ``arguments`` 为 ``{}``。
    #: 内容为问题说明 + 模型原文，供工具参数校验直接报回模型。
    #: 对应上游 v1.3.0 ``ToolCall.argumentsError``（types.ts:448，issue #10897）。
    arguments_error: str | None = Field(default=None, alias="argumentsError")


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


class ToolReference(BaseModel):
    """工具引用的最小形态（仅名字）。对应上游 v1.3.0 ``ToolReference``（types.ts:761）。

    用于 :attr:`SystemMessage.tools_removed`：撤销工具只需名字，不需完整定义。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str


class SystemMessage(BaseModel):
    """system 消息（transcript 内的提示词与工具声明载体）。

    对应上游 v1.3.0 ``SystemMessage``（types.ts:546，v0.86.0 引入）。

    **首条** system message 是系统提示词本身，``tools_added`` 声明初始工具集；
    其后的 system message 表示"从这个位置起"的增量变更：

    - ``content`` 追加指令；
    - ``sections`` 按名字替换/删除（值为 ``None`` 即删除）具名段落；
    - ``tools_added`` / ``tools_removed`` 改变可用工具集。

    按顺序回放全部 system message 即得到当前的提示词与工具集。
    接受会话中途 system 消息的 provider 就地在原位发送；其余 provider 先把
    它们折叠回首条 system message（见 :mod:`pi_ai.transcript`）。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    role: Literal["system"] = "system"
    #: 指令文本。首条为基础 prompt；其后为追加指令。
    content: str | list[TextContent] = ""
    #: 具名有序段落，按 ``content`` 之后原样渲染。首条声明，后续按名字替换，
    #: ``None`` 删除。建议每个段落自带分隔（标签/标题），便于模型把更新与原文对应。
    #: 避免使用纯数字型名字（JSON 对象会重排这些键）。
    sections: dict[str, str | None] | None = None
    #: 在此位置起**变为可用**的工具完整定义。
    tools_added: list[Tool] | None = Field(default=None, alias="toolsAdded")
    #: 在此位置起**不再可用**的工具。对应上游 ``toolsRemoved``。
    tools_removed: list[ToolReference] | None = Field(default=None, alias="toolsRemoved")
    timestamp: int = 0


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
    #: 本次响应用到的 provider 原生 effort 级别；遗留/非托管响应为 None。
    #: 对应上游 ``providerThinkingLevel``。
    provider_thinking_level: str | None = Field(default=None, alias="providerThinkingLevel")
    #: agent loop 本次请求的 pi 思考级别。agent loop 之外及遗留响应为 None。
    #: 对应上游 v1.3.0 ``AssistantMessage.thinkingLevel``（types.ts:578）。
    thinking_level: ModelThinkingLevel | None = Field(default=None, alias="thinkingLevel")
    diagnostics: list[dict[str, Any]] | None = None
    usage: Usage = Field(default_factory=Usage)
    stop_reason: StopReason = Field(default="pending", alias="stopReason")
    error_message: str | None = Field(default=None, alias="errorMessage")
    raw_stop_reason: str | None = Field(default=None, alias="rawStopReason")
    #: provider 是否显式结束了本轮（如 Codex end_turn）。仅用于调试，
    #: 不影响 agent 控制流。仅类型对齐，本端未实现 Responses/Codex API。
    end_turn: bool | None = Field(default=None, alias="endTurn")
    timestamp: int = 0
    #: 从 ``timestamp`` 到响应结束的毫秒数（单调时钟）。由
    #: :class:`~pi_ai.event_stream.AssistantMessageEventStream` 在它观察到开始的
    #: 那条响应的最终消息上写入；遗留消息与稍后才取回的 deferred 结果不设置。
    #: 对应上游 v1.3.0 ``AssistantMessage.durationMs``（types.ts:600，issue #10549）。
    duration_ms: int | None = Field(default=None, alias="durationMs")


class ToolResultMessage(BaseModel):
    """工具结果消息。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str = Field(alias="toolCallId")
    tool_name: str = Field(alias="toolName")
    content: list[ToolResultContentBlock] = Field(default_factory=list)
    details: Any = None
    usage: Usage | None = None
    #: 本次结果之后变得可用的工具名。v1.3.0 起上游改由
    #: ``SystemMessage.tools_added`` 表达（``addedToolNames`` 已删除）。
    #: 本端保留该字段仅为对齐 v0.85.1；运行时未被任何 provider 读取。
    added_tool_names: list[str] | None = Field(default=None, alias="addedToolNames")
    is_error: bool = Field(default=False, alias="isError")
    timestamp: int = 0
    #: 工具执行耗时（毫秒，单调时钟）。遗留结果为 None。
    #: 对应上游 v1.3.0 ``ToolResultMessage.durationMs``（types.ts:640）。
    duration_ms: int | None = Field(default=None, alias="durationMs")


#: 消息联合（按 role 判别）。对应上游 ``Message``。
#: v1.3.0：新增 ``SystemMessage`` 成员（上游 types.ts:644）。
Message = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="role"),
]


# ============================================================
# 模型类型族：API 标识 / 图像 / 分类器（v1.3.0）
# ============================================================
#
# 上游 v1.3.0 把「模型」分成三个类型族并给出统一判别：
# ``Model``（chat，``type`` 可省）/ ``ImageModel``（``type: "image"``）/
# ``ClassifierModel``（``type: "classifier"``），由 ``ModelType`` + ``ModelTypeMap``
# + ``AnyModel`` 表达，``utils/model-operations.ts`` 提供 ``getModelType`` /
# ``isModelType`` 判别。
#
# **本批只补「类型层面的公开符号」**：这些类是数据形状（上游的 ``interface``），
# 不是能力实现。本端**未**实现 ``generateImages()`` / ``classify()`` 及其 provider
# 接线（那属于 `PORTING.md` §4 登记的 NEW-CAPABILITY，需用户拍板）。
#
# 上游把 id/name/api/provider/baseUrl/input/inputLimits/cost/headers 抽成
# ``BaseModel``；本端**不导出**该名——它会与 ``pydantic.BaseModel`` 冲突，
# 且下游未请求该符号。此处用私有基类承载同一组字段。

#: 已知的**图像** API 协议名。上游 v1.3.0 ``KnownImageApi``（types.ts:75）。
#: 注意是单数 ``Image``——v0.85.1 的 ``KnownImagesApi`` 复数命名已废弃。
KnownImageApi = Literal["openrouter-images"]

#: 图像 API 名：已知值或任意字符串。
ImageApi = str

#: 已知的**分类器** API 协议名。上游 v1.3.0 ``KnownClassifierApi``（types.ts:79）。
KnownClassifierApi = Literal[
    "typesafe-system-one",
    "cloudflare-workers-ai-system-one",
    "llama-cpp-classify",
    "openai-decisions",
]

#: 分类器 API 名：已知值或任意字符串。
ClassifierApi = str

#: JSON 值。对应上游 ``JsonValue``（types.ts:476）。
#:
#: **本端偏离**：上游是递归类型（数组/对象可嵌套 JSON）。Python 侧无法优雅表达——
#: Pydantic v2 对隐式递归别名（``list["JsonValue"]``）会 ``RecursionError``，
#: 须改用 PEP 695 ``type`` 语句（需 3.12+）或 ``typing_extensions.TypeAliasType``
#: （会让本包新增一个未声明依赖）。两者代价都高于收益，故退化为 ``Any``。
#: 这与既有的同类偏离一致（``ToolCall.arguments`` 亦为 ``dict[str, Any]``，见
#: 「有意偏离上游」表）。仅影响运行期校验精度，不影响类型标注可用性。
JsonValue: TypeAlias = Any

#: JSON 对象。对应上游 ``JsonObject``（types.ts:477）。``ClassifierContext.state`` 用到。
JsonObject: TypeAlias = dict[str, Any]

#: 图像 API 的输入内容块。对应上游 ``ImagesInputContent``（types.ts:646）。
ImagesInputContent = Annotated[TextContent | ImageContent, Field(discriminator="type")]

#: 图像 API 的输出内容块。对应上游 ``ImagesOutputContent``（types.ts:647）。
ImagesOutputContent = Annotated[TextContent | ImageContent, Field(discriminator="type")]

#: 图像生成的终止原因。对应上游 ``ImagesStopReason``（types.ts:653）。
ImagesStopReason = Literal["stop", "error", "aborted"]


class ImagesContext(BaseModel):
    """图像生成请求上下文。对应上游 ``ImagesContext``（types.ts:649-651）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    input: list[ImagesInputContent] = Field(default_factory=list)


class AssistantImages(BaseModel):
    """图像生成结果。对应上游 ``AssistantImages``（types.ts:657-664）。

    v1.3.0 起 ``api``/``provider`` 用单数族名与 ``ProviderId``（上游删除了
    ``ImagesApi``/``KnownImagesProvider``/``ImagesProviderId`` 的复数命名）。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api: ImageApi = ""
    provider: ProviderId = ""
    model: str = ""
    output: list[ImagesOutputContent] = Field(default_factory=list)
    response_id: str | None = Field(default=None, alias="responseId")
    usage: Usage | None = None
    stop_reason: ImagesStopReason = Field(default="stop", alias="stopReason")
    error_message: str | None = Field(default=None, alias="errorMessage")
    #: Unix 毫秒时间戳。
    timestamp: int = 0


# ---- 分类器问题（入参） ----

#: 分类器问题类型。对应上游 ``ClassifierQuestion``（types.ts:684）。
ClassifierStopReason = Literal["stop", "error", "aborted"]


class ClassifierChoiceQuestion(BaseModel):
    """多选问题：从 ``criteria`` 的键里选一个。上游 ``ClassifierChoiceQuestion``。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["choice"] = "choice"
    instructions: str
    criteria: dict[str, str] = Field(default_factory=dict)


class ClassifierScoreQuestion(BaseModel):
    """打分问题：对 ``criteria`` 各项打分。上游 ``ClassifierScoreQuestion``。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["score"] = "score"
    instructions: str
    criteria: list[str] = Field(default_factory=list)


class ClassifierBoolQuestion(BaseModel):
    """布尔问题。上游 ``ClassifierBoolQuestion``。

    注意 ``criteria`` 的键是 ``"true"``/``"false"`` 两个字符串——JSON 对象键必须
    是字符串，故不能直接建模成 ``bool`` 键。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["bool"] = "bool"
    instructions: str
    criteria: dict[str, str] = Field(default_factory=dict)


#: 分类器问题联合（按 ``type`` 判别）。对应上游 ``ClassifierQuestion``。
ClassifierQuestion = Annotated[
    ClassifierChoiceQuestion | ClassifierScoreQuestion | ClassifierBoolQuestion,
    Field(discriminator="type"),
]


class ClassifierContext(BaseModel):
    """分类请求上下文。对应上游 ``ClassifierContext``（types.ts:686-698）。

    ``images`` 只应由 ``input`` 含 ``"image"`` 的模型接收；其他模型返回 error 结果
    （该判定属能力实现，本端未做，见 ``PORTABLE.md`` §4）。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    state: JsonObject = Field(default_factory=dict)
    images: list[ImageContent] | None = None
    questions: dict[str, ClassifierQuestion] = Field(default_factory=dict)


# ---- 分类器答案（出参） ----


class ClassifierChoiceAnswer(BaseModel):
    """多选答案。对应上游 ``ClassifierChoiceAnswer``（types.ts:700-705）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["choice"] = "choice"
    choice: str
    probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float = 0.0


class ClassifierScoreAnswer(BaseModel):
    """打分答案。对应上游 ``ClassifierScoreAnswer``（types.ts:707-711）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["score"] = "score"
    score: float = 0.0
    confidence: float = 0.0


class ClassifierBoolAnswer(BaseModel):
    """布尔答案。对应上游 ``ClassifierBoolAnswer``（types.ts:713-717）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["bool"] = "bool"
    probability: float = 0.0


#: 分类器答案联合（按 ``type`` 判别）。对应上游 ``ClassifierAnswer``。
ClassifierAnswer = Annotated[
    ClassifierChoiceAnswer | ClassifierScoreAnswer | ClassifierBoolAnswer,
    Field(discriminator="type"),
]


class ClassifierResult(BaseModel):
    """分类结果。对应上游 ``ClassifierResult``（types.ts:722-732）。

    ``usage`` 是分类服务报告的 token 用量（按其目录价计价），服务不报告时为 None。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api: ClassifierApi = ""
    provider: ProviderId = ""
    model: str = ""
    answers: dict[str, ClassifierAnswer] = Field(default_factory=dict)
    usage: Usage | None = None
    stop_reason: ClassifierStopReason = Field(default="stop", alias="stopReason")
    error_message: str | None = Field(default=None, alias="errorMessage")
    #: Unix 毫秒时间戳。
    timestamp: int = 0


# ============================================================
# Tool / Context
# ============================================================


class Tool(BaseModel):
    """工具定义。``parameters`` 是 JSON Schema dict（对应上游 typebox TSchema）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    constrained_sampling: dict[str, Any] | Literal[False] | None = Field(
        default=None, alias="constrainedSampling"
    )

    def to_json_schema(self) -> dict[str, Any]:
        """返回给 LLM 的 JSON Schema。"""
        schema = dict(self.parameters)
        schema.pop("title", None)
        return schema


class Context(BaseModel):
    """**公开**调用入参。``system_prompt`` / ``tools`` 是首条 system message 的简写。

    对应上游 v1.3.0 ``Context``（types.ts:771）。``normalize_context()`` 会把这两个
    字段折进 transcript 的首条 :class:`SystemMessage`，之后才交给 provider。
    调用方（含 pi-agent-core）继续用本类型即可，无需感知 transcript。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    system_prompt: str | None = Field(default=None, alias="systemPrompt")
    messages: list[Message] = Field(default_factory=list)
    tools: list[Tool] | None = None


class TranscriptContext:
    """**归一化后**的请求上下文，传给 provider 与 API 实现。

    对应上游 v1.3.0 ``TranscriptContext``（types.ts:785）。提示词与工具声明由
    transcript 内的 system message 承载，不再挂在 context 上。

    上游用 unique symbol brand 让裸 ``Context`` 无法意外流入 provider 代码；
    Python 无等价机制，改为**只有** :func:`pi_ai.transcript.normalize_context`
    会构造本类（构造器接受 messages 列表，不做校验），以此表达同一约定。
    """

    __slots__ = ("messages",)

    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages

    def __repr__(self) -> str:
        return f"TranscriptContext(messages={len(self.messages)} items)"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TranscriptContext):
            return NotImplemented
        return self.messages == other.messages

    def __hash__(self) -> int:  # pragma: no cover - 可变容器，仅为显式声明不可哈希
        raise TypeError("TranscriptContext 不可哈希")


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
    #: 可选：chat 是默认模型类型，故不带 ``type`` 的模型即 chat 模型。
    #: 混合模型列表请用 :func:`pi_ai.model_operations.is_model_type` 收窄，
    #: 不要直接比较 ``type``。对应上游 ``Model.type``（types.ts:846）。
    type: Literal["chat"] | None = None
    reasoning: bool = False
    thinking_level_map: dict[str, str | None] | None = Field(default=None, alias="thinkingLevelMap")
    input: list[Literal["text", "image"]] = Field(default_factory=list)
    cost: ModelCost = Field(default_factory=ModelCost)
    context_window: int = Field(default=0, alias="contextWindow")
    max_tokens: int = Field(default=0, alias="maxTokens")
    #: 模型级默认采样参数。对应上游 ``Model.samplingParams``。
    #: 由 OpenAI-compatible 适配器合并进请求体；请求级 ``StreamOptions.sampling_params``
    #: 按 key 覆盖模型级。其他 API 忽略。
    sampling_params: dict[str, Any] | None = Field(default=None, alias="samplingParams")
    #: 按**生效的 pi 思考级别**选取的采样参数覆盖。对应上游 v1.3.0
    #: ``Model.samplingParamsByThinkingLevel``（types.ts:859）。
    #: 合并顺序：``sampling_params`` → 本级 → 请求级（后者覆盖前者）。
    sampling_params_by_thinking_level: dict[str, dict[str, Any]] | None = Field(
        default=None, alias="samplingParamsByThinkingLevel"
    )
    headers: dict[str, str] | None = None
    compat: dict[str, Any] | None = None


class _ModelShapeBase(BaseModel):
    """三种模型类型共有的字段。**私有**，不导出。

    对应上游 ``BaseModel``（types.ts:825-836）。之所以不导出该名：它会与
    ``pydantic.BaseModel`` 冲突，且下游未请求该符号；本端只在内部复用以避免
    字段重复。若日后需要公开，请另起名（如 ``ModelBase``）并同步下游。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    name: str
    api: Api
    provider: ProviderId
    base_url: str = Field(alias="baseUrl")
    input: list[Literal["text", "image"]] = Field(default_factory=list)
    cost: ModelCost = Field(default_factory=ModelCost)
    headers: dict[str, str] | None = None


def _default_image_output() -> list[Literal["text", "image"]]:
    """``ImageModel.output`` 的默认值：只输出图像。

    用具名函数（而非 ``lambda``）是为了让 mypy 看到精确的
    ``list[Literal["text", "image"]]`` 返回类型——``lambda: ["image"]`` 会被推断为
    ``Callable[[], list[str]]``，与字段类型不兼容（mypy ``arg-type``）。
    """
    return ["image"]


class ImageModel(_ModelShapeBase):
    """图像生成模型。对应上游 ``ImageModel``（types.ts:875-879）。

    仅可用于图像生成操作；``output`` **始终包含** ``"image"``，含 ``"text"``
    表示该模型还能返回文本块。

    **本端只提供形状**：``generateImages()`` 与其 provider 接线未实现
    （见 ``PORTING.md`` §4 的 NEW-CAPABILITY 登记）。
    """

    type: Literal["image"] = "image"
    output: list[Literal["text", "image"]] = Field(default_factory=_default_image_output)


class ClassifierModel(_ModelShapeBase):
    """结构化分类模型。对应上游 ``ClassifierModel``（types.ts:882-885）。

    仅可用于 ``classify()`` 操作。

    **本端只提供形状**：``classify()`` 与四个分类后端（typesafe-system-one /
    cloudflare-workers-ai-system-one / llama-cpp-classify / openai-decisions）
    均未实现。
    """

    type: Literal["classifier"] = "classifier"
    context_window: int = Field(default=0, alias="contextWindow")


class ModelTypeMap(TypedDict):
    """每种模型类型对应的模型形状。对应上游 ``ModelTypeMap``（types.ts:888-892）。

    .. note::
       Python 的 ``TypedDict`` **不支持**用 ``TypeVar`` 下标（``ModelTypeMap[TType]``
       会报 ``expects no type arguments``）。用字面量键下标是支持的
       （``ModelTypeMap["chat"]``，与上游写法一一对应）。下游若要写
       ``getModelsOfType<TType extends ModelType>(type: TType): ModelTypeMap[TType][]``
       这类泛型方法，Python 侧请改用 ``@overload`` 或
       :func:`pi_ai.model_operations.is_model_type` 收窄——见 ``PORTING.md``。
    """

    chat: Model
    image: ImageModel
    classifier: ClassifierModel


#: 模型类型判别值。对应上游 ``ModelType``（types.ts:895）。
ModelType = Literal["chat", "image", "classifier"]

#: provider 能列出的任意模型。对应上游 ``AnyModel``（types.ts:898）。
#: 用 :func:`pi_ai.model_operations.is_model_type` 收窄。
AnyModel: TypeAlias = Model | ImageModel | ClassifierModel


# ============================================================
# 流式选项
# ============================================================


class StreamOptions(BaseModel):
    """流式调用选项。函数类型的字段（onPayload/onResponse/signal）不放进模型，
    由 provider 实现按需从 options 取用。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True, arbitrary_types_allowed=True)

    temperature: float | None = None
    max_tokens: int | None = Field(default=None, alias="maxTokens")
    #: 请求级采样参数。对应上游 ``StreamOptions.samplingParams``。
    #: 由 OpenAI-compatible 适配器在具名字段之后合并进请求体（因此自定义键覆盖
    #: 具名字段），合并时按 key 覆盖 ``Model.sampling_params``。例如可传
    #: ``top_p``/``top_k``/``min_p``/``repetition_penalty`` 给 llama.cpp/vLLM/SGLang
    #: 等服务器。其他 API 忽略。
    sampling_params: dict[str, Any] | None = Field(default=None, alias="samplingParams")
    api_key: str | None = Field(default=None, alias="apiKey")
    http_client: Any = Field(default=None, alias="httpClient", exclude=True)
    transport: Transport | None = None
    cache_retention: CacheRetention | None = Field(default=None, alias="cacheRetention")
    session_id: str | None = Field(default=None, alias="sessionId")
    headers: dict[str, str | None] | None = None
    timeout_ms: int | None = Field(default=None, alias="timeoutMs")
    max_retries: int | None = Field(default=None, alias="maxRetries")
    max_retry_delay_ms: int | None = Field(default=None, alias="maxRetryDelayMs")
    cancel_event: Any = Field(default=None, alias="cancelEvent", exclude=True)
    #: 可选：在 Pi 归一化**之前**观察每个已解析的 provider 流事件。事件数据由
    #: 适配器拥有，必须视为只读。适配器支持是显式的；不支持的适配器不调用它。
    #: 对应上游 v1.3.0 ``StreamOptions.onProviderStreamEvent``（types.ts:223）。
    #: 签名：``(data, model) -> None | Awaitable[None]``。
    on_provider_stream_event: Any = Field(default=None, alias="onProviderStreamEvent", exclude=True)
    metadata: dict[str, Any] | None = None
    env: dict[str, str] | None = None


class SimpleStreamOptions(StreamOptions):
    """带思考级别的简化流式选项。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True, arbitrary_types_allowed=True)

    reasoning: ThinkingLevel | None = None
    thinking_budgets: dict[str, int] | None = Field(default=None, alias="thinkingBudgets")
    #: provider 中性的工具选择（"auto"/"none"）。省略时适配器使用
    #: provider 私有行为。对应上游 ``SimpleStreamOptions.toolChoice``。
    tool_choice: ToolChoice | None = Field(default=None, alias="toolChoice")


__all__ = [
    # 字面量
    "Api",
    "KnownApi",
    "KnownProvider",
    "ProviderId",
    "ThinkingLevel",
    "ModelThinkingLevel",
    "ToolChoice",
    "ThinkingTokenBudgetField",
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
    "ToolReference",
    "SystemMessage",
    "UserMessage",
    "AssistantMessage",
    "ToolResultMessage",
    "Message",
    # tool / context
    "Tool",
    "Context",
    "TranscriptContext",
    # model
    "ModelCostRates",
    "ModelCostTier",
    "ModelCost",
    "Model",
    # 选项
    "StreamOptions",
    "SimpleStreamOptions",
]
