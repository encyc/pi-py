"""SQLite schema 与迁移 —— 移植 ``durable/src/storage/sqlite/migrations.ts``（140 行）。

**9 张表 / 16 个索引**（``schema.ts:5-34``）。DDL 逐字对照上游：``STRICT`` 表、
``json_valid`` CHECK、以及 ``entry_heads_by_conversation`` 的部分索引。

⚠️ ``durable_metadata.next_id`` 是 **TEXT**：上游注释说是因为 node:sqlite 拒绝超出 JS
安全整数范围的 INTEGER 结果（``migrations.ts:12``）。Python 整数没有这个限制，但**必须保持
TEXT** 才能与上游库文件互通（见 PORTABLE.md 的 R4 与偏离 #4）。
"""

from __future__ import annotations

from dataclasses import dataclass

from pi_durable.storage.sqlite.database import SqliteDatabase
from pi_durable.storage.sqlite.schema import DEFAULT_SQLITE_SCHEMA, SqliteSchema

__all__ = [
    "CURRENT_SQLITE_SCHEMA_VERSION",
    "SQLITE_MIGRATIONS",
    "SqliteMigration",
    "apply_sqlite_migrations",
    "create_sqlite_migrations",
]


@dataclass(frozen=True, slots=True)
class SqliteMigration:
    version: int
    statements: tuple[str, ...]


def create_sqlite_migrations(schema: SqliteSchema) -> tuple[SqliteMigration, ...]:
    """为一组 SQLite 标识符构建不可变、有序的 schema 历史（``migrations.ts:10-91``）。"""
    tables = schema.tables
    indexes = schema.indexes
    initial: tuple[str, ...] = (
        f"""CREATE TABLE {tables["durable_metadata"]} (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            next_id TEXT NOT NULL,
            next_seq INTEGER NOT NULL
        ) STRICT""",
        f"INSERT INTO {tables['durable_metadata']} (singleton, next_id, next_seq) VALUES (1, '2', 1)",
        f"""CREATE TABLE {tables["record_ids"]} (
            id INTEGER PRIMARY KEY,
            record_type TEXT NOT NULL CHECK (record_type IN ('conversation', 'entry', 'task', 'submission', 'document'))
        ) STRICT""",
        f"""CREATE TABLE {tables["conversations"]} (
            id INTEGER PRIMARY KEY,
            owner_conversation_id INTEGER,
            owner_task_id INTEGER,
            record TEXT NOT NULL CHECK (json_valid(record))
        ) STRICT""",
        f"CREATE INDEX {indexes['conversations_by_owner_conversation']} "
        f"ON {tables['conversations']} (owner_conversation_id, id)",
        f"CREATE INDEX {indexes['conversations_by_owner_task']} "
        f"ON {tables['conversations']} (owner_task_id, id)",
        f"""CREATE TABLE {tables["entries"]} (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            head INTEGER,
            commit_seq INTEGER NOT NULL,
            record TEXT NOT NULL CHECK (json_valid(record))
        ) STRICT""",
        f"CREATE INDEX {indexes['entries_by_conversation']} "
        f"ON {tables['entries']} (conversation_id, id DESC)",
        f"CREATE INDEX {indexes['entry_heads_by_conversation']} "
        f"ON {tables['entries']} (conversation_id, id DESC) WHERE head IS NOT NULL",
        f"""CREATE TABLE {tables["tasks"]} (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'waiting', 'completing', 'terminal')),
            abort_requested INTEGER NOT NULL CHECK (abort_requested IN (0, 1)),
            background INTEGER NOT NULL CHECK (background IN (0, 1)),
            record TEXT NOT NULL CHECK (json_valid(record))
        ) STRICT""",
        f"CREATE INDEX {indexes['tasks_by_status']} ON {tables['tasks']} (status, id)",
        f"CREATE INDEX {indexes['tasks_by_conversation']} ON {tables['tasks']} (conversation_id, id)",
        f"CREATE INDEX {indexes['tasks_by_kind']} ON {tables['tasks']} (kind, id)",
        f"CREATE INDEX {indexes['tasks_by_abort_requested']} ON {tables['tasks']} (abort_requested, id)",
        f"CREATE INDEX {indexes['tasks_by_background']} ON {tables['tasks']} (background, id)",
        f"""CREATE TABLE {tables["submissions"]} (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            request_id TEXT,
            status TEXT NOT NULL CHECK (status IN ('queued', 'placed', 'done', 'unanswered')),
            record TEXT NOT NULL CHECK (json_valid(record))
        ) STRICT""",
        f"CREATE INDEX {indexes['submissions_by_request']} "
        f"ON {tables['submissions']} (conversation_id, request_id)",
        f"CREATE INDEX {indexes['submissions_by_conversation']} "
        f"ON {tables['submissions']} (conversation_id, id)",
        f"CREATE INDEX {indexes['submissions_by_status']} ON {tables['submissions']} (status, id)",
        f"""CREATE TABLE {tables["documents"]} (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            family INTEGER NOT NULL CHECK (family IN (0, 1)),
            key_value TEXT NOT NULL,
            scope_kind TEXT NOT NULL CHECK (scope_kind IN ('session', 'conversation', 'task')),
            owner_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            retired_at INTEGER,
            record TEXT NOT NULL CHECK (json_valid(record))
        ) STRICT""",
        f"CREATE INDEX {indexes['documents_by_address']} "
        f"ON {tables['documents']} (kind, scope_kind, owner_id, family, key_value, created_at DESC, retired_at)",
        f"CREATE INDEX {indexes['documents_by_scope']} "
        f"ON {tables['documents']} (scope_kind, owner_id, id)",
        f"CREATE INDEX {indexes['documents_by_scope_kind']} "
        f"ON {tables['documents']} (scope_kind, owner_id, kind, id)",
        f"""CREATE TABLE {tables["document_revisions"]} (
            document_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('base', 'delta')),
            version INTEGER NOT NULL,
            content TEXT NOT NULL CHECK (json_valid(content)),
            PRIMARY KEY (document_id, seq)
        ) STRICT""",
        f"CREATE INDEX {indexes['document_revisions_by_kind']} "
        f"ON {tables['document_revisions']} (document_id, kind, seq DESC)",
    )
    return (SqliteMigration(version=1, statements=initial),)


SQLITE_MIGRATIONS: tuple[SqliteMigration, ...] = create_sqlite_migrations(DEFAULT_SQLITE_SCHEMA)

CURRENT_SQLITE_SCHEMA_VERSION = SQLITE_MIGRATIONS[-1].version if SQLITE_MIGRATIONS else 0


async def apply_sqlite_migrations(
    database: SqliteDatabase,
    migrations: tuple[SqliteMigration, ...] | None = None,
    schema: SqliteSchema | None = None,
) -> None:
    """原子地施加所有待办迁移（``migrations.ts:99-140``）。"""
    actual_migrations = SQLITE_MIGRATIONS if migrations is None else migrations
    actual_schema = DEFAULT_SQLITE_SCHEMA if schema is None else schema

    for index, migration in enumerate(actual_migrations):
        if migration.version != index + 1:
            raise RuntimeError(
                "Durable SQLite migrations must have contiguous versions starting at 1"
            )

    durable_schema = actual_schema.tables["durable_schema"]

    async def migrate(transaction: object) -> None:
        tx = transaction
        await tx.exec(  # type: ignore[attr-defined]
            f"""CREATE TABLE IF NOT EXISTS {durable_schema} (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL CHECK (version >= 0)
            ) STRICT"""
        )
        await tx.run(  # type: ignore[attr-defined]
            f"INSERT OR IGNORE INTO {durable_schema} (singleton, version) VALUES (1, 0)"
        )
        row = await tx.get(  # type: ignore[attr-defined]
            f"SELECT version FROM {durable_schema} WHERE singleton = 1"
        )
        if row is None:
            raise RuntimeError("Durable SQLite schema metadata is missing")
        current_version = actual_migrations[-1].version if actual_migrations else 0
        if int(row["version"]) > current_version:
            raise RuntimeError(
                f"Durable SQLite schema version {row['version']} is newer than supported "
                f"version {current_version}"
            )
        for migration in actual_migrations:
            if migration.version <= int(row["version"]):
                continue
            for statement in migration.statements:
                await tx.exec(statement)  # type: ignore[attr-defined]
            await tx.run(  # type: ignore[attr-defined]
                f"UPDATE {durable_schema} SET version = ? WHERE singleton = 1", migration.version
            )

    await database.transaction(migrate)
