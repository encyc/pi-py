"""Session 阶段 2 第 1–2 步的测试：提交变更纯函数 + 表读取。

* 第 1 步：``apply_submission_change`` / ``settle_submission`` / ``place_submission``
* 第 2 步：``conversation`` / ``entry`` / ``task`` / ``scan_*`` / ``latest_head_marker`` /
  ``submission`` / ``submission_by_request``，以及 **读后写** 不变量

第 3–5 步（写入暂存 / 任务状态机 / 文档）尚未实现，本文件不碰。
内存与**真实 SQLite** 两个后端都跑一遍读取路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.entries import UserEntry
from pi_durable.errors import ReadAfterWrite
from pi_durable.session import (
    Transaction,
    TransactionHost,
    TransactionScope,
    apply_submission_change,
)
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage

pytestmark = pytest.mark.asyncio


def queued_input(id: int = 1, conversation_id: int = 1) -> dict[str, Any]:
    return {
        "id": id,
        "conversationId": conversation_id,
        "requestId": "r1",
        "type": "input",
        "status": "queued",
    }


def queued_write(id: int = 2, conversation_id: int = 1) -> dict[str, Any]:
    return {
        "id": id,
        "conversationId": conversation_id,
        "requestId": "r2",
        "type": "write",
        "status": "queued",
    }


# --------------------------------------------------------------------------------------
# 第 1 步：apply_submission_change（纯函数）
# --------------------------------------------------------------------------------------


async def test_placement_turns_queued_input_into_placed() -> None:
    out = apply_submission_change(queued_input(), {"status": "placed", "entry": 9})
    assert out["status"] == "placed"
    assert out["entry"] == 9


async def test_placement_turns_queued_write_straight_into_done() -> None:
    """写入型没有「已放置」这个状态——它直接完成（``transaction.ts:71-72``）。"""
    out = apply_submission_change(queued_write(), {"status": "placed", "entry": 9})
    assert out["status"] == "done"
    assert out["entry"] == 9
    assert out["type"] == "write"


async def test_placement_rejects_a_non_queued_record() -> None:
    placed = {**queued_input(), "status": "placed", "entry": 9}
    with pytest.raises(RuntimeError, match="is not queued"):
        apply_submission_change(placed, {"status": "placed", "entry": 10})


async def test_only_a_placed_input_can_be_answered() -> None:
    with pytest.raises(RuntimeError, match="is not a placed input"):
        apply_submission_change(queued_input(), {"status": "done", "answer": 11})


async def test_answering_a_placed_input() -> None:
    placed = {**queued_input(), "status": "placed", "entry": 9}
    out = apply_submission_change(placed, {"status": "done", "answer": 11})
    assert out["status"] == "done"
    assert out["answer"] == 11
    assert out["entry"] == 9


async def test_unanswered_merges_and_keeps_the_entry() -> None:
    """``unanswered`` 的输入保留自己的 entry（``transaction.ts:78``）。"""
    placed = {**queued_input(), "status": "placed", "entry": 9}
    out = apply_submission_change(placed, {"status": "unanswered", "reason": "closed"})
    assert out["status"] == "unanswered"
    assert out["reason"] == "closed"
    assert out["entry"] == 9


async def test_settled_records_never_change() -> None:
    """已结算的提交保持原样——这是「结算一次」的语义。"""
    for status in ("done", "unanswered"):
        settled = {**queued_input(), "status": status, "entry": 9}
        for change in (
            {"status": "placed", "entry": 10},
            {"status": "done", "answer": 11},
            {"status": "unanswered", "reason": "later"},
        ):
            assert apply_submission_change(settled, change) is settled


async def test_apply_returns_a_new_record_not_a_mutation() -> None:
    current = queued_input()
    out = apply_submission_change(current, {"status": "placed", "entry": 9})
    assert out is not current
    assert current["status"] == "queued"  # 入参没被改


# --------------------------------------------------------------------------------------
# 第 2 步：表读取（内存 + 真实 SQLite）
# --------------------------------------------------------------------------------------


async def make_storage(backend: str, tmp_path: Path) -> Any:
    if backend == "memory":
        return MemoryStorage()
    return await open_node_sqlite_storage(str(tmp_path / "durable.db"))


async def seed(storage: Any) -> dict[str, Any]:
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
    entry_id = await storage.mint_id()
    task_id = await storage.mint_id()
    submission_id = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "entry",
                "value": {"id": entry_id, "conversationId": 1, "kind": "pi.user", "head": entry_id},
            },
            {
                "type": "task",
                "value": {
                    "id": task_id,
                    "conversationId": 1,
                    "kind": "test.task",
                    "version": 1,
                    "input": {},
                    "state": {"status": "pending", "checkpoint": {}},
                    "background": False,
                    "abortRequested": False,
                },
            },
            {
                "type": "submission",
                "value": {
                    "id": submission_id,
                    "conversationId": 1,
                    "requestId": "req-1",
                    "type": "input",
                    "status": "queued",
                },
            },
        ],
        CTX,
    )
    return {"entry": entry_id, "task": task_id, "submission": submission_id}


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_table_reads_round_trip(backend: str, tmp_path: Path) -> None:
    storage = await make_storage(backend, tmp_path)
    try:
        seeded = await seed(storage)
        tx = Transaction(TransactionHost(storage), CTX)

        conversation = await tx.conversation(1)
        assert conversation is not None and conversation["id"] == 1
        assert await tx.conversation(999) is None

        entry = await tx.entry(seeded["entry"])
        assert entry is not None and entry["kind"] == "pi.user"

        task = await tx.task(seeded["task"])
        assert task is not None and task["state"]["status"] == "pending"

        submission = await tx.submission(seeded["submission"])
        assert submission is not None and submission["status"] == "queued"

        by_request = await tx.submission_by_request(1, "req-1")
        assert by_request is not None and by_request["id"] == seeded["submission"]
        assert await tx.submission_by_request(1, "nope") is None

        marker = await tx.latest_head_marker(1)
        assert marker is not None and marker["head"] == seeded["entry"]

        assert [c["id"] for c in (await tx.scan_conversations({}, 10))["items"]] == [1]
        assert [e["id"] for e in (await tx.scan_entries({"conversationId": 1}, 10))["items"]] == [
            seeded["entry"]
        ]
        assert [t["id"] for t in (await tx.scan_tasks({}, 10))["items"]] == [seeded["task"]]
    finally:
        await storage.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_entry_token_narrows_by_kind(backend: str, tmp_path: Path) -> None:
    storage = await make_storage(backend, tmp_path)
    try:
        seeded = await seed(storage)
        tx = Transaction(TransactionHost(storage), CTX)

        assert await tx.entry(seeded["entry"], UserEntry) is not None
        # 同一个条目换个令牌就读不到
        from pi_durable.entries import AssistantEntry

        assert await tx.entry(seeded["entry"], AssistantEntry) is None
    finally:
        await storage.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_scan_reads_accept_a_cursor(backend: str, tmp_path: Path) -> None:
    storage = await make_storage(backend, tmp_path)
    try:
        await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
        for _ in range(2):  # 一共 3 个会话：1 + 两个新 mint 的
            id = await storage.mint_id()
            await storage.commit([{"type": "conversation", "value": {"id": id}}], CTX)

        tx = Transaction(TransactionHost(storage), CTX)
        first = await tx.scan_conversations({}, 2)
        assert len(first["items"]) == 2
        assert "next" in first

        second = await tx.scan_conversations({}, 2, first["next"])
        assert len(second["items"]) == 1
    finally:
        await storage.close(CTX)


# --------------------------------------------------------------------------------------
# 第 2 步的核心不变量：读后写
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_reads_are_rejected_after_a_table_write(backend: str, tmp_path: Path) -> None:
    """``settle_submission`` 会置位「本事务写过表」，此后任何**表读取**都被拒。

    理由：此时「已提交状态」**不含**本事务自己的写入，按它读会给出错的答案。
    """
    storage = await make_storage(backend, tmp_path)
    try:
        seeded = await seed(storage)
        tx = Transaction(TransactionHost(storage), CTX)
        assert await tx.conversation(1) is not None  # 写之前可以读

        tx.settle_submission(seeded["submission"], {"status": "unanswered", "reason": "closed"})

        for operation in (
            tx.conversation(1),
            tx.entry(seeded["entry"]),
            tx.task(seeded["task"]),
            tx.scan_conversations({}, 10),
            tx.scan_entries({"conversationId": 1}, 10),
            tx.scan_tasks({}, 10),
            tx.latest_head_marker(1),
            tx.submission(seeded["submission"]),
            tx.submission_by_request(1, "req-1"),
        ):
            with pytest.raises(ReadAfterWrite):
                await operation
    finally:
        await storage.close(CTX)


async def test_place_submission_also_poisons_reads() -> None:
    storage = MemoryStorage()
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
    tx = Transaction(TransactionHost(storage), CTX)
    tx.place_submission(5, 9)
    with pytest.raises(ReadAfterWrite):
        await tx.conversation(1)


async def test_staged_changes_keep_order_and_are_detached() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    settlement: dict[str, Any] = {"status": "unanswered", "reason": "closed"}
    tx.settle_submission(1, settlement)  # type: ignore[arg-type]
    tx.place_submission(2, 9)

    staged = list(tx.staged_submission_changes())
    assert [id for id, _ in staged] == [1, 2]
    assert staged[1][1] == {"status": "placed", "entry": 9}

    # 调用方之后改自己那份 settlement，不该影响已暂存的
    settlement["reason"] = "mutated"
    assert staged[0][1]["reason"] == "closed"


# --------------------------------------------------------------------------------------
# 生命周期
# --------------------------------------------------------------------------------------


async def test_reads_are_rejected_after_settle_failure() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    await tx.settle_failure()
    with pytest.raises(RuntimeError, match="Transaction has settled"):
        await tx.conversation(1)


async def test_staging_is_rejected_after_settle_failure() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    await tx.settle_failure()
    with pytest.raises(RuntimeError, match="Transaction has settled"):
        tx.settle_submission(1, {"status": "unanswered", "reason": "r"})  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="Transaction has settled"):
        tx.place_submission(1, 2)


async def test_task_reads_are_memoized_within_one_transaction() -> None:
    """同一事务里读同一个任务两次只碰一次存储（``transaction.ts:980-984``）。"""
    calls: list[int] = []
    storage = MemoryStorage()
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
    task_id = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "task",
                "value": {
                    "id": task_id,
                    "conversationId": 1,
                    "kind": "k",
                    "version": 1,
                    "input": {},
                    "state": {"status": "pending", "checkpoint": {}},
                    "background": False,
                    "abortRequested": False,
                },
            }
        ],
        CTX,
    )

    host = TransactionHost(storage)
    original = host.storage.task

    async def counting_task(id: int, context: Any) -> Any:
        calls.append(id)
        return await original(id, context)

    host.storage.task = counting_task  # type: ignore[method-assign]
    tx = Transaction(host, CTX)
    assert await tx.task(task_id) is not None
    assert await tx.task(task_id) is not None
    assert calls == [task_id]


async def test_transaction_scope_defaults() -> None:
    storage = MemoryStorage()
    host = TransactionHost(storage)
    assert host.storage is storage
    assert isinstance(host.now(), float)

    assert TransactionScope().conversation_id is None
    assert TransactionScope(conversation_id=7, task_id=9).conversation_id == 7

    fixed = TransactionHost(storage, now=lambda: 1234.5)
    assert fixed.now() == 1234.5
