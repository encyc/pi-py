"""类型化条目种类 —— 移植 ``durable/src/entries.ts``（34 行）。

一个条目令牌就是「一个 kind 字符串 + 一个类型守卫」。上游用泛型 ``Entry<D>`` 让 ``data``
在编译期收窄；Python 在运行期只做 :meth:`EntryToken.is_` 的 kind 比较，
``data`` 的形状交给调用方（这是 PORTING.md 偏离 #6 的同一取舍：类型收紧靠 mypy，不靠运行期）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pi_chord.json import JsonValue
from pi_durable.types import EntryRecord

__all__ = [
    "AssistantEntry",
    "CompactionEntry",
    "EntryToken",
    "ResetEntry",
    "SystemEntry",
    "ToolResultEntry",
    "UserEntry",
    "define_entry",
]


@dataclass(frozen=True, slots=True)
class EntryToken:
    """一个类型化条目种类（上游 ``defineEntry(kind)`` 的返回值）。"""

    kind: str

    def is_(self, entry: Mapping[str, Any] | None) -> bool:
        """按 ``kind`` 收窄（上游的 ``is()`` 类型守卫）。

        上游签名是 ``(entry: EntryRecord | undefined) => entry is TypedEntry<D>``；
        Python 没有类型守卫语法，所以返回 ``bool``，静态收窄由调用方用 ``cast`` 表达。
        """
        return entry is not None and entry.get("kind") == self.kind

    def __call__(self, entry: Mapping[str, Any] | None) -> bool:
        """让令牌本身可当谓词用（``filter(ToolResultEntry, entries)``）。"""
        return self.is_(entry)


def define_entry(kind: str) -> EntryToken:
    """定义一个类型化条目种类（``entries.ts:5-13``）。"""
    if not isinstance(kind, str) or not kind:
        raise TypeError("Entry kind must be a non-empty string")
    return EntryToken(kind)


#: 用户输入：``model`` 是 ``[UserMessage]``。由提交写入（``entries.ts:16``）。
UserEntry = define_entry("pi.user")
#: 提供方结果（任意 stop reason）：``model`` 是 ``[AssistantMessage]``（``entries.ts:18``）。
AssistantEntry = define_entry("pi.assistant")
#: 位置性提示与工具变更：``model`` 是内容为空的 ``[SystemMessage]``（``entries.ts:20``）。
SystemEntry = define_entry("pi.system")
#: 工具结果：``model`` 是 ``[ToolResultMessage]``，其内容以渲染后的诊断块结尾；
#: ``data`` 持有结构化诊断（可能为空）（``entries.ts:24``）。
ToolResultEntry = define_entry("pi.tool-result")
#: 新上下文的起点：``head`` 恒为 ``"self"``；普通重置时 ``model`` 缺失，
#: 交接时是携带交接文本的 ``[UserMessage]``（``entries.ts:29``）。
ResetEntry = define_entry("pi.reset")
#: 压缩摘要：``model`` 是包着摘要的 ``[UserMessage]``，``head`` 是第一个保留的条目（``entries.ts:33``）。
CompactionEntry = define_entry("pi.compaction")


def as_typed(token: EntryToken, entry: EntryRecord | None) -> EntryRecord | None:
    """``token.is_`` 通过则返回条目，否则 ``None``（上游 ``tx.entry(token, id)`` 的语义）。"""
    if not token.is_(entry):
        return None
    return entry


def tool_result_diagnostics(entry: Mapping[str, Any] | None) -> list[JsonValue]:
    """``ToolResultEntry`` 的结构化诊断；不合类型时是空列表（``entries.ts:24`` 的 ``data``）。"""
    if not ToolResultEntry.is_(entry):
        return []
    data = (entry or {}).get("data")
    if not isinstance(data, dict):
        return []
    diagnostics = data.get("diagnostics")
    return diagnostics if isinstance(diagnostics, list) else []


def compaction_reason(entry: Mapping[str, Any] | None) -> Any:
    """``CompactionEntry`` 的 ``reason``；不合类型时是 ``None``（``entries.ts:33``）。"""
    if not CompactionEntry.is_(entry):
        return None
    data = (entry or {}).get("data")
    return data.get("reason") if isinstance(data, dict) else None
