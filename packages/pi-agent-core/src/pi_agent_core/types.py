"""pi-agent-core 核心类型。

对应上游 ``packages/agent/src/types.ts``。

关键契约：
- ``AgentTool.execute`` 是用户实现工具的入口。Python 版用 asyncio.Event
  替代 TS 的 AbortSignal 做取消。
- ``AgentEvent`` 是 agent 层事件（不同于 pi-ai 的流式事件），覆盖 agent/turn/
  message/tool 四层生命周期。
- 错误编码为消息（stop_reason="error"/"aborted"），不抛异常。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pi_ai import (
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    ImageContent,
    Message,
    Model,
    SimpleStreamOptions,
    TextContent,
    ThinkingLevel,
    ToolCall,
    ToolResultMessage,
)

# ============================================================
# 基础别名
# ============================================================

#: 工具执行模式。
ToolExecutionMode = Literal["sequential", "parallel"]

#: 消息队列模式。
QueueMode = Literal["all", "one-at-a-time"]

#: agent 循环中的消息（复用 pi-ai 的 Message 联合）。
AgentMessage = Message

#: 工具调用块（从 AssistantMessage.content 提取）。
AgentToolCall = ToolCall


# ============================================================
# AgentToolResult / AgentTool
# ============================================================


@dataclass
class AgentToolResult:
    """工具执行结果（对应上游 ``AgentToolResult``）。

    - ``content``：返回给模型的内容。
    - ``details``：给日志/UI 的结构化详情（不发给模型）。
    - ``structured_content``：与工具 ``output_schema`` 匹配的机器可读结果，给**程序调用方**
      （不发给模型；对模型可见的仍是 ``content``）。声明了 ``output_schema`` 的工具应当始终设置它。
    - ``is_error``：不抛异常地报告失败。模型把 ``content`` 当作错误结果看待（与抛异常一致），
      但 ``details`` / ``structured_content`` 仍保留给 UI 与程序调用方。
    - ``usage``：本次工具执行自身的用量（可选），不计入主 LLM 上下文计费。
    - ``terminate``：提示本 batch 之后应停止（仅当批次里所有 result 都 terminate=True 才生效）。
    """

    content: list[TextContent | ImageContent] = field(default_factory=list)
    details: Any = None
    structured_content: Any = None
    is_error: bool = False
    usage: Any = None
    added_tool_names: list[str] | None = None
    terminate: bool = False


#: 工具执行时的进度回调（流式更新）。
AgentToolUpdateCallback = Callable[[AgentToolResult], None]


class AgentTool(Protocol):
    """工具契约。继承 pi-ai 的 Tool（name/description/parameters），增加执行逻辑。

    用户实现工具时遵守 ``execute`` 签名：
        async def execute(
            tool_call_id: str,
            params: dict,           # 已校验
            cancel_event: asyncio.Event | None,
            on_update: Callable | None,
        ) -> AgentToolResult

    失败语义：抛异常 = 失败（被循环捕获转成 error result）；也可以在返回值里设
    ``is_error=True``，这样 ``details`` / ``structured_content`` 仍能传给程序调用方。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    label: str
    execution_mode: ToolExecutionMode | None

    @property
    def output_schema(self) -> dict[str, Any] | None:
        """期望出现在 ``AgentToolResult.structured_content`` 里的 JSON Schema。

        对应上游 ``outputSchema``（可选）。声明为只读 property 是为了让「用具体 dict 类型
        标注该属性的实现」也能满足本 Protocol（可变属性的不变性会让 ``dict`` 与
        ``dict | None`` 互不兼容）。
        """
        ...

    def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        cancel_event: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> Awaitable[AgentToolResult]: ...


# ============================================================
# AgentContext / AgentState
# ============================================================


@dataclass
class AgentContext:
    """agent 调用上下文快照。"""

    system_prompt: str
    messages: list[AgentMessage]
    tools: list[AgentTool] | None = None


# 占位默认 Model（实际使用时由 AgentOptions.initial_state 覆盖）
_DEFAULT_MODEL = Model(
    id="unknown",
    name="unknown",
    api="unknown",
    provider="unknown",
    base_url="",
    reasoning=False,
    input=[],
    context_window=0,
    max_tokens=0,
)


@dataclass
class AgentState:
    """Agent 的可变状态（有状态 Agent 维护）。

    对应上游 ``AgentState``。响应式字段（is_streaming 等）每次更新都新建，
    便于上层观察变化。

    ``system_prompt`` 是**只读**属性（对齐上游 v1.3.0 的 ``readonly systemPrompt``）：
    上游把它交给 transcript 拥有（改提示词要 append system message），本端当前仍由
    ``AgentContext.system_prompt`` 承载，因此只在构造 / ``initial_state`` 时赋值一次，
    之后赋值会抛 ``AttributeError``。
    """

    def __init__(
        self,
        system_prompt: str = "",
        model: Model | None = None,
        thinking_level: ThinkingLevel | None = None,
        tools: list[AgentTool] | None = None,
        messages: list[AgentMessage] | None = None,
        is_streaming: bool = False,
        streaming_message: AgentMessage | None = None,
        pending_tool_calls: set[str] | None = None,
        error_message: str | None = None,
    ) -> None:
        self._system_prompt = system_prompt
        self.model: Model = model if model is not None else _DEFAULT_MODEL
        self.thinking_level: ThinkingLevel | None = thinking_level
        self.tools: list[AgentTool] = list(tools) if tools else []
        self.messages: list[AgentMessage] = list(messages) if messages else []
        self.is_streaming = is_streaming
        self.streaming_message: AgentMessage | None = streaming_message
        self.pending_tool_calls: set[str] = set(pending_tool_calls or ())
        self.error_message: str | None = error_message

    @property
    def system_prompt(self) -> str:
        """系统提示词（只读；见类 docstring）。"""
        return self._system_prompt

    def _init_system_prompt(self, value: str) -> None:
        """仅限构造期写入（``AgentOptions.initial_state`` 走这里）。"""
        self._system_prompt = value


# ============================================================
# 回合钩子（对应上游 AgentTurnContext / FinishTurn / PrepareRequest）
# ============================================================


@dataclass
class AgentTurnContext:
    """传给「回合完成」回调的上下文（对应上游 ``AgentTurnContext``）。

    同时支持属性访问（``turn.message``）与历史沿用的下标访问（``turn["message"]``），
    后者保留是为了不破坏既有 ``prepare_next_turn`` 实现。
    """

    #: 完成本回合的 assistant 消息。
    message: AssistantMessage | None = None
    #: 本回合产出的工具结果消息。
    tool_results: list[ToolResultMessage] = field(default_factory=list)
    #: 本回合的 assistant 消息与工具结果都已追加后的当前 agent 上下文。
    context: AgentContext | None = None
    #: 若此刻退出，本次 loop 调用将返回的消息（prompt 启动含初始 prompt；续跑不含既有上下文）。
    new_messages: list[AgentMessage] = field(default_factory=list)

    _ALIASES = {"toolResults": "tool_results", "newMessages": "new_messages"}

    def __getitem__(self, key: str) -> Any:
        return getattr(self, self._ALIASES.get(key, key))


#: 回合结束决策。``"end"`` = 本回合后结束本次 run；``"continue"`` = 保证再发起一次请求；
#: ``None`` = 保持默认调度。也接受上游的对象形式 ``{"action": "end" | "continue"}``。
AgentTurnDecision = Literal["continue", "end"]

#: 回合完成回调（对应上游 ``FinishTurn``）。在 ``turn_end`` **之前**运行，决策在其**之后**生效；
#: error / aborted 响应也会调用它，但其决策被忽略（那两类是硬退出）。
FinishTurn = Callable[..., Any]

#: 请求时使用的思考级别。pi-ai 的 ``ThinkingLevel`` 不含 ``"off"``，
#: 而上游 ``PrepareRequestContext.thinkingLevel`` 允许 ``"off"``（表示清除 reasoning），
#: 故本包单独定义。
RequestThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


#: 请求前回调的入参（对应上游 ``PrepareRequestContext``）。
@dataclass
class PrepareRequestContext:
    """每次 provider 请求前传给 ``prepare_request`` 的运行时状态。"""

    context: AgentContext
    model: Model
    thinking_level: RequestThinkingLevel


#: ``prepare_request`` 的返回值（对应上游 ``AgentRequestUpdate``）：替换本次请求的运行时状态。
@dataclass
class AgentRequestUpdate:
    """替换本次请求使用的运行时状态。未设置的字段保持原值。

    也接受等价 dict：``{"context": ..., "model": ..., "thinkingLevel"/"thinking_level": ...}``。
    """

    context: AgentContext | None = None
    model: Model | None = None
    thinking_level: RequestThinkingLevel | None = None


#: 请求前回调（对应上游 ``PrepareRequest``）。每次会话型 provider 请求前运行，**含第一次**；
#: 此时待处理消息已经追加并发出。
PrepareRequest = Callable[..., Any]


# ============================================================
# 流函数与配置
# ============================================================

#: 流函数抽象（默认 pi-ai 的 stream_simple）。
StreamFn = Callable[
    [Model, Context, SimpleStreamOptions | None],
    Any,  # EventStream[AssistantMessageEvent, AssistantMessage]
]


@dataclass
class AgentLoopConfig:
    """agent 循环配置。

    不继承 SimpleStreamOptions（Pydantic），改为独立 dataclass，避免
    dataclass + Pydantic 继承冲突。stream 选项字段（api_key/reasoning 等）
    直接声明为可选字段，在 LLM 调用边界提取。

    钩子字段都是可选的，宿主按需提供。
    """

    model: Model = field(default_factory=lambda: _DEFAULT_MODEL)
    # stream 选项（对应 SimpleStreamOptions 的子集）
    api_key: str | None = None
    reasoning: ThinkingLevel | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    session_id: str | None = None
    thinking_budgets: dict[str, int] | None = None
    # 钩子
    convert_to_llm: Any = None
    transform_context: Any = None
    get_api_key: Any = None
    #: 回合完成后（``turn_end`` 之前）询问是否继续/结束。替代 v0.85.1 的 ``should_stop_after_turn``。
    finish_turn: FinishTurn | None = None
    #: 每次会话型 provider 请求前运行（含第一次），可替换 context/model/thinking level。
    prepare_request: PrepareRequest | None = None
    prepare_next_turn: Any = None
    get_steering_messages: Any = None
    get_follow_up_messages: Any = None
    tool_execution: ToolExecutionMode = "parallel"
    before_tool_call: Any = None
    after_tool_call: Any = None


# ============================================================
# Agent 事件（agent 层，覆盖四层生命周期）
# ============================================================


@dataclass
class AgentStartEvent:
    type: Literal["agent_start"] = "agent_start"


@dataclass
class AgentEndEvent:
    type: Literal["agent_end"] = "agent_end"
    messages: list[AgentMessage] = field(default_factory=list)


@dataclass
class TurnStartEvent:
    type: Literal["turn_start"] = "turn_start"


@dataclass
class TurnEndEvent:
    type: Literal["turn_end"] = "turn_end"
    message: AgentMessage = None  # type: ignore[assignment]
    tool_results: list[ToolResultMessage] = field(default_factory=list)


@dataclass
class MessageStartEvent:
    type: Literal["message_start"] = "message_start"
    message: AgentMessage = None  # type: ignore[assignment]


@dataclass
class MessageUpdateEvent:
    """assistant 消息流式更新（携带底层 pi-ai 流式事件）。"""

    type: Literal["message_update"] = "message_update"
    message: AgentMessage = None  # type: ignore[assignment]
    assistant_message_event: AssistantMessageEvent | None = None


@dataclass
class MessageEndEvent:
    type: Literal["message_end"] = "message_end"
    message: AgentMessage = None  # type: ignore[assignment]


@dataclass
class ToolExecutionStartEvent:
    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_call_id: str = ""
    tool_name: str = ""
    args: Any = None


@dataclass
class ToolExecutionUpdateEvent:
    type: Literal["tool_execution_update"] = "tool_execution_update"
    tool_call_id: str = ""
    tool_name: str = ""
    args: Any = None
    partial_result: AgentToolResult | None = None


@dataclass
class ToolExecutionEndEvent:
    type: Literal["tool_execution_end"] = "tool_execution_end"
    tool_call_id: str = ""
    tool_name: str = ""
    result: AgentToolResult | None = None
    is_error: bool = False


#: Agent 事件联合。
AgentEvent = (
    AgentStartEvent
    | AgentEndEvent
    | TurnStartEvent
    | TurnEndEvent
    | MessageStartEvent
    | MessageUpdateEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionUpdateEvent
    | ToolExecutionEndEvent
)


__all__ = [
    "ToolExecutionMode",
    "QueueMode",
    "AgentMessage",
    "AgentToolCall",
    "AgentToolResult",
    "AgentToolUpdateCallback",
    "AgentTool",
    "AgentContext",
    "AgentState",
    "AgentTurnContext",
    "AgentTurnDecision",
    "FinishTurn",
    "RequestThinkingLevel",
    "PrepareRequestContext",
    "AgentRequestUpdate",
    "PrepareRequest",
    "StreamFn",
    "AgentLoopConfig",
    "AgentStartEvent",
    "AgentEndEvent",
    "TurnStartEvent",
    "TurnEndEvent",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "MessageEndEvent",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "ToolExecutionEndEvent",
    "AgentEvent",
]
