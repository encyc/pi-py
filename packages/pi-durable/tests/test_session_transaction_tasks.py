"""Session 阶段 2 第 4 步的测试：任务状态机。

``create_task`` / ``set_task`` / ``_stamp_times`` / ``staged_tasks``，
以及装配期对「替换」的校验。

第 5 步（``doc()`` / ``DocumentPlan``）未实现，本文件不碰。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.session import Transaction, TransactionHost, TransactionScope
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage

pytestmark = pytest.mark.asyncio


@dataclass
class DemoDefinition:
    name: str = "demo.task"
    version: int = 1

    def initial(self, input: Any) -> dict[str, Any]:
        return {"phase": "ready", "input": input}


@dataclass
class DemoTask:
    definition: DemoDefinition


async def make_storage(backend: str, tmp_path: Path) -> Any:
    if backend == "memory":
        return MemoryStorage()
    return await open_node_sqlite_storage(str(tmp_path / "durable.db"))


async def seed_conversation(storage: Any) -> int:
    await storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)
    return 1


def task_record(
    id: int, conversation_id: int, status: str = "pending", **extra: Any
) -> dict[str, Any]:
    return {
        "id": id,
        "conversationId": conversation_id,
        "kind": "demo.task",
        "version": 1,
        "input": {},
        "state": {"status": status, "checkpoint": {"phase": "ready"}},
        "background": False,
        "abortRequested": False,
        **extra,
    }


# --------------------------------------------------------------------------------------
# create_task
# --------------------------------------------------------------------------------------


async def test_create_task_shape() -> None:
    storage = MemoryStorage()
    await seed_conversation(storage)
    tx = Transaction(TransactionHost(storage), CTX)

    id = await tx.create_task(
        DemoTask(DemoDefinition()),
        {"n": 1},
        {"ownership": {"kind": "conversation"}, "conversationId": 1},
    )

    staged = tx.staged_tasks()
    assert len(staged) == 1
    record = staged[0]
    assert record["id"] == id
    assert record["conversationId"] == 1
    assert record["kind"] == "demo.task"
    assert record["version"] == 1
    assert record["input"] == {"n": 1}
    assert record["state"] == {
        "status": "pending",
        "checkpoint": {"phase": "ready", "input": {"n": 1}},
    }
    assert record["background"] is False
    assert record["abortRequested"] is False
    assert "owner" not in record
    assert "abandonOnRestart" not in record


async def test_create_task_takes_the_conversation_from_the_scope() -> None:
    storage = MemoryStorage()
    await seed_conversation(storage)
    tx = Transaction(TransactionHost(storage), CTX, TransactionScope(conversation_id=1))
    await tx.create_task(DemoTask(DemoDefinition()), {}, {"ownership": {"kind": "conversation"}})
    assert tx.staged_tasks()[0]["conversationId"] == 1


async def test_create_task_requires_a_conversation() -> None:
    storage = MemoryStorage()
    await seed_conversation(storage)
    tx = Transaction(TransactionHost(storage), CTX)
    with pytest.raises(TypeError, match="requires options.conversationId"):
        await tx.create_task(
            DemoTask(DemoDefinition()), {}, {"ownership": {"kind": "conversation"}}
        )


async def test_create_task_rejects_an_unknown_conversation() -> None:
    storage = MemoryStorage()
    tx = Transaction(TransactionHost(storage), CTX)
    with pytest.raises(RuntimeError, match="Conversation 9 does not exist"):
        await tx.create_task(
            DemoTask(DemoDefinition()),
            {},
            {"ownership": {"kind": "conversation"}, "conversationId": 9},
        )


async def test_create_task_honours_abandon_on_restart() -> None:
    storage = MemoryStorage()
    await seed_conversation(storage)
    tx = Transaction(TransactionHost(storage), CTX)
    await tx.create_task(
        DemoTask(DemoDefinition()),
        {},
        {
            "ownership": {"kind": "conversation"},
            "conversationId": 1,
            "abandonOnRestart": True,
            "background": True,
        },
    )
    record = tx.staged_tasks()[0]
    assert record["abandonOnRestart"] is True
    assert record["background"] is True


# --------------------------------------------------------------------------------------
# create_task：子任务
# --------------------------------------------------------------------------------------


async def test_child_task_inherits_the_owner_conversation() -> None:
    storage = MemoryStorage()
    await seed_conversation(storage)
    owner_id = await storage.mint_id()
    await storage.commit([{"type": "task", "value": task_record(owner_id, 1)}], CTX)

    tx = Transaction(TransactionHost(storage), CTX)
    id = await tx.create_task(
        DemoTask(DemoDefinition()), {}, {"ownership": {"kind": "task", "taskId": owner_id}}
    )

    record = tx.staged_tasks()[0]
    assert record["id"] == id
    assert record["owner"] == owner_id
    assert record["conversationId"] == 1


async def test_child_task_cannot_be_background() -> None:
    storage = MemoryStorage()
    await seed_conversation(storage)
    owner_id = await storage.mint_id()
    await storage.commit([{"type": "task", "value": task_record(owner_id, 1)}], CTX)

    tx = Transaction(TransactionHost(storage), CTX)
    with pytest.raises(TypeError, match="A child task cannot be background"):
        await tx.create_task(
            DemoTask(DemoDefinition()),
            {},
            {"ownership": {"kind": "task", "taskId": owner_id}, "background": True},
        )


async def test_child_task_cannot_live_elsewhere() -> None:
    storage = MemoryStorage()
    await seed_conversation(storage)
    other = await storage.mint_id()
    await storage.commit([{"type": "conversation", "value": {"id": other}}], CTX)
    owner_id = await storage.mint_id()
    await storage.commit([{"type": "task", "value": task_record(owner_id, 1)}], CTX)

    tx = Transaction(TransactionHost(storage), CTX)
    with pytest.raises(RuntimeError, match="lives in its owner's conversation 1"):
        await tx.create_task(
            DemoTask(DemoDefinition()),
            {},
            {"ownership": {"kind": "task", "taskId": owner_id}, "conversationId": other},
        )


async def test_child_task_requires_an_existing_owner() -> None:
    storage = MemoryStorage()
    await seed_conversation(storage)
    tx = Transaction(TransactionHost(storage), CTX)
    with pytest.raises(RuntimeError, match="Task owner 999 does not exist"):
        await tx.create_task(
            DemoTask(DemoDefinition()), {}, {"ownership": {"kind": "task", "taskId": 999}}
        )


# --------------------------------------------------------------------------------------
# set_task
# --------------------------------------------------------------------------------------


async def test_set_task_replaces_and_stamps_running() -> None:
    storage = MemoryStorage()
    await seed_conversation(storage)
    task_id = await storage.mint_id()
    await storage.commit([{"type": "task", "value": task_record(task_id, 1)}], CTX)

    tx = Transaction(TransactionHost(storage, now=lambda: 111.0), CTX)
    tx.set_task(task_record(task_id, 1, status="running"))
    staged = tx.staged_tasks()[0]
    assert staged["state"]["status"] == "running"
    assert staged["startedAt"] == 111.0
    assert "endedAt" not in staged


async def test_set_task_stamps_terminal_and_keeps_the_callers_started_at() -> None:
    """``set_task`` 只认**本事务的候选**与**调用方传进来的值**，**不查已提交记录**。

    所以调用方（任务的运行时）必须把自己读到的 ``startedAt`` 带上——它本来就会带上，
    因为它是在当前记录的基础上改状态。
    """
    storage = MemoryStorage()
    await seed_conversation(storage)
    task_id = await storage.mint_id()
    await storage.commit(
        [{"type": "task", "value": task_record(task_id, 1, status="running", startedAt=100.0)}], CTX
    )

    tx = Transaction(TransactionHost(storage, now=lambda: 222.0), CTX)
    tx.set_task(task_record(task_id, 1, status="terminal", startedAt=100.0))
    staged = tx.staged_tasks()[0]
    assert staged["startedAt"] == 100.0  # 调用方带来的值被保留
    assert staged["endedAt"] == 222.0


async def test_stamp_times_prefers_the_transaction_candidate_over_the_value() -> None:
    """``candidate?.startedAt ?? value.startedAt ?? now(...)``——候选优先于入参（``transaction.ts:954``）。"""
    storage = MemoryStorage()
    await seed_conversation(storage)
    task_id = await storage.mint_id()
    await storage.commit([{"type": "task", "value": task_record(task_id, 1)}], CTX)

    tx = Transaction(TransactionHost(storage, now=lambda: 999.0), CTX)
    tx.set_task(task_record(task_id, 1, status="running"))  # 候选盖章 999.0
    tx.set_task(task_record(task_id, 1, status="running", startedAt=123.0))  # 入参想改成 123
    # 候选已有的 999.0 优先，入参的 123.0 被忽略
    assert tx.staged_tasks()[0]["startedAt"] == 999.0


async def test_stamp_times_never_overwrites_an_existing_stamp() -> None:
    """一旦盖上就不再改：候选里已有的时间戳优先于新盖章（``transaction.ts:955-956``）。"""
    storage = MemoryStorage()
    await seed_conversation(storage)
    task_id = await storage.mint_id()
    await storage.commit([{"type": "task", "value": task_record(task_id, 1)}], CTX)

    tx = Transaction(TransactionHost(storage, now=lambda: 999.0), CTX)
    tx.set_task(task_record(task_id, 1, status="running"))
    tx.set_task(task_record(task_id, 1, status="running"))  # 第二次
    assert tx.staged_tasks()[0]["startedAt"] == 999.0


async def test_stamp_times_preserves_a_zero_timestamp() -> None:
    """⚠️ 上游用 ``??``（只跳过 null/undefined），所以时间戳 **0 是有效值**。

    用 Python 的 ``or`` 会把 0 当成「没值」而重新盖章——这条测试就是钉住这一点。
    """
    storage = MemoryStorage()
    await seed_conversation(storage)
    task_id = await storage.mint_id()
    await storage.commit([{"type": "task", "value": task_record(task_id, 1)}], CTX)

    tx = Transaction(TransactionHost(storage, now=lambda: 0.0), CTX)
    tx.set_task(task_record(task_id, 1, status="running"))
    assert tx.staged_tasks()[0]["startedAt"] == 0.0

    # 再替换一次，now() 给别的值：候选里已有的 0 必须被保留
    tx2 = Transaction(TransactionHost(storage, now=lambda: 555.0), CTX)
    tx2.set_task(task_record(task_id, 1, status="running", startedAt=0.0))
    assert tx2.staged_tasks()[0]["startedAt"] == 0.0


async def test_set_task_rejects_a_terminal_candidate_change() -> None:
    storage = MemoryStorage()
    await seed_conversation(storage)
    tx = Transaction(TransactionHost(storage), CTX)
    tx.set_task(task_record(5, 1, status="terminal"))
    with pytest.raises(RuntimeError, match="already has a terminal candidate"):
        tx.set_task(task_record(5, 1, status="running"))


async def test_set_task_rejects_a_conversation_change() -> None:
    storage = MemoryStorage()
    await seed_conversation(storage)
    tx = Transaction(TransactionHost(storage), CTX)
    tx.set_task(task_record(5, 1))
    with pytest.raises(RuntimeError, match="cannot change conversations"):
        tx.set_task(task_record(5, 2))


# --------------------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_assembly_includes_task_writes(backend: str, tmp_path: Path) -> None:
    """第 4 步落地后，任务写入**不再**触发 fail-fast，而是进到批里。"""
    storage = await make_storage(backend, tmp_path)
    try:
        await seed_conversation(storage)
        tx = Transaction(TransactionHost(storage), CTX)
        id = await tx.create_task(
            DemoTask(DemoDefinition()),
            {"n": 1},
            {"ownership": {"kind": "conversation"}, "conversationId": 1},
        )

        writes = await tx.settle_success()

        kinds = [w["type"] for w in writes]
        assert "task" in kinds
        task_write = [w for w in writes if w["type"] == "task"][0]
        assert task_write["value"]["id"] == id

        await storage.commit(writes, CTX)
        stored = await storage.task(id, CTX)
        assert stored is not None
        assert stored["state"]["status"] == "pending"
    finally:
        await storage.close(CTX)


async def test_assembly_rejects_replacing_a_missing_task() -> None:
    storage = MemoryStorage()
    await seed_conversation(storage)
    tx = Transaction(TransactionHost(storage), CTX)
    tx.set_task(task_record(999, 1, status="running"))
    with pytest.raises(RuntimeError, match="Task 999 does not exist"):
        await tx.settle_success()


async def test_assembly_rejects_replacing_a_committed_terminal_task() -> None:
    storage = MemoryStorage()
    await seed_conversation(storage)
    task_id = await storage.mint_id()
    await storage.commit(
        [{"type": "task", "value": task_record(task_id, 1, status="terminal")}], CTX
    )

    tx = Transaction(TransactionHost(storage), CTX)
    tx.set_task(task_record(task_id, 1, status="running"))
    with pytest.raises(RuntimeError, match="is already terminal"):
        await tx.settle_success()


async def test_owned_conversation_cannot_be_created_in_the_owners_finishing_commit() -> None:
    """spec §5.5：任务不能在结束自己的那次提交里创建被拥有的工作。

    第 4 步让这条**真的**可测了：``set_task`` 能把同一事务里的任务推到终局，
    拥有者校验看的就是这个最终候选。
    """
    storage = MemoryStorage()
    await seed_conversation(storage)
    owner_id = await storage.mint_id()
    await storage.commit([{"type": "task", "value": task_record(owner_id, 1)}], CTX)

    tx = Transaction(TransactionHost(storage), CTX)
    tx.set_task(task_record(owner_id, 1, status="terminal"))
    await tx.create_conversation({"kind": "task", "taskId": owner_id})
    with pytest.raises(RuntimeError, match="is terminal"):
        await tx.settle_success()


async def test_task_created_in_this_transaction_can_own_a_conversation() -> None:
    """本事务**刚创建**的任务可以立刻当拥有者（校验看的是候选，不是已提交状态）。"""
    storage = MemoryStorage()
    await seed_conversation(storage)
    tx = Transaction(TransactionHost(storage), CTX)
    owner_id = await tx.create_task(
        DemoTask(DemoDefinition()), {}, {"ownership": {"kind": "conversation"}, "conversationId": 1}
    )
    conversation = await tx.create_conversation({"kind": "task", "taskId": owner_id})
    assert conversation["owner"] == {"conversationId": 1, "taskId": owner_id}
    await tx.settle_success()
