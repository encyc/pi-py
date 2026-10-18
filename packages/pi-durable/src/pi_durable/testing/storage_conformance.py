"""``Storage`` 的可执行规范 —— 移植 ``durable/src/testing/storage-conformance.ts``（1,663 行 / 25 条用例）。

这是 :class:`~pi_durable.types.Storage` 的**唯一语义口径**：内存后端、SQLite 后端、
JSONL 后端都要跑同一套用例。上游 ``docs/spec.md`` §10 末句「The semantic conformance suite
covers memory, SQLite, and JSONL」说的就是它。

与 ``env_conformance`` 一样，本模块**与测试框架解耦**：用例只依赖
:class:`~pi_durable.testing.types.StorageConformanceAssertions` 的六个断言原语，
由 :mod:`pi_durable.testing.runner` 适配到 pytest。**用例的名字、顺序与断言语义与上游逐条一致**——
名字就是验收口径，改名会让「25 条全过」这个说法失去意义。

静态类型的一处**有意放宽**
----------------------------

用例体刻意像 JS 对象字面量那样自由导航记录（``{...record, status: "done", answer: id}`` 这种
展开在 TS 里合法，在 Python 的 TypedDict 判别联合上会与静态收窄冲突）。因此**用例体收到的
``storage`` 句柄标注为 ``Any``**，而 :func:`create_storage_conformance` 的公开签名仍是
:class:`~pi_durable.types.Storage`。套件的价值在于运行时验证（24 条用例实跑），
不在于对用例体本身做静态检查——上游同理，它的用例是数据驱动的一段代码。

移植约定
--------

* ``expect(x).toBe(y)`` → :meth:`_Expect.to_be`；``toEqual`` → ``to_equal``；
  ``toMatchObject`` → ``to_match_object``；``toBeUndefined`` → ``to_be_undefined``；
  ``toBeDefined`` → ``to_be_defined``；``toBeGreaterThan`` → ``to_be_greater_than``；
  ``toHaveLength`` → ``to_have_length``；``rejects.toThrow`` → ``rejects_to_throw``。
* JS 的对象字面量 → Python ``dict``（记录就是 JSON，见 ``pi_durable.types`` 的模块文档）。
* ``await expect(promise).rejects.toThrow(msg)`` 需要**先拿到协程**再断言，所以 Python 侧
  写成 ``await expect_raises(coro, msg)``；这是唯一无法逐字对应的地方，语义相同。
"""

from __future__ import annotations

import copy
import json
from typing import Any, cast

# 上游此文件用裸 `context`（`storage-conformance.ts:27`）；保持同名以便逐行对照。
from pi_chord.context import BACKGROUND_CONTEXT as context  # noqa: N811
from pi_chord.delta import Op
from pi_durable.ids import id_from_number
from pi_durable.testing.types import (
    StorageConformanceAssertions,
    StorageConformanceCase,
    StorageConformanceOptions,
)
from pi_durable.types import (
    ROOT_CONVERSATION_ID,
    ConversationId,
    Cursor,
    DocumentCreate,
    EntryRecord,
    JsonObject,
    ScanOrder,
    Storage,
    StorageWrite,
    SubmissionRecord,
)

__all__ = ["create_storage_conformance"]

StoredTask = dict[str, Any]

_MAX_SAFE_INTEGER = 2**53 - 1


# --------------------------------------------------------------------------------------
# 断言门面（storage-conformance.ts:60-88）
# --------------------------------------------------------------------------------------


class _Rejects:
    def __init__(self, awaitable: Any, assertions: StorageConformanceAssertions) -> None:
        self._awaitable = awaitable
        self._assertions = assertions

    async def to_throw(self, message_includes: str) -> None:
        await self._assertions.rejects(self._awaitable, message_includes)


class _Expect:
    """``assertionFacade`` 的对应物（``storage-conformance.ts:72-88``）。"""

    def __init__(self, actual: Any, assertions: StorageConformanceAssertions) -> None:
        self._actual = actual
        self._assertions = assertions

    def to_be(self, expected: Any) -> None:
        self._assertions.strict_equal(self._actual, expected)

    def to_be_defined(self) -> None:
        self._assertions.ok(self._actual is not None, "Expected value to be defined")

    def to_be_greater_than(self, expected: float) -> None:
        self._assertions.greater_than(self._actual, expected)

    def to_be_undefined(self) -> None:
        self._assertions.strict_equal(self._actual, None)

    def to_be_true(self) -> None:
        self._assertions.strict_equal(self._actual, True)

    def to_equal(self, expected: Any) -> None:
        self._assertions.deep_equal(self._actual, expected)

    def to_have_length(self, expected: int) -> None:
        self._assertions.strict_equal(len(self._actual), expected)

    def to_match_object(self, expected: Any) -> None:
        self._assertions.partial_deep_equal(self._actual, expected)

    @property
    def rejects(self) -> _Rejects:
        return _Rejects(self._actual, self._assertions)


def _case(
    options: StorageConformanceOptions,
    name: str,
    test: Any,
) -> StorageConformanceCase:
    return StorageConformanceCase(name=name, run=lambda: options.with_storage(test))


# --------------------------------------------------------------------------------------
# 夹具（storage-conformance.ts:29-54）
# --------------------------------------------------------------------------------------


async def _create_root(storage: Storage) -> ConversationId:
    await storage.commit([{"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}}], context)
    return ROOT_CONVERSATION_ID


def _pending_task(id: Any, conversation_id: Any, phase: str = "ready") -> StoredTask:
    return {
        "id": id,
        "conversationId": conversation_id,
        "kind": "test.task",
        "version": 1,
        "input": {"value": id},
        "state": {"status": "pending", "checkpoint": {"phase": phase}},
        "background": False,
        "abortRequested": False,
    }


def _entry(
    id: Any,
    conversation_id: Any,
    kind: str = "message",
    extra: dict[str, Any] | None = None,
) -> EntryRecord:
    merged: dict[str, Any] = {"id": id, "conversationId": conversation_id, "kind": kind}
    merged.update(extra or {})
    return cast(EntryRecord, merged)


def _ids(page: Any) -> list[Any]:
    """``page.items.map(({ id }) => id)``。"""
    return [item["id"] for item in page["items"]]


def _doc_ids(page: Any) -> list[Any]:
    return [item["id"] for item in page["items"]]


def _round_trip(cursor: Cursor) -> Any:
    """游标必须能安全地 JSON 往返（``JSON.parse(JSON.stringify(...))``）。"""
    return json.loads(json.dumps(cursor))


def _entry_write(value: EntryRecord) -> StorageWrite:
    return cast(StorageWrite, {"type": "entry", "value": value})


def _task_write(value: StoredTask) -> StorageWrite:
    return cast(StorageWrite, {"type": "task", "value": value})


def _submission_write(value: SubmissionRecord) -> StorageWrite:
    return cast(StorageWrite, {"type": "submission", "value": value})


def _conversation_write(value: dict[str, Any]) -> StorageWrite:
    return cast(StorageWrite, {"type": "conversation", "value": value})


def _document_create(record: DocumentCreate, content: JsonObject, version: int = 1) -> StorageWrite:
    return {
        "type": "document.create",
        "record": record,
        "content": {"kind": "base", "version": version, "value": content},
    }


# --------------------------------------------------------------------------------------
# 25 条用例（顺序与名字与上游逐条一致）
# --------------------------------------------------------------------------------------


def create_storage_conformance(
    options: StorageConformanceOptions,
) -> tuple[StorageConformanceCase, ...]:
    """创建与测试框架无关的用例；``with_storage`` 必须对每条用例恰好调用并等待回调一次
    （``storage-conformance.ts:94-95``）。"""
    assertions = options.assertions

    def expect(actual: Any) -> _Expect:
        return _Expect(actual, assertions)

    async def expect_rejects(awaitable: Any, message_includes: str) -> None:
        await assertions.rejects(awaitable, message_includes)

    cases: list[StorageConformanceCase] = []

    # 1 ────────────────────────────────────────────────────────────────────────────────
    async def reserves_root(storage: Any) -> None:
        expect(await storage.mint_id()).to_be(2)
        expect(await _create_root(storage)).to_be(ROOT_CONVERSATION_ID)
        expect(await storage.conversation(ROOT_CONVERSATION_ID, context)).to_equal(
            {"id": ROOT_CONVERSATION_ID}
        )
        await expect_rejects(
            storage.commit([_conversation_write({"id": ROOT_CONVERSATION_ID})], context),
            f"ID {ROOT_CONVERSATION_ID} already belongs to conversation",
        )

    cases.append(_case(options, "reserves ID 1 for the immutable root conversation", reserves_root))

    # 2 ────────────────────────────────────────────────────────────────────────────────
    async def mixed_tables_atomic(storage: Any) -> None:
        root_id = await _create_root(storage)
        entry_id = await storage.mint_id()
        task_id = await storage.mint_id()
        submission_id = await storage.mint_id()
        task = _pending_task(task_id, root_id)
        input_record: SubmissionRecord = {
            "id": submission_id,
            "conversationId": root_id,
            "requestId": "request-1",
            "type": "input",
            "status": "placed",
            "entry": entry_id,
        }
        initial_seq = await storage.commit(
            [
                _entry_write(_entry(entry_id, root_id, "user", {"data": {"text": "hello"}})),
                _task_write(task),
                _submission_write(input_record),
            ],
            context,
        )
        expect(await storage.entry(entry_id, context)).to_equal(
            {
                "entry": _entry(entry_id, root_id, "user", {"data": {"text": "hello"}}),
                "commitSeq": initial_seq,
            }
        )
        expect(await storage.task(task_id, context)).to_equal(task)
        expect(await storage.submission(submission_id, context)).to_equal(input_record)

        transient_entry_id = await storage.mint_id()
        running_task: StoredTask = {
            **task,
            "state": {"status": "running", "checkpoint": {"phase": "effect"}},
        }
        done_input: Any = {
            **input_record,
            "status": "done",
            "answer": transient_entry_id,
        }
        await expect_rejects(
            storage.commit(
                [
                    _task_write(running_task),
                    _submission_write(done_input),
                    _entry_write(_entry(transient_entry_id, root_id, "assistant")),
                    _conversation_write({"id": root_id}),
                ],
                context,
            ),
            f"ID {root_id} already belongs to conversation",
        )
        expect(await storage.task(task_id, context)).to_equal(task)
        expect(await storage.submission(submission_id, context)).to_equal(input_record)
        expect(await storage.entry(transient_entry_id, context)).to_be_undefined()
        after_rollback_seq = await storage.commit(
            [_entry_write(_entry(await storage.mint_id(), root_id, "after-rollback"))], context
        )
        expect(after_rollback_seq).to_be_greater_than(initial_seq)

    cases.append(
        _case(
            options,
            "commits mixed table writes atomically and rolls all of them back on failure",
            mixed_tables_atomic,
        )
    )

    # 3 ────────────────────────────────────────────────────────────────────────────────
    async def detaches_records(storage: Any) -> None:
        root_id = await _create_root(storage)
        entry_id = await storage.mint_id()
        task_id = await storage.mint_id()
        submission_id = await storage.mint_id()
        entry_data: dict[str, Any] = {"nested": [1, 2]}
        checkpoint: dict[str, Any] = {"phase": "ready", "nested": {"count": 1}}
        detail: dict[str, Any] = {"codes": ["initial"]}
        await storage.commit(
            [
                _entry_write(_entry(entry_id, root_id, "note", {"data": entry_data})),
                _task_write(
                    {
                        **_pending_task(task_id, root_id),
                        "state": {"status": "pending", "checkpoint": checkpoint},
                    }
                ),
                _submission_write(
                    {
                        "id": submission_id,
                        "conversationId": root_id,
                        "type": "input",
                        "status": "unanswered",
                        "reason": "failed",
                        "detail": detail,
                    }
                ),
            ],
            context,
        )

        entry_data["nested"].append(3)
        checkpoint["nested"]["count"] = 2
        detail["codes"].append("mutated")
        first_entry = await storage.entry(entry_id, context)
        expect(first_entry["entry"]["data"] if first_entry else None).to_equal({"nested": [1, 2]})
        first_task = await storage.task(task_id, context)
        expect(first_task["state"] if first_task else None).to_equal(
            {"status": "pending", "checkpoint": {"phase": "ready", "nested": {"count": 1}}}
        )
        first_submission = await storage.submission(submission_id, context)
        expect(first_submission["detail"] if first_submission else None).to_equal(
            {"codes": ["initial"]}
        )

        read_entry = (await storage.entry(entry_id, context))["entry"]
        read_entry["data"]["nested"].append(9)
        read_task = await storage.task(task_id, context)
        read_task["state"]["checkpoint"]["nested"]["count"] = 9
        read_input = await storage.submission(submission_id, context)
        read_input["detail"]["codes"].append("read mutation")

        again_entry = await storage.entry(entry_id, context)
        expect(again_entry["entry"]["data"] if again_entry else None).to_equal({"nested": [1, 2]})
        again_task = await storage.task(task_id, context)
        expect(again_task["state"] if again_task else None).to_equal(
            {"status": "pending", "checkpoint": {"phase": "ready", "nested": {"count": 1}}}
        )
        again_submission = await storage.submission(submission_id, context)
        expect(again_submission["detail"] if again_submission else None).to_equal(
            {"codes": ["initial"]}
        )

    cases.append(
        _case(options, "detaches retained writes and every returned record", detaches_records)
    )

    # 4 ────────────────────────────────────────────────────────────────────────────────
    async def prototype_keys(storage: Any) -> None:
        root_id = await _create_root(storage)
        entry_id = await storage.mint_id()
        data = json.loads(
            '{"__proto__":{"polluted":false},"constructor":{"label":"stored"},"toString":"value"}'
        )
        await storage.commit(
            [_entry_write(_entry(entry_id, root_id, "note", {"data": data}))], context
        )

        data["__proto__"]["polluted"] = True
        data["constructor"]["label"] = "mutated"
        first_read = (await storage.entry(entry_id, context))["entry"]["data"]
        # Python 的 dict 没有原型链：`__proto__` 只是一个普通键，改动不会外溢。
        expect(type(first_read)).to_be(dict)
        expect("__proto__" in first_read).to_be(True)
        expect(first_read["__proto__"]).to_equal({"polluted": False})
        expect(first_read["constructor"]).to_equal({"label": "stored"})
        expect(first_read["toString"]).to_be("value")
        expect(dict[str, Any]().get("polluted")).to_be_undefined()  # noqa: SIM118 - 断言普通 dict 没有该键

        first_read["__proto__"]["polluted"] = True
        second_read = (await storage.entry(entry_id, context))["entry"]["data"]
        expect(second_read["__proto__"]).to_equal({"polluted": False})
        expect(second_read["constructor"]).to_equal({"label": "stored"})
        expect(second_read["toString"]).to_be("value")

    cases.append(
        _case(
            options,
            "detaches prototype-like JSON keys without changing object prototypes",
            prototype_keys,
        )
    )

    # 5 ────────────────────────────────────────────────────────────────────────────────
    async def out_of_order_ids(storage: Any) -> None:
        root_id = await _create_root(storage)
        await storage.commit(
            [
                _entry_write(_entry(id_from_number(30), root_id)),
                _entry_write(_entry(id_from_number(10), root_id)),
                _entry_write(
                    _entry(id_from_number(20), root_id, "marker", {"head": id_from_number(10)})
                ),
            ],
            context,
        )
        expect(
            _ids(await storage.scan_entries({"conversationId": root_id}, 10, None, context))
        ).to_equal([30, 20, 10])
        marker = await storage.find_latest_head_marker(root_id, None, context)
        expect(marker["id"] if marker else None).to_be(20)

    cases.append(_case(options, "indexes entries committed out of ID order", out_of_order_ids))

    # 6 ────────────────────────────────────────────────────────────────────────────────
    async def cursor_below_last_item(storage: Any) -> None:
        root_id = await _create_root(storage)
        oldest_id = await storage.mint_id()
        middle_id = await storage.mint_id()
        newest_id = await storage.mint_id()
        await storage.commit(
            [
                _entry_write(_entry(oldest_id, root_id)),
                _entry_write(_entry(middle_id, root_id)),
                _entry_write(_entry(newest_id, root_id)),
            ],
            context,
        )
        first = await storage.scan_entries({"conversationId": root_id}, 2, None, context)
        expect(_ids(first)).to_equal([newest_id, middle_id])
        appended_id = await storage.mint_id()
        await storage.commit([_entry_write(_entry(appended_id, root_id))], context)
        second = await storage.scan_entries({"conversationId": root_id}, 2, first["next"], context)
        expect(_ids(second)).to_equal([oldest_id])
        expect(second.get("next")).to_be_undefined()

    cases.append(
        _case(
            options,
            "continues an entry cursor below its last item after a newer commit",
            cursor_below_last_item,
        )
    )

    # 7 ────────────────────────────────────────────────────────────────────────────────
    async def paginates_conversations(storage: Any) -> None:
        root_id = await _create_root(storage)
        second_id = await storage.mint_id()
        third_id = await storage.mint_id()
        await storage.commit(
            [
                _conversation_write({"id": third_id}),
                _conversation_write({"id": second_id}),
            ],
            context,
        )
        first = await storage.scan_conversations({}, 2, None, context)
        expect(_ids(first)).to_equal([root_id, second_id])
        expect(first.get("next")).to_be_defined()
        cursor = _round_trip(first["next"])
        second = await storage.scan_conversations({}, 2, cursor, context)
        expect(_ids(second)).to_equal([third_id])
        expect(second.get("next")).to_be_undefined()

    cases.append(
        _case(
            options,
            "paginates conversations by opaque cursor in ascending ID order",
            paginates_conversations,
        )
    )

    # 8 ────────────────────────────────────────────────────────────────────────────────
    async def owner_edges(storage: Any) -> None:
        root_id = await _create_root(storage)
        other_owner_id = await storage.mint_id()
        first_task_id = await storage.mint_id()
        second_task_id = await storage.mint_id()
        first_id = await storage.mint_id()
        second_id = await storage.mint_id()
        third_id = await storage.mint_id()
        await storage.commit(
            [
                _conversation_write({"id": other_owner_id}),
                _conversation_write(
                    {"id": first_id, "owner": {"conversationId": root_id, "taskId": first_task_id}}
                ),
                _conversation_write(
                    {
                        "id": second_id,
                        "owner": {"conversationId": root_id, "taskId": second_task_id},
                    }
                ),
                _conversation_write(
                    {
                        "id": third_id,
                        "owner": {"conversationId": other_owner_id, "taskId": first_task_id},
                    }
                ),
            ],
            context,
        )
        first = await storage.scan_conversations({"ownerConversationId": root_id}, 1, None, context)
        expect(_ids(first)).to_equal([first_id])
        expect(first.get("next")).to_be_defined()
        second = await storage.scan_conversations(
            {"ownerConversationId": root_id}, 1, first["next"], context
        )
        expect(_ids(second)).to_equal([second_id])
        expect(second.get("next")).to_be_undefined()
        expect(
            _ids(
                await storage.scan_conversations({"ownerTaskId": first_task_id}, 10, None, context)
            )
        ).to_equal([first_id, third_id])
        expect(
            _ids(
                await storage.scan_conversations(
                    {"ownerConversationId": root_id, "ownerTaskId": first_task_id},
                    10,
                    None,
                    context,
                )
            )
        ).to_equal([first_id])

    cases.append(
        _case(options, "filters and pages conversations by durable owner edges", owner_edges)
    )

    # 9 ────────────────────────────────────────────────────────────────────────────────
    async def deep_fork_history(storage: Any) -> None:
        root_id = await _create_root(storage)
        root_first = await storage.mint_id()
        root_fork_point = await storage.mint_id()
        root_excluded_same_commit = await storage.mint_id()
        root_entries_seq = await storage.commit(
            [
                _entry_write(_entry(root_first, root_id)),
                _entry_write(_entry(root_fork_point, root_id, "marker", {"head": root_first})),
                _entry_write(_entry(root_excluded_same_commit, root_id)),
            ],
            context,
        )
        child_id = await storage.mint_id()
        await storage.commit(
            [
                _conversation_write(
                    {"id": child_id, "parent": {"conversationId": root_id, "at": root_fork_point}}
                )
            ],
            context,
        )
        child_fork_point = await storage.mint_id()
        child_excluded = await storage.mint_id()
        await storage.commit(
            [
                _entry_write(_entry(child_fork_point, child_id, "note")),
                _entry_write(_entry(child_excluded, child_id)),
            ],
            context,
        )
        root_excluded_later = await storage.mint_id()
        await storage.commit([_entry_write(_entry(root_excluded_later, root_id))], context)
        grandchild_id = await storage.mint_id()
        await storage.commit(
            [
                _conversation_write(
                    {
                        "id": grandchild_id,
                        "parent": {"conversationId": child_id, "at": child_fork_point},
                    }
                )
            ],
            context,
        )
        grandchild_head = await storage.mint_id()
        grandchild_tail = await storage.mint_id()
        grandchild_entries_seq = await storage.commit(
            [
                _entry_write(
                    _entry(grandchild_head, grandchild_id, "marker", {"head": grandchild_head})
                ),
                _entry_write(_entry(grandchild_tail, grandchild_id)),
            ],
            context,
        )
        child_excluded_later = await storage.mint_id()
        await storage.commit([_entry_write(_entry(child_excluded_later, child_id))], context)

        first = await storage.scan_entries({"conversationId": grandchild_id}, 2, None, context)
        expect(_ids(first)).to_equal([grandchild_tail, grandchild_head])
        second = await storage.scan_entries(
            {"conversationId": grandchild_id}, 2, first["next"], context
        )
        expect(_ids(second)).to_equal([child_fork_point, root_fork_point])
        third = await storage.scan_entries(
            {"conversationId": grandchild_id}, 2, second["next"], context
        )
        expect(_ids(third)).to_equal([root_first])
        expect(third.get("next")).to_be_undefined()

        ascending: dict[str, Any] = {"conversationId": grandchild_id, "order": "ascending"}
        first_up = await storage.scan_entries(ascending, 2, None, context)
        expect(_ids(first_up)).to_equal([root_first, root_fork_point])
        second_up = await storage.scan_entries(ascending, 2, first_up["next"], context)
        expect(_ids(second_up)).to_equal([child_fork_point, grandchild_head])
        third_up = await storage.scan_entries(
            {"conversationId": grandchild_id}, 2, second_up["next"], context
        )
        expect(_ids(third_up)).to_equal([grandchild_tail])
        expect(third_up.get("next")).to_be_undefined()
        expect(
            _ids(
                await storage.scan_entries(
                    {**ascending, "minEntryId": root_fork_point, "maxEntryId": child_fork_point},
                    10,
                    None,
                    context,
                )
            )
        ).to_equal([root_fork_point, child_fork_point])
        await expect_rejects(
            storage.scan_entries(
                {"conversationId": grandchild_id, "order": "descending"},
                2,
                first_up["next"],
                context,
            ),
            "cursor",
        )

        current_marker = await storage.find_latest_head_marker(grandchild_id, None, context)
        expect(current_marker["id"] if current_marker else None).to_be(grandchild_head)
        expect(current_marker["head"] if current_marker else None).to_be(grandchild_head)
        historical_marker = await storage.find_latest_head_marker(
            grandchild_id, child_fork_point, context
        )
        expect(historical_marker["id"] if historical_marker else None).to_be(root_fork_point)
        expect(historical_marker["head"] if historical_marker else None).to_be(root_first)
        expect(
            await storage.find_latest_head_marker(grandchild_id, root_first, context)
        ).to_be_undefined()

        active_first = await storage.scan_entries(
            {
                "conversationId": grandchild_id,
                "minEntryId": current_marker["head"] if current_marker else None,
            },
            1,
            None,
            context,
        )
        expect(_ids(active_first)).to_equal([grandchild_tail])
        expect(active_first.get("next")).to_be_defined()
        active_second = await storage.scan_entries(
            {
                "conversationId": grandchild_id,
                "minEntryId": current_marker["head"] if current_marker else None,
            },
            1,
            active_first["next"],
            context,
        )
        expect(_ids(active_second)).to_equal([grandchild_head])
        expect(active_second.get("next")).to_be_undefined()
        expect(
            _ids(
                await storage.scan_entries(
                    {
                        "conversationId": grandchild_id,
                        "minEntryId": historical_marker["head"] if historical_marker else None,
                        "maxEntryId": child_fork_point,
                    },
                    10,
                    None,
                    context,
                )
            )
        ).to_equal([child_fork_point, root_fork_point, root_first])

        expect(await storage.entry(root_first, context)).to_equal(
            {"entry": _entry(root_first, root_id), "commitSeq": root_entries_seq}
        )
        marker_entry = await storage.entry(root_fork_point, context)
        expect(marker_entry["commitSeq"] if marker_entry else None).to_be(root_entries_seq)
        gh = await storage.entry(grandchild_head, context)
        expect(gh["commitSeq"] if gh else None).to_be(grandchild_entries_seq)
        gt = await storage.entry(grandchild_tail, context)
        expect(gt["commitSeq"] if gt else None).to_be(grandchild_entries_seq)
        expect(await storage.entry(id_from_number(999_999), context)).to_be_undefined()

        expect(await storage.entry_in_conversation(grandchild_id, root_first, context)).to_equal(
            {"entry": _entry(root_first, root_id), "commitSeq": root_entries_seq}
        )
        cf = await storage.entry_in_conversation(grandchild_id, child_fork_point, context)
        expect(cf["entry"]["conversationId"] if cf else None).to_be(child_id)
        gtl = await storage.entry_in_conversation(grandchild_id, grandchild_tail, context)
        expect(gtl["commitSeq"] if gtl else None).to_be(grandchild_entries_seq)
        expect(
            await storage.entry_in_conversation(grandchild_id, root_excluded_same_commit, context)
        ).to_be_undefined()
        expect(
            await storage.entry_in_conversation(grandchild_id, root_excluded_later, context)
        ).to_be_undefined()
        expect(
            await storage.entry_in_conversation(grandchild_id, child_excluded, context)
        ).to_be_undefined()
        expect(
            await storage.entry_in_conversation(grandchild_id, child_excluded_later, context)
        ).to_be_undefined()
        expect(
            await storage.entry_in_conversation(root_id, grandchild_head, context)
        ).to_be_undefined()
        expect(
            await storage.entry_in_conversation(grandchild_id, id_from_number(999_999), context)
        ).to_be_undefined()
        await expect_rejects(
            storage.entry_in_conversation(id_from_number(999_999), root_first, context),
            "Unknown conversation",
        )
        await expect_rejects(
            storage.scan_entries({"conversationId": id_from_number(999_999)}, 10, None, context),
            "Unknown conversation",
        )

    cases.append(
        _case(
            options,
            "scans deep fork history newest-first through every ancestor cap",
            deep_fork_history,
        )
    )

    # 10 ───────────────────────────────────────────────────────────────────────────────
    async def either_order(storage: Any) -> None:
        root_id = await _create_root(storage)
        conversation_ids = [root_id]
        task_ids: list[Any] = []
        submission_ids: list[Any] = []
        for _ in range(3):
            conversation_id = await storage.mint_id()
            task_id = await storage.mint_id()
            submission_id = await storage.mint_id()
            await storage.commit(
                [
                    _conversation_write({"id": conversation_id}),
                    _task_write(_pending_task(task_id, root_id)),
                    _submission_write(
                        {
                            "id": submission_id,
                            "conversationId": root_id,
                            "type": "input",
                            "status": "queued",
                        }
                    ),
                ],
                context,
            )
            conversation_ids.append(conversation_id)
            task_ids.append(task_id)
            submission_ids.append(submission_id)

        async def scan_conversations(order: ScanOrder | None, cursor: Cursor | None) -> Any:
            query = {} if order is None else {"order": order}
            return await storage.scan_conversations(query, 2, cursor, context)

        async def scan_tasks(order: ScanOrder | None, cursor: Cursor | None) -> Any:
            query = {} if order is None else {"order": order}
            return await storage.scan_tasks(query, 2, cursor, context)

        async def scan_submissions(order: ScanOrder | None, cursor: Cursor | None) -> Any:
            query = {} if order is None else {"order": order}
            return await storage.scan_submissions(query, 2, cursor, context)

        scans = (
            (scan_conversations, conversation_ids),
            (scan_tasks, task_ids),
            (scan_submissions, submission_ids),
        )

        async def all_of(scan: Any, order: ScanOrder | None) -> list[Any]:
            found: list[Any] = []
            page = await scan(order, None)
            found.extend(_ids(page))
            while page.get("next") is not None:
                page = await scan(None, _round_trip(page["next"]))
                found.extend(_ids(page))
            return found

        for scan, ids in scans:
            reversed_ids = list(reversed(ids))
            expect(await all_of(scan, None)).to_equal(ids)
            expect(await all_of(scan, "ascending")).to_equal(ids)
            expect(await all_of(scan, "descending")).to_equal(reversed_ids)
            descending = await scan("descending", None)
            page = await scan("descending", descending["next"])
            expect(_ids(page)).to_equal(reversed_ids[2:4])
            await expect_rejects(scan("ascending", descending["next"]), "cursor")

    cases.append(
        _case(
            options,
            "scans tables in either ID order and continues a cursor in its order",
            either_order,
        )
    )

    # 11 ───────────────────────────────────────────────────────────────────────────────
    async def replaces_task_records(storage: Any) -> None:
        root_id = await _create_root(storage)
        first_id = await storage.mint_id()
        second_id = await storage.mint_id()
        third_id = await storage.mint_id()
        first = {**_pending_task(first_id, root_id), "memos": {"winner": "first"}}
        second = {**_pending_task(second_id, root_id), "background": True}
        third = {**_pending_task(third_id, root_id), "abortRequested": True}
        await storage.commit([_task_write(first), _task_write(second), _task_write(third)], context)
        running = {
            **first,
            "state": {"status": "running", "checkpoint": {"phase": "effect", "attempt": 1}},
            "abortRequested": True,
        }
        await storage.commit([_task_write(running)], context)
        expect(await storage.task(first_id, context)).to_equal(running)

        terminal = {
            "id": first_id,
            "conversationId": root_id,
            "kind": first["kind"],
            "version": first["version"],
            "input": first["input"],
            "state": {
                "status": "terminal",
                "outcome": {"status": "completed", "result": {"entryId": 99}},
            },
            "background": False,
            "abortRequested": True,
        }
        await storage.commit([_task_write(terminal)], context)
        expect(await storage.task(first_id, context)).to_equal(terminal)

        pending_page = await storage.scan_tasks({"status": "pending"}, 1, None, context)
        expect(_ids(pending_page)).to_equal([second_id])
        expect(pending_page.get("next")).to_be_defined()
        pending_second = await storage.scan_tasks(
            {"status": "pending"}, 1, pending_page["next"], context
        )
        expect(_ids(pending_second)).to_equal([third_id])
        expect(
            (
                await storage.scan_tasks(
                    {"status": "terminal", "abortRequested": True}, 10, None, context
                )
            )["items"]
        ).to_equal([terminal])
        expect(_ids(await storage.scan_tasks({"background": True}, 10, None, context))).to_equal(
            [second_id]
        )

    cases.append(
        _case(
            options,
            "replaces complete task records and pages filtered task scans",
            replaces_task_records,
        )
    )

    # 12 ───────────────────────────────────────────────────────────────────────────────
    async def waiting_and_completing(storage: Any) -> None:
        root_id = await _create_root(storage)
        owner_id = await storage.mint_id()
        waiting_id = await storage.mint_id()
        completing_id = await storage.mint_id()
        owner = _pending_task(owner_id, root_id)
        waiting = {
            **_pending_task(waiting_id, root_id),
            "owner": owner_id,
            "state": {
                "status": "waiting",
                "checkpoint": {"phase": "next"},
                "on": [owner_id],
                "policy": "allSettled",
            },
            "memos": {"kept": True},
        }
        base = {k: v for k, v in _pending_task(completing_id, root_id).items() if k != "state"}
        completing = {
            **base,
            "owner": owner_id,
            "state": {
                "status": "completing",
                "outcome": {"status": "failed", "error": {"message": "held"}},
            },
        }
        await storage.commit(
            [_task_write(owner), _task_write(waiting), _task_write(completing)], context
        )
        expect(await storage.task(waiting_id, context)).to_equal(waiting)
        expect(await storage.task(completing_id, context)).to_equal(completing)

        async def scan(status: str) -> Any:
            return (await storage.scan_tasks({"status": status}, 10, None, context))["items"]

        expect(await scan("waiting")).to_equal([waiting])
        expect(await scan("completing")).to_equal([completing])
        expect(_ids({"items": await scan("pending")})).to_equal([owner_id])
        terminal = {
            **completing,
            "state": {"status": "terminal", "outcome": completing["state"]["outcome"]},
        }
        await storage.commit([_task_write(terminal)], context)
        expect(await scan("completing")).to_equal([])
        expect(await scan("terminal")).to_equal([terminal])

    cases.append(
        _case(
            options,
            "stores owners and scans waiting and completing tasks by status",
            waiting_and_completing,
        )
    )

    # 13 ───────────────────────────────────────────────────────────────────────────────
    async def request_ids(storage: Any) -> None:
        root_id = await _create_root(storage)
        second_conversation_id = await storage.mint_id()
        await storage.commit([_conversation_write({"id": second_conversation_id})], context)
        first_id = await storage.mint_id()
        second_id = await storage.mint_id()
        other_conversation_submission_id = await storage.mint_id()
        first: SubmissionRecord = {
            "id": first_id,
            "conversationId": root_id,
            "requestId": "same",
            "type": "input",
            "status": "queued",
        }
        second: SubmissionRecord = {
            "id": second_id,
            "conversationId": root_id,
            "requestId": "other",
            "type": "input",
            "status": "queued",
        }
        other_conversation: SubmissionRecord = {
            "id": other_conversation_submission_id,
            "conversationId": second_conversation_id,
            "requestId": "same",
            "type": "input",
            "status": "queued",
        }
        await storage.commit(
            [
                _submission_write(first),
                _submission_write(second),
                _submission_write(other_conversation),
            ],
            context,
        )
        expect(await storage.submission_by_request(root_id, "same", context)).to_equal(first)
        expect(
            await storage.submission_by_request(second_conversation_id, "same", context)
        ).to_equal(other_conversation)

        placed_second: Any = {
            **second,
            "status": "placed",
            "entry": await storage.mint_id(),
        }
        await storage.commit([_submission_write(placed_second)], context)
        expect(await storage.submission(second_id, context)).to_equal(placed_second)
        expect(await storage.submission_by_request(root_id, "other", context)).to_equal(
            placed_second
        )

        async def scan_ids(query: dict[str, Any]) -> Any:
            found: list[Any] = []
            cursor: Cursor | None = None
            while True:
                page = await storage.scan_submissions(query, 1, cursor, context)
                found.extend(_ids(page))
                cursor = page.get("next")
                if cursor is None:
                    break
            return found

        expect(await scan_ids({})).to_equal([first_id, second_id, other_conversation_submission_id])
        expect(await scan_ids({"conversationId": root_id})).to_equal([first_id, second_id])
        expect(await scan_ids({"status": "queued"})).to_equal(
            [first_id, other_conversation_submission_id]
        )
        expect(await scan_ids({"status": "placed"})).to_equal([second_id])
        expect(
            await scan_ids({"conversationId": second_conversation_id, "status": "queued"})
        ).to_equal([other_conversation_submission_id])
        expect(
            await scan_ids({"conversationId": second_conversation_id, "status": "placed"})
        ).to_equal([])
        expect(
            (await storage.scan_submissions({"status": "placed"}, 10, None, context))["items"]
        ).to_equal([placed_second])

    cases.append(
        _case(
            options,
            "indexes request IDs per conversation and replaces complete submission records",
            request_ids,
        )
    )

    # 14 ───────────────────────────────────────────────────────────────────────────────
    async def passive_writes(storage: Any) -> None:
        root_id = await _create_root(storage)
        done_id = await storage.mint_id()
        failed_id = await storage.mint_id()
        queued_done: SubmissionRecord = {
            "id": done_id,
            "conversationId": root_id,
            "requestId": "passive-done",
            "type": "write",
            "status": "queued",
        }
        queued_failed: SubmissionRecord = {
            "id": failed_id,
            "conversationId": root_id,
            "requestId": "passive-failed",
            "type": "write",
            "status": "queued",
        }
        await storage.commit(
            [_submission_write(queued_done), _submission_write(queued_failed)], context
        )
        done: Any = {
            **queued_done,
            "status": "done",
            "entry": await storage.mint_id(),
        }
        unanswered: Any = {
            **queued_failed,
            "status": "unanswered",
            "reason": "closed",
            "detail": {"retryable": False},
        }
        await storage.commit([_submission_write(done), _submission_write(unanswered)], context)
        expect(await storage.submission(done_id, context)).to_equal(done)
        expect(await storage.submission_by_request(root_id, "passive-done", context)).to_equal(done)
        expect(await storage.submission(failed_id, context)).to_equal(unanswered)
        expect(await storage.submission_by_request(root_id, "passive-failed", context)).to_equal(
            unanswered
        )

    cases.append(
        _case(
            options,
            "stores passive write submissions without input-only lifecycle states",
            passive_writes,
        )
    )

    # 15 ───────────────────────────────────────────────────────────────────────────────
    async def rewindable_documents(storage: Any) -> None:
        root_id = await _create_root(storage)
        first_id = await storage.mint_id()
        first_record: DocumentCreate = {
            "id": first_id,
            "kind": "conversation.notes",
            "scope": {"kind": "conversation", "conversationId": root_id},
            "history": "rewindable",
            "fork": "asOf",
        }
        initial: dict[str, Any] = {"items": ["a"], "nested": {"count": 1}}
        created_at = await storage.commit([_document_create(first_record, initial)], context)
        appended = ["b"]
        ops: list[Op] = [
            ("p", ("items",), 1, 0, appended),  # type: ignore[list-item]
            ("s", ("nested", "count"), 2),
        ]
        changed_at = await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": first_id,
                    "content": {"kind": "delta", "version": 1, "ops": ops},
                }
            ],
            context,
        )

        initial["items"].append("caller mutation")
        appended.append("caller mutation")
        expect(await storage.document(first_id, created_at, context)).to_match_object(
            {"version": 1, "value": {"items": ["a"], "nested": {"count": 1}}, "deltasSinceBase": 0}
        )
        changed = await storage.document(first_id, changed_at, context)
        expect(changed["value"] if changed else None).to_equal(
            {"items": ["a", "b"], "nested": {"count": 2}}
        )
        expect(changed["deltasSinceBase"] if changed else None).to_be(1)
        changed["value"]["items"].append("read mutation")
        current = await storage.document(first_id, "current", context)
        expect(current["value"] if current else None).to_equal(
            {"items": ["a", "b"], "nested": {"count": 2}}
        )

        checkpoint_at = await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": first_id,
                    "content": {
                        "kind": "base",
                        "version": 2,
                        "value": {"items": ["checkpoint"], "nested": {"count": 3}},
                    },
                }
            ],
            context,
        )
        replaced_at = await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": first_id,
                    "content": {
                        "kind": "delta",
                        "version": 2,
                        "ops": [("r", {"items": ["replacement"], "nested": {"count": 4}})],
                    },
                }
            ],
            context,
        )
        expect(await storage.document(first_id, changed_at, context)).to_match_object(
            {"version": 1, "value": {"items": ["a", "b"], "nested": {"count": 2}}}
        )
        expect(await storage.document(first_id, checkpoint_at, context)).to_match_object(
            {
                "version": 2,
                "value": {"items": ["checkpoint"], "nested": {"count": 3}},
                "deltasSinceBase": 0,
            }
        )
        expect(await storage.document(first_id, replaced_at, context)).to_match_object(
            {"value": {"items": ["replacement"], "nested": {"count": 4}}, "deltasSinceBase": 1}
        )
        current_again = await storage.document(first_id, "current", context)
        expect(current_again["deltasSinceBase"] if current_again else None).to_be(1)

        second_id = await storage.mint_id()
        retired_at = await storage.commit(
            [
                _document_create({**first_record, "id": second_id}, {"items": ["new"]}),
                {"type": "document.retire", "id": first_id},
                {
                    "type": "document.change",
                    "id": first_id,
                    "content": {"kind": "delta", "version": 2, "ops": [("s", ("retiring",), True)]},
                },
            ],
            context,
        )
        address = {"kind": first_record["kind"], "scope": first_record["scope"]}
        found = await storage.find_document(address, changed_at, context)
        expect(found["id"] if found else None).to_be(first_id)
        expect(await storage.find_document(address, retired_at, context)).to_match_object(
            {"id": second_id, "createdAt": retired_at}
        )
        expect(
            _doc_ids(
                await storage.scan_documents(
                    {"scope": first_record["scope"], "at": changed_at}, 10, None, context
                )
            )
        ).to_equal([first_id])
        expect(
            _doc_ids(
                await storage.scan_documents(
                    {"scope": first_record["scope"], "at": retired_at}, 10, None, context
                )
            )
        ).to_equal([second_id])
        expect(await storage.document(first_id, retired_at, context)).to_be_undefined()
        second_doc = await storage.document(second_id, "current", context)
        expect(second_doc["value"] if second_doc else None).to_equal({"items": ["new"]})

    cases.append(
        _case(
            options,
            "reconstructs rewindable documents and preserves half-open incarnations",
            rewindable_documents,
        )
    )

    # 16 ───────────────────────────────────────────────────────────────────────────────
    async def long_document_tails(storage: Any) -> None:
        root_id = await _create_root(storage)
        id = await storage.mint_id()
        record: DocumentCreate = {
            "id": id,
            "kind": "conversation.long-tail",
            "scope": {"kind": "conversation", "conversationId": root_id},
            "history": "rewindable",
            "fork": "asOf",
        }
        initial: dict[str, Any] = {
            "revision": 0,
            "rows": [{"value": value, "stable": f"row-{value}"} for value in range(512)],
        }
        created_at = await storage.commit([_document_create(record, initial)], context)
        before_replacement: dict[str, Any] = copy.deepcopy(initial)
        before_replacement_at = created_at
        for revision in range(1, 25):
            index = (revision * 17) % len(before_replacement["rows"])
            before_replacement["rows"][index]["value"] = -revision
            before_replacement["revision"] = revision
            before_replacement_at = await storage.commit(
                [
                    {
                        "type": "document.change",
                        "id": id,
                        "content": {
                            "kind": "delta",
                            "version": 1,
                            "ops": [
                                ("s", ("rows", index, "value"), -revision),
                                ("s", ("revision",), revision),
                            ],
                        },
                    }
                ],
                context,
            )

        replacement: dict[str, Any] = {
            "revision": 100,
            "rows": [{"value": 10_000 + value, "stable": f"new-{value}"} for value in range(512)],
        }
        replacement_snapshot: dict[str, Any] = copy.deepcopy(replacement)
        replacement_at = await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": id,
                    "content": {"kind": "delta", "version": 1, "ops": [("r", replacement)]},
                }
            ],
            context,
        )
        replacement["rows"][0]["value"] = -999

        current: dict[str, Any] = copy.deepcopy(replacement_snapshot)
        for revision in range(101, 125):
            index = (revision * 19) % len(current["rows"])
            current["rows"][index]["value"] = -revision
            current["revision"] = revision
            await storage.commit(
                [
                    {
                        "type": "document.change",
                        "id": id,
                        "content": {
                            "kind": "delta",
                            "version": 1,
                            "ops": [
                                ("s", ("rows", index, "value"), -revision),
                                ("s", ("revision",), revision),
                            ],
                        },
                    }
                ],
                context,
            )

        at_created = await storage.document(id, created_at, context)
        expect(at_created["value"] if at_created else None).to_equal(initial)
        at_before = await storage.document(id, before_replacement_at, context)
        expect(at_before["value"] if at_before else None).to_equal(before_replacement)
        at_replacement = await storage.document(id, replacement_at, context)
        expect(at_replacement["value"] if at_replacement else None).to_equal(replacement_snapshot)
        read = await storage.document(id, "current", context)
        expect(read["value"] if read else None).to_equal(current)
        read["value"]["rows"][0]["value"] = -1_000
        read_again = await storage.document(id, "current", context)
        expect(read_again["value"] if read_again else None).to_equal(current)

    cases.append(
        _case(
            options,
            "streams long document tails across root replacement deltas",
            long_document_tails,
        )
    )

    # 17 ───────────────────────────────────────────────────────────────────────────────
    async def copies_document_bases(storage: Any) -> None:
        root_id = await _create_root(storage)
        child_id = await storage.mint_id()
        second_child_id = await storage.mint_id()
        await storage.commit(
            [_conversation_write({"id": child_id}), _conversation_write({"id": second_child_id})],
            context,
        )
        source_id = await storage.mint_id()
        source_record: DocumentCreate = {
            "id": source_id,
            "kind": "copy.source",
            "scope": {"kind": "conversation", "conversationId": root_id},
            "history": "rewindable",
            "fork": "asOf",
        }
        created_at = await storage.commit(
            [_document_create(source_record, {"count": 1, "rows": [{"value": "base"}]}, version=2)],
            context,
        )
        await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": source_id,
                    "content": {
                        "kind": "delta",
                        "version": 2,
                        "ops": [
                            ("s", ("count",), 2),
                            ("p", ("rows",), 1, 0, [{"value": "current"}]),
                        ],
                    },
                }
            ],
            context,
        )
        historical_copy_id = await storage.mint_id()
        current_copy_id = await storage.mint_id()
        retired_copy_id = await storage.mint_id()

        def child_record(id: Any, conversation_id: Any) -> DocumentCreate:
            return {
                "id": id,
                "kind": source_record["kind"],
                "scope": {"kind": "conversation", "conversationId": conversation_id},
                "history": "rewindable",
                "fork": "asOf",
            }

        await storage.commit(
            [
                {
                    "type": "document.copy",
                    "record": child_record(historical_copy_id, child_id),
                    "source": {"id": source_id, "at": created_at},
                },
                {
                    "type": "document.copy",
                    "record": child_record(current_copy_id, second_child_id),
                    "source": {"id": source_id, "at": "current"},
                },
                {
                    "type": "document.copy",
                    "record": child_record(retired_copy_id, root_id),
                    "source": {"id": source_id, "at": "current"},
                },
                {"type": "document.retire", "id": retired_copy_id},
            ],
            context,
        )
        expect(await storage.document(historical_copy_id, "current", context)).to_match_object(
            {"version": 2, "value": {"count": 1, "rows": [{"value": "base"}]}}
        )
        expect(await storage.document(current_copy_id, "current", context)).to_match_object(
            {"version": 2, "value": {"count": 2, "rows": [{"value": "base"}, {"value": "current"}]}}
        )
        expect(await storage.document(retired_copy_id, "current", context)).to_be_undefined()

        await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": source_id,
                    "content": {"kind": "base", "version": 2, "value": {"count": 99, "rows": []}},
                },
                {"type": "document.retire", "id": source_id},
            ],
            context,
        )
        detached = await storage.document(current_copy_id, "current", context)
        expect(detached["value"] if detached else None).to_equal(
            {"count": 2, "rows": [{"value": "base"}, {"value": "current"}]}
        )

        latest_source_id = await storage.mint_id()
        latest_copy_id = await storage.mint_id()
        latest_source: DocumentCreate = {
            "id": latest_source_id,
            "kind": "copy.latest",
            "scope": {"kind": "conversation", "conversationId": root_id},
            "history": "latest",
            "fork": "current",
        }
        await storage.commit(
            [_document_create(latest_source, {"retained": "copy"}, version=4)], context
        )
        await storage.commit(
            [
                {
                    "type": "document.copy",
                    "record": {
                        **latest_source,
                        "id": latest_copy_id,
                        "scope": {"kind": "conversation", "conversationId": child_id},
                    },
                    "source": {"id": latest_source_id, "at": "current"},
                }
            ],
            context,
        )
        await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": latest_source_id,
                    "content": {"kind": "base", "version": 4, "value": {"retained": "source-only"}},
                },
                {"type": "document.retire", "id": latest_source_id},
            ],
            context,
        )
        expect(await storage.document(latest_copy_id, "current", context)).to_match_object(
            {"version": 4, "value": {"retained": "copy"}}
        )

        conflict_id = await storage.mint_id()
        conflict_error: Any = None
        try:
            await storage.commit(
                [
                    {
                        "type": "document.copy",
                        "record": child_record(conflict_id, child_id),
                        "source": {"id": current_copy_id, "at": "current"},
                    },
                    {"type": "document.retire", "id": current_copy_id},
                ],
                context,
            )
        except Exception as error:  # noqa: BLE001 - 用例断言「抛了错」，具体类型无关
            conflict_error = error
        expect(isinstance(conflict_error, Exception)).to_be(True)
        expect(await storage.document(conflict_id, "current", context)).to_be_undefined()
        survivor = await storage.document(current_copy_id, "current", context)
        expect(survivor["value"] if survivor else None).to_equal(
            {"count": 2, "rows": [{"value": "base"}, {"value": "current"}]}
        )

        mismatch_id = await storage.mint_id()
        mismatch_error: Any = None
        try:
            await storage.commit(
                [
                    {
                        "type": "document.copy",
                        "record": {**child_record(mismatch_id, child_id), "kind": "copy.mismatch"},
                        "source": {"id": current_copy_id, "at": "current"},
                    }
                ],
                context,
            )
        except Exception as error:  # noqa: BLE001
            mismatch_error = error
        expect(isinstance(mismatch_error, Exception)).to_be(True)
        expect(await storage.document(mismatch_id, "current", context)).to_be_undefined()

    cases.append(
        _case(
            options,
            "copies stored document bases independently and rejects ambiguous sources",
            copies_document_bases,
        )
    )

    # 18 ───────────────────────────────────────────────────────────────────────────────
    async def version_transitions(storage: Any) -> None:
        await _create_root(storage)
        id = await storage.mint_id()
        record: DocumentCreate = {
            "id": id,
            "kind": "session.settings",
            "scope": {"kind": "session"},
        }
        await storage.commit([_document_create(record, {"count": 1})], context)
        await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": id,
                    "content": {"kind": "delta", "version": 1, "ops": [("s", ("count",), 2)]},
                }
            ],
            context,
        )
        migrated_at = await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": id,
                    "content": {"kind": "base", "version": 2, "value": {"count": 3}},
                }
            ],
            context,
        )
        expect(await storage.document(id, "current", context)).to_match_object(
            {"version": 2, "value": {"count": 3}}
        )
        await expect_rejects(
            storage.document(id, migrated_at, context), "does not retain historical content"
        )
        await expect_rejects(
            storage.commit(
                [
                    {
                        "type": "document.change",
                        "id": id,
                        "content": {"kind": "delta", "version": 1, "ops": [("s", ("count",), 4)]},
                    }
                ],
                context,
            ),
            "version transition requires a base",
        )
        unchanged = await storage.document(id, "current", context)
        expect(unchanged["value"] if unchanged else None).to_equal({"count": 3})
        await storage.commit([{"type": "document.retire", "id": id}], context)
        expect(await storage.document(id, "current", context)).to_be_undefined()

    cases.append(
        _case(
            options,
            "uses bases for version transitions and rejects historical reads of current-only documents",
            version_transitions,
        )
    )

    # 19 ───────────────────────────────────────────────────────────────────────────────
    async def logical_addresses(storage: Any) -> None:
        root_id = await _create_root(storage)
        first_id = await storage.mint_id()
        second_id = await storage.mint_id()
        conversation_id = await storage.mint_id()
        task_id = await storage.mint_id()
        task_singleton_id = await storage.mint_id()
        task_family_id = await storage.mint_id()
        task_other_kind_id = await storage.mint_id()
        created_at = await storage.commit(
            [
                _task_write(_pending_task(task_id, root_id)),
                _document_create(
                    {
                        "id": first_id,
                        "kind": "cache",
                        "scope": {"kind": "session"},
                        "key": "__proto__",
                    },
                    {"owner": "first"},
                ),
                _document_create(
                    {
                        "id": second_id,
                        "kind": "cache",
                        "scope": {"kind": "session"},
                        "key": "constructor",
                    },
                    {"owner": "second"},
                ),
                _document_create(
                    {
                        "id": conversation_id,
                        "kind": "cache",
                        "scope": {"kind": "conversation", "conversationId": root_id},
                        "history": "latest",
                        "fork": "current",
                        "key": "__proto__",
                    },
                    {"owner": "conversation"},
                ),
                _document_create(
                    {
                        "id": task_singleton_id,
                        "kind": "task.cache",
                        "scope": {"kind": "task", "taskId": task_id},
                    },
                    {"owner": "singleton"},
                ),
                _document_create(
                    {
                        "id": task_family_id,
                        "kind": "task.cache",
                        "scope": {"kind": "task", "taskId": task_id},
                        "key": "member",
                    },
                    {"owner": "family"},
                ),
                _document_create(
                    {
                        "id": task_other_kind_id,
                        "kind": "task.other",
                        "scope": {"kind": "task", "taskId": task_id},
                    },
                    {"owner": "other"},
                ),
            ],
            context,
        )
        found = await storage.find_document(
            {"kind": "cache", "scope": {"kind": "session"}, "key": "__proto__"}, "current", context
        )
        expect(found["id"] if found else None).to_be(first_id)
        expect(
            (
                await storage.scan_documents(
                    {"scope": {"kind": "session"}, "at": "current"}, 1, None, context
                )
            )["items"]
        ).to_have_length(1)
        first = await storage.scan_documents(
            {"scope": {"kind": "session"}, "at": "current"}, 1, None, context
        )
        second = await storage.scan_documents(
            {"scope": {"kind": "session"}, "at": "current"}, 1, first["next"], context
        )
        expect(_doc_ids({"items": [*first["items"], *second["items"]]})).to_equal(
            [first_id, second_id]
        )
        expect(
            _doc_ids(
                await storage.scan_documents(
                    {"scope": {"kind": "conversation", "conversationId": root_id}, "at": "current"},
                    10,
                    None,
                    context,
                )
            )
        ).to_equal([conversation_id])
        singleton = await storage.find_document(
            {"kind": "task.cache", "scope": {"kind": "task", "taskId": task_id}}, "current", context
        )
        expect(singleton["id"] if singleton else None).to_be(task_singleton_id)
        family = await storage.find_document(
            {"kind": "task.cache", "scope": {"kind": "task", "taskId": task_id}, "key": "member"},
            "current",
            context,
        )
        expect(family["id"] if family else None).to_be(task_family_id)
        expect(
            _doc_ids(
                await storage.scan_documents(
                    {
                        "scope": {"kind": "task", "taskId": task_id},
                        "at": "current",
                        "kind": "task.cache",
                    },
                    10,
                    None,
                    context,
                )
            )
        ).to_equal([task_singleton_id, task_family_id])
        await expect_rejects(
            storage.document(task_singleton_id, created_at, context),
            "does not retain historical content",
        )

    cases.append(
        _case(
            options,
            "indexes logical addresses and exact-scope scans independently",
            logical_addresses,
        )
    )

    # 20 ───────────────────────────────────────────────────────────────────────────────
    async def lifecycle_failures_atomic(storage: Any) -> None:
        root_id = await _create_root(storage)
        first_id = await storage.mint_id()
        second_id = await storage.mint_id()
        record: DocumentCreate = {
            "id": first_id,
            "kind": "singleton",
            "scope": {"kind": "session"},
        }
        await storage.commit([_document_create(record, {"value": 1})], context)
        await expect_rejects(
            storage.commit(
                [
                    _document_create({**record, "id": second_id}, {"value": 2}),
                    {
                        "type": "document.change",
                        "id": first_id,
                        "content": {"kind": "delta", "version": 1, "ops": []},
                    },
                ],
                context,
            ),
            "already has a current incarnation",
        )
        first_doc = await storage.document(first_id, "current", context)
        expect(first_doc["value"] if first_doc else None).to_equal({"value": 1})
        expect(await storage.document(second_id, "current", context)).to_be_undefined()

        empty_id = await storage.mint_id()
        empty_at = await storage.commit(
            [
                _document_create(
                    {
                        "id": empty_id,
                        "kind": record["kind"],
                        "key": "empty",
                        "scope": {"kind": "conversation", "conversationId": root_id},
                        "history": "rewindable",
                        "fork": "initial",
                    },
                    {},
                ),
                {"type": "document.retire", "id": empty_id},
            ],
            context,
        )
        expect(await storage.document(empty_id, "current", context)).to_be_undefined()
        expect(await storage.document(empty_id, empty_at, context)).to_be_undefined()
        expect(
            await storage.find_document(
                {
                    "kind": record["kind"],
                    "scope": {"kind": "conversation", "conversationId": root_id},
                    "key": "empty",
                },
                empty_at,
                context,
            )
        ).to_be_undefined()

    cases.append(
        _case(
            options,
            "keeps document lifecycle failures atomic and gives create-plus-retire an empty lifetime",
            lifecycle_failures_atomic,
        )
    )

    # 21 ───────────────────────────────────────────────────────────────────────────────
    async def rollback_tables_and_indexes(storage: Any) -> None:
        root_id = await _create_root(storage)
        task_id = await storage.mint_id()
        submission_id = await storage.mint_id()
        document_id = await storage.mint_id()
        task = _pending_task(task_id, root_id)
        submission: SubmissionRecord = {
            "id": submission_id,
            "conversationId": root_id,
            "requestId": "atomic",
            "type": "input",
            "status": "queued",
        }
        record: DocumentCreate = {
            "id": document_id,
            "kind": "atomic",
            "scope": {"kind": "session"},
        }
        baseline_seq = await storage.commit(
            [
                _task_write(task),
                _submission_write(submission),
                _document_create(record, {"count": 1}),
            ],
            context,
        )
        entry_id = await storage.mint_id()
        conflicting_document_id = await storage.mint_id()
        await expect_rejects(
            storage.commit(
                [
                    _task_write(
                        {**task, "state": {"status": "running", "checkpoint": {"phase": "effect"}}}
                    ),
                    _submission_write(
                        cast(
                            SubmissionRecord,
                            {**submission, "status": "unanswered", "reason": "failed"},
                        )
                    ),
                    _entry_write(_entry(entry_id, root_id, "transient")),
                    _document_create({**record, "id": conflicting_document_id}, {"count": 2}),
                ],
                context,
            ),
            "already has a current incarnation",
        )
        expect(await storage.task(task_id, context)).to_equal(task)
        expect(
            (await storage.scan_tasks({"status": "pending"}, 10, None, context))["items"]
        ).to_equal([task])
        expect(await storage.submission_by_request(root_id, "atomic", context)).to_equal(submission)
        expect(await storage.entry(entry_id, context)).to_be_undefined()
        expect(
            await storage.document(conflicting_document_id, "current", context)
        ).to_be_undefined()
        still_there = await storage.find_document(
            {"kind": record["kind"], "scope": record["scope"]}, "current", context
        )
        expect(still_there["id"] if still_there else None).to_be(document_id)
        after_rollback_seq = await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": document_id,
                    "content": {"kind": "delta", "version": 1, "ops": [("s", ("count",), 3)]},
                }
            ],
            context,
        )
        expect(after_rollback_seq).to_be_greater_than(baseline_seq)

    cases.append(
        _case(
            options,
            "rolls back record tables and secondary indexes when a document command fails",
            rollback_tables_and_indexes,
        )
    )

    # 22 ───────────────────────────────────────────────────────────────────────────────
    async def lossless_string_identities(storage: Any) -> None:
        root_id = await _create_root(storage)
        first = "\ud800"
        second = "\ud801"
        first_task_id = await storage.mint_id()
        second_task_id = await storage.mint_id()
        first_submission_id = await storage.mint_id()
        second_submission_id = await storage.mint_id()
        first_kind_document_id = await storage.mint_id()
        second_kind_document_id = await storage.mint_id()
        first_key_document_id = await storage.mint_id()
        second_key_document_id = await storage.mint_id()
        await storage.commit(
            [
                _task_write({**_pending_task(first_task_id, root_id), "kind": first}),
                _task_write({**_pending_task(second_task_id, root_id), "kind": second}),
                _submission_write(
                    {
                        "id": first_submission_id,
                        "conversationId": root_id,
                        "requestId": first,
                        "type": "input",
                        "status": "queued",
                    }
                ),
                _submission_write(
                    {
                        "id": second_submission_id,
                        "conversationId": root_id,
                        "requestId": second,
                        "type": "input",
                        "status": "queued",
                    }
                ),
                _document_create(
                    {"id": first_kind_document_id, "kind": first, "scope": {"kind": "session"}},
                    {"identity": "first kind"},
                ),
                _document_create(
                    {"id": second_kind_document_id, "kind": second, "scope": {"kind": "session"}},
                    {"identity": "second kind"},
                ),
                _document_create(
                    {
                        "id": first_key_document_id,
                        "kind": "family",
                        "key": first,
                        "scope": {"kind": "session"},
                    },
                    {"identity": "first key"},
                ),
                _document_create(
                    {
                        "id": second_key_document_id,
                        "kind": "family",
                        "key": second,
                        "scope": {"kind": "session"},
                    },
                    {"identity": "second key"},
                ),
            ],
            context,
        )
        expect(_ids(await storage.scan_tasks({"kind": first}, 10, None, context))).to_equal(
            [first_task_id]
        )
        expect(_ids(await storage.scan_tasks({"kind": second}, 10, None, context))).to_equal(
            [second_task_id]
        )
        first_task = await storage.task(first_task_id, context)
        expect(first_task["kind"] if first_task else None).to_be(first)
        second_task = await storage.task(second_task_id, context)
        expect(second_task["kind"] if second_task else None).to_be(second)
        by_first = await storage.submission_by_request(root_id, first, context)
        expect(by_first["requestId"] if by_first else None).to_be(first)
        expect(by_first["id"] if by_first else None).to_be(first_submission_id)
        by_second = await storage.submission_by_request(root_id, second, context)
        expect(by_second["id"] if by_second else None).to_be(second_submission_id)
        kind_first = await storage.find_document(
            {"kind": first, "scope": {"kind": "session"}}, "current", context
        )
        expect(kind_first["id"] if kind_first else None).to_be(first_kind_document_id)
        kind_second = await storage.find_document(
            {"kind": second, "scope": {"kind": "session"}}, "current", context
        )
        expect(kind_second["id"] if kind_second else None).to_be(second_kind_document_id)
        key_first = await storage.find_document(
            {"kind": "family", "key": first, "scope": {"kind": "session"}}, "current", context
        )
        expect(key_first["id"] if key_first else None).to_be(first_key_document_id)
        key_second = await storage.find_document(
            {"kind": "family", "key": second, "scope": {"kind": "session"}}, "current", context
        )
        expect(key_second["id"] if key_second else None).to_be(second_key_document_id)
        expect(
            _doc_ids(
                await storage.scan_documents(
                    {"scope": {"kind": "session"}, "at": "current", "kind": first},
                    10,
                    None,
                    context,
                )
            )
        ).to_equal([first_kind_document_id])

    cases.append(
        _case(options, "keeps indexed string identities lossless", lossless_string_identities)
    )

    # 23 ───────────────────────────────────────────────────────────────────────────────
    async def global_id_namespace(storage: Any) -> None:
        root_id = await _create_root(storage)
        explicit_entry_id = id_from_number(100)
        await storage.commit([_entry_write(_entry(explicit_entry_id, root_id))], context)
        expect(await storage.mint_id()).to_be(101)
        await expect_rejects(
            storage.commit(
                [_task_write(_pending_task(id_from_number(explicit_entry_id), root_id))], context
            ),
            f"ID {explicit_entry_id} already belongs to entry",
        )
        await storage.commit(
            [_entry_write(_entry(id_from_number(_MAX_SAFE_INTEGER), root_id, "last-id"))], context
        )
        await expect_rejects(storage.mint_id(), "ID space is exhausted")
        await expect_rejects(storage.mint_id(), "ID space is exhausted")

    cases.append(
        _case(
            options,
            "keeps one global record ID namespace and rejects exhausted ID minting",
            global_id_namespace,
        )
    )

    # 24 ───────────────────────────────────────────────────────────────────────────────
    async def rejects_after_close(storage: Any) -> None:
        await _create_root(storage)
        await storage.close(context)
        await expect_rejects(storage.conversation(ROOT_CONVERSATION_ID, context), "closed")
        await expect_rejects(storage.commit([], context), "closed")
        await expect_rejects(storage.mint_id(), "closed")

    cases.append(_case(options, "rejects every operation after close", rejects_after_close))

    return tuple(cases)
