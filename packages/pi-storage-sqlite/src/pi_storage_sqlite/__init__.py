"""pi-storage-sqlite —— **已废弃**，请改用 ``pi_durable.storage.sqlite``。

迁移原因
--------
本包原对齐上游 ``@earendil-works/pi-session-backend-sqlite-node``
（``packages/session-backends/sqlite-node``）。该包已在 **v1.2.0 被整体删除**
（连同 ``session-backends``），且原来的 session/entry 数据模型在新的
``conversation``/``entry``/``task``/``submission``/``document`` 模型里
**没有对应物**：

- 旧 ``SqliteSessionRepo`` / ``SqliteSessionStorage`` 与新 ``Storage`` 接口
  （15 个方法）**唯一重合的方法名是 ``close``**。
- 旧 schema（``sessions``/``entries``/``scalar_values``/``list_values``/
  ``usage_ledger``）与新 schema（**9 表 / 16 索引 + 独立 ``durable_schema`` 版本表**）
  完全不同。

因此本包**不再提供旧 API**——保留「看起来能用但语义不同」的名字比直接删掉更危险。
新实现在 :mod:`pi_durable.storage.sqlite`，并已通过上游 ``storage-conformance``
套件（24/24，内存与 SQLite 两个后端各跑一遍）。

用法
----
::

    # 旧（已失效）
    from pi_storage_sqlite import SqliteSessionStorage

    # 新
    from pi_durable.storage.sqlite import (
        SqliteStorage,
        SqliteStorageOptions,
        open_node_sqlite_storage,
    )

迁移细节见 ``PORTING.md``。
"""

from __future__ import annotations

import warnings

__version__ = "0.85.1"
__upstream_ref__ = "无 —— 上游 @earendil-works/pi-session-backend-sqlite-node 已于 v1.2.0 删除"

from pi_durable.storage.sqlite import (  # noqa: E402
    CURRENT_SQLITE_SCHEMA_VERSION,
    DEFAULT_SQLITE_SCHEMA,
    SqliteDatabase,
    SqliteExecutor,
    SqliteStorage,
    SqliteStorageOptions,
)
from pi_durable.storage.sqlite.node import (  # noqa: E402
    open_node_sqlite_database,
    open_node_sqlite_storage,
)

_DEPRECATED_MESSAGE = (
    "pi-storage-sqlite 已废弃并向 pi-durable 合并。上游对应的 "
    "@earendil-works/pi-session-backend-sqlite-node 已于 v1.2.0 被删除，"
    "旧的数据模型在新实现中没有对应物，因此 `{name}` 无法转发。"
    "请改用 pi_durable.storage.sqlite（见 packages/pi-storage-sqlite/PORTING.md）。"
)

#: 旧公开名 —— 一律不转发，访问时给出可执行的迁移指引。
_REMOVED_NAMES = {
    "SqliteSessionRepo": "open_node_sqlite_storage / SqliteStorage",
    "SqliteSessionStorage": "open_node_sqlite_storage / SqliteStorage",
    "SqliteRunResult": "SqliteExecutor.run 的返回值",
    "open_database": "open_node_sqlite_database",
    "apply_migrations": "pi_durable.storage.sqlite.apply_sqlite_migrations",
}

__all__ = [
    "__version__",
    "__upstream_ref__",
    "CURRENT_SQLITE_SCHEMA_VERSION",
    "DEFAULT_SQLITE_SCHEMA",
    "SqliteDatabase",
    "SqliteExecutor",
    "SqliteStorage",
    "SqliteStorageOptions",
    "open_node_sqlite_database",
    "open_node_sqlite_storage",
]


def __getattr__(name: str) -> object:
    """把已移除的旧名字变成**带迁移指引的 AttributeError**，而不是静默 ImportError。"""
    if name in _REMOVED_NAMES:
        warnings.warn(
            f"`{name}` 在向 pi-durable 合并时被移除（旧数据模型无对应物）；"
            f"请改用 {_REMOVED_NAMES[name]}。",
            DeprecationWarning,
            stacklevel=2,
        )
        raise AttributeError(_DEPRECATED_MESSAGE.format(name=name))
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
