"""SQLite 数据库封装 + migration 执行。

对应上游 ``packages/storage/sqlite-node/src/sqlite/types.ts``（接口）+
``index.ts``（node:sqlite 适配）+ ``migrations.ts``（migration 执行器）。

用 stdlib ``sqlite3`` 替代 node:sqlite。同步 API（sqlite3 本身是同步的），
对应上游的 DatabaseSync。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

#: 初始 schema SQL（作为包资源嵌入）。
_MIGRATION_SQL = (Path(__file__).parent / "migrations.sql").read_text(encoding="utf-8")


class SqliteRunResult:
    """执行结果。"""

    def __init__(self, changes: int, last_insert_rowid: int | None = None) -> None:
        self.changes = changes
        self.last_insert_rowid = last_insert_rowid


class SqliteDatabase:
    """SQLite 数据库封装。同步 API。

    对应上游 ``SqliteDatabase`` 接口。用 ``sqlite3.Connection`` 实现。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    def exec(self, sql: str) -> None:
        """执行多条 SQL（无参数）。对应 ``executescript``。"""
        self._conn.executescript(sql)

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        """执行单条带参数 SQL。"""
        return self._conn.execute(sql, params)

    def query_one(self, sql: str, params: Any = ()) -> dict[str, Any] | None:
        """查询单行。"""
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def query_all(self, sql: str, params: Any = ()) -> list[dict[str, Any]]:
        """查询多行。"""
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def run(self, sql: str, params: Any = ()) -> SqliteRunResult:
        """执行写操作，返回 changes/lastrowid。"""
        cursor = self._conn.execute(sql, params)
        # cursor.rowcount 是该语句影响的行数（DELETE/UPDATE/INSERT）
        changes = cursor.rowcount if cursor.rowcount >= 0 else 0
        return SqliteRunResult(
            changes=changes,
            last_insert_rowid=cursor.lastrowid if cursor.lastrowid else None,
        )

    def transaction(self, fn: Any) -> Any:
        """在事务中执行 fn。失败自动回滚。"""
        self._conn.execute("BEGIN")
        try:
            result = fn()
            self._conn.execute("COMMIT")
            return result
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self._conn.close()


def configure_database(db: SqliteDatabase) -> None:
    """配置 PRAGMA（与上游一致）。"""
    db.exec("PRAGMA journal_mode=WAL")
    db.exec("PRAGMA synchronous=FULL")
    db.exec("PRAGMA busy_timeout=5000")


def ensure_migrations_table(db: SqliteDatabase) -> None:
    """创建 migrations 记录表。"""
    db.exec("CREATE TABLE IF NOT EXISTS migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")


def apply_migrations(db: SqliteDatabase) -> None:
    """执行未应用的 migration。"""
    ensure_migrations_table(db)
    applied = {
        row["id"] for row in db.query_all("SELECT id FROM migrations ORDER BY applied_at, id")
    }
    migration_id = "001_initial.sql"
    if migration_id in applied:
        return

    def _apply() -> None:
        db.exec(_MIGRATION_SQL)
        db.run(
            "INSERT INTO migrations (id, applied_at) VALUES (?, ?)",
            (migration_id, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )

    db.transaction(_apply)


def open_database(path: str) -> SqliteDatabase:
    """打开/创建数据库：确保目录 → 打开 → 配置 PRAGMA → 执行 migration。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    db = SqliteDatabase(conn)
    configure_database(db)
    apply_migrations(db)
    return db


__all__ = [
    "SqliteRunResult",
    "SqliteDatabase",
    "configure_database",
    "ensure_migrations_table",
    "apply_migrations",
    "open_database",
]
