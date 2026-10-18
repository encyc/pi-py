"""``MemoryStorage`` 的补充行为测试。

**规范口径是 :mod:`pi_durable.testing.storage_conformance`**（上游 ``storage-conformance.ts``
的 24 条用例），由 ``tests/test_storage_conformance.py`` 对每个后端跑一遍。
本文件是它的**补充**，覆盖一致性套件没有单独成例、但值得钉住的边界：
单条错误消息、关闭后的逐方法行为、以及少量直接可读的语义示例。
"""

from __future__ import annotations

from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.errors import StorageRequestError
from pi_durable.storage.memory import MemoryStorage
from pi_durable.types import ROOT_CONVERSATION_ID, StorageWrite

pytestmark = pytest.mark.asyncio


@pytest.fixture
def storage() -> MemoryStorage:
    return MemoryStorage()


async def create_root(storage: MemoryStorage) -> int:
    await storage.commit([{"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}}], CTX)
    return ROOT_CONVERSATION_ID


def entry_write(id: int, conversation_id: int, kind: str = "user", **extra: Any) -> StorageWrite:
    value = {"id": id, "conversationId": conversation_id, "kind": kind, **extra}
    return {"type": "entry", "value": value}  # type: ignore[typeddict-item]


# --------------------------------------------------------------------------------------
# 1. 根会话与 ID 命名空间
# --------------------------------------------------------------------------------------


async def test_mint_id_starts_at_two(storage: MemoryStorage) -> None:
    """``ROOT_CONVERSATION_ID`` 是保留的 1，所以第一个分配的 ID 是 2。"""
    assert await storage.mint_id() == 2


async def test_root_conversation_is_reserved_and_unique(storage: MemoryStorage) -> None:
    assert await create_root(storage) == ROOT_CONVERSATION_ID
    assert await storage.conversation(ROOT_CONVERSATION_ID, CTX) == {"id": ROOT_CONVERSATION_ID}

    with pytest.raises(RuntimeError, match="already belongs to conversation"):
        await storage.commit([{"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}}], CTX)


async def test_one_global_id_namespace(storage: MemoryStorage) -> None:
    """一个 ID 只能属于一种记录；跨表复用要报错。"""
    root = await create_root(storage)
    id = await storage.mint_id()

    await storage.commit([entry_write(id, root)], CTX)

    with pytest.raises(RuntimeError, match="already belongs to entry"):
        await storage.commit([{"type": "conversation", "value": {"id": id}}], CTX)


async def test_commit_sequence_must_strictly_increase(storage: MemoryStorage) -> None:
    await create_root(storage)

    with pytest.raises(RuntimeError, match="does not strictly increase"):
        storage.prepare_commit([], seq=0)


async def test_id_space_exhaustion(storage: MemoryStorage) -> None:
    storage._next_id = 2**53  # noqa: SLF001 - 用例需要把游标推到边界
    with pytest.raises(RuntimeError, match="ID space is exhausted"):
        await storage.mint_id()


# --------------------------------------------------------------------------------------
# 2. 原子性与解除关联
# --------------------------------------------------------------------------------------


async def test_mixed_table_commit_is_atomic(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    entry_id = await storage.mint_id()
    task_id = await storage.mint_id()
    submission_id = await storage.mint_id()

    task = {
        "id": task_id,
        "conversationId": root,
        "kind": "demo",
        "version": 1,
        "input": {},
        "background": False,
        "abortRequested": False,
        "state": {"status": "pending", "checkpoint": {}},
    }
    submission = {
        "id": submission_id,
        "conversationId": root,
        "requestId": "request-1",
        "type": "input",
        "status": "placed",
        "entry": entry_id,
    }
    seq = await storage.commit(
        [
            entry_write(entry_id, root, data={"text": "hello"}),
            {"type": "task", "value": task},  # type: ignore[typeddict-item]
            {"type": "submission", "value": submission},  # type: ignore[typeddict-item]
        ],
        CTX,
    )

    stored_entry = await storage.entry(entry_id, CTX)
    assert stored_entry is not None
    assert stored_entry["commitSeq"] == seq
    assert await storage.task(task_id, CTX) == task
    assert await storage.submission(submission_id, CTX) == submission


async def test_failed_commit_rolls_everything_back(storage: MemoryStorage) -> None:
    """一次提交里有一个坏写入，整批都不落盘。"""
    root = await create_root(storage)
    good = await storage.mint_id()
    bad = await storage.mint_id()

    # 先占用 bad 这个 ID 作为会话，于是下面那批里的 entry 用了同一个 ID 会冲突
    await storage.commit([{"type": "conversation", "value": {"id": bad}}], CTX)

    with pytest.raises(RuntimeError, match="already belongs to conversation"):
        await storage.commit([entry_write(good, root), entry_write(bad, root)], CTX)

    assert await storage.entry(good, CTX) is None


async def test_returned_records_are_detached(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    id = await storage.mint_id()
    value = {"id": id, "conversationId": root, "kind": "user", "data": {"text": "hi"}}
    await storage.commit([{"type": "entry", "value": value}], CTX)  # type: ignore[typeddict-item]

    read = await storage.entry(id, CTX)
    assert read is not None
    read["entry"]["data"]["text"] = "mutated"  # type: ignore[index]

    again = await storage.entry(id, CTX)
    assert again is not None
    assert again["entry"]["data"] == {"text": "hi"}


async def test_retained_writes_are_detached(storage: MemoryStorage) -> None:
    """提交后改动调用方自己那份写入，不影响已存状态。"""
    root = await create_root(storage)
    id = await storage.mint_id()
    value = {"id": id, "conversationId": root, "kind": "user", "data": {"text": "hi"}}
    await storage.commit([{"type": "entry", "value": value}], CTX)  # type: ignore[typeddict-item]

    value["data"]["text"] = "mutated"

    stored = await storage.entry(id, CTX)
    assert stored is not None
    assert stored["entry"]["data"] == {"text": "hi"}


async def test_prototype_like_json_keys_are_plain_keys(storage: MemoryStorage) -> None:
    """``__proto__`` / ``constructor`` 这类键在 JS 上会碰到原型链；Python 里就是普通键。"""
    root = await create_root(storage)
    id = await storage.mint_id()
    data = {"__proto__": {"polluted": True}, "constructor": "x"}

    await storage.commit([entry_write(id, root, data=data)], CTX)

    stored = await storage.entry(id, CTX)
    assert stored is not None
    assert stored["entry"]["data"] == data
    assert dict({}) == {}  # 全局 dict 未被污染


# --------------------------------------------------------------------------------------
# 3. 条目索引与 fork 历史
# --------------------------------------------------------------------------------------


async def test_entries_scan_newest_first_by_default(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    ids = [await storage.mint_id() for _ in range(3)]
    # 刻意乱序提交，验证索引按 ID 排序而与提交顺序无关
    for id in (ids[1], ids[2], ids[0]):
        await storage.commit([entry_write(id, root)], CTX)

    page = await storage.scan_entries({"conversationId": root}, 10, None, CTX)

    assert [item["id"] for item in page["items"]] == sorted(ids, reverse=True)


async def test_entry_scan_ascending_order(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    ids = [await storage.mint_id() for _ in range(3)]
    for id in ids:
        await storage.commit([entry_write(id, root)], CTX)

    page = await storage.scan_entries({"conversationId": root, "order": "ascending"}, 10, None, CTX)

    assert [item["id"] for item in page["items"]] == ids


async def test_unknown_conversation_is_a_request_error(storage: MemoryStorage) -> None:
    with pytest.raises(StorageRequestError, match="Unknown conversation"):
        await storage.scan_entries({"conversationId": 99}, 10, None, CTX)


async def test_fork_history_scans_through_ancestors(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    base = [await storage.mint_id() for _ in range(3)]
    for id in base:
        await storage.commit([entry_write(id, root)], CTX)

    child = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "conversation",
                "value": {"id": child, "parent": {"conversationId": root, "at": base[1]}},
            }
        ],
        CTX,
    )
    own = await storage.mint_id()
    await storage.commit([entry_write(own, child)], CTX)

    page = await storage.scan_entries({"conversationId": child}, 10, None, CTX)
    got = [item["id"] for item in page["items"]]

    # 自己的条目 + 祖先在 base[1]（含）之前的条目
    assert got == [own, base[1], base[0]]


async def test_fork_at_returns_only_ancestry_visible_entry(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    ids = [await storage.mint_id() for _ in range(3)]
    for id in ids:
        await storage.commit([entry_write(id, root)], CTX)

    # 自己的条目不在它的祖先范围内
    assert await storage.entry_in_conversation(root, ids[0], CTX) is not None
    assert await storage.entry(ids[0], CTX) is not None


async def test_find_latest_head_marker(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    first = await storage.mint_id()
    second = await storage.mint_id()
    await storage.commit([entry_write(first, root, head=first)], CTX)
    await storage.commit([entry_write(second, root, head=second)], CTX)

    marker = await storage.find_latest_head_marker(root, None, CTX)

    assert marker is not None
    assert marker["id"] == second
    assert marker["head"] == second


async def test_find_latest_head_marker_honours_cutoff(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    first = await storage.mint_id()
    second = await storage.mint_id()
    await storage.commit([entry_write(first, root, head=first)], CTX)
    await storage.commit([entry_write(second, root, head=second)], CTX)

    marker = await storage.find_latest_head_marker(root, first, CTX)

    assert marker is not None
    assert marker["id"] == first


# --------------------------------------------------------------------------------------
# 4. 游标与分页
# --------------------------------------------------------------------------------------


async def test_pagination_returns_cursor_and_continues(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    ids = [await storage.mint_id() for _ in range(5)]
    for id in ids:
        await storage.commit([entry_write(id, root)], CTX)

    first = await storage.scan_entries({"conversationId": root}, 2, None, CTX)
    assert [item["id"] for item in first["items"]] == [ids[4], ids[3]]
    assert "next" in first

    second = await storage.scan_entries({"conversationId": root}, 2, first["next"], CTX)
    assert [item["id"] for item in second["items"]] == [ids[2], ids[1]]


async def test_cursor_conflicting_order_is_rejected(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    for _ in range(4):
        await storage.commit([entry_write(await storage.mint_id(), root)], CTX)

    first = await storage.scan_entries({"conversationId": root}, 2, None, CTX)
    with pytest.raises(StorageRequestError, match="cursor continues a descending scan"):
        await storage.scan_entries(
            {"conversationId": root, "order": "ascending"}, 2, first["next"], CTX
        )


async def test_invalid_cursor_is_rejected(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    await storage.commit([entry_write(await storage.mint_id(), root)], CTX)

    with pytest.raises(StorageRequestError, match="Invalid storage cursor"):
        await storage.scan_entries({"conversationId": root}, 10, {"after": "nope"}, CTX)


async def test_invalid_scan_order_is_rejected(storage: MemoryStorage) -> None:
    with pytest.raises(StorageRequestError, match="Invalid scan order"):
        await storage.scan_conversations({"order": "sideways"}, 10, None, CTX)  # type: ignore[typeddict-item]


async def test_no_cursor_when_page_is_last(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    await storage.commit([entry_write(await storage.mint_id(), root)], CTX)

    page = await storage.scan_entries({"conversationId": root}, 10, None, CTX)

    assert "next" not in page


# --------------------------------------------------------------------------------------
# 5. 会话的归属过滤
# --------------------------------------------------------------------------------------


async def test_conversation_owner_edges_filter(storage: MemoryStorage) -> None:
    await create_root(storage)
    task_id = await storage.mint_id()
    owned = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "conversation",
                "value": {
                    "id": owned,
                    "owner": {"conversationId": ROOT_CONVERSATION_ID, "taskId": task_id},
                },
            }
        ],
        CTX,
    )

    by_conversation = await storage.scan_conversations(
        {"ownerConversationId": ROOT_CONVERSATION_ID}, 10, None, CTX
    )
    by_task = await storage.scan_conversations({"ownerTaskId": task_id}, 10, None, CTX)

    assert [item["id"] for item in by_conversation["items"]] == [owned]
    assert [item["id"] for item in by_task["items"]] == [owned]


# --------------------------------------------------------------------------------------
# 6. 任务与提交的索引
# --------------------------------------------------------------------------------------


def task_record(id: int, conversation_id: int, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": id,
        "conversationId": conversation_id,
        "kind": "demo",
        "version": 1,
        "input": {},
        "background": False,
        "abortRequested": False,
        "state": {"status": status, "checkpoint": {}},
        **extra,
    }


async def test_task_status_index_follows_replacement(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    id = await storage.mint_id()
    await storage.commit([{"type": "task", "value": task_record(id, root, "pending")}], CTX)  # type: ignore[typeddict-item]

    assert [
        t["id"] for t in (await storage.scan_tasks({"status": "pending"}, 10, None, CTX))["items"]
    ] == [id]
    assert (await storage.scan_tasks({"status": "running"}, 10, None, CTX))["items"] == []

    await storage.commit([{"type": "task", "value": task_record(id, root, "running")}], CTX)  # type: ignore[typeddict-item]

    assert (await storage.scan_tasks({"status": "pending"}, 10, None, CTX))["items"] == []
    assert [
        t["id"] for t in (await storage.scan_tasks({"status": "running"}, 10, None, CTX))["items"]
    ] == [id]


async def test_task_filters(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    a = await storage.mint_id()
    b = await storage.mint_id()
    await storage.commit(
        [{"type": "task", "value": task_record(a, root, "pending", kind="alpha")}], CTX
    )  # type: ignore[typeddict-item]
    await storage.commit(
        [{"type": "task", "value": task_record(b, root, "pending", kind="beta", background=True)}],  # type: ignore[typeddict-item]
        CTX,
    )

    assert [
        t["id"] for t in (await storage.scan_tasks({"kind": "alpha"}, 10, None, CTX))["items"]
    ] == [a]
    assert [
        t["id"] for t in (await storage.scan_tasks({"background": True}, 10, None, CTX))["items"]
    ] == [b]


async def test_submission_request_index_is_replaced(storage: MemoryStorage) -> None:
    root = await create_root(storage)
    entry_id = await storage.mint_id()
    id = await storage.mint_id()
    base = {
        "id": id,
        "conversationId": root,
        "requestId": "req-1",
        "type": "input",
        "status": "queued",
    }
    await storage.commit([{"type": "submission", "value": base}], CTX)  # type: ignore[typeddict-item]

    found = await storage.submission_by_request(root, "req-1", CTX)
    assert found is not None and found["status"] == "queued"

    placed = {**base, "status": "placed", "entry": entry_id}
    await storage.commit([{"type": "submission", "value": placed}], CTX)  # type: ignore[typeddict-item]

    found = await storage.submission_by_request(root, "req-1", CTX)
    assert found is not None and found["status"] == "placed"
    assert (await storage.scan_submissions({"status": "queued"}, 10, None, CTX))["items"] == []
    assert len((await storage.scan_submissions({"status": "placed"}, 10, None, CTX))["items"]) == 1


async def test_submission_request_index_is_cleared_when_request_id_goes_away(
    storage: MemoryStorage,
) -> None:
    root = await create_root(storage)
    id = await storage.mint_id()
    with_request = {
        "id": id,
        "conversationId": root,
        "requestId": "req-1",
        "type": "write",
        "status": "queued",
    }
    await storage.commit([{"type": "submission", "value": with_request}], CTX)  # type: ignore[typeddict-item]

    without_request = {k: v for k, v in with_request.items() if k != "requestId"}
    await storage.commit([{"type": "submission", "value": without_request}], CTX)  # type: ignore[typeddict-item]

    assert await storage.submission_by_request(root, "req-1", CTX) is None


# --------------------------------------------------------------------------------------
# 7. 文档
# --------------------------------------------------------------------------------------


async def test_document_create_materializes_base(storage: MemoryStorage) -> None:
    id = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {"id": id, "kind": "demo", "scope": {"kind": "session"}},
                "content": {"kind": "base", "version": 1, "value": {"count": 0}},
            }
        ],
        CTX,
    )

    stored = await storage.document(id, "current", CTX)
    assert stored is not None
    assert stored["value"] == {"count": 0}
    assert stored["version"] == 1
    assert stored["deltasSinceBase"] == 0


async def test_document_address_lookup_and_retire(storage: MemoryStorage) -> None:
    id = await storage.mint_id()
    address = {"kind": "demo", "scope": {"kind": "session"}}
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {"id": id, "kind": "demo", "scope": {"kind": "session"}},
                "content": {"kind": "base", "version": 1, "value": {}},
            }
        ],
        CTX,
    )

    found = await storage.find_document(address, "current", CTX)
    assert found is not None and found["id"] == id

    await storage.commit([{"type": "document.retire", "id": id}], CTX)

    assert await storage.find_document(address, "current", CTX) is None
    assert await storage.document(id, "current", CTX) is None
    # 历史成员关系仍然可查
    assert await storage.find_document(address, 1, CTX) is not None


async def test_create_plus_retire_has_empty_lifetime(storage: MemoryStorage) -> None:
    """同一批里 create + retire：两个边界都盖同一个 seq，生命周期为空。"""
    id = await storage.mint_id()
    seq = await storage.commit(
        [
            {
                "type": "document.create",
                "record": {"id": id, "kind": "demo", "scope": {"kind": "session"}},
                "content": {"kind": "base", "version": 1, "value": {}},
            },
            {"type": "document.retire", "id": id},
        ],
        CTX,
    )

    assert (
        await storage.find_document({"kind": "demo", "scope": {"kind": "session"}}, "current", CTX)
        is None
    )
    assert (
        await storage.find_document({"kind": "demo", "scope": {"kind": "session"}}, seq, CTX)
        is None
    )


async def test_document_delta_requires_matching_version(storage: MemoryStorage) -> None:
    id = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {"id": id, "kind": "demo", "scope": {"kind": "session"}},
                "content": {"kind": "base", "version": 1, "value": {"a": 1}},
            }
        ],
        CTX,
    )

    with pytest.raises(RuntimeError, match="version transition requires a base"):
        await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": id,
                    "content": {"kind": "delta", "version": 2, "ops": []},
                }
            ],
            CTX,
        )


async def test_document_delta_without_base_is_rejected(storage: MemoryStorage) -> None:
    id = await storage.mint_id()
    with pytest.raises(RuntimeError, match="Unknown document"):
        await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": id,
                    "content": {"kind": "delta", "version": 1, "ops": []},
                }
            ],
            CTX,
        )


async def test_two_content_commands_for_one_document_are_rejected(storage: MemoryStorage) -> None:
    id = await storage.mint_id()
    with pytest.raises(RuntimeError, match="more than one content command"):
        await storage.commit(
            [
                {
                    "type": "document.create",
                    "record": {"id": id, "kind": "demo", "scope": {"kind": "session"}},
                    "content": {"kind": "base", "version": 1, "value": {}},
                },
                {
                    "type": "document.change",
                    "id": id,
                    "content": {"kind": "base", "version": 1, "value": {}},
                },
            ],
            CTX,
        )


async def test_unknown_document_retire_is_rejected(storage: MemoryStorage) -> None:
    id = await storage.mint_id()
    with pytest.raises(RuntimeError, match="Unknown document"):
        await storage.commit([{"type": "document.retire", "id": id}], CTX)


async def test_current_only_document_rejects_historical_read(storage: MemoryStorage) -> None:
    conversation = await create_root(storage)
    id = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {
                    "id": id,
                    "kind": "demo",
                    "scope": {"kind": "conversation", "conversationId": conversation},
                    "history": "latest",
                    "fork": "current",
                },
                "content": {"kind": "base", "version": 1, "value": {}},
            }
        ],
        CTX,
    )

    with pytest.raises(StorageRequestError, match="does not retain historical content"):
        await storage.document(id, 1, CTX)


async def test_scan_documents_filters_by_scope_and_kind(storage: MemoryStorage) -> None:
    a = await storage.mint_id()
    b = await storage.mint_id()
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {"id": a, "kind": "alpha", "scope": {"kind": "session"}},
                "content": {"kind": "base", "version": 1, "value": {}},
            },
            {
                "type": "document.create",
                "record": {"id": b, "kind": "beta", "scope": {"kind": "session"}},
                "content": {"kind": "base", "version": 1, "value": {}},
            },
        ],
        CTX,
    )

    all_docs = await storage.scan_documents(
        {"scope": {"kind": "session"}, "at": "current"}, 10, None, CTX
    )
    assert [d["id"] for d in all_docs["items"]] == [a, b]

    only_alpha = await storage.scan_documents(
        {"scope": {"kind": "session"}, "at": "current", "kind": "alpha"}, 10, None, CTX
    )
    assert [d["id"] for d in only_alpha["items"]] == [a]


# --------------------------------------------------------------------------------------
# 8. 关闭
# --------------------------------------------------------------------------------------


async def test_every_operation_is_rejected_after_close(storage: MemoryStorage) -> None:
    await create_root(storage)
    await storage.close(CTX)

    with pytest.raises(RuntimeError, match="MemoryStorage is closed"):
        await storage.mint_id()
    with pytest.raises(RuntimeError, match="MemoryStorage is closed"):
        await storage.commit([], CTX)
    with pytest.raises(RuntimeError, match="MemoryStorage is closed"):
        await storage.conversation(ROOT_CONVERSATION_ID, CTX)
    with pytest.raises(RuntimeError, match="MemoryStorage is closed"):
        await storage.scan_tasks({}, 10, None, CTX)
