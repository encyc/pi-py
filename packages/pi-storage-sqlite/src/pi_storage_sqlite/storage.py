"""SQLite 会话存储实现。

对应上游 ``packages/storage/sqlite-node/src/sqlite/storage/`` 目录。

实现 ``SessionStorage`` 协议（来自 ``pi_agent_core.harness.session``），
使 SQLite 成为 ``Session`` 的一个可选后端。

核心设计（与上游一致）：
- ``session_entries`` 表存所有条目，payload 列只存 type-specific JSON。
- ``session_sequences`` 维护 per-session 自增 entry_seq。
- 每次 append 在事务内完成（entry + leaf 指针 + 序号推进）。
- branch_entries 物化分支路径（此处简化：查询时回溯 parent_id，不预物化）。
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from typing import Any

from pi_agent_core.harness.session import SessionEntry

from .database import SqliteDatabase, open_database


def _create_entry_id() -> str:
    return uuid.uuid4().hex[:16]


def _create_timestamp() -> str:
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime()) + f"{int(time.time() * 1000) % 1000:03d}"
    )


def _serialize_payload(data: Any) -> str:
    """序列化 entry data（Pydantic model → dict → JSON）。"""
    if data is None:
        return "null"
    if hasattr(data, "model_dump"):
        return json.dumps(data.model_dump(by_alias=True, mode="json"), ensure_ascii=False)
    return json.dumps(data, ensure_ascii=False)


def _deserialize_payload(raw: str) -> Any:
    """反序列化 payload。"""
    if not raw or raw == "null":
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    # 尝试还原 Pydantic message（role 判别）
    if isinstance(data, dict):
        from pi_agent_core.harness.session import _deserialize_message

        return _deserialize_message(data)
    return data


# ============================================================
# SQLiteSessionStorage
# ============================================================


class SqliteSessionStorage:
    """SQLite 会话存储后端。实现 ``SessionStorage`` 协议。

    每个实例绑定一个 session_id + 一个 SqliteDatabase 连接。
    """

    def __init__(
        self,
        db: SqliteDatabase,
        session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._db = db
        self._session_id = session_id
        self._metadata = metadata or {}

    # ---- SessionStorage 协议 ----

    def create_entry_id(self) -> str:
        return _create_entry_id()

    def create_timestamp(self) -> str:
        return _create_timestamp()

    def get_metadata(self) -> dict[str, Any]:
        row = self._db.query_one("SELECT metadata FROM sessions WHERE id = ?", (self._session_id,))
        if row and row["metadata"]:
            with contextlib.suppress(json.JSONDecodeError):
                meta: dict[str, Any] = json.loads(row["metadata"])
                return meta
        return dict(self._metadata)

    def get_leaf_id(self) -> str | None:
        row = self._db.query_one(
            "SELECT active_leaf_id FROM sessions WHERE id = ?", (self._session_id,)
        )
        return row["active_leaf_id"] if row else None

    def set_leaf_id(self, entry_id: str | None) -> None:
        self._db.run(
            "UPDATE sessions SET active_leaf_id = ? WHERE id = ?",
            (entry_id, self._session_id),
        )

    def get_entries(self) -> list[SessionEntry]:
        rows = self._db.query_all(
            "SELECT id, parent_id, type, timestamp, payload FROM session_entries "
            "WHERE session_id = ? ORDER BY entry_seq",
            (self._session_id,),
        )
        return [
            SessionEntry(
                id=r["id"],
                parent_id=r["parent_id"],
                timestamp=r["timestamp"],
                type=r["type"],
                data=_deserialize_payload(r["payload"]),
            )
            for r in rows
        ]

    def get_entry(self, entry_id: str) -> SessionEntry | None:
        row = self._db.query_one(
            "SELECT id, parent_id, type, timestamp, payload FROM session_entries "
            "WHERE session_id = ? AND id = ?",
            (self._session_id, entry_id),
        )
        if not row:
            return None
        return SessionEntry(
            id=row["id"],
            parent_id=row["parent_id"],
            timestamp=row["timestamp"],
            type=row["type"],
            data=_deserialize_payload(row["payload"]),
        )

    def append_entry(self, entry: SessionEntry) -> None:
        """追加条目（事务内）。"""

        def _do() -> None:
            seq = self._get_next_seq()
            # 插入 entry
            self._db.run(
                "INSERT INTO session_entries (session_id, id, entry_seq, parent_id, type, timestamp, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self._session_id,
                    entry.id,
                    seq,
                    entry.parent_id,
                    entry.type,
                    entry.timestamp,
                    _serialize_payload(entry.data),
                ),
            )
            # 推进序号
            self._advance_sequence()
            # 更新 leaf 指针
            self._db.run(
                "UPDATE sessions SET active_leaf_id = ? WHERE id = ?",
                (entry.id, self._session_id),
            )

        self._db.transaction(_do)

    def get_label(self) -> str | None:
        entries = self.get_entries()
        for entry in reversed(entries):
            if entry.type == "label" and isinstance(entry.data, str):
                return entry.data
        return None

    def set_label(self, label: str | None) -> None:
        # label 通过 append_entry(type="label") 追加，这里仅更新 metadata
        meta = self.get_metadata()
        if label:
            meta["_label"] = label
        else:
            meta.pop("_label", None)
        self._db.run(
            "UPDATE sessions SET metadata = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), self._session_id),
        )

    # ---- 内部 ----

    def _get_next_seq(self) -> int:
        row = self._db.query_one(
            "SELECT next_seq FROM session_sequences WHERE session_id = ?",
            (self._session_id,),
        )
        return row["next_seq"] if row else 1

    def _advance_sequence(self) -> None:
        next_seq = self._get_next_seq()
        self._db.run(
            "UPDATE session_sequences SET next_seq = ? WHERE session_id = ?",
            (next_seq + 1, self._session_id),
        )


# ============================================================
# Repo：多 session 管理（create/open/list/delete）
# ============================================================


class SqliteSessionRepo:
    """会话仓库：管理单个 SQLite 文件中的多个 session。

    对应上游 ``SqliteSessionRepo``。
    """

    def __init__(self, database_path: str) -> None:
        self._path = database_path
        self._db: SqliteDatabase | None = None

    @property
    def db(self) -> SqliteDatabase:
        if self._db is None:
            self._db = open_database(self._path)
        return self._db

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def create(
        self,
        cwd: str = "",
        parent_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, SqliteSessionStorage]:
        """创建新 session。返回 (session_id, storage)。"""
        session_id = uuid.uuid4().hex[:16]
        created_at = _create_timestamp()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False) if metadata else None

        def _do() -> None:
            self.db.run(
                "INSERT INTO sessions (id, created_at, cwd, parent_session_id, metadata, active_leaf_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, created_at, cwd, parent_session_id, meta_json, None),
            )
            self.db.run(
                "INSERT INTO session_sequences (session_id, next_seq) VALUES (?, ?)",
                (session_id, 1),
            )

        self.db.transaction(_do)
        storage = SqliteSessionStorage(self.db, session_id, metadata)
        return session_id, storage

    def open(self, session_id: str) -> SqliteSessionStorage:
        """打开已有 session。"""
        row = self.db.query_one("SELECT id FROM sessions WHERE id = ?", (session_id,))
        if not row:
            raise KeyError(f"Session not found: {session_id}")
        return SqliteSessionStorage(self.db, session_id)

    def list(self, cwd: str | None = None) -> list[dict[str, Any]]:
        """列出 session 元数据。"""
        if cwd:
            rows = self.db.query_all(
                "SELECT id, created_at, cwd, parent_session_id, metadata, active_leaf_id "
                "FROM sessions WHERE cwd = ? ORDER BY created_at DESC",
                (cwd,),
            )
        else:
            rows = self.db.query_all(
                "SELECT id, created_at, cwd, parent_session_id, metadata, active_leaf_id "
                "FROM sessions ORDER BY created_at DESC"
            )
        result = []
        for r in rows:
            meta = {}
            if r["metadata"]:
                with contextlib.suppress(json.JSONDecodeError):
                    meta = json.loads(r["metadata"])
            result.append(
                {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "cwd": r["cwd"],
                    "parent_session_id": r["parent_session_id"],
                    "metadata": meta,
                    "active_leaf_id": r["active_leaf_id"],
                }
            )
        return result

    def delete(self, session_id: str) -> None:
        """删除 session（事务内，按固定顺序删各表）。"""

        def _do() -> None:
            self.db.run("DELETE FROM branch_entries WHERE session_id = ?", (session_id,))
            self.db.run("DELETE FROM session_entries WHERE session_id = ?", (session_id,))
            self.db.run("DELETE FROM entry_materialized WHERE session_id = ?", (session_id,))
            self.db.run("DELETE FROM session_materialized WHERE session_id = ?", (session_id,))
            self.db.run("DELETE FROM session_sequences WHERE session_id = ?", (session_id,))
            result = self.db.run("DELETE FROM sessions WHERE id = ?", (session_id,))
            if result.changes == 0:
                raise KeyError(f"Session not found: {session_id}")

        self.db.transaction(_do)


__all__ = ["SqliteSessionStorage", "SqliteSessionRepo"]
