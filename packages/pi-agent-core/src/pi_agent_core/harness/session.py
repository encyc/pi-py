"""会话（Session）持久化与树结构。

对应上游 ``packages/agent/src/harness/session/``。

会话以**树结构**存储条目（message / compaction / branch_summary / thinking_level_change
/ model_change / active_tools_change / label 等）。每个条目有 id、parent_id、
timestamp。从叶节点回溯到根（或到 compaction）构成当前上下文。

提供两种后端：
- ``InMemorySessionStorage``：纯内存，测试与临时场景。
- ``JsonlSessionStorage``：JSONL 文件持久化，每行一个条目。

SDK 场景下，会话的核心价值是**保存/恢复对话**与**分支**（fork）。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pi_ai import AssistantMessage, Message, ToolResultMessage, UserMessage

# ============================================================
# 条目类型
# ============================================================

#: 条目类型。
SessionEntryType = Literal[
    "message",
    "compaction",
    "branch_summary",
    "thinking_level_change",
    "model_change",
    "active_tools_change",
    "label",
    "session_info",
]


@dataclass
class SessionEntry:
    """会话树的一个条目。"""

    id: str
    parent_id: str | None
    timestamp: str
    type: SessionEntryType
    data: Any = None
    label: str | None = None


def _create_entry_id() -> str:
    return uuid.uuid4().hex[:16]


def _create_timestamp() -> str:
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime()) + f"{int(time.time() * 1000) % 1000:03d}"
    )


# ============================================================
# 存储抽象
# ============================================================


class SessionStorage(Protocol):
    """会话存储后端协议。"""

    def create_entry_id(self) -> str: ...
    def create_timestamp(self) -> str: ...
    def get_metadata(self) -> dict[str, Any]: ...
    def get_leaf_id(self) -> str | None: ...
    def set_leaf_id(self, entry_id: str | None) -> None: ...
    def get_entries(self) -> list[SessionEntry]: ...
    def get_entry(self, entry_id: str) -> SessionEntry | None: ...
    def append_entry(self, entry: SessionEntry) -> None: ...
    def get_label(self) -> str | None: ...
    def set_label(self, label: str | None) -> None: ...


# ============================================================
# 内存存储
# ============================================================


class InMemorySessionStorage:
    """纯内存会话存储。"""

    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        self._metadata = metadata or {}
        self._entries: dict[str, SessionEntry] = {}
        self._order: list[str] = []
        self._leaf_id: str | None = None
        self._label: str | None = None

    def create_entry_id(self) -> str:
        return _create_entry_id()

    def create_timestamp(self) -> str:
        return _create_timestamp()

    def get_metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    def get_leaf_id(self) -> str | None:
        return self._leaf_id

    def set_leaf_id(self, entry_id: str | None) -> None:
        self._leaf_id = entry_id

    def get_entries(self) -> list[SessionEntry]:
        return [self._entries[eid] for eid in self._order]

    def get_entry(self, entry_id: str) -> SessionEntry | None:
        return self._entries.get(entry_id)

    def append_entry(self, entry: SessionEntry) -> None:
        self._entries[entry.id] = entry
        self._order.append(entry.id)
        self._leaf_id = entry.id

    def get_label(self) -> str | None:
        return self._label

    def set_label(self, label: str | None) -> None:
        self._label = label


# ============================================================
# JSONL 存储
# ============================================================


class JsonlSessionStorage:
    """JSONL 文件会话存储。每行一个条目 JSON。

    文件格式：
    - 第 1 行：metadata（含 leaf_id / label）
    - 第 2+ 行：entry JSON（按追加顺序）
    """

    def __init__(self, file_path: str | Path, metadata: dict[str, Any] | None = None) -> None:
        self._path = Path(file_path)
        self._metadata: dict[str, Any] = metadata or {}
        self._entries: dict[str, SessionEntry] = {}
        self._order: list[str] = []
        self._leaf_id: str | None = None
        self._label: str | None = None
        if self._path.exists():
            self._load()
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._save()

    def create_entry_id(self) -> str:
        return _create_entry_id()

    def create_timestamp(self) -> str:
        return _create_timestamp()

    def get_metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    def get_leaf_id(self) -> str | None:
        return self._leaf_id

    def set_leaf_id(self, entry_id: str | None) -> None:
        self._leaf_id = entry_id
        self._save()

    def get_entries(self) -> list[SessionEntry]:
        return [self._entries[eid] for eid in self._order]

    def get_entry(self, entry_id: str) -> SessionEntry | None:
        return self._entries.get(entry_id)

    def append_entry(self, entry: SessionEntry) -> None:
        self._entries[entry.id] = entry
        self._order.append(entry.id)
        self._leaf_id = entry.id
        self._save()

    def get_label(self) -> str | None:
        return self._label

    def set_label(self, label: str | None) -> None:
        self._label = label
        self._save()

    def _save(self) -> None:
        meta = {**self._metadata, "_leaf_id": self._leaf_id, "_label": self._label}
        lines = [json.dumps({"_meta": meta}, ensure_ascii=False)]
        for eid in self._order:
            e = self._entries[eid]
            lines.append(
                json.dumps(
                    {
                        "id": e.id,
                        "parent_id": e.parent_id,
                        "timestamp": e.timestamp,
                        "type": e.type,
                        "data": _serialize_message(e.data),
                        "label": e.label,
                    },
                    ensure_ascii=False,
                )
            )
        self._path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _load(self) -> None:
        lines = self._path.read_text(encoding="utf-8").strip().split("\n")
        if not lines:
            return
        # 第一行是 meta
        meta_obj = json.loads(lines[0])
        meta = meta_obj.get("_meta", {})
        self._metadata = {k: v for k, v in meta.items() if k not in ("_leaf_id", "_label")}
        self._leaf_id = meta.get("_leaf_id")
        self._label = meta.get("_label")
        for line in lines[1:]:
            if not line.strip():
                continue
            obj = json.loads(line)
            entry = SessionEntry(
                id=obj["id"],
                parent_id=obj.get("parent_id"),
                timestamp=obj.get("timestamp", ""),
                type=obj.get("type", "message"),
                data=_deserialize_message(obj.get("data")),
                label=obj.get("label"),
            )
            self._entries[entry.id] = entry
            self._order.append(entry.id)


def _serialize_message(data: Any) -> Any:
    """序列化 message 数据（Pydantic model → dict）。"""
    if data is None:
        return None
    if hasattr(data, "model_dump"):
        return data.model_dump(by_alias=True, mode="json")
    return data


def _deserialize_message(data: Any) -> Any:
    """反序列化 message 数据（dict → Pydantic model）。"""
    if data is None or not isinstance(data, dict):
        return data
    role = data.get("role")
    if role == "user":
        return UserMessage.model_validate(data)
    if role == "assistant":
        return AssistantMessage.model_validate(data)
    if role == "toolResult":
        return ToolResultMessage.model_validate(data)
    return data


# ============================================================
# Session 类
# ============================================================


class Session:
    """一个会话：树结构条目 + 当前叶节点。

    核心操作：
    - append_message / append_compaction / append_label：追加条目
    - get_branch：从叶节点回溯到根（或到 compaction）
    - build_context：从分支构建 AgentMessage 列表
    - move_to：切换到另一个叶节点（分支切换）
    """

    def __init__(self, storage: SessionStorage) -> None:
        self._storage = storage

    @property
    def storage(self) -> SessionStorage:
        return self._storage

    @property
    def leaf_id(self) -> str | None:
        return self._storage.get_leaf_id()

    def get_entry(self, entry_id: str) -> SessionEntry | None:
        return self._storage.get_entry(entry_id)

    def get_entries(self) -> list[SessionEntry]:
        return self._storage.get_entries()

    def get_label(self) -> str | None:
        return self._storage.get_label()

    # ---- 追加条目 ----

    def append_message(self, message: Message) -> SessionEntry:
        """追加一条消息。"""
        entry = SessionEntry(
            id=self._storage.create_entry_id(),
            parent_id=self.leaf_id,
            timestamp=self._storage.create_timestamp(),
            type="message",
            data=message,
        )
        self._storage.append_entry(entry)
        return entry

    def append_compaction(self, summary: str, retained_tail: list[Message]) -> SessionEntry:
        """追加一个 compaction 条目（上下文压缩点）。"""
        entry = SessionEntry(
            id=self._storage.create_entry_id(),
            parent_id=self.leaf_id,
            timestamp=self._storage.create_timestamp(),
            type="compaction",
            data={
                "summary": summary,
                "retained_tail": [_serialize_message(m) for m in retained_tail],
            },
        )
        self._storage.append_entry(entry)
        return entry

    def append_label(self, label: str) -> SessionEntry:
        entry = SessionEntry(
            id=self._storage.create_entry_id(),
            parent_id=self.leaf_id,
            timestamp=self._storage.create_timestamp(),
            type="label",
            data=label,
        )
        self._storage.append_entry(entry)
        self._storage.set_label(label)
        return entry

    def append_thinking_level_change(self, level: str | None) -> SessionEntry:
        entry = SessionEntry(
            id=self._storage.create_entry_id(),
            parent_id=self.leaf_id,
            timestamp=self._storage.create_timestamp(),
            type="thinking_level_change",
            data=level,
        )
        self._storage.append_entry(entry)
        return entry

    def append_model_change(self, model: dict[str, Any]) -> SessionEntry:
        entry = SessionEntry(
            id=self._storage.create_entry_id(),
            parent_id=self.leaf_id,
            timestamp=self._storage.create_timestamp(),
            type="model_change",
            data=model,
        )
        self._storage.append_entry(entry)
        return entry

    # ---- 分支与上下文 ----

    def get_branch(self, from_id: str | None = None) -> list[SessionEntry]:
        """从指定节点（默认叶节点）回溯到根（或到 compaction）。

        如果路径上遇到 compaction，从 compaction 开始（含）。
        """
        leaf = from_id or self.leaf_id
        if leaf is None:
            return []
        path: list[SessionEntry] = []
        current: str | None = leaf
        while current:
            entry = self._storage.get_entry(current)
            if entry is None:
                break
            path.append(entry)
            if entry.type == "compaction":
                break
            current = entry.parent_id
        path.reverse()
        return path

    def build_context(self) -> list[Message]:
        """从当前分支构建 AgentMessage 列表。

        - compaction 条目：展开为 summary 文本 + retained_tail 消息。
        - message 条目：直接取 data。
        - 其他类型条目：跳过（不影响消息序列）。
        """
        messages: list[Message] = []
        for entry in self.get_branch():
            if entry.type == "message":
                if isinstance(entry.data, (UserMessage, AssistantMessage, ToolResultMessage)):
                    messages.append(entry.data)
            elif entry.type == "compaction" and isinstance(entry.data, dict):
                summary = entry.data.get("summary", "")
                if summary:
                    messages.append(
                        UserMessage(content=f"[Previous conversation summary]\n{summary}")
                    )
                for raw in entry.data.get("retained_tail", []):
                    if isinstance(raw, dict):
                        msg = _deserialize_message(raw)
                        if isinstance(msg, (UserMessage, AssistantMessage, ToolResultMessage)):
                            messages.append(msg)
        return messages

    def move_to(self, entry_id: str | None) -> None:
        """切换叶节点（分支切换）。"""
        self._storage.set_leaf_id(entry_id)

    def fork(self, from_id: str | None = None) -> Session:
        """从指定节点 fork 出一个新会话（共享到该点的历史）。"""
        branch = self.get_branch(from_id)
        new_storage = InMemorySessionStorage(metadata=self._storage.get_metadata())
        new_session = Session(new_storage)
        for entry in branch:
            new_entry = SessionEntry(
                id=new_storage.create_entry_id(),
                parent_id=new_storage.get_leaf_id(),
                timestamp=entry.timestamp,
                type=entry.type,
                data=entry.data,
                label=entry.label,
            )
            new_storage.append_entry(new_entry)
        return new_session


__all__ = [
    "SessionEntry",
    "SessionEntryType",
    "SessionStorage",
    "InMemorySessionStorage",
    "JsonlSessionStorage",
    "Session",
]
