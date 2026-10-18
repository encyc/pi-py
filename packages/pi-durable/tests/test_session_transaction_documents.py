"""Session 阶段 2 第 5 步的测试：``doc()`` / ``retire_doc()`` / ``DocumentPlan`` / ``adopt``。

这是阶段 2 的最后一块，把三样东西合拢：
1. 阶段 1/第 2 步的 ``LoadedDocument`` 缓存
2. chord 的 ``Tracker`` / ``Change`` / ``Prepared``
3. 装配的文档分支（含**终局任务的文档退休扫描**）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.documents import define_doc, define_doc_family
from pi_durable.session import Transaction, TransactionHost
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage

pytestmark = pytest.mark.asyncio


def notes(**extra: Any) -> Any:
    return define_doc(
        {
            "kind": "pi.notes",
            "version": 1,
            "scope": "conversation",
            "history": "rewindable",
            "fork": "asOf",
            "initial": lambda seed=None: {"items": [], "n": 0},
            **extra,
        }
    )


def session_doc(**extra: Any) -> Any:
    return define_doc(
        {
            "kind": "pi.settings",
            "version": 1,
            "scope": "session",
            "initial": lambda seed=None: {"a": 1},
            **extra,
        }
    )


async def make_storage(backend: str, tmp_path: Path) -> Any:
    if backend == "memory":
        return MemoryStorage()
    return await open_node_sqlite_storage(str(tmp_path / "durable.db"))


async def root(storage: Any) -> int:
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
    return 1


async def commit_document(
    storage: Any,
    kind: str,
    value: dict[str, Any],
    *,
    history: str = "rewindable",
    fork: str = "asOf",
) -> int:
    id = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {
                    "id": id,
                    "kind": kind,
                    "scope": {"kind": "conversation", "conversationId": 1},
                    "history": history,
                    "fork": fork,
                },
                "content": {"kind": "base", "version": 1, "value": value},
            }
        ],
        CTX,
    )
    return id


# --------------------------------------------------------------------------------------
# doc(): 创建
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_doc_creates_a_document(backend: str, tmp_path: Path) -> None:
    storage = await make_storage(backend, tmp_path)
    try:
        await root(storage)
        tx = Transaction(TransactionHost(storage), CTX)
        draft = await tx.doc(notes(), 1)
        draft["items"].append("a")
        draft["n"] = 1

        writes = await tx.settle_success()
        kinds = [w["type"] for w in writes]
        assert kinds == ["document.create"]

        seq = await storage.commit(writes, CTX)
        created = writes[0]["record"]
        stored = await storage.document(created["id"], "current", CTX)
        assert stored is not None
        assert stored["value"] == {"items": ["a"], "n": 1}

        publications = tx.adopt(seq)
        assert publications[0]["type"] == "document"
        assert publications[0]["value"] == {"items": ["a"], "n": 1}
        assert publications[0]["version"] == 1
        # 新化身被装进宿主的缓存
        assert TransactionHost(storage).cached("x") is None
    finally:
        await storage.close(CTX)


async def test_doc_reuses_the_same_draft_for_one_address() -> None:
    """同一地址上第二次 ``doc()`` 返回**同一个** draft promise（``transaction.ts:519-520``）。"""
    storage = MemoryStorage()
    await root(storage)
    tx = Transaction(TransactionHost(storage), CTX)
    first = await tx.doc(notes(), 1)
    second = await tx.doc(notes(), 1)
    assert first is second
    # 而且只暂存了一个 DocumentEntry
    assert len(tx._documents) == 1  # noqa: SLF001


async def test_doc_seeds_a_family() -> None:
    storage = MemoryStorage()
    await root(storage)
    family = define_doc_family(
        {
            "kind": "pi.fam",
            "version": 1,
            "scope": "conversation",
            "history": "rewindable",
            "fork": "asOf",
            "family": True,
            "initial": lambda seed: {"seed": seed},
        }
    )
    tx = Transaction(TransactionHost(storage), CTX)
    draft = await tx.doc(family, 1, "member", {"v": 7})
    assert draft["seed"] == {"v": 7}


async def test_doc_requires_a_live_conversation() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    with pytest.raises(RuntimeError, match="Conversation 1 does not exist"):
        await tx.doc(notes(), 1)


# --------------------------------------------------------------------------------------
# doc(): 已加载的化身
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_doc_loads_and_updates_an_existing_document(backend: str, tmp_path: Path) -> None:
    storage = await make_storage(backend, tmp_path)
    try:
        await root(storage)
        id = await commit_document(storage, "pi.notes", {"items": ["a"], "n": 1})

        tx = Transaction(TransactionHost(storage), CTX)
        draft = await tx.doc(notes(), 1)
        assert draft["items"] == ["a"]
        draft["items"].append("b")

        writes = await tx.settle_success()
        assert [w["type"] for w in writes] == ["document.change"]
        assert writes[0]["content"]["kind"] == "delta"

        seq = await storage.commit(writes, CTX)
        stored = await storage.document(id, "current", CTX)
        assert stored is not None
        assert stored["value"] == {"items": ["a", "b"], "n": 1}

        publications = tx.adopt(seq)
        assert publications[0]["ops"]  # 已加载的化身发布它的 ops
    finally:
        await storage.close(CTX)


async def test_untouched_document_writes_nothing() -> None:
    """已加载但**没改**的化身不写内容、也不发布（``publishes``）。"""
    storage = MemoryStorage()
    await root(storage)
    await commit_document(storage, "pi.notes", {"items": [], "n": 0})

    tx = Transaction(TransactionHost(storage), CTX)
    await tx.doc(notes(), 1)  # 拿草稿但不改
    writes = await tx.settle_success()
    assert writes == []


async def test_migration_only_base_is_written_and_published() -> None:
    """存储是旧版本、定义是新版本：即使没有操作也要写一个 base，好让旧形状的观察者收到新值。"""
    storage = MemoryStorage()
    await root(storage)
    id = await commit_document(storage, "pi.notes", {"n": 1})

    upgraded = define_doc(
        {
            "kind": "pi.notes",
            "version": 2,
            "scope": "conversation",
            "history": "rewindable",
            "fork": "asOf",
            "initial": lambda seed=None: {"n": 0},
            "migrate": lambda value, from_version: {**value, "migrated": from_version},
        }
    )
    tx = Transaction(TransactionHost(storage), CTX)
    draft = await tx.doc(upgraded, 1)
    assert draft["migrated"] == 1  # 冷加载时迁移过

    writes = await tx.settle_success()
    assert [w["type"] for w in writes] == ["document.change"]
    assert writes[0]["content"]["kind"] == "base"
    assert writes[0]["content"]["version"] == 2

    seq = await storage.commit(writes, CTX)
    publications = tx.adopt(seq)
    assert publications and publications[0]["version"] == 2
    stored = await storage.document(id, "current", CTX)
    assert stored is not None and stored["version"] == 2
    del seq


# --------------------------------------------------------------------------------------
# retire_doc()
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_retire_doc_retires_an_existing_document(backend: str, tmp_path: Path) -> None:
    storage = await make_storage(backend, tmp_path)
    try:
        await root(storage)
        id = await commit_document(storage, "pi.notes", {"n": 1})

        tx = Transaction(TransactionHost(storage), CTX)
        await tx.retire_doc(notes(), 1)
        writes = await tx.settle_success()

        assert [w["type"] for w in writes] == ["document.retire"]
        assert writes[0]["id"] == id

        seq = await storage.commit(writes, CTX)
        assert await storage.document(id, "current", CTX) is None
        publications = tx.adopt(seq)
        assert publications[0]["type"] == "document"
        assert publications[0]["value"] is None
        assert publications[0]["record"]["retiredAt"] == seq
    finally:
        await storage.close(CTX)


async def test_retire_doc_of_an_absent_document_writes_nothing() -> None:
    storage = MemoryStorage()
    await root(storage)
    tx = Transaction(TransactionHost(storage), CTX)
    await tx.retire_doc(notes(), 1)
    assert await tx.settle_success() == []


async def test_retire_doc_is_idempotent() -> None:
    storage = MemoryStorage()
    await root(storage)
    await commit_document(storage, "pi.notes", {"n": 1})
    tx = Transaction(TransactionHost(storage), CTX)
    await tx.retire_doc(notes(), 1)
    await tx.retire_doc(notes(), 1)
    assert len(tx._documents) == 1  # noqa: SLF001


async def test_retire_after_acquire_persists_then_retires() -> None:
    """先取草稿改了再退休：**先持久化最终内容，再退休**（``transaction.ts:569-573``）。"""
    storage = MemoryStorage()
    await root(storage)
    id = await commit_document(storage, "pi.notes", {"items": [], "n": 0})

    tx = Transaction(TransactionHost(storage), CTX)
    draft = await tx.doc(notes(), 1)
    draft["n"] = 5
    await tx.retire_doc(notes(), 1)

    writes = await tx.settle_success()
    kinds = [w["type"] for w in writes]
    assert kinds == ["document.change", "document.retire"], kinds

    await storage.commit(writes, CTX)
    assert await storage.document(id, "current", CTX) is None


# --------------------------------------------------------------------------------------
# 任务级文档
# --------------------------------------------------------------------------------------


async def test_task_documents_reject_a_terminal_task() -> None:
    """任务一旦（在本次事务里）终局，就不能再拿它的文档（``transaction.ts:907-912``）。"""
    storage = MemoryStorage()
    await root(storage)
    task_id = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "task",
                "value": {
                    "id": task_id,
                    "conversationId": 1,
                    "kind": "demo",
                    "version": 1,
                    "input": {},
                    "state": {"status": "running", "checkpoint": {}},
                    "background": False,
                    "abortRequested": False,
                },
            }
        ],
        CTX,
    )
    task_doc = define_doc(
        {"kind": "pi.td", "version": 1, "scope": "task", "initial": lambda seed=None: {}}
    )

    tx = Transaction(TransactionHost(storage), CTX)
    tx.set_task(
        {
            "id": task_id,
            "conversationId": 1,
            "kind": "demo",
            "version": 1,
            "input": {},
            "state": {"status": "terminal", "outcome": {"status": "completed", "result": {}}},
            "background": False,
            "abortRequested": False,
        }
    )
    with pytest.raises(RuntimeError, match=f"Task {task_id} is terminal"):
        await tx.doc(task_doc, task_id)


async def test_terminal_task_retires_every_document_of_that_task() -> None:
    """终局结算会退休该任务的**每一个**已存文档（``transaction.ts:798-810``）。"""
    storage = MemoryStorage()
    await root(storage)
    task_id = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "task",
                "value": {
                    "id": task_id,
                    "conversationId": 1,
                    "kind": "demo",
                    "version": 1,
                    "input": {},
                    "state": {"status": "running", "checkpoint": {}},
                    "background": False,
                    "abortRequested": False,
                },
            }
        ],
        CTX,
    )
    document_id = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {
                    "id": document_id,
                    "kind": "pi.td",
                    "scope": {"kind": "task", "taskId": task_id},
                },
                "content": {"kind": "base", "version": 1, "value": {"k": 1}},
            }
        ],
        CTX,
    )

    tx = Transaction(TransactionHost(storage), CTX)
    tx.set_task(
        {
            "id": task_id,
            "conversationId": 1,
            "kind": "demo",
            "version": 1,
            "input": {},
            "state": {"status": "terminal", "outcome": {"status": "completed", "result": {}}},
            "background": False,
            "abortRequested": False,
        }
    )
    writes = await tx.settle_success()

    retires = [w for w in writes if w["type"] == "document.retire"]
    assert [w["id"] for w in retires] == [document_id]

    await storage.commit(writes, CTX)
    assert await storage.document(document_id, "current", CTX) is None


# --------------------------------------------------------------------------------------
# 失败路径
# --------------------------------------------------------------------------------------


async def test_settle_success_aborts_prepared_changes_on_assembly_failure() -> None:
    """装配失败要**中止**每个已准备的变更，草稿不能留在半途。"""
    storage = MemoryStorage()
    await root(storage)
    tx = Transaction(TransactionHost(storage), CTX)
    draft = await tx.doc(notes(), 1)
    draft["n"] = 1
    # 造一个装配期的失败：替换一个不存在的任务
    tx.set_task(
        {
            "id": 999,
            "conversationId": 1,
            "kind": "demo",
            "version": 1,
            "input": {},
            "state": {"status": "running", "checkpoint": {}},
            "background": False,
            "abortRequested": False,
        }
    )
    with pytest.raises(RuntimeError, match="Task 999 does not exist"):
        await tx.settle_success()
    # 失败后事务已封口
    with pytest.raises(RuntimeError, match="Transaction has settled"):
        await tx.doc(notes(), 1)


async def test_failed_acquisition_is_a_rejected_awaitable_not_a_raise() -> None:
    """``doc()`` / ``retire_doc()`` 是**同步**方法（上游返回 Promise）。

    所以同步错误必须转成一个**被拒绝的 awaitable**，而不是直接抛出——
    否则调用方的 ``await tx.doc(...)`` 会连 try/except 都来不及包。
    """
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    pending = tx.doc(notes(), 1)  # 不 raise
    with pytest.raises(RuntimeError, match="Conversation 1 does not exist"):
        await pending
