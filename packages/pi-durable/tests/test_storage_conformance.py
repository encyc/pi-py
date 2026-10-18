"""``Storage`` 一致性套件跑各后端 —— 上游 ``testing/storage-conformance.ts`` 的 24 条用例。

上游 ``docs/spec.md`` §10 末句：**「The semantic conformance suite covers memory, SQLite, and
JSONL.」** 本文件对已移植的后端各跑一遍同一套用例——用例体与名字来自上游，不含本仓自创断言。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from pi_durable.storage.memory import MemoryStorage
from pi_durable.testing import storage_conformance_cases
from pi_durable.testing.runner import conformance_ids
from pi_durable.types import Storage

# --------------------------------------------------------------------------------------
# 各后端提供 with_storage
# --------------------------------------------------------------------------------------


async def _with_memory(use: Callable[[Storage], Awaitable[None]]) -> None:
    """内存后端：每个用例一个全新的实例。"""
    await use(MemoryStorage())


async def _with_sqlite(use: Callable[[Storage], Awaitable[None]]) -> None:
    """SQLite 后端：**真实文件**（不是 ``:memory:``），每个用例一个新文件。

    用临时文件而非内存库，是为了让用例真正走过磁盘路径：``STRICT`` 表、部分索引、
    ``json_valid`` CHECK、以及序列化往返都在其中。
    """
    import tempfile

    from pi_durable.storage.sqlite.node import open_node_sqlite_storage

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "durable.db"
        storage = await open_node_sqlite_storage(str(path))
        try:
            await use(storage)
        finally:
            await storage.close(_CONTEXT)


from pi_chord.context import BACKGROUND_CONTEXT as _CONTEXT  # noqa: E402

MEMORY_CASES = storage_conformance_cases(_with_memory)
SQLITE_CASES = storage_conformance_cases(_with_sqlite)


# --------------------------------------------------------------------------------------
# 内存后端
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", MEMORY_CASES, ids=conformance_ids(MEMORY_CASES))
async def test_memory_storage_conformance(case: object) -> None:
    await case.run()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# SQLite 后端（真实文件）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", SQLITE_CASES, ids=conformance_ids(SQLITE_CASES))
async def test_sqlite_storage_conformance(case: object) -> None:
    await case.run()  # type: ignore[attr-defined]
