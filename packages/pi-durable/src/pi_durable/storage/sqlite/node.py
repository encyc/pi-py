"""stdlib ``sqlite3`` 适配器 —— 移植 ``durable/src/storage/sqlite/node.ts``（216 行）。

上游用 node:sqlite 的 ``DatabaseSync`` + ``StatementSync``，并把所有操作排进一条
**串行操作队列**（``node.ts:31-120``）以模拟异步。Python 的 ``sqlite3`` 是同步的，
本端用一把可重入的串行锁 + ``asyncio.to_thread`` 之外的直接调用即可满足同一契约：
**一次只跑一个操作、事务期间排斥其它操作**。

PRAGMA 与上游一致（``node.ts:194-196``）：``journal_mode = WAL``、``synchronous = NORMAL``、
``wal_autocheckpoint``（默认 1,000 页）。``busy_timeout`` 默认 5,000 ms（``node.ts:21``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from pi_durable.storage.sqlite.database import SqliteDatabase, SqliteExecutor, SqliteValue
from pi_durable.storage.sqlite.migrations import apply_sqlite_migrations
from pi_durable.storage.sqlite.schema import SqliteSchema, validate_sqlite_schema_prefix
from pi_durable.storage.sqlite.storage import SqliteStorage

__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_WAL_AUTO_CHECKPOINT_PAGES",
    "NodeSqliteDatabase",
    "NodeSqliteDatabaseOptions",
    "open_node_sqlite_database",
    "open_node_sqlite_storage",
]

_T = TypeVar("_T")

#: ``node.ts:20``。
DEFAULT_WAL_AUTO_CHECKPOINT_PAGES = 1_000
#: ``node.ts:21``。
DEFAULT_BUSY_TIMEOUT_MS = 5_000


class NodeSqliteDatabaseOptions:
    """``node.ts`` 的 ``NodeSqliteDatabaseOptions``。"""

    def __init__(
        self,
        *,
        wal_auto_checkpoint_pages: int = DEFAULT_WAL_AUTO_CHECKPOINT_PAGES,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self.wal_auto_checkpoint_pages = wal_auto_checkpoint_pages
        self.busy_timeout_ms = busy_timeout_ms


class _NodeSqliteTransaction(SqliteExecutor):
    """事务句柄：与数据库共用连接，但**不经那把排他锁**（``node.ts`` 的 `NodeSqliteTransaction`）。

    这正是上游的契约要求——「事务里的所有工作都必须用这个句柄」（``database.ts:23-27``）：
    如果事务内的调用再去抢同一把锁，就会自锁。句柄在回调结束后失效。
    """

    __slots__ = ("_connection", "_owner", "_valid")

    def __init__(self, connection: sqlite3.Connection, owner: NodeSqliteDatabase) -> None:
        self._connection = connection
        self._owner = owner
        self._valid = True

    def _invalidate(self) -> None:
        self._valid = False

    def _check(self) -> None:
        if not self._valid:
            raise RuntimeError("SQLite transaction handle is no longer valid")

    async def exec(self, sql: str) -> None:
        self._check()
        self._owner._exec_direct(self._connection, sql)

    async def run(self, sql: str, *params: SqliteValue) -> None:
        self._check()
        self._connection.execute(sql, params)

    async def get(self, sql: str, *params: SqliteValue) -> dict[str, object] | None:
        self._check()
        row = self._connection.execute(sql, params).fetchone()
        return None if row is None else dict(row)

    async def all(self, sql: str, *params: SqliteValue) -> list[dict[str, object]]:
        self._check()
        return [dict(row) for row in self._connection.execute(sql, params).fetchall()]


class NodeSqliteDatabase(SqliteDatabase):
    """stdlib ``sqlite3`` 的异步门面（``node.ts:135-…`` 的 ``NodeSqliteDatabase``）。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._lock = asyncio.Lock()
        self._closed = False

    # ---- SqliteExecutor ---------------------------------------------------------------

    @staticmethod
    def _exec_direct(connection: sqlite3.Connection, sql: str) -> None:
        """``exec`` 跑 SQL 文本，可以含多条语句（``database.ts:12``）。"""
        connection.executescript(sql) if ";" in sql.strip().rstrip(";") else connection.execute(sql)

    async def exec(self, sql: str) -> None:
        await self._run(lambda: self._exec_direct(self._connection, sql))

    async def run(self, sql: str, *params: SqliteValue) -> None:
        await self._run(lambda: self._connection.execute(sql, params))

    async def get(self, sql: str, *params: SqliteValue) -> dict[str, object] | None:
        def work() -> dict[str, object] | None:
            cursor = self._connection.execute(sql, params)
            row = cursor.fetchone()
            return None if row is None else dict(row)

        return await self._run(work)

    async def all(self, sql: str, *params: SqliteValue) -> list[dict[str, object]]:
        def work() -> list[dict[str, object]]:
            cursor = self._connection.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

        return await self._run(work)

    # ---- SqliteDatabase ---------------------------------------------------------------

    async def transaction(self, callback: Callable[[SqliteExecutor], Awaitable[_T]]) -> _T:
        """一次事务（``database.ts:18-34`` 的契约）。

        回调失败时**先回滚、再以同一个错误抛出**；回滚本身也失败时，抛出的错误里同时带上
        两者（Python 的 :class:`ExceptionGroup`），这样调用方不会把回调的错误误当成
        「已保证回滚」。事务期间用同一把锁排斥其它操作。
        """
        async with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            handle = _NodeSqliteTransaction(self._connection, self)
            try:
                result = await callback(handle)
            except BaseException as error:
                try:
                    self._connection.rollback()
                except Exception as rollback_error:
                    message = "SQLite transaction failed and rollback failed"
                    if isinstance(error, Exception):
                        raise ExceptionGroup(message, [error, rollback_error]) from error
                    raise BaseExceptionGroup(message, [error, rollback_error]) from error
                raise
            finally:
                handle._invalidate()
            try:
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._lock:
            try:
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self._connection.close()

    async def _run(self, operation: Callable[[], _T]) -> _T:
        if self._closed:
            raise RuntimeError("SQLite database is closed")
        async with self._lock:
            return operation()


async def open_node_sqlite_database(
    path: str,
    options: NodeSqliteDatabaseOptions | None = None,
) -> NodeSqliteDatabase:
    """打开并配置一个 stdlib ``sqlite3`` 门面（``node.ts:183-206``）。"""
    resolved = options or NodeSqliteDatabaseOptions()
    if path != ":memory:":
        # 上游 ``mkdir(dirname(path), {recursive: true})``（``node.ts:190``）。
        # 打开数据库前的一次本地 mkdir，不是需要让出事件循环的 IO——
        # 与 ``env/node.py`` 里「stdlib 没有 fs/promises 对等物」同一取舍（PORTING.md 偏离 #16）。
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    connection = sqlite3.connect(
        path,
        timeout=resolved.busy_timeout_ms / 1000,
        isolation_level=None,  # 自己做 BEGIN/COMMIT
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.isolation_level = None
    adapter = NodeSqliteDatabase(connection)
    try:
        await adapter.exec("PRAGMA journal_mode = WAL")
        await adapter.exec("PRAGMA synchronous = NORMAL")
        await adapter.exec(f"PRAGMA wal_autocheckpoint = {resolved.wal_auto_checkpoint_pages}")
        return adapter
    except BaseException:
        with contextlib.suppress(Exception):
            # 保留最初的配置失败，而不是关闭时的失败（``node.ts:199-203``）。
            await adapter.close()
        raise


async def open_node_sqlite_storage(
    path: str,
    options: NodeSqliteDatabaseOptions | None = None,
    *,
    prefix: str | None = None,
    schema: SqliteSchema | None = None,
) -> SqliteStorage:
    """打开或创建文件型 durable 存储（``node.ts:208-215``）。"""
    if prefix is not None:
        validate_sqlite_schema_prefix(prefix)
    database = await open_node_sqlite_database(path, options)
    await apply_sqlite_migrations(database, schema=schema)
    storage = SqliteStorage(database, schema)
    return storage


# 保留：上游 NodeSqliteStorageOptions 还带 prefix，已在 open_node_sqlite_storage 上暴露
_Any = Any
