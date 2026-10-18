"""转发壳测试：验证废弃包的转发行为与迁移指引。

本包已并入 ``pi-durable``（见 ``PORTING.md``）。这里测的不是存储行为
——存储行为由 ``pi_durable`` 的 ``storage-conformance`` 套件负责（24/24）。
这里测的是**壳本身**：新名字可转发、旧名字给出可执行的迁移指引。
"""

from __future__ import annotations

import warnings

import pytest


def test_import_and_metadata() -> None:
    import pi_storage_sqlite

    assert pi_storage_sqlite.__version__ == "0.85.1"
    # 上游对应包已删除，因此不再声称对齐某个上游版本。
    assert "删除" in pi_storage_sqlite.__upstream_ref__


def test_forwards_new_api_from_pi_durable() -> None:
    """新 API 应转发自 pi_durable.storage.sqlite，且是**同一个对象**。"""
    import pi_storage_sqlite as shell
    from pi_durable.storage import sqlite as upstream
    from pi_durable.storage.sqlite import node as upstream_node

    assert shell.SqliteStorage is upstream.SqliteStorage
    assert shell.SqliteStorageOptions is upstream.SqliteStorageOptions
    assert shell.SqliteDatabase is upstream.SqliteDatabase
    assert shell.SqliteExecutor is upstream.SqliteExecutor
    # ``open_node_*`` 对应上游 ``./storage/sqlite/node`` 子路径，不在 __init__ 里。
    assert shell.open_node_sqlite_storage is upstream_node.open_node_sqlite_storage
    assert shell.open_node_sqlite_database is upstream_node.open_node_sqlite_database


@pytest.mark.parametrize(
    "old_name",
    [
        "SqliteSessionRepo",
        "SqliteSessionStorage",
        "SqliteRunResult",
        "open_database",
        "apply_migrations",
    ],
)
def test_removed_names_raise_with_migration_hint(old_name: str) -> None:
    """旧名字必须**明确失败并给出迁移路径**，而不是静默提供语义不同的替代品。"""
    import pi_storage_sqlite

    with (
        pytest.warns(DeprecationWarning, match="pi-durable"),
        pytest.raises(AttributeError) as exc,
    ):
        getattr(pi_storage_sqlite, old_name)

    message = str(exc.value)
    assert old_name in message
    assert "pi_durable.storage.sqlite" in message
    assert "PORTING.md" in message


def test_unknown_attribute_raises_plain_attribute_error() -> None:
    """不属于「已移除名单」的名字应给普通 AttributeError，不误报废弃警告。"""
    import pi_storage_sqlite

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # 任何警告都会失败
        with pytest.raises(AttributeError, match="no attribute"):
            _ = pi_storage_sqlite.definitely_not_a_real_name


def test_removed_names_absent_from_all() -> None:
    """旧名字不得出现在 ``__all__`` 里（否则 ``import *`` 会静默带出坏名字）。"""
    import pi_storage_sqlite

    for old_name in pi_storage_sqlite._REMOVED_NAMES:
        assert old_name not in pi_storage_sqlite.__all__
