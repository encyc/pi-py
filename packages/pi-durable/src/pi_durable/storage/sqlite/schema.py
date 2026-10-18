"""SQLite 标识符 —— 移植 ``durable/src/storage/sqlite/schema.ts``（69 行）。

表名与索引名是**不可变的标识符历史**：现有值永不改动或删除，改名要新增一项，
好让历史迁移仍然渲染出它们原本的对象名（``schema.ts:1-4``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "CURRENT_SQLITE_SCHEMA_VERSION",
    "DEFAULT_SQLITE_SCHEMA",
    "SqliteSchema",
    "create_sqlite_schema",
    "validate_sqlite_schema_prefix",
]

_TABLE_NAMES = {
    "durable_schema": "durable_schema",
    "durable_metadata": "durable_metadata",
    "record_ids": "record_ids",
    "conversations": "conversations",
    "entries": "entries",
    "tasks": "tasks",
    "submissions": "submissions",
    "documents": "documents",
    "document_revisions": "document_revisions",
}

_INDEX_NAMES = {
    "conversations_by_owner_conversation": "conversations_by_owner_conversation",
    "conversations_by_owner_task": "conversations_by_owner_task",
    "entries_by_conversation": "entries_by_conversation",
    "entry_heads_by_conversation": "entry_heads_by_conversation",
    "tasks_by_status": "tasks_by_status",
    "tasks_by_conversation": "tasks_by_conversation",
    "tasks_by_kind": "tasks_by_kind",
    "tasks_by_abort_requested": "tasks_by_abort_requested",
    "tasks_by_background": "tasks_by_background",
    "submissions_by_request": "submissions_by_request",
    "submissions_by_conversation": "submissions_by_conversation",
    "submissions_by_status": "submissions_by_status",
    "documents_by_address": "documents_by_address",
    "documents_by_scope": "documents_by_scope",
    "documents_by_scope_kind": "documents_by_scope_kind",
    "document_revisions_by_kind": "document_revisions_by_kind",
}

_PREFIX_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class SqliteSchema:
    """一组渲染好的表名与索引名（``schema.ts:38-41``）。"""

    tables: dict[str, str]
    indexes: dict[str, str]


def validate_sqlite_schema_prefix(prefix: str) -> None:
    """校验前缀可以安全地拼进 SQLite 标识符（``schema.ts:43-51``）。"""
    if not _PREFIX_PATTERN.match(prefix):
        raise ValueError(f"Invalid SQLite schema prefix {prefix!r}")
    if prefix.lower().startswith("sqlite_"):
        raise ValueError(f"SQLite schema prefix {prefix!r} is reserved")


def create_sqlite_schema(prefix: str | None = None) -> SqliteSchema:
    """构建每次迁移与存储查询都用的标识符（``schema.ts:59-67``）。"""
    if prefix is not None:
        validate_sqlite_schema_prefix(prefix)
    actual = prefix or ""
    return SqliteSchema(
        tables={key: f"{actual}{name}" for key, name in _TABLE_NAMES.items()},
        indexes={key: f"{actual}{name}" for key, name in _INDEX_NAMES.items()},
    )


DEFAULT_SQLITE_SCHEMA = create_sqlite_schema()

#: 当前 schema 版本；由 :mod:`pi_durable.storage.sqlite.migrations` 填充。
CURRENT_SQLITE_SCHEMA_VERSION = 1
