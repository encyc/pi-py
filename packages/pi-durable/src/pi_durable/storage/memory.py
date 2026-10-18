"""内存参考后端 —— 移植 ``durable/src/storage/memory.ts``（864 行）。

``Storage`` 的**解除关联**（detached）参考实现。读出的值与留存下来的写入都刻意做深拷贝，
以匹配「序列化后端」的所有权边界——这是**后端一致性**要求，不是校验。

与上游的两处形状差异
--------------------

* **``clone`` 不需要处理「无原型对象」**：上游 ``clone``（``memory.ts:94-114``）要区分
  ``Object.getPrototypeOf(value) === null``，并用 ``Object.defineProperty`` 绕开
  ``__proto__`` / ``constructor`` 这类键在普通对象上会触发原型 setter 的问题
  （一致性套件专门有用例 *detaches prototype-like JSON keys without changing object
  prototypes*）。Python 的 ``dict`` 没有原型链，``d["__proto__"] = ...`` 就是普通键，
  所以这里用 :func:`copy.deepcopy` 即可，语义等价且更简单。
* **不冻结**：上游 ``prepareCommit`` 会 ``freeze()`` 深冻结留存写入（``memory.ts:116-120``）。
  Python 没有 ``Object.freeze`` 的对等物；本实现只保证**解除关联**（deepcopy），
  调用方改自己那份不会影响已提交状态。这已是一致性套件断言的性质。
"""

from __future__ import annotations

import copy
import math
from collections.abc import Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pi_chord.context import Context
from pi_chord.delta import Op, apply_immutable_batches
from pi_durable.errors import StorageRequestError
from pi_durable.ids import id_from_number, seq_from_number
from pi_durable.storage.scan import ScanStart, next_cursor, scan_start
from pi_durable.types import (
    ConversationId,
    ConversationQuery,
    ConversationRecord,
    Cursor,
    DocumentAddress,
    DocumentContent,
    DocumentCreate,
    DocumentId,
    DocumentPoint,
    DocumentQuery,
    DocumentRecord,
    EntryId,
    EntryQuery,
    EntryRecord,
    Id,
    Page,
    ScanOrder,
    Seq,
    StorageWrite,
    StoredDocument,
    SubmissionId,
    SubmissionQuery,
    SubmissionRecord,
    TaskId,
    TaskQuery,
    TaskRecord,
)

_T = TypeVar("_T")

__all__ = ["MemoryStorage", "PreparedMemoryCommit"]

_TABLE_NAMES = ("conversation", "entry", "task", "submission", "document")
_TASK_STATUSES = ("pending", "running", "waiting", "completing", "terminal")
_SUBMISSION_STATUSES = ("queued", "placed", "done", "unanswered")


def clone(value: _T) -> _T:
    """深拷贝一份解除关联的值（``memory.ts:94-114``）。"""
    return copy.deepcopy(value)


# --------------------------------------------------------------------------------------
# 有序 ID 数组的二分（memory.ts:130-169）
# --------------------------------------------------------------------------------------


def _lower_bound(ids: Sequence[int], target: float) -> int:
    """第一个 ``>= target`` 的下标。"""
    low, high = 0, len(ids)
    while low < high:
        middle = (low + high) >> 1
        if ids[middle] < target:
            low = middle + 1
        else:
            high = middle
    return low


def _upper_bound(ids: Sequence[int], target: float) -> int:
    """第一个 ``> target`` 的下标。``target`` 为 ``inf`` 时返回 ``len(ids)``。"""
    low, high = 0, len(ids)
    while low < high:
        middle = (low + high) >> 1
        if ids[middle] <= target:
            low = middle + 1
        else:
            high = middle
    return low


def _insert_sorted(ids: list[int], id: int) -> None:
    if not ids or ids[-1] < id:
        ids.append(id)
    else:
        ids.insert(_lower_bound(ids, id), id)


def _remove_sorted(ids: list[int], id: int) -> None:
    index = _lower_bound(ids, id)
    if index < len(ids) and ids[index] == id:
        del ids[index]


def _insert_map_id(index: dict[Any, list[int]], key: Any, id: int) -> None:
    ids = index.get(key)
    if ids is None:
        ids = []
        index[key] = ids
    _insert_sorted(ids, id)


# --------------------------------------------------------------------------------------
# 文档 key（memory.ts:171-200）
# --------------------------------------------------------------------------------------


def _scope_key(scope: Mapping[str, Any]) -> str:
    kind = scope["kind"]
    if kind == "session":
        return '["session"]'
    if kind == "conversation":
        return f'["conversation",{scope["conversationId"]}]'
    return f'["task",{scope["taskId"]}]'


def _address_key(address: Mapping[str, Any]) -> str:
    key = address.get("key")
    key_part = '["singleton"]' if key is None else f'["family",{_json_string(key)}]'
    return f"[{_json_string(address['kind'])},{_scope_key(address['scope'])},{key_part}]"


def _json_string(value: str) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _record_address_key(record: Mapping[str, Any]) -> str:
    return _address_key(
        {"kind": record["kind"], "scope": record["scope"], "key": record.get("key")}
    )


def _is_alive_at(record: DocumentRecord, at: DocumentPoint) -> bool:
    if at == "current":
        return record.get("retiredAt") is None
    return record["createdAt"] <= at and (
        record.get("retiredAt") is None or at < record["retiredAt"]
    )


def _is_current_only(record: DocumentRecord) -> bool:
    return record["scope"]["kind"] != "conversation" or record.get("history") == "latest"


# --------------------------------------------------------------------------------------
# 分页（memory.ts:202-221）
# --------------------------------------------------------------------------------------


def _page(values: list[Any], limit: int, order: ScanOrder = "ascending") -> Page:
    items = values[:limit]
    if len(values) <= limit:
        return {"items": clone(items)}
    return {"items": clone(items), "next": next_cursor(items[-1]["id"], order)}


def _scan_indexes(ids: Sequence[int], start: ScanStart) -> Generator[int, None, None]:
    """按扫描方向给出 ``ids`` 的下标，跳过游标之前的项（``memory.ts:213-221``）。"""
    if start.order == "ascending":
        first = 0 if start.after is None else _upper_bound(ids, start.after)
        yield from range(first, len(ids))
    else:
        end = len(ids) if start.after is None else _lower_bound(ids, start.after)
        yield from range(end - 1, -1, -1)


def _cursor_id(cursor: Cursor | None) -> int | None:
    """取游标里的 ``after``（``memory.ts:122-128``）。"""
    if cursor is None:
        return None
    after = cursor.get("after")
    if after is None:
        return None
    if not isinstance(after, int) or isinstance(after, bool) or abs(after) > 2**53 - 1:
        raise StorageRequestError("Invalid storage cursor")
    return after


# --------------------------------------------------------------------------------------
# 文档动作
# --------------------------------------------------------------------------------------


@dataclass
class _DocumentAction:
    create: DocumentCreate | None = None
    content: DocumentContent | None = None
    retire: bool = False


@dataclass
class _StoredDocumentState:
    record: DocumentRecord
    revisions: list[dict[str, Any]]


@dataclass
class _DocumentAddressIndex:
    ids: list[int] = field(default_factory=list)
    current_id: int | None = None


@dataclass
class _State:
    record_types: dict[int, str] = field(default_factory=dict)
    conversations: dict[int, ConversationRecord] = field(default_factory=dict)
    conversation_ids: list[int] = field(default_factory=list)
    conversation_ids_by_owner_conversation: dict[int, list[int]] = field(default_factory=dict)
    conversation_ids_by_owner_task: dict[int, list[int]] = field(default_factory=dict)
    entries: dict[int, EntryRecord] = field(default_factory=dict)
    entry_ids: dict[int, list[int]] = field(default_factory=dict)
    head_entry_ids: dict[int, list[int]] = field(default_factory=dict)
    entry_commit_seqs: dict[int, Seq] = field(default_factory=dict)
    tasks: dict[int, TaskRecord] = field(default_factory=dict)
    task_ids: list[int] = field(default_factory=list)
    task_ids_by_status: dict[str, list[int]] = field(
        default_factory=lambda: {status: [] for status in _TASK_STATUSES}
    )
    submissions: dict[int, SubmissionRecord] = field(default_factory=dict)
    submission_ids: list[int] = field(default_factory=list)
    submission_ids_by_status: dict[str, list[int]] = field(
        default_factory=lambda: {status: [] for status in _SUBMISSION_STATUSES}
    )
    submission_ids_by_request: dict[int, dict[str, int]] = field(default_factory=dict)
    documents: dict[int, _StoredDocumentState] = field(default_factory=dict)
    document_addresses: dict[str, _DocumentAddressIndex] = field(default_factory=dict)
    document_ids_by_scope: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class PreparedMemoryCommit:
    """一次已完整校验、已解除关联的状态变更；``apply()`` 不再做任何可能失败的动作
    （``memory.ts:223-229``）。"""

    seq: Seq
    writes: list[StorageWrite]
    _apply: Any

    def apply(self) -> Seq:
        return self._apply()  # type: ignore[no-any-return]


class MemoryStorage:
    """``Storage`` 的内存参考实现（``memory.ts:231-864``）。"""

    def __init__(self) -> None:
        self._state = _State()
        self._next_id = 2
        self._next_seq = 1
        self._closed = False

    # ---- 提交 ------------------------------------------------------------------------

    async def commit(self, writes: Sequence[StorageWrite], context: Context) -> Seq:
        del context
        return self.prepare_commit(list(writes)).apply()

    def prepare_commit(
        self, writes: Sequence[StorageWrite], seq: Seq | None = None
    ) -> PreparedMemoryCommit:
        """校验并解除关联一次提交，但**不改动可观察状态**（``memory.ts:267-289``）。"""
        self._assert_open()
        actual_seq = self._next_seq if seq is None else seq
        if not _is_safe_integer(actual_seq) or actual_seq < self._next_seq:
            raise RuntimeError(f"Commit sequence {actual_seq} does not strictly increase")

        detached = self._resolve_document_copies([clone(write) for write in writes])
        self._check_global_ids(detached)
        actions = self._prepare_document_actions(detached)
        self._check_document_actions(actions)

        applied = False

        def apply() -> Seq:
            nonlocal applied
            if not applied:
                applied = True
                self._apply_prepared_commit(detached, actions, actual_seq)
            return actual_seq

        return PreparedMemoryCommit(seq=seq_from_number(actual_seq), writes=detached, _apply=apply)

    def _resolve_document_copies(self, writes: list[StorageWrite]) -> list[StorageWrite]:
        """把 ``document.copy`` 就地解析成 ``document.create``（``memory.ts:291-328``）。"""
        if not any(write["type"] == "document.copy" for write in writes):
            return writes

        changed: set[int] = set()
        for write in writes:
            if write["type"] == "document.create" or write["type"] == "document.copy":
                changed.add(write["record"]["id"])
            elif write["type"] == "document.change" or write["type"] == "document.retire":
                changed.add(write["id"])

        resolved: list[StorageWrite] = []
        for write in writes:
            if write["type"] != "document.copy":
                resolved.append(write)
                continue
            assert write["type"] == "document.copy"
            try:
                source = write["source"]
                record = write["record"]
                if source["id"] in changed:
                    raise RuntimeError(
                        f"Fork source document {source['id']} is changed in the copy batch"
                    )
                stored = self._materialize_document(source["id"], source["at"])
                if stored is None:
                    raise RuntimeError(f"Fork source document {source['id']} cannot be read")
                source_record = stored["record"]
                if (
                    source_record["scope"]["kind"] != "conversation"
                    or record["scope"]["kind"] != "conversation"
                    or source_record["kind"] != record["kind"]
                    or source_record.get("key") != record.get("key")
                    or source_record.get("history") != record.get("history")
                    or source_record.get("fork") != record.get("fork")
                ):
                    raise RuntimeError(
                        f"Fork source document {source['id']} does not match the copied record"
                    )
                resolved.append(
                    {
                        "type": "document.create",
                        "record": record,
                        "content": {
                            "kind": "base",
                            "version": stored["version"],
                            "value": stored["value"],
                        },
                    }
                )
            except Exception as error:
                raise RuntimeError(f"Document copy {write['record']['id']} was rejected") from error
        return resolved

    def _apply_prepared_commit(
        self,
        prepared: list[StorageWrite],
        document_actions: dict[int, _DocumentAction],
        seq: Seq,
    ) -> Seq:
        state = self._state
        for write in prepared:
            # 每个分支用**各自的局部变量名**：mypy 把同一个变量名在首次赋值处定型，
            # 复用 `value` 会让后面几个分支拿到第一个分支的类型。
            if write["type"] == "conversation":
                conversation = write["value"]
                state.record_types[conversation["id"]] = "conversation"
                state.conversations[conversation["id"]] = conversation
                _insert_sorted(state.conversation_ids, conversation["id"])
                owner = conversation.get("owner")
                if owner is not None:
                    _insert_map_id(
                        state.conversation_ids_by_owner_conversation,
                        owner["conversationId"],
                        conversation["id"],
                    )
                    _insert_map_id(
                        state.conversation_ids_by_owner_task, owner["taskId"], conversation["id"]
                    )
                self._next_id = max(self._next_id, conversation["id"] + 1)
            elif write["type"] == "entry":
                entry = write["value"]
                state.record_types[entry["id"]] = "entry"
                state.entries[entry["id"]] = entry
                state.entry_commit_seqs[entry["id"]] = seq
                entry_ids = state.entry_ids.setdefault(entry["conversationId"], [])
                _insert_sorted(entry_ids, entry["id"])
                if entry.get("head") is not None:
                    heads = state.head_entry_ids.setdefault(entry["conversationId"], [])
                    _insert_sorted(heads, entry["id"])
                self._next_id = max(self._next_id, entry["id"] + 1)
            elif write["type"] == "task":
                task = write["value"]
                state.record_types[task["id"]] = "task"
                previous_task = state.tasks.get(task["id"])
                task_status = task["state"]["status"]
                if previous_task is None:
                    _insert_sorted(state.task_ids, task["id"])
                    _insert_sorted(state.task_ids_by_status[task_status], task["id"])
                elif previous_task["state"]["status"] != task_status:
                    _remove_sorted(
                        state.task_ids_by_status[previous_task["state"]["status"]], task["id"]
                    )
                    _insert_sorted(state.task_ids_by_status[task_status], task["id"])
                state.tasks[task["id"]] = task
                self._next_id = max(self._next_id, task["id"] + 1)
            elif write["type"] == "submission":
                submission = write["value"]
                state.record_types[submission["id"]] = "submission"
                previous_submission = state.submissions.get(submission["id"])
                submission_status = submission["status"]
                if previous_submission is None:
                    _insert_sorted(state.submission_ids, submission["id"])
                    _insert_sorted(
                        state.submission_ids_by_status[submission_status], submission["id"]
                    )
                elif previous_submission["status"] != submission_status:
                    _remove_sorted(
                        state.submission_ids_by_status[previous_submission["status"]],
                        submission["id"],
                    )
                    _insert_sorted(
                        state.submission_ids_by_status[submission_status], submission["id"]
                    )
                if (
                    previous_submission is not None
                    and previous_submission.get("requestId") is not None
                ):
                    previous_requests = state.submission_ids_by_request.get(
                        previous_submission["conversationId"]
                    )
                    if (
                        previous_requests is not None
                        and previous_requests.get(previous_submission["requestId"])
                        == submission["id"]
                    ):
                        del previous_requests[previous_submission["requestId"]]
                        if not previous_requests:
                            del state.submission_ids_by_request[
                                previous_submission["conversationId"]
                            ]
                state.submissions[submission["id"]] = submission
                if submission.get("requestId") is not None:
                    requests = state.submission_ids_by_request.setdefault(
                        submission["conversationId"], {}
                    )
                    requests[submission["requestId"]] = submission["id"]
                self._next_id = max(self._next_id, submission["id"] + 1)
            elif write["type"] == "document.copy":
                raise RuntimeError("Prepared document copy was not resolved")

        self._apply_document_actions(document_actions, seq)
        self._next_seq = seq + 1
        return seq

    # ---- 读取 ------------------------------------------------------------------------

    async def mint_id(self) -> Id:
        self._assert_open()
        if not _is_safe_integer(self._next_id):
            raise RuntimeError("ID space is exhausted")
        value = self._next_id
        self._next_id += 1
        return id_from_number(value)

    async def conversation(self, id: ConversationId, context: Context) -> ConversationRecord | None:
        del context
        self._assert_open()
        value = self._state.conversations.get(id)
        return None if value is None else clone(value)

    async def scan_conversations(
        self,
        query: ConversationQuery,
        limit: int,
        cursor: Cursor | None,
        context: Context,
    ) -> Page:
        del context
        self._assert_open()
        state = self._state
        if query.get("ownerTaskId") is not None:
            ids = state.conversation_ids_by_owner_task.get(query["ownerTaskId"], [])
        elif query.get("ownerConversationId") is not None:
            ids = state.conversation_ids_by_owner_conversation.get(query["ownerConversationId"], [])
        else:
            ids = state.conversation_ids

        start = scan_start(query.get("order"), cursor, "ascending")
        values: list[ConversationRecord] = []
        for index in _scan_indexes(ids, start):
            if len(values) > limit:
                break
            value = state.conversations[ids[index]]
            owner_conversation = query.get("ownerConversationId")
            if owner_conversation is not None:
                owner: Mapping[str, Any] = value.get("owner") or {}
                if owner.get("conversationId") != owner_conversation:
                    continue
            values.append(value)
        return _page(values, limit, start.order)

    async def entry(self, id: EntryId, context: Context) -> dict[str, Any] | None:
        del context
        self._assert_open()
        entry = self._state.entries.get(id)
        if entry is None:
            return None
        result: dict[str, Any] = {
            "entry": clone(entry),
            "commitSeq": self._state.entry_commit_seqs[id],
        }
        return result

    async def entry_in_conversation(
        self, conversation_id: ConversationId, id: EntryId, context: Context
    ) -> dict[str, Any] | None:
        del context
        self._assert_open()
        entry = next(self._visible_entries(conversation_id, id, id), None)
        if entry is None:
            return None
        found: dict[str, Any] = {
            "entry": clone(entry),
            "commitSeq": self._state.entry_commit_seqs[id],
        }
        return found

    async def find_latest_head_marker(
        self,
        conversation_id: ConversationId,
        at_or_before_entry_id: EntryId | None,
        context: Context,
    ) -> EntryRecord | None:
        del context
        self._assert_open()
        state = self._state
        if conversation_id not in state.conversations:
            raise StorageRequestError(f"Unknown conversation: {conversation_id}")
        current_id = conversation_id
        upper: float = math.inf if at_or_before_entry_id is None else at_or_before_entry_id
        while True:
            ids = state.head_entry_ids.get(current_id, [])
            index = _upper_bound(ids, upper) - 1
            if index >= 0:
                entry = state.entries[ids[index]]
                marker: EntryRecord = clone(entry)
                marker["head"] = entry["head"]
                return clone(marker)
            conversation = state.conversations[current_id]
            parent = conversation.get("parent")
            if parent is None:
                return None
            upper = min(upper, parent["at"])
            current_id = parent["conversationId"]

    async def scan_entries(
        self,
        query: EntryQuery,
        limit: int,
        cursor: Cursor | None,
        context: Context,
    ) -> Page:
        del context
        self._assert_open()
        start = scan_start(query.get("order"), cursor, "descending")
        # 游标收窄的是扫描「远离」的那一侧边界（``memory.ts:524-528``）。
        min_entry_id: float = query.get("minEntryId", -math.inf)
        max_entry_id: float = query.get("maxEntryId", math.inf)
        if start.after is not None and start.order == "descending":
            max_entry_id = min(max_entry_id, start.after - 1)
        if start.after is not None and start.order == "ascending":
            min_entry_id = max(min_entry_id, start.after + 1)

        entries = (
            self._visible_entries(query["conversationId"], min_entry_id, max_entry_id)
            if start.order == "descending"
            else self._visible_entries_ascending(
                query["conversationId"], min_entry_id, max_entry_id
            )
        )
        visible: list[EntryRecord] = []
        for entry in entries:
            visible.append(entry)
            if len(visible) > limit:
                break
        return _page(visible, limit, start.order)

    async def task(self, id: TaskId, context: Context) -> TaskRecord | None:
        del context
        self._assert_open()
        value = self._state.tasks.get(id)
        return None if value is None else clone(value)

    async def scan_tasks(
        self,
        query: TaskQuery,
        limit: int,
        cursor: Cursor | None,
        context: Context,
    ) -> Page:
        del context
        self._assert_open()
        start = scan_start(query.get("order"), cursor, "ascending")
        status = query.get("status")
        ids = self._state.task_ids if status is None else self._state.task_ids_by_status[status]
        values: list[TaskRecord] = []
        for index in _scan_indexes(ids, start):
            if len(values) > limit:
                break
            value = self._state.tasks[ids[index]]
            if (
                query.get("conversationId") is not None
                and value["conversationId"] != query["conversationId"]
            ):
                continue
            if query.get("kind") is not None and value["kind"] != query["kind"]:
                continue
            if (
                query.get("abortRequested") is not None
                and value["abortRequested"] != query["abortRequested"]
            ):
                continue
            if query.get("background") is not None and value["background"] != query["background"]:
                continue
            values.append(value)
        return _page(values, limit, start.order)

    async def submission(self, id: SubmissionId, context: Context) -> SubmissionRecord | None:
        del context
        self._assert_open()
        value = self._state.submissions.get(id)
        return None if value is None else clone(value)

    async def scan_submissions(
        self,
        query: SubmissionQuery,
        limit: int,
        cursor: Cursor | None,
        context: Context,
    ) -> Page:
        del context
        self._assert_open()
        start = scan_start(query.get("order"), cursor, "ascending")
        status = query.get("status")
        ids = (
            self._state.submission_ids
            if status is None
            else self._state.submission_ids_by_status[status]
        )
        values: list[SubmissionRecord] = []
        for index in _scan_indexes(ids, start):
            if len(values) > limit:
                break
            value = self._state.submissions[ids[index]]
            if (
                query.get("conversationId") is not None
                and value["conversationId"] != query["conversationId"]
            ):
                continue
            values.append(value)
        return _page(values, limit, start.order)

    async def submission_by_request(
        self, conversation_id: ConversationId, request_id: str, context: Context
    ) -> SubmissionRecord | None:
        del context
        self._assert_open()
        id = self._state.submission_ids_by_request.get(conversation_id, {}).get(request_id)
        if id is None:
            return None
        return clone(self._state.submissions[id])

    async def find_document(
        self, address: DocumentAddress, at: DocumentPoint, context: Context
    ) -> DocumentRecord | None:
        del context
        self._assert_open()
        index = self._state.document_addresses.get(_address_key(address))
        if at == "current":
            if index is None or index.current_id is None:
                return None
            return clone(self._state.documents[index.current_id].record)
        for id in index.ids if index is not None else []:
            record = self._state.documents[id].record
            if _is_alive_at(record, at):
                return clone(record)
        return None

    async def document(
        self, id: DocumentId, at: DocumentPoint, context: Context
    ) -> StoredDocument | None:
        del context
        self._assert_open()
        stored = self._materialize_document(id, at)
        if stored is None:
            return None
        return {
            "record": clone(stored["record"]),
            "version": stored["version"],
            "value": clone(stored["value"]),
            "deltasSinceBase": stored["deltasSinceBase"],
        }

    async def scan_documents(
        self,
        query: DocumentQuery,
        limit: int,
        cursor: Cursor | None,
        context: Context,
    ) -> Page:
        del context
        self._assert_open()
        ids = self._state.document_ids_by_scope.get(_scope_key(query["scope"]), [])
        after = _cursor_id(cursor)
        start = 0 if after is None else _upper_bound(ids, after)
        values: list[DocumentRecord] = []
        index = start
        while index < len(ids) and len(values) <= limit:
            record = self._state.documents[ids[index]].record
            index += 1
            if query.get("kind") is not None and record["kind"] != query["kind"]:
                continue
            if _is_alive_at(record, query["at"]):
                values.append(record)
        return _page(values, limit)

    async def close(self, context: Context) -> None:
        del context
        self._closed = True

    # ---- 内部：文档物化 ---------------------------------------------------------------

    def _materialize_document(self, id: DocumentId, at: DocumentPoint) -> StoredDocument | None:
        """按选定基点重放增量，物化出解除关联的值（``memory.ts:659-676``）。"""
        stored = self._state.documents.get(id)
        if stored is None:
            return None
        if at != "current" and _is_current_only(stored.record):
            raise StorageRequestError(f"Document {id} does not retain historical content")
        if not _is_alive_at(stored.record, at):
            return None

        revisions = (
            stored.revisions
            if at == "current"
            else [revision for revision in stored.revisions if revision["seq"] <= at]
        )
        base_index = len(revisions) - 1
        while base_index >= 0 and revisions[base_index]["kind"] != "base":
            base_index -= 1
        if base_index < 0:
            raise RuntimeError(f"Document {id} is missing a required base")
        base = revisions[base_index]
        value = apply_immutable_batches(
            base["value"],
            _document_delta_batches(id, base["version"], revisions, base_index + 1),
        )
        return {
            "record": stored.record,
            "version": base["version"],
            "value": value,
            "deltasSinceBase": len(revisions) - base_index - 1,
        }

    # ---- 内部：可见条目（fork 感知） --------------------------------------------------

    def _visible_entries(
        self,
        conversation_id: ConversationId,
        min_entry_id: float = -math.inf,
        max_entry_id: float = math.inf,
    ) -> Generator[EntryRecord, None, None]:
        """可见条目，**从新到旧**（``memory.ts:678-701``）。"""
        state = self._state
        if conversation_id not in state.conversations:
            raise StorageRequestError(f"Unknown conversation: {conversation_id}")
        current_id = conversation_id
        upper = max_entry_id
        while True:
            ids = state.entry_ids.get(current_id, [])
            for index in range(_upper_bound(ids, upper) - 1, -1, -1):
                id = ids[index]
                if id < min_entry_id:
                    break
                yield state.entries[id]
            conversation = state.conversations[current_id]
            parent = conversation.get("parent")
            if parent is None:
                break
            upper = min(upper, parent["at"])
            if upper < min_entry_id:
                break
            current_id = parent["conversationId"]

    def _visible_entries_ascending(
        self,
        conversation_id: ConversationId,
        min_entry_id: float = -math.inf,
        max_entry_id: float = math.inf,
    ) -> Generator[EntryRecord, None, None]:
        """可见条目，**从旧到新**：fork 链的各段从根会话向前（``memory.ts:703-731``）。"""
        state = self._state
        if conversation_id not in state.conversations:
            raise StorageRequestError(f"Unknown conversation: {conversation_id}")
        segments: list[tuple[int, float]] = []
        current_id = conversation_id
        upper = max_entry_id
        while True:
            segments.append((current_id, upper))
            conversation = state.conversations[current_id]
            parent = conversation.get("parent")
            if parent is None:
                break
            upper = min(upper, parent["at"])
            if upper < min_entry_id:
                break
            current_id = parent["conversationId"]

        for segment_id, segment_upper in reversed(segments):
            ids = state.entry_ids.get(segment_id, [])
            for index in range(_lower_bound(ids, min_entry_id), len(ids)):
                id = ids[index]
                if id > segment_upper:
                    break
                yield state.entries[id]

    # ---- 内部：校验 -------------------------------------------------------------------

    def _check_global_ids(self, writes: Sequence[StorageWrite]) -> None:
        """全局 ID 命名空间：一个 ID 只属于一种记录（``memory.ts:733-753``）。"""
        claimed: dict[int, str] = {}
        for write in writes:
            if write["type"] == "document.change" or write["type"] == "document.retire":
                continue
            if write["type"] == "document.create" or write["type"] == "document.copy":
                table = "document"
                id = write["record"]["id"]
            elif write["type"] == "conversation":
                table = "conversation"
                id = write["value"]["id"]
            elif write["type"] == "entry":
                table = "entry"
                id = write["value"]["id"]
            elif write["type"] == "task":
                table = "task"
                id = write["value"]["id"]
            else:
                table = "submission"
                id = write["value"]["id"]
            existing = self._state.record_types.get(id)
            earlier = claimed.get(id)
            if table in ("conversation", "entry", "document"):
                if existing is not None:
                    raise RuntimeError(f"ID {id} already belongs to {existing}")
                if earlier is not None:
                    raise RuntimeError(f"ID {id} is written more than once")
            else:
                if existing is not None and existing != table:
                    raise RuntimeError(f"ID {id} already belongs to {existing}")
                if earlier is not None and earlier != table:
                    raise RuntimeError(f"ID {id} is written as two record types")
            claimed[id] = table

    def _prepare_document_actions(
        self, writes: Sequence[StorageWrite]
    ) -> dict[int, _DocumentAction]:
        """把一批写入归并成每个文档化身一个动作（``memory.ts:755-786``）。"""
        actions: dict[int, _DocumentAction] = {}
        for write in writes:
            if write["type"] not in ("document.create", "document.change", "document.retire"):
                continue
            id = write["record"]["id"] if write["type"] == "document.create" else write["id"]
            action = actions.get(id)
            if action is None:
                action = _DocumentAction()
                actions[id] = action
            if write["type"] == "document.create":
                if action.create is not None or action.content is not None:
                    raise RuntimeError(f"Document {id} has more than one content command")
                action.create = write["record"]
                action.content = write["content"]
            elif write["type"] == "document.change":
                if action.content is not None:
                    raise RuntimeError(f"Document {id} has more than one content command")
                action.content = write["content"]
            else:
                if action.retire:
                    raise RuntimeError(f"Document {id} is retired more than once")
                action.retire = True
        return actions

    def _check_document_actions(self, actions: dict[int, _DocumentAction]) -> None:
        """校验存在性、版本迁移与「一个地址只能有一个当前化身」（``memory.ts:788-815``）。"""
        live_counts: dict[str, int] = {}
        for id, action in actions.items():
            existing = self._state.documents.get(id)
            if action.create is None and existing is None:
                raise RuntimeError(f"Unknown document: {id}")
            if action.create is not None and existing is not None:
                raise RuntimeError(f"Document {id} already exists")
            if existing is not None and existing.record.get("retiredAt") is not None:
                raise RuntimeError(f"Document {id} is retired")
            previous = (
                existing.revisions[-1] if existing is not None and existing.revisions else None
            )
            if action.content is not None and action.content["kind"] == "delta":
                if previous is None:
                    raise RuntimeError(f"Document {id} delta has no base")
                if previous["version"] != action.content["version"]:
                    raise RuntimeError(f"Document {id} version transition requires a base")

            key = _record_address_key(
                action.create if action.create is not None else existing.record  # type: ignore[union-attr]
            )
            index = self._state.document_addresses.get(key)
            current_id = index.current_id if index is not None else None
            live = live_counts.get(key)
            if live is None:
                live = 0 if current_id is None else 1
            if action.retire and current_id == id:
                live -= 1
            if action.create is not None and not action.retire:
                live += 1
            live_counts[key] = live

        for live in live_counts.values():
            if live > 1:
                raise RuntimeError("Document address already has a current incarnation")

    def _apply_document_actions(self, actions: dict[int, _DocumentAction], seq: Seq) -> None:
        """把文档动作落到状态上（``memory.ts:817-859``）。"""
        state = self._state
        for id, action in actions.items():
            stored = state.documents.get(id)
            if action.create is not None:
                record: DocumentRecord = dict(action.create)  # type: ignore[assignment]
                record["createdAt"] = seq
                if action.retire:
                    record["retiredAt"] = seq
                content = dict(action.content)  # type: ignore[arg-type]
                content["seq"] = seq
                stored = _StoredDocumentState(record=record, revisions=[content])
                state.record_types[id] = "document"
                state.documents[id] = stored

                key = _record_address_key(record)
                address = state.document_addresses.get(key)
                if address is None:
                    address = _DocumentAddressIndex()
                    state.document_addresses[key] = address
                _insert_sorted(address.ids, id)

                scope_key = _scope_key(record["scope"])
                scope_ids = state.document_ids_by_scope.setdefault(scope_key, [])
                _insert_sorted(scope_ids, id)
                self._next_id = max(self._next_id, id + 1)
            elif action.content is not None:
                revision = dict(action.content)
                revision["seq"] = seq
                assert stored is not None
                if revision["kind"] == "base" and _is_current_only(stored.record):
                    stored.revisions = [revision]
                else:
                    stored.revisions.append(revision)

            assert stored is not None
            if action.retire and action.create is None:
                stored.record = {**stored.record, "retiredAt": seq}
            if action.retire and _is_current_only(stored.record):
                stored.revisions = []
            if action.create is not None or action.retire:
                address = state.document_addresses[_record_address_key(stored.record)]
                if action.retire and address.current_id == id:
                    address.current_id = None
                if action.create is not None and not action.retire:
                    address.current_id = id

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("MemoryStorage is closed")


def _is_safe_integer(value: object) -> bool:
    """JS ``Number.isSafeInteger``。"""
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) <= 2**53 - 1


def _document_delta_batches(
    id: DocumentId,
    version: int,
    revisions: Sequence[dict[str, Any]],
    start: int,
) -> Iterable[Sequence[Op]]:
    """选定基点之后的增量序列；跨过版本边界而没有基点时报错（``memory.ts:52-65``）。"""
    for index in range(start, len(revisions)):
        revision = revisions[index]
        if revision["kind"] != "delta" or revision["version"] != version:
            raise RuntimeError(f"Document {id} crosses a stored version boundary without a base")
        yield revision["ops"]
