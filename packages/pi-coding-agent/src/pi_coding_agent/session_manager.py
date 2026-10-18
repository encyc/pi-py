"""会话条目的类型（**类型专用切分**）。

> ⚠️ **这不是 ``core/session-manager.ts`` 的移植。** 上游那个模块 **2,013 行**（会话读写、
> 分支、压缩、标签……）**未移植**；这里**只取 :data:`SessionEntry` 这一个联合类型**，
> 因为 ``extensions/codemode/execute.ts`` 的 ``readCodemodeStore(branch)`` 需要它。
> 与该类型无关的上游内容一律不在本模块。

上游出处：``core/session-manager.ts:57-175`` 左右的类型区（``SessionEntryBase`` 与 11 个变体）。

**为什么是「类型专用切分」而不是「部分移植」**：本模块**没有运行期行为** ——
没有读写会话、没有分支逻辑，只有形状定义。所以它不会与上游的实现漂移，
只会与上游的**类型**漂移，而那是 `git diff` 能一眼看出来的。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pi_ai import Usage


#: 所有变体的公共字段（上游 ``SessionEntryBase``）。
class SessionEntryBase(TypedDict):
    """每条会话条目都有的字段。

    **有意偏离**：上游 ``SessionEntryBase`` 含 ``type: string``，各变体把它收窄成
    ``Literal[...]``。**Python 的 TypedDict 不允许收窄父类的字段类型**（mypy 报
    "Definition of field \"type\" incompatible with base class"），因此这里基类**不含**
    ``type``，由每个变体自己声明。运行期形状与上游一致，只是静态继承关系差一层。
    """

    id: str
    parentId: str | None
    timestamp: str


class SessionMessageEntry(SessionEntryBase):
    """一条消息（``AgentMessage`` 的形状由 pi-agent-core 决定，此处按载荷保留）。"""

    type: Literal["message"]
    message: dict[str, Any]


class ThinkingLevelChangeEntry(SessionEntryBase):
    type: Literal["thinking_level_change"]
    thinkingLevel: str


class ModelChangeEntry(SessionEntryBase):
    type: Literal["model_change"]
    provider: str
    modelId: str


class UsageEntry(SessionEntryBase):
    type: Literal["usage"]
    kind: str
    provider: str
    model: str
    usage: Usage
    note: str


class CompactionEntry(SessionEntryBase):
    type: Literal["compaction"]
    summary: str
    firstKeptEntryId: str
    tokensBefore: int
    details: Any
    usage: Usage
    fromHook: bool
    systemMessage: Any


class BranchSummaryEntry(SessionEntryBase):
    type: Literal["branch_summary"]
    fromId: str
    summary: str
    details: Any
    usage: Usage
    fromHook: bool


class CustomEntry(SessionEntryBase):
    """自定义条目；``codemode`` 的 ``store()`` 写入就是 ``customType="codemode-store"``。"""

    type: Literal["custom"]
    customType: str
    data: Any


class CustomMessageEntry(SessionEntryBase):
    type: Literal["custom_message"]
    customType: str
    content: Any
    details: Any
    display: bool


class ContextEditEntry(SessionEntryBase):
    type: Literal["context_edit"]
    content: Any


class LabelEntry(SessionEntryBase):
    type: Literal["label"]
    targetId: str
    label: str | None


class SessionInfoEntry(SessionEntryBase):
    type: Literal["session_info"]
    name: str


#: 一条会话条目（上游 ``SessionEntry`` 的 11 变体联合）。
SessionEntry = (
    SessionMessageEntry
    | ThinkingLevelChangeEntry
    | ModelChangeEntry
    | UsageEntry
    | CompactionEntry
    | BranchSummaryEntry
    | CustomEntry
    | CustomMessageEntry
    | ContextEditEntry
    | LabelEntry
    | SessionInfoEntry
)

__all__ = [
    "BranchSummaryEntry",
    "CompactionEntry",
    "ContextEditEntry",
    "CustomEntry",
    "CustomMessageEntry",
    "LabelEntry",
    "ModelChangeEntry",
    "SessionEntry",
    "SessionEntryBase",
    "SessionInfoEntry",
    "SessionMessageEntry",
    "ThinkingLevelChangeEntry",
    "UsageEntry",
]
