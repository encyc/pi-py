"""Session 阶段 2 第 3 步的测试：写入暂存与装配。

* ``create_conversation`` / ``create_root_conversation`` / ``fork_conversation``
* ``append_entry`` / ``create_submission``
* ``settle_success`` 装配原子批（含 **装配期解析提交变更**）
* ``staged_conversations`` / ``staged_tasks``

第 4–5 步（任务状态机、``doc()`` / ``DocumentPlan``）未实现，本文件只验证「装配遇到它们
必须**报错**而不是静默丢写入」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.entries import UserEntry
from pi_durable.session import Transaction, TransactionHost, TransactionScope
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage
from pi_durable.types import ROOT_CONVERSATION_ID

pytestmark = pytest.mark.asyncio


async def make_storage(backend: str, tmp_path: Path) -> Any:
    if backend == "memory":
        return MemoryStorage()
    return await open_node_sqlite_storage(str(tmp_path / "durable.db"))


async def add_task(
    storage: Any, *, status: str = "pending", abort: bool = False, conversation_id: int = 1
) -> int:
    task_id = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "task",
                "value": {
                    "id": task_id,
                    "conversationId": conversation_id,
                    "kind": "test.task",
                    "version": 1,
                    "input": {},
                    "state": {"status": status, "checkpoint": {}},
                    "background": False,
                    "abortRequested": abort,
                },
            }
        ],
        CTX,
    )
    return task_id


# --------------------------------------------------------------------------------------
# create_conversation
# --------------------------------------------------------------------------------------


async def test_create_root_conversation_uses_the_reserved_id() -> None:
    tx = Transaction(TransactionHost(MemoryStorage()), CTX)
    root = await tx.create_root_conversation()
    assert root == {"id": ROOT_CONVERSATION_ID}
    assert tx.staged_conversations() == [{"id": ROOT_CONVERSATION_ID}]


async def test_create_ownerless_conversation() -> None:
    tx = Transaction(TransactionHost(MemoryStorage()), CTX)
    record = await tx.create_conversation({"kind": "ownerless"})
    assert "owner" not in record
    assert "parent" not in record
    assert record["id"] >= 2


async def test_create_conversation_owned_by_a_task() -> None:
    storage = MemoryStorage()
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
    task_id = await add_task(storage)

    tx = Transaction(TransactionHost(storage), CTX)
    record = await tx.create_conversation({"kind": "task", "taskId": task_id})
    assert record["owner"] == {"conversationId": 1, "taskId": task_id}


async def test_create_conversation_rejects_a_missing_owner_task() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    with pytest.raises(RuntimeError, match="owner task 999 does not exist"):
        await tx.create_conversation({"kind": "task", "taskId": 999})


# --------------------------------------------------------------------------------------
# append_entry
# --------------------------------------------------------------------------------------


async def test_append_entry_head_self_becomes_its_own_id() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    await tx.create_root_conversation()
    entry = await tx.append_entry(1, {"kind": "pi.reset", "head": "self"})
    assert entry["head"] == entry["id"]


async def test_append_entry_keeps_an_explicit_head_and_omits_an_absent_one() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    await tx.create_root_conversation()
    first = await tx.append_entry(1, {"kind": "pi.user"})
    assert "head" not in first  # 上游用 omitUndefinedProperties 把整个键省掉
    second = await tx.append_entry(1, {"kind": "pi.system", "head": first["id"]})
    assert second["head"] == first["id"]


async def test_append_entry_takes_its_kind_from_the_token() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    await tx.create_root_conversation()
    entry = await tx.append_entry(1, {"data": {"text": "hi"}}, UserEntry)
    assert entry["kind"] == "pi.user"


async def test_append_entry_stamps_by_task_id_from_scope() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX, TransactionScope(task_id=42))
    await tx.create_root_conversation()
    assert (await tx.append_entry(1, {"kind": "pi.user"}))["byTaskId"] == 42

    plain = Transaction(TransactionHost(MemoryStorage()), CTX)
    await plain.create_root_conversation()
    assert "byTaskId" not in (await plain.append_entry(1, {"kind": "pi.user"}))


async def test_append_entry_requires_a_live_or_created_conversation() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    with pytest.raises(RuntimeError, match="Conversation 7 does not exist"):
        await tx.append_entry(7, {"kind": "pi.user"})

    # 本事务刚创建的会话可以直接追加
    created = await tx.create_conversation({"kind": "ownerless"})
    entry = await tx.append_entry(created["id"], {"kind": "pi.user"})
    assert entry["conversationId"] == created["id"]


# --------------------------------------------------------------------------------------
# create_submission
# --------------------------------------------------------------------------------------


async def test_create_submission_is_raw() -> None:
    """裸提交：不施加准入规则（``transaction.ts:430-438``）。"""
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    await tx.create_root_conversation()
    record = await tx.create_submission(
        {"conversationId": 1, "requestId": "r1", "type": "input", "status": "queued"}
    )
    assert record["status"] == "queued"
    assert record["id"] >= 2


async def test_create_submission_requires_a_conversation() -> None:
    tx = Transaction(TransactionHost(MemoryStorage()), CTX)
    with pytest.raises(RuntimeError, match="Conversation 9 does not exist"):
        await tx.create_submission(
            {"conversationId": 9, "requestId": "r", "type": "input", "status": "queued"}
        )


# --------------------------------------------------------------------------------------
# settle_success 装配
# --------------------------------------------------------------------------------------


async def test_settle_success_assembles_conversations_entries_and_submissions() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    await tx.create_root_conversation()
    entry = await tx.append_entry(1, {"kind": "pi.user"})
    submission = await tx.create_submission(
        {"conversationId": 1, "requestId": "r1", "type": "input", "status": "queued"}
    )

    writes = await tx.settle_success()

    assert [w["type"] for w in writes] == ["conversation", "entry", "submission"]
    assert writes[1]["value"]["id"] == entry["id"]  # type: ignore[index]
    assert writes[2]["value"]["id"] == submission["id"]  # type: ignore[index]


async def test_submission_changes_resolve_during_assembly() -> None:
    """**第 3 步最关键的一条**：``place_submission`` 的效果在装配期才落到记录上。

    调用 ``place_submission`` 时记录还是 ``queued``；装配后写出去的是 ``placed``。
    它也因此**不是**调用方的表读取，可以在第一次表写入之后调用（``transaction.ts:442-445``）。
    """
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    await tx.create_root_conversation()
    entry = await tx.append_entry(1, {"kind": "pi.user"})
    submission = await tx.create_submission(
        {"conversationId": 1, "requestId": "r1", "type": "input", "status": "queued"}
    )
    tx.place_submission(submission["id"], entry["id"])

    writes = await tx.settle_success()

    staged = [w for w in writes if w["type"] == "submission"][0]
    assert staged["value"]["status"] == "placed"  # type: ignore[index]
    assert staged["value"]["entry"] == entry["id"]  # type: ignore[index]


async def test_settlement_against_a_committed_submission() -> None:
    """针对**已提交**记录结算：装配期回退到存储读取（``transaction.ts:823``）。"""
    storage = MemoryStorage()
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
    submission_id = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "submission",
                "value": {
                    "id": submission_id,
                    "conversationId": 1,
                    "requestId": "r1",
                    "type": "write",
                    "status": "queued",
                },
            }
        ],
        CTX,
    )

    tx = Transaction(TransactionHost(storage), CTX)
    tx.settle_submission(submission_id, {"status": "unanswered", "reason": "closed"})
    writes = await tx.settle_success()

    staged = [w for w in writes if w["type"] == "submission"][0]
    assert staged["value"]["status"] == "unanswered"  # type: ignore[index]
    assert staged["value"]["reason"] == "closed"  # type: ignore[index]


async def test_settle_success_rejects_an_unknown_submission() -> None:
    tx = Transaction(TransactionHost(MemoryStorage()), CTX)
    tx.settle_submission(999, {"status": "unanswered", "reason": "r"})
    with pytest.raises(RuntimeError, match="Submission 999 does not exist"):
        await tx.settle_success()


async def test_settle_success_seals_the_transaction() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    await tx.create_root_conversation()
    await tx.settle_success()

    with pytest.raises(RuntimeError, match="Transaction has settled"):
        await tx.create_conversation({"kind": "ownerless"})
    with pytest.raises(RuntimeError, match="Transaction has settled"):
        await tx.append_entry(1, {"kind": "pi.user"})


async def test_settle_success_refuses_pending_operations() -> None:
    """回调在未完成的 Tx 操作之前就结算了——必须拒绝（``transaction.ts:677-681``）。"""
    import asyncio

    storage = MemoryStorage()
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
    tx = Transaction(TransactionHost(storage), CTX)

    # 一个**会结束**的未决操作。上游的 `Promise.allSettled(this.#pendingOperations)` 语义就是
    # 「先等它们全部落定，再报错」——所以这里不能用一个永不落定的 Future，
    # 否则 settle_success 会一直等下去（这正是上游的行为，不是 bug）。
    pending = asyncio.ensure_future(asyncio.sleep(0))
    tx._pending_operations.add(pending)  # noqa: SLF001 - 模拟一个尚未结束的操作
    with pytest.raises(RuntimeError, match="settled before its pending Tx operations"):
        await tx.settle_success()
    assert pending.done()


async def test_staged_views() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    await tx.create_root_conversation()
    await tx.create_conversation({"kind": "ownerless"})

    assert [c["id"] for c in tx.staged_conversations()] == [ROOT_CONVERSATION_ID, 2]
    # 第 4 步（任务写入）未实现，所以恒为空——方法本身已完整
    assert tx.staged_tasks() == []


# --------------------------------------------------------------------------------------
# 拥有者校验（装配期）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["terminal", "completing"])
async def test_settle_rejects_an_owner_that_is_ending(status: str) -> None:
    """一个任务**不能**在结束自己的那次提交里创建被拥有的工作（spec §5.5）。"""
    storage = MemoryStorage()
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
    task_id = await add_task(storage, status=status)

    tx = Transaction(TransactionHost(storage), CTX)
    await tx.create_conversation({"kind": "task", "taskId": task_id})
    with pytest.raises(RuntimeError, match=f"is {status}"):
        await tx.settle_success()


async def test_settle_rejects_an_abort_marked_owner() -> None:
    storage = MemoryStorage()
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
    task_id = await add_task(storage, abort=True)

    tx = Transaction(TransactionHost(storage), CTX)
    await tx.create_conversation({"kind": "task", "taskId": task_id})
    with pytest.raises(RuntimeError, match="is abort-marked"):
        await tx.settle_success()


# --------------------------------------------------------------------------------------
# 第 4–5 步未实现：必须**报错**，不能静默丢写入
# --------------------------------------------------------------------------------------


async def test_fork_copies_assemble_into_document_copy_writes() -> None:
    """``fork_conversation`` 暂存的分叉文档副本会装配成 ``document.copy`` 写入。

    ⚠️ 这条测试**曾经**断言相反的事（第 5 步未实现时 ``settle_success`` 必须报错）。
    第 5 步落地后它翻成正向：副本**进到了批里**。

    保留它的原因：静默丢掉这些副本会让分叉出来的子会话**少一批文档**，
    所以「副本确实进了批」这件事值得有一条测试盯着。
    """
    storage = MemoryStorage()
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
    # 文档必须先落：分叉点的 as-of 读取的是**条目所在提交**的 seq，
    # 在那之前不存在的文档当然选不中（与阶段 1 的 fork 用例同一条语义）。
    document_id = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {
                    "id": document_id,
                    "kind": "pi.asof",
                    "scope": {"kind": "conversation", "conversationId": 1},
                    "history": "rewindable",
                    "fork": "asOf",
                },
                "content": {"kind": "base", "version": 1, "value": {"n": 1}},
            }
        ],
        CTX,
    )
    entry_id = await storage.mint_id()
    entry_seq = await storage.commit(
        [{"type": "entry", "value": {"id": entry_id, "conversationId": 1, "kind": "pi.user"}}], CTX
    )

    tx = Transaction(TransactionHost(storage), CTX)
    record = await tx.fork_conversation(1, entry_id, {"kind": "ownerless"})
    assert record["parent"] == {"conversationId": 1, "at": entry_id}

    writes = await tx.settle_success()

    kinds = [w["type"] for w in writes]
    assert kinds == ["conversation", "document.copy"], kinds
    copy = writes[1]
    assert copy["record"]["scope"] == {"kind": "conversation", "conversationId": record["id"]}
    # as-of 组钉在**条目所在提交**的 seq 上
    assert copy["source"] == {"id": document_id, "at": entry_seq}

    # 整批提交后子会话真的有这份文档
    seq = await storage.commit(writes, CTX)
    copied = await storage.document(copy["record"]["id"], "current", CTX)
    assert copied is not None
    assert copied["value"] == {"n": 1}

    publications = tx.adopt(seq)
    assert "document.copy" in [p["type"] for p in publications]


async def test_fork_transaction_cannot_change_its_source_documents() -> None:
    """分叉事务里改「分叉来源」的文档必须报错（``transaction.ts:882-901``）。"""
    from pi_durable.documents import define_doc

    storage = MemoryStorage()
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
    document_id = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {
                    "id": document_id,
                    "kind": "pi.asof",
                    "scope": {"kind": "conversation", "conversationId": 1},
                    "history": "rewindable",
                    "fork": "asOf",
                },
                "content": {"kind": "base", "version": 1, "value": {"n": 1}},
            }
        ],
        CTX,
    )
    entry_id = await storage.mint_id()
    await storage.commit(
        [{"type": "entry", "value": {"id": entry_id, "conversationId": 1, "kind": "pi.user"}}], CTX
    )

    tx = Transaction(TransactionHost(storage), CTX)
    await tx.fork_conversation(1, entry_id, {"kind": "ownerless"})

    # 分叉来源的**原始**文档地址上再取一个草稿并改它 → 装配时必须被拒
    source_token = define_doc(
        {
            "kind": "pi.asof",
            "version": 1,
            "scope": "conversation",
            "history": "rewindable",
            "fork": "asOf",
            "initial": lambda seed: {"n": 0},
        }
    )
    draft = await tx.doc(source_token, 1)
    draft["n"] = 99
    with pytest.raises(RuntimeError, match="Cannot change fork source document"):
        await tx.settle_success()


async def test_fork_without_copies_still_assembles() -> None:
    """没有可复制文档的分叉可以正常装配——校验只在**真有**副本时拦。"""
    storage = MemoryStorage()
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
    entry_id = await storage.mint_id()
    await storage.commit(
        [{"type": "entry", "value": {"id": entry_id, "conversationId": 1, "kind": "pi.user"}}], CTX
    )

    tx = Transaction(TransactionHost(storage), CTX)
    await tx.fork_conversation(1, entry_id, {"kind": "ownerless"})
    writes = await tx.settle_success()
    assert [w["type"] for w in writes] == ["conversation"]


# --------------------------------------------------------------------------------------
# 真实 SQLite 端到端
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_staged_batch_commits_and_reads_back(backend: str, tmp_path: Path) -> None:
    storage = await make_storage(backend, tmp_path)
    try:
        tx = Transaction(TransactionHost(storage), CTX)
        await tx.create_root_conversation()
        entry = await tx.append_entry(1, {"kind": "pi.user", "data": {"text": "hello"}})
        submission = await tx.create_submission(
            {"conversationId": 1, "requestId": "r1", "type": "input", "status": "queued"}
        )
        tx.place_submission(submission["id"], entry["id"])

        writes = await tx.settle_success()
        seq = await storage.commit(writes, CTX)

        stored_entry = await storage.entry(entry["id"], CTX)
        assert stored_entry is not None
        assert stored_entry["entry"]["data"] == {"text": "hello"}
        assert stored_entry["commitSeq"] == seq

        placed = await storage.submission_by_request(1, "r1", CTX)
        assert placed is not None
        assert placed["status"] == "placed"
        assert placed["entry"] == entry["id"]
    finally:
        await storage.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_conversation_created_hook_is_awaited(backend: str, tmp_path: Path) -> None:
    """``conversationCreated`` 钩子在每个新建/分叉会话上跑一次（``transaction.ts:337``）。"""
    storage = await make_storage(backend, tmp_path)
    seen: list[int] = []

    async def on_created(tx: Any, record: Any) -> None:
        seen.append(record["id"])

    try:
        tx = Transaction(TransactionHost(storage, conversation_created=on_created), CTX)
        await tx.create_root_conversation()
        assert seen == [ROOT_CONVERSATION_ID]
    finally:
        await storage.close(CTX)
