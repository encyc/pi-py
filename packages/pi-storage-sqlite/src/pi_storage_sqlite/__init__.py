"""pi-storage-sqlite: Python port of @earendil-works/pi-storage-sqlite-node.

基于 stdlib ``sqlite3`` 的 agent 会话存储后端。

对应上游 ``packages/storage/sqlite-node``（TypeScript，使用 node:sqlite）。
"""

from __future__ import annotations

__version__ = "0.81.1"
__upstream_ref__ = "earendil-works/pi@v0.81.1"

from .database import SqliteDatabase, SqliteRunResult, apply_migrations, open_database
from .storage import SqliteSessionRepo, SqliteSessionStorage

__all__ = [
    "__version__",
    "__upstream_ref__",
    "SqliteDatabase",
    "SqliteRunResult",
    "open_database",
    "apply_migrations",
    "SqliteSessionRepo",
    "SqliteSessionStorage",
]
