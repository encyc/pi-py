"""流式事件类型。

对应上游 ``AssistantMessageEvent`` discriminated union（12 个变体）。
上游用单一联合类型，``type`` 字段判别。这里拆成多个 BaseModel 类 +
``AssistantMessageEvent`` 联合，便于构造与类型检查。

事件协议（与上游一致）：
- 流必须以 ``start`` 开头。
- 然后是若干 partial 更新（``text_*`` / ``thinking_*`` / ``toolcall_*``），
  每个都带 ``partial: AssistantMessage``，可随时读取累积的部分消息。
- 以 ``done``（成功，带最终 message）或 ``error``（失败，带 error message）终止。
- 注意 ``toolcall_*`` 事件名无下划线（与上游一致）。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .types import AssistantMessage, StopReason, ToolCall


class _EventBase(BaseModel):
    """事件基类：允许原地修改 partial。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class StartEvent(_EventBase):
    type: Literal["start"] = "start"
    partial: AssistantMessage


class TextStartEvent(_EventBase):
    type: Literal["text_start"] = "text_start"
    content_index: int = Field(alias="contentIndex")
    partial: AssistantMessage


class TextDeltaEvent(_EventBase):
    type: Literal["text_delta"] = "text_delta"
    content_index: int = Field(alias="contentIndex")
    delta: str
    partial: AssistantMessage


class TextEndEvent(_EventBase):
    type: Literal["text_end"] = "text_end"
    content_index: int = Field(alias="contentIndex")
    content: str
    partial: AssistantMessage


class ThinkingStartEvent(_EventBase):
    type: Literal["thinking_start"] = "thinking_start"
    content_index: int = Field(alias="contentIndex")
    partial: AssistantMessage


class ThinkingDeltaEvent(_EventBase):
    type: Literal["thinking_delta"] = "thinking_delta"
    content_index: int = Field(alias="contentIndex")
    delta: str
    partial: AssistantMessage


class ThinkingEndEvent(_EventBase):
    type: Literal["thinking_end"] = "thinking_end"
    content_index: int = Field(alias="contentIndex")
    content: str
    partial: AssistantMessage


class ToolCallStartEvent(_EventBase):
    type: Literal["toolcall_start"] = "toolcall_start"
    content_index: int = Field(alias="contentIndex")
    partial: AssistantMessage


class ToolCallDeltaEvent(_EventBase):
    type: Literal["toolcall_delta"] = "toolcall_delta"
    content_index: int = Field(alias="contentIndex")
    delta: str
    partial: AssistantMessage


class ToolCallEndEvent(_EventBase):
    type: Literal["toolcall_end"] = "toolcall_end"
    content_index: int = Field(alias="contentIndex")
    tool_call: ToolCall = Field(alias="toolCall")
    partial: AssistantMessage


class DoneEvent(_EventBase):
    type: Literal["done"] = "done"
    reason: Literal["stop", "length", "toolUse"]
    message: AssistantMessage


class ErrorEvent(_EventBase):
    type: Literal["error"] = "error"
    reason: Literal["aborted", "error"]
    error: AssistantMessage


#: 助手消息事件联合。对应上游 ``AssistantMessageEvent``。
AssistantMessageEvent = Annotated[
    StartEvent
    | TextStartEvent
    | TextDeltaEvent
    | TextEndEvent
    | ThinkingStartEvent
    | ThinkingDeltaEvent
    | ThinkingEndEvent
    | ToolCallStartEvent
    | ToolCallDeltaEvent
    | ToolCallEndEvent
    | DoneEvent
    | ErrorEvent,
    Field(discriminator="type"),
]


# 用于类型注解的便捷别名
NonTerminalEvent = (
    "StartEvent | TextStartEvent | TextDeltaEvent | TextEndEvent | "
    "ThinkingStartEvent | ThinkingDeltaEvent | ThinkingEndEvent | "
    "ToolCallStartEvent | ToolCallDeltaEvent | ToolCallEndEvent"
)

TerminalEvent = "DoneEvent | ErrorEvent"


def _terminal_reason(stop_reason: StopReason) -> tuple[str, str]:
    """根据 stop_reason 决定终止事件类型与原因。"""
    if stop_reason in ("stop", "length", "toolUse"):
        return "done", stop_reason
    return "error", stop_reason


__all__ = [
    "StartEvent",
    "TextStartEvent",
    "TextDeltaEvent",
    "TextEndEvent",
    "ThinkingStartEvent",
    "ThinkingDeltaEvent",
    "ThinkingEndEvent",
    "ToolCallStartEvent",
    "ToolCallDeltaEvent",
    "ToolCallEndEvent",
    "DoneEvent",
    "ErrorEvent",
    "AssistantMessageEvent",
    "NonTerminalEvent",
    "TerminalEvent",
]
