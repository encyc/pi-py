"""SQLite 后端的**真实文件**集成测试。

验收标准明确要求 sqlite 后端用真实文件测，而不是 ``:memory:``。一致性套件
（``tests/test_storage_conformance.py``）已经对每个用例开一个临时文件跑一遍；
本文件把「确实落在磁盘上」这件事单独钉成断言，作为可读的证据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.storage.sqlite.migrations import CURRENT_SQLITE_SCHEMA_VERSION
from pi_durable.storage.sqlite.node import open_node_sqlite_storage
from pi_durable.storage.sqlite.schema import DEFAULT_SQLITE_SCHEMA
from pi_durable.types import ROOT_CONVERSATION_ID


async def test_storage_writes_a_real_file_with_wal(tmp_path: Path) -> None:
    """数据库落在真实路径上，并启用了 WAL（上游 ``node.ts:194``）。"""
    path = tmp_path / "nested" / "durable.db"  # 父目录不存在：上游会 recursive mkdir

    storage = await open_node_sqlite_storage(str(path))
    try:
        await storage.commit([{"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}}], CTX)
        # 上游打开时设了 `PRAGMA journal_mode = WAL`（``node.ts:194``）。关闭时会
        # `wal_checkpoint(TRUNCATE)` 把 -wal 并回主库，所以要在**打开期间**断言。
        db = storage._database  # noqa: SLF001 - 用例要读 PRAGMA
        mode = await db.get("PRAGMA journal_mode")
        assert mode is not None and str(mode["journal_mode"]) == "wal"
        assert Path(f"{path}-wal").exists()  # noqa: ASYNC240 - 测试断言，非 IO
    finally:
        await storage.close(CTX)

    assert path.exists()
    assert path.stat().st_size > 0
    assert not Path(f"{path}-wal").exists()  # noqa: ASYNC240 - close() 做了 checkpoint


async def test_state_survives_reopen(tmp_path: Path) -> None:
    """关掉再打开：记录、ID 游标、提交序号都要跨重开保持。"""
    path = tmp_path / "durable.db"

    first = await open_node_sqlite_storage(str(path))
    await first.commit([{"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}}], CTX)
    entry_id = await first.mint_id()
    await first.commit(
        [
            {
                "type": "entry",
                "value": {
                    "id": entry_id,
                    "conversationId": ROOT_CONVERSATION_ID,
                    "kind": "user",
                    "data": {"text": "persisted"},
                },
            }
        ],
        CTX,
    )
    await first.close(CTX)

    second = await open_node_sqlite_storage(str(path))
    try:
        entry = await second.entry(entry_id, CTX)
        assert entry is not None
        assert entry["entry"]["data"] == {"text": "persisted"}
        # ID 游标跨重开继续，不会重新发出已用过的 ID
        assert await second.mint_id() == entry_id + 1
        page = await second.scan_entries({"conversationId": ROOT_CONVERSATION_ID}, 10, None, CTX)
        assert [item["id"] for item in page["items"]] == [entry_id]
    finally:
        await second.close(CTX)


async def test_schema_has_the_nine_tables_and_sixteen_indexes(tmp_path: Path) -> None:
    """schema 与上游一致：**9 张表 / 16 个索引**（``schema.ts:5-34``）。

    这条同时是对 :mod:`pi_durable.storage.sqlite.migrations` 的 DDL 的回归——
    表或索引少一个都会红。
    """
    path = tmp_path / "schema.db"
    storage = await open_node_sqlite_storage(str(path))
    db = storage._database  # noqa: SLF001 - 用例就是要直接查 sqlite_master
    try:
        tables = await db.all(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        names = {str(row["name"]) for row in tables}
        expected_tables = set(DEFAULT_SQLITE_SCHEMA.tables.values())
        assert expected_tables <= names, f"missing tables: {expected_tables - names}"
        assert (
            len([n for n in names if n.startswith("durable")]) == 2
        )  # durable_schema + durable_metadata

        indexes = await db.all(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        )
        index_names = {str(row["name"]) for row in indexes}
        expected_indexes = set(DEFAULT_SQLITE_SCHEMA.indexes.values())
        assert expected_indexes <= index_names, f"missing indexes: {expected_indexes - index_names}"
        assert len(expected_indexes) == 16
        assert len(expected_tables) == 9

        row = await db.get(
            f"SELECT version FROM {DEFAULT_SQLITE_SCHEMA.tables['durable_schema']} WHERE singleton = 1"
        )
        assert row is not None
        assert int(str(row["version"])) == CURRENT_SQLITE_SCHEMA_VERSION
    finally:
        await storage.close(CTX)


async def test_invalid_prefix_is_rejected(tmp_path: Path) -> None:
    """前缀校验：``sqlite_`` 是保留的，非法标识符要报错（``schema.ts:43-51``）。"""
    from pi_durable.storage.sqlite.schema import validate_sqlite_schema_prefix

    with pytest.raises(ValueError, match="reserved"):
        validate_sqlite_schema_prefix("sqlite_x")
    with pytest.raises(ValueError, match="Invalid SQLite schema prefix"):
        validate_sqlite_schema_prefix("1bad")


async def test_migrations_are_contiguous_from_one() -> None:
    from pi_durable.storage.sqlite.migrations import SQLITE_MIGRATIONS

    assert [migration.version for migration in SQLITE_MIGRATIONS] == list(
        range(1, len(SQLITE_MIGRATIONS) + 1)
    )
