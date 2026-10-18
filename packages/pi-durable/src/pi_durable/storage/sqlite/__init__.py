"""SQLite 存储后端 —— ``durable/src/storage/sqlite/``。

公开面与上游一致：``SqliteStorage``、迁移与 schema 标识符，以及 Node 侧的
``open_node_sqlite_storage`` / ``open_node_sqlite_database``。

``cloudflare.ts``（192 行）**明确排除**：它依赖 Cloudflare Durable Object 的
``ctx.storage``，Python 无对等物（见 ``PORTING.md``）。
"""

from __future__ import annotations

from pi_durable.storage.sqlite.database import SqliteDatabase, SqliteExecutor, SqliteValue
from pi_durable.storage.sqlite.migrations import (
    CURRENT_SQLITE_SCHEMA_VERSION,
    SQLITE_MIGRATIONS,
    SqliteMigration,
    apply_sqlite_migrations,
)
from pi_durable.storage.sqlite.schema import (
    DEFAULT_SQLITE_SCHEMA,
    SqliteSchema,
    create_sqlite_schema,
    validate_sqlite_schema_prefix,
)
from pi_durable.storage.sqlite.storage import SqliteStorage, SqliteStorageOptions

__all__ = [
    "CURRENT_SQLITE_SCHEMA_VERSION",
    "DEFAULT_SQLITE_SCHEMA",
    "SQLITE_MIGRATIONS",
    "SqliteDatabase",
    "SqliteExecutor",
    "SqliteMigration",
    "SqliteSchema",
    "SqliteStorage",
    "SqliteStorageOptions",
    "SqliteValue",
    "apply_sqlite_migrations",
    "create_sqlite_schema",
    "validate_sqlite_schema_prefix",
]
