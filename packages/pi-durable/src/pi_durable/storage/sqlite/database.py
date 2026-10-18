"""可移植的 SQLite 门面 —— 移植 ``durable/src/storage/sqlite/database.ts``（38 行）。

上游要求所有操作都是**异步**的，这样适配器可以在 harness 运行时之外执行；Python 的
``sqlite3`` 是同步的，本端用 ``async def`` 包一层以保持签名一致（与 ``env/node.py``
对 ``fs/promises`` 的处理同一取舍，见 PORTING.md 偏离 #16）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

__all__ = ["SqliteDatabase", "SqliteExecutor", "SqliteValue"]

_T = TypeVar("_T")

#: 可移植 SQLite 核心支持的值（``database.ts:2``）。
SqliteValue = None | int | float | str | bytes


class SqliteExecutor(Protocol):
    """数据库与其事务句柄共享的异步 SQL 操作（``database.ts:11-16``）。

    适配器可以按 SQL 文本缓存预编译语句，所以调用方**传绑定值**而不是拼字符串。
    """

    async def exec(self, sql: str) -> None: ...

    async def run(self, sql: str, *params: SqliteValue) -> None: ...

    async def get(self, sql: str, *params: SqliteValue) -> dict[str, object] | None: ...

    async def all(self, sql: str, *params: SqliteValue) -> list[dict[str, object]]: ...


class SqliteDatabase(SqliteExecutor, Protocol):
    """``SqliteStorage`` 所需的最小数据库门面（``database.ts:35-38``）。

    事务回调里的**所有**工作都必须用传进来的那个句柄；回调结束后该句柄即失效。适配器必须把
    无关操作与其它事务排队到该事务结束之后。

    回调失败时，适配器必须**先回滚、再以同一个错误**抛出；若回滚本身也失败，必须以
    **另一个**错误抛出（例如 :class:`ExceptionGroup`），这样调用方不会把回调的错误误当成
    「已保证回滚」。
    """

    async def transaction(self, callback: Callable[[SqliteExecutor], Awaitable[_T]]) -> _T: ...

    async def close(self) -> None: ...
