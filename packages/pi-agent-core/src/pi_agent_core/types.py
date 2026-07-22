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
    """工具执行结果。

    - ``content``：返回给模型的内容。
    - ``details``：给日志/UI 的结构化详情（不发给模型）。
    - ``terminate``：提示本 batch 之后应停止（仅当批次里所有 result 都 terminate=True 才生效）。
    """

    content: list[TextContent | ImageContent] = field(default_factory=list)
    details: Any = None
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

    失败语义：抛异常 = 失败（被循环捕获转成 error result）；返回 = 成功。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    label: str
    execution_mode: ToolExecutionMode | None

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
    """

    system_prompt: str = ""
    model: Model = field(default_factory=lambda: _DEFAULT_MODEL)
    thinking_level: ThinkingLevel | None = None
    tools: list[AgentTool] = field(default_factory=list)
    messages: list[AgentMessage] = field(default_factory=list)
    is_streaming: bool = False
    streaming_message: AgentMessage | None = None
    pending_tool_calls: set[str] = field(default_factory=set)
    error_message: str | None = None


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
    should_stop_after_turn: Any = None
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
