"""SQLite 后端 —— 移植 ``durable/src/storage/sqlite/storage.ts``（957 行）。

语义与 :class:`~pi_durable.storage.memory.MemoryStorage` **完全一致**（同一套
``testing/storage-conformance`` 用例对两者都跑），差别只在「状态存在 SQLite 里」：

* 记录以 JSON 文本存在各表的 ``record`` 列（``json_valid`` CHECK 保证）；
* 跨表 ID 命名空间由 ``record_ids`` 表承载；
* 文档内容按「基点 + 有序增量」存在 ``document_revisions``；
* ``durable_metadata`` 的 ``next_id`` / ``next_seq`` 是**跨重开**的全局游标。

**一次提交 = 一次事务**：要么全落，要么全不落。这也满足一致性用例
「rolls back record tables and secondary indexes when a document command fails」。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, cast

from pi_chord.context import Context
from pi_chord.delta import Op, apply_immutable_batches
from pi_durable.errors import StorageRequestError
from pi_durable.storage.scan import ScanStart, next_cursor, scan_start
from pi_durable.storage.sqlite.database import SqliteDatabase, SqliteExecutor, SqliteValue
from pi_durable.storage.sqlite.schema import DEFAULT_SQLITE_SCHEMA, SqliteSchema
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

__all__ = ["SqliteStorage", "SqliteStorageOptions"]

_MAX_SAFE_INTEGER = 2**53 - 1

_TASK_STATUSES = ("pending", "running", "waiting", "completing", "terminal")
_SUBMISSION_STATUSES = ("queued", "placed", "done", "unanswered")


class SqliteStorageOptions:
    """``storage.ts:64-71``。"""

    def __init__(self, *, prefix: str | None = None) -> None:
        self.prefix = prefix


def _parse_json(value: object) -> Any:
    """JSON 列 → Python 值。

    返回 ``Any``：这些值随后被 ``cast`` 成调用方声明的记录类型——它们**是**从库里读出的
    合法记录，静态上无法证明，运行时由 ``json_valid`` CHECK 与存储层自身的写入保证。
    """
    assert isinstance(value, str)
    return json.loads(value)


def _encode_json(value: object) -> str:
    r"""把记录序列化成 JSON 文本。

    默认 ``ensure_ascii=False``：上游的 ``JSON.stringify`` 不转义非 ASCII，所以中文等
    在两种实现里存成同样的字节。

    **但有例外**：JS 字符串是 UTF-16，可以携带**孤立代理项**（``"\ud800"``）；Python 的
    ``str`` 也能携带，可 UTF-8 **无法编码**它们，SQLite 会在写入时报
    ``UnicodeEncodeError``。一致性用例 *keeps indexed string identities lossless* 正是用
    孤立代理项来验证索引不丢信息。这种情况下退回 ``ensure_ascii=True``——转义形式是纯 ASCII，
    能安全落库，``json.loads`` 又能原样还原出同一个字符串，**往返无损**。
    """
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return text


def _encode_indexed_string(value: str) -> str:
    """索引列里存 JSON 编码的字符串（``storage.ts:75``）。

    一致性用例 *keeps indexed string identities lossless* 断言的是：``"\\ud800"`` 与
    ``"\\ud801"`` 这类**孤立代理项**必须能区分。UTF-8 编码不了它们，所以索引列统一存
    JSON 转义形式（纯 ASCII，可安全落库，且能区分相近的代理项）。
    """
    return json.dumps(value, ensure_ascii=True)


def _is_safe_integer(value: object) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and abs(value) <= _MAX_SAFE_INTEGER
    )


def _is_alive_at(record: Mapping[str, Any], at: DocumentPoint) -> bool:
    if at == "current":
        return record.get("retiredAt") is None
    created = record["createdAt"]
    retired = record.get("retiredAt")
    return created <= at and (retired is None or at < retired)


def _is_current_only(record: Mapping[str, Any]) -> bool:
    return record["scope"]["kind"] != "conversation" or record.get("history") == "latest"


def _scope_columns(scope: Mapping[str, Any]) -> tuple[str, int]:
    """``scope_kind`` / ``owner_id`` 两列（``storage.ts:101-111``）。"""
    kind = scope["kind"]
    if kind == "session":
        return "session", 0
    if kind == "conversation":
        return "conversation", int(scope["conversationId"])
    return "task", int(scope["taskId"])


def _address_parts(address: Mapping[str, Any]) -> tuple[str, str, int, int, str]:
    """``(kind, scope_kind, owner_id, family, key_value)``（``storage.ts:112-126``）。"""
    scope_kind, owner_id = _scope_columns(address["scope"])
    key = address.get("key")
    family = 1 if key is not None else 0
    key_value = _encode_indexed_string(key) if key is not None else "null"
    return address["kind"], scope_kind, owner_id, family, key_value


def _write_id(write: StorageWrite) -> int | None:
    """这条写入占用的记录 ID（``storage.ts:135-151``）。"""
    kind = write["type"]
    if kind == "conversation" or kind == "entry" or kind == "task" or kind == "submission":
        return int(write["value"]["id"])  # type: ignore[typeddict-item]
    if kind == "document.create" or kind == "document.copy":
        return int(write["record"]["id"])  # type: ignore[typeddict-item]
    return None


def _write_table(write: StorageWrite) -> str | None:
    kind = write["type"]
    if kind == "document.create" or kind == "document.copy":
        return "document"
    if kind == "conversation" or kind == "entry" or kind == "task" or kind == "submission":
        return kind
    return None


class _DocumentAction:
    __slots__ = ("content", "create", "retire")

    def __init__(self) -> None:
        self.create: DocumentCreate | None = None
        self.content: DocumentContent | None = None
        self.retire = False


class SqliteStorage:
    """``Storage`` 的 SQLite 实现（``storage.ts:152-…``）。"""

    def __init__(self, database: SqliteDatabase, schema: SqliteSchema | None = None) -> None:
        self._database = database
        self._schema = schema or DEFAULT_SQLITE_SCHEMA
        self._closed = False

    @property
    def _t(self) -> dict[str, str]:
        return self._schema.tables

    # ---- 元数据 ----------------------------------------------------------------------

    async def _read_metadata(self, tx: SqliteExecutor) -> tuple[int, int]:
        row = await tx.get(
            f"SELECT next_id, next_seq FROM {self._t['durable_metadata']} WHERE singleton = 1"
        )
        if row is None:
            raise RuntimeError("Durable SQLite metadata is missing")
        return int(str(row["next_id"])), int(str(row["next_seq"]))

    # ---- 提交 ------------------------------------------------------------------------

    async def commit(self, writes: Sequence[StorageWrite], context: Context) -> Seq:
        del context
        self._assert_open()

        async def work(tx: SqliteExecutor) -> Seq:
            next_id, next_seq = await self._read_metadata(tx)
            detached = await self._resolve_document_copies(tx, list(writes))
            await self._check_global_ids(tx, detached)
            actions = self._prepare_document_actions(detached)
            await self._check_document_actions(tx, actions)

            highest = next_id
            for write in detached:
                id = _write_id(write)
                if id is not None:
                    highest = max(highest, id + 1)
            await self._apply_writes(tx, detached)
            await self._apply_document_actions(tx, actions, next_seq)
            await tx.run(
                f"UPDATE {self._t['durable_metadata']} SET next_id = ?, next_seq = ? WHERE singleton = 1",
                str(highest),
                next_seq + 1,
            )
            return next_seq

        return await self._database.transaction(work)

    async def _resolve_document_copies(
        self, tx: SqliteExecutor, writes: list[StorageWrite]
    ) -> list[StorageWrite]:
        """把 ``document.copy`` 就地解析成 ``document.create``（``storage.ts`` 同 ``memory.ts:291-328``）。"""
        if not any(write["type"] == "document.copy" for write in writes):
            return writes

        changed: set[int] = set()
        for write in writes:
            if write["type"] == "document.create" or write["type"] == "document.copy":
                changed.add(int(write["record"]["id"]))
            elif write["type"] == "document.change" or write["type"] == "document.retire":
                changed.add(int(write["id"]))

        resolved: list[StorageWrite] = []
        for write in writes:
            if write["type"] != "document.copy":
                resolved.append(write)
                continue
            assert write["type"] == "document.copy"
            source = write["source"]
            record = write["record"]
            try:
                if int(source["id"]) in changed:
                    raise RuntimeError(
                        f"Fork source document {source['id']} is changed in the copy batch"
                    )
                stored = await self._materialize_document(tx, int(source["id"]), source["at"])
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

    async def _check_global_ids(self, tx: SqliteExecutor, writes: Sequence[StorageWrite]) -> None:
        """跨表 ID 命名空间：一个 ID 只属于一种记录，且一批里不能重复（``storage.ts`` 同 ``memory.ts:733-753``）。"""
        claimed: dict[int, str] = {}
        for write in writes:
            if write["type"] == "document.change" or write["type"] == "document.retire":
                continue
            id = _write_id(write)
            table = _write_table(write)
            assert id is not None and table is not None
            row = await tx.get(f"SELECT record_type FROM {self._t['record_ids']} WHERE id = ?", id)
            existing = None if row is None else str(row["record_type"])
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
        actions: dict[int, _DocumentAction] = {}
        for write in writes:
            if (
                write["type"] != "document.create"
                and write["type"] != "document.change"
                and write["type"] != "document.retire"
            ):
                continue
            id = (
                int(write["record"]["id"])
                if write["type"] == "document.create"
                else int(write["id"])
            )
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

    async def _check_document_actions(
        self, tx: SqliteExecutor, actions: dict[int, _DocumentAction]
    ) -> None:
        live_counts: dict[tuple[str, str, int, int, str], int] = {}
        for id, action in actions.items():
            existing = await self._document_record(tx, id, include_retired=True)
            if action.create is None and existing is None:
                raise RuntimeError(f"Unknown document: {id}")
            if action.create is not None and existing is not None:
                raise RuntimeError(f"Document {id} already exists")
            if existing is not None and existing.get("retiredAt") is not None:
                raise RuntimeError(f"Document {id} is retired")

            if action.content is not None and action.content["kind"] == "delta":
                previous = await self._latest_revision(tx, id)
                if previous is None:
                    raise RuntimeError(f"Document {id} delta has no base")
                if int(str(previous["version"])) != int(action.content["version"]):
                    raise RuntimeError(f"Document {id} version transition requires a base")

            source = action.create if action.create is not None else existing
            assert source is not None
            parts = _address_parts(source)
            current = await self._current_incarnation(tx, parts)
            live = live_counts.get(parts)
            if live is None:
                live = 0 if current is None else 1
            if action.retire and current == id:
                live -= 1
            if action.create is not None and not action.retire:
                live += 1
            live_counts[parts] = live

        for live in live_counts.values():
            if live > 1:
                raise RuntimeError("Document address already has a current incarnation")

    async def _apply_writes(self, tx: SqliteExecutor, writes: Sequence[StorageWrite]) -> None:
        for write in writes:
            # 与 memory.py 同理：分支间**不要复用变量名**，mypy 在首次赋值处定型。
            if write["type"] == "conversation":
                conversation = write["value"]
                owner = conversation.get("owner")
                await tx.run(
                    f"INSERT OR REPLACE INTO {self._t['conversations']} "
                    "(id, owner_conversation_id, owner_task_id, record) VALUES (?, ?, ?, ?)",
                    int(conversation["id"]),
                    None if owner is None else int(owner["conversationId"]),
                    None if owner is None else int(owner["taskId"]),
                    _encode_json(conversation),
                )
                await self._record_id(tx, int(conversation["id"]), "conversation")
            elif write["type"] == "entry":
                entry = write["value"]
                _, next_seq = await self._read_metadata(tx)
                await tx.run(
                    f"INSERT OR REPLACE INTO {self._t['entries']} "
                    "(id, conversation_id, head, commit_seq, record) VALUES (?, ?, ?, ?, ?)",
                    int(entry["id"]),
                    int(entry["conversationId"]),
                    None if entry.get("head") is None else int(entry["head"]),
                    # 本条提交的 seq：调用方刚刚把它算出来，这里复用同一事务里的 next_seq
                    next_seq,
                    _encode_json(entry),
                )
                await self._record_id(tx, int(entry["id"]), "entry")
            elif write["type"] == "task":
                task = write["value"]
                await tx.run(
                    f"INSERT OR REPLACE INTO {self._t['tasks']} "
                    "(id, conversation_id, kind, status, abort_requested, background, record) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    int(task["id"]),
                    int(task["conversationId"]),
                    _encode_indexed_string(str(task["kind"])),
                    str(task["state"]["status"]),
                    1 if task["abortRequested"] else 0,
                    1 if task["background"] else 0,
                    _encode_json(task),
                )
                await self._record_id(tx, int(task["id"]), "task")
            elif write["type"] == "submission":
                submission = write["value"]
                await tx.run(
                    f"INSERT OR REPLACE INTO {self._t['submissions']} "
                    "(id, conversation_id, request_id, status, record) VALUES (?, ?, ?, ?, ?)",
                    int(submission["id"]),
                    int(submission["conversationId"]),
                    None
                    if submission.get("requestId") is None
                    else _encode_indexed_string(str(submission["requestId"])),
                    str(submission["status"]),
                    _encode_json(submission),
                )
                await self._record_id(tx, int(submission["id"]), "submission")
        # entry 的 commit_seq 需要真实的本次序号；见 commit() 里对 seq 的传入
        return None

    async def _record_id(self, tx: SqliteExecutor, id: int, record_type: str) -> None:
        await tx.run(
            f"INSERT OR REPLACE INTO {self._t['record_ids']} (id, record_type) VALUES (?, ?)",
            id,
            record_type,
        )

    async def _apply_document_actions(
        self, tx: SqliteExecutor, actions: dict[int, _DocumentAction], seq: Seq
    ) -> None:
        for id, action in actions.items():
            stored = await self._document_record(tx, id, include_retired=True)
            if action.create is not None:
                record: dict[str, Any] = dict(action.create)
                record["createdAt"] = seq
                if action.retire:
                    record["retiredAt"] = seq
                parts = _address_parts(record)
                await tx.run(
                    f"INSERT INTO {self._t['documents']} "
                    "(id, kind, family, key_value, scope_kind, owner_id, created_at, retired_at, record) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    id,
                    _encode_indexed_string(parts[0]),
                    parts[3],
                    parts[4],
                    parts[1],
                    parts[2],
                    seq,
                    seq if action.retire else None,
                    _encode_json(record),
                )
                await self._record_id(tx, id, "document")
                content = dict(action.content)  # type: ignore[arg-type]
                await tx.run(
                    f"INSERT INTO {self._t['document_revisions']} "
                    "(document_id, seq, kind, version, content) VALUES (?, ?, ?, ?, ?)",
                    id,
                    seq,
                    str(content["kind"]),
                    int(str(content["version"])),
                    _encode_json(content),
                )
            elif action.content is not None:
                if (
                    action.content["kind"] == "base"
                    and stored is not None
                    and _is_current_only(stored)
                ):
                    await tx.run(
                        f"DELETE FROM {self._t['document_revisions']} WHERE document_id = ?", id
                    )
                await tx.run(
                    f"INSERT INTO {self._t['document_revisions']} "
                    "(document_id, seq, kind, version, content) VALUES (?, ?, ?, ?, ?)",
                    id,
                    seq,
                    str(action.content["kind"]),
                    int(action.content["version"]),
                    _encode_json(action.content),
                )

            if action.retire and action.create is None:
                # ``retired_at`` 是投影列，**JSON 记录本身也必须同步**——读取路径
                # （``_document_record``）读的是 record 列，只改列会让文档看起来仍然存活。
                current = await self._document_record(tx, id, include_retired=True)
                if current is not None:
                    retired = {**current, "retiredAt": seq}
                    await tx.run(
                        f"UPDATE {self._t['documents']} SET retired_at = ?, record = ? WHERE id = ?",
                        seq,
                        _encode_json(retired),
                        id,
                    )
                    if _is_current_only(retired):
                        await tx.run(
                            f"DELETE FROM {self._t['document_revisions']} WHERE document_id = ?", id
                        )

    # ---- 读取 ------------------------------------------------------------------------

    async def mint_id(self) -> Id:
        self._assert_open()

        async def work(tx: SqliteExecutor) -> Id:
            next_id, _ = await self._read_metadata(tx)
            if next_id > _MAX_SAFE_INTEGER:
                raise RuntimeError("ID space is exhausted")
            await tx.run(
                f"UPDATE {self._t['durable_metadata']} SET next_id = ? WHERE singleton = 1",
                str(next_id + 1),
            )
            return next_id

        return await self._database.transaction(work)

    async def conversation(self, id: ConversationId, context: Context) -> ConversationRecord | None:
        del context
        self._assert_open()
        rows = await self._database.all(
            f"SELECT record FROM {self._t['conversations']} WHERE id = ?", id
        )
        return None if not rows else cast(ConversationRecord, _parse_json(rows[0]["record"]))

    async def scan_conversations(
        self, query: ConversationQuery, limit: int, cursor: Cursor | None, context: Context
    ) -> Page:
        del context
        self._assert_open()
        start = scan_start(query.get("order"), cursor, "ascending")
        clauses: list[str] = []
        params: list[SqliteValue] = []
        owner_conversation = query.get("ownerConversationId")
        owner_task = query.get("ownerTaskId")
        if owner_conversation is not None:
            clauses.append("owner_conversation_id = ?")
            params.append(int(owner_conversation))
        if owner_task is not None:
            clauses.append("owner_task_id = ?")
            params.append(int(owner_task))
        rows = await self._scan_rows("conversations", "record", clauses, params, start, limit, "id")
        values = [_parse_json(row["record"]) for row in rows]
        return self._page(values, limit, start.order)

    async def entry(self, id: EntryId, context: Context) -> dict[str, Any] | None:
        del context
        self._assert_open()
        rows = await self._database.all(
            f"SELECT record, commit_seq FROM {self._t['entries']} WHERE id = ?", id
        )
        if not rows:
            return None
        return {
            "entry": cast(EntryRecord, _parse_json(rows[0]["record"])),
            "commitSeq": int(str(rows[0]["commit_seq"])),
        }

    async def entry_in_conversation(
        self, conversation_id: ConversationId, id: EntryId, context: Context
    ) -> dict[str, Any] | None:
        del context
        self._assert_open()
        if await self._conversation_record(conversation_id) is None:
            raise StorageRequestError(f"Unknown conversation: {conversation_id}")
        if not await self._entry_visible(conversation_id, id, id):
            return None
        rows = await self._database.all(
            f"SELECT record, commit_seq FROM {self._t['entries']} WHERE id = ?", id
        )
        if not rows:
            return None
        return {
            "entry": cast(EntryRecord, _parse_json(rows[0]["record"])),
            "commitSeq": int(str(rows[0]["commit_seq"])),
        }

    async def find_latest_head_marker(
        self,
        conversation_id: ConversationId,
        at_or_before_entry_id: EntryId | None,
        context: Context,
    ) -> EntryRecord | None:
        del context
        self._assert_open()
        if await self._conversation_record(conversation_id) is None:
            raise StorageRequestError(f"Unknown conversation: {conversation_id}")
        current_id = conversation_id
        upper: float = math.inf if at_or_before_entry_id is None else at_or_before_entry_id
        while True:
            rows = await self._database.all(
                f"SELECT record FROM {self._t['entries']} "
                "WHERE conversation_id = ? AND head IS NOT NULL AND id <= ? ORDER BY id DESC LIMIT 1",
                current_id,
                upper,
            )
            if rows:
                marker = cast(EntryRecord, _parse_json(rows[0]["record"]))
                marker["head"] = marker["head"]
                return marker
            conversation = await self._conversation_record(current_id)
            if conversation is None:
                return None
            parent = conversation.get("parent")
            if parent is None:
                return None
            upper = min(upper, int(parent["at"]))
            current_id = int(parent["conversationId"])

    async def scan_entries(
        self, query: EntryQuery, limit: int, cursor: Cursor | None, context: Context
    ) -> Page:
        del context
        self._assert_open()
        conversation_id = query["conversationId"]
        if await self._conversation_record(conversation_id) is None:
            raise StorageRequestError(f"Unknown conversation: {conversation_id}")
        start = scan_start(query.get("order"), cursor, "descending")
        min_entry: float = query.get("minEntryId", -math.inf)
        max_entry: float = query.get("maxEntryId", math.inf)
        if start.after is not None and start.order == "descending":
            max_entry = min(max_entry, start.after - 1)
        if start.after is not None and start.order == "ascending":
            min_entry = max(min_entry, start.after + 1)

        values: list[EntryRecord] = []
        if start.order == "descending":
            for entry in await self._visible_entries_desc(conversation_id, min_entry, max_entry):
                values.append(entry)
                if len(values) > limit:
                    break
        else:
            for entry in await self._visible_entries_asc(conversation_id, min_entry, max_entry):
                values.append(entry)
                if len(values) > limit:
                    break
        return self._page(values, limit, start.order)

    async def task(self, id: TaskId, context: Context) -> TaskRecord | None:
        del context
        self._assert_open()
        rows = await self._database.all(f"SELECT record FROM {self._t['tasks']} WHERE id = ?", id)
        return None if not rows else cast(TaskRecord, _parse_json(rows[0]["record"]))

    async def scan_tasks(
        self, query: TaskQuery, limit: int, cursor: Cursor | None, context: Context
    ) -> Page:
        del context
        self._assert_open()
        start = scan_start(query.get("order"), cursor, "ascending")
        clauses: list[str] = []
        params: list[SqliteValue] = []
        for key, column in (
            ("conversationId", "conversation_id"),
            ("kind", "kind"),
            ("status", "status"),
        ):
            value = query.get(key)
            if value is not None:
                clauses.append(f"{column} = ?")
                # kind 是「被索引的字符串」：写侧做了 JSON 编码，读侧必须一致，
                # 否则孤立代理项（用例 *keeps indexed string identities lossless*）会对不上。
                params.append(
                    _encode_indexed_string(str(value))
                    if column == "kind"
                    else cast(SqliteValue, value)
                )
        for key, column in (("abortRequested", "abort_requested"), ("background", "background")):
            value = query.get(key)
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(1 if value else 0)
        rows = await self._scan_rows("tasks", "record", clauses, params, start, limit, "id")
        return self._page([_parse_json(row["record"]) for row in rows], limit, start.order)

    async def submission(self, id: SubmissionId, context: Context) -> SubmissionRecord | None:
        del context
        self._assert_open()
        rows = await self._database.all(
            f"SELECT record FROM {self._t['submissions']} WHERE id = ?", id
        )
        return None if not rows else cast(SubmissionRecord, _parse_json(rows[0]["record"]))

    async def scan_submissions(
        self, query: SubmissionQuery, limit: int, cursor: Cursor | None, context: Context
    ) -> Page:
        del context
        self._assert_open()
        start = scan_start(query.get("order"), cursor, "ascending")
        clauses: list[str] = []
        params: list[SqliteValue] = []
        if query.get("conversationId") is not None:
            clauses.append("conversation_id = ?")
            params.append(int(query["conversationId"]))
        if query.get("status") is not None:
            clauses.append("status = ?")
            params.append(str(query["status"]))
        rows = await self._scan_rows("submissions", "record", clauses, params, start, limit, "id")
        return self._page([_parse_json(row["record"]) for row in rows], limit, start.order)

    async def submission_by_request(
        self, conversation_id: ConversationId, request_id: str, context: Context
    ) -> SubmissionRecord | None:
        del context
        self._assert_open()
        rows = await self._database.all(
            f"SELECT record FROM {self._t['submissions']} "
            "WHERE conversation_id = ? AND request_id = ? LIMIT 1",
            int(conversation_id),
            _encode_indexed_string(request_id),
        )
        return None if not rows else cast(SubmissionRecord, _parse_json(rows[0]["record"]))

    async def find_document(
        self, address: DocumentAddress, at: DocumentPoint, context: Context
    ) -> DocumentRecord | None:
        del context
        self._assert_open()
        parts = _address_parts(address)
        return await self._find_by_address(self._database, parts, at)

    async def document(
        self, id: DocumentId, at: DocumentPoint, context: Context
    ) -> StoredDocument | None:
        del context
        self._assert_open()
        return await self._materialize_document(self._database, int(id), at)

    async def scan_documents(
        self, query: DocumentQuery, limit: int, cursor: Cursor | None, context: Context
    ) -> Page:
        del context
        self._assert_open()
        scope_kind, owner_id = _scope_columns(query["scope"])
        clauses = ["scope_kind = ?", "owner_id = ?"]
        params: list[SqliteValue] = [scope_kind, owner_id]
        if query.get("kind") is not None:
            clauses.append("kind = ?")
            params.append(_encode_indexed_string(str(query["kind"])))
        after = self._cursor_id(cursor)
        if after is not None:
            clauses.append("id > ?")
            params.append(after)
        where = " AND ".join(clauses)
        rows = await self._database.all(
            f"SELECT record FROM {self._t['documents']} WHERE {where} ORDER BY id ASC", *params
        )
        values: list[DocumentRecord] = []
        for row in rows:
            record = _parse_json(row["record"])
            if _is_alive_at(record, query["at"]):
                values.append(cast(DocumentRecord, record))
                if len(values) > limit:
                    break
        return self._page(values, limit)

    async def close(self, context: Context) -> None:
        del context
        if self._closed:
            return
        self._closed = True
        await self._database.close()

    # ---- 内部 ------------------------------------------------------------------------

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("SqliteStorage is closed")

    def _cursor_id(self, cursor: Cursor | None) -> int | None:
        if cursor is None:
            return None
        after = cursor.get("after")
        if after is None:
            return None
        if not _is_safe_integer(after):
            raise StorageRequestError("Invalid storage cursor")
        return int(after)  # type: ignore[arg-type]

    def _page(self, values: list[Any], limit: int, order: ScanOrder = "ascending") -> Page:
        items = values[:limit]
        if len(values) <= limit:
            return {"items": items}
        return {"items": items, "next": next_cursor(items[-1]["id"], order)}

    async def _scan_rows(
        self,
        table: str,
        column: str,
        clauses: list[str],
        params: list[SqliteValue],
        start: ScanStart,
        limit: int,
        order_column: str,
    ) -> list[dict[str, object]]:
        """按方向与游标取 ``limit + 1`` 行（多取一行用来判断是否还有下一页）。"""
        where = list(clauses)
        args = list(params)
        if start.after is not None:
            where.append(f"{order_column} {'>' if start.order == 'ascending' else '<'} ?")
            args.append(start.after)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        direction = "ASC" if start.order == "ascending" else "DESC"
        return await self._database.all(
            f"SELECT {column} FROM {self._t[table]}{clause} "
            f"ORDER BY {order_column} {direction} LIMIT ?",
            *args,
            limit + 1,
        )

    async def _conversation_record(self, id: int) -> ConversationRecord | None:
        rows = await self._database.all(
            f"SELECT record FROM {self._t['conversations']} WHERE id = ?", id
        )
        return None if not rows else cast(ConversationRecord, _parse_json(rows[0]["record"]))

    async def _ancestry(self, conversation_id: int) -> list[tuple[int, float]]:
        """从该会话到根的 ``(conversationId, upperBound)`` 链，**由近及远**。"""
        chain: list[tuple[int, float]] = []
        current = conversation_id
        upper: float = math.inf
        while True:
            chain.append((current, upper))
            conversation = await self._conversation_record(current)
            if conversation is None:
                break
            parent = conversation.get("parent")
            if parent is None:
                break
            upper = min(upper, int(parent["at"]))
            current = int(parent["conversationId"])
        return chain

    async def _entry_visible(self, conversation_id: int, min_id: float, max_id: float) -> bool:
        for entry in await self._visible_entries_desc(conversation_id, min_id, max_id):
            if entry is not None:
                return True
        return False

    async def _visible_entries_desc(
        self, conversation_id: int, min_entry: float, max_entry: float
    ) -> list[EntryRecord]:
        """可见条目，从新到旧（``memory.ts:678-701`` 的 SQL 版）。"""
        values: list[EntryRecord] = []
        for current, upper in await self._ancestry(conversation_id):
            bound = min(upper, max_entry)
            rows = await self._database.all(
                f"SELECT record FROM {self._t['entries']} "
                "WHERE conversation_id = ? AND id >= ? AND id <= ? ORDER BY id DESC",
                current,
                min_entry,
                bound,
            )
            values.extend(_parse_json(row["record"]) for row in rows)
        values.sort(key=lambda entry: entry["id"], reverse=True)
        return values

    async def _visible_entries_asc(
        self, conversation_id: int, min_entry: float, max_entry: float
    ) -> list[EntryRecord]:
        """可见条目，从旧到新：各段从根向前（``memory.ts:703-731``）。"""
        values: list[EntryRecord] = []
        for current, upper in reversed(await self._ancestry(conversation_id)):
            bound = min(upper, max_entry)
            rows = await self._database.all(
                f"SELECT record FROM {self._t['entries']} "
                "WHERE conversation_id = ? AND id >= ? AND id <= ? ORDER BY id ASC",
                current,
                min_entry,
                bound,
            )
            values.extend(_parse_json(row["record"]) for row in rows)
        return values

    async def _document_record(
        self, tx: SqliteExecutor, id: int, *, include_retired: bool = False
    ) -> DocumentRecord | None:
        rows = await tx.all(f"SELECT record FROM {self._t['documents']} WHERE id = ?", id)
        if not rows:
            return None
        record = cast(DocumentRecord, _parse_json(rows[0]["record"]))
        if not include_retired and record.get("retiredAt") is not None:
            return None
        return record

    async def _latest_revision(self, tx: SqliteExecutor, id: int) -> dict[str, Any] | None:
        rows = await tx.all(
            f"SELECT kind, version, content FROM {self._t['document_revisions']} "
            "WHERE document_id = ? ORDER BY seq DESC LIMIT 1",
            id,
        )
        return None if not rows else dict(rows[0])

    async def _current_incarnation(
        self, tx: SqliteExecutor, parts: tuple[str, str, int, int, str]
    ) -> int | None:
        rows = await tx.all(
            f"SELECT id FROM {self._t['documents']} "
            "WHERE kind = ? AND scope_kind = ? AND owner_id = ? AND family = ? AND key_value = ? "
            "AND retired_at IS NULL ORDER BY id DESC LIMIT 1",
            _encode_indexed_string(parts[0]),
            parts[1],
            parts[2],
            parts[3],
            parts[4],
        )
        return None if not rows else int(str(rows[0]["id"]))

    async def _find_by_address(
        self, tx: SqliteExecutor, parts: tuple[str, str, int, int, str], at: DocumentPoint
    ) -> DocumentRecord | None:
        if at == "current":
            current = await self._current_incarnation(tx, parts)
            if current is None:
                return None
            return await self._document_record(tx, current)
        rows = await tx.all(
            f"SELECT record FROM {self._t['documents']} "
            "WHERE kind = ? AND scope_kind = ? AND owner_id = ? AND family = ? AND key_value = ? "
            "ORDER BY id ASC",
            _encode_indexed_string(parts[0]),
            parts[1],
            parts[2],
            parts[3],
            parts[4],
        )
        for row in rows:
            record = _parse_json(row["record"])
            if _is_alive_at(record, at):
                return cast(DocumentRecord, record)
        return None

    async def _materialize_document(
        self, tx: SqliteExecutor, id: int, at: DocumentPoint
    ) -> StoredDocument | None:
        """选定基点、重放增量，物化出值（``memory.ts:659-676`` 的 SQL 版）。"""
        record = await self._document_record(tx, id, include_retired=True)
        if record is None:
            return None
        if at != "current" and _is_current_only(record):
            raise StorageRequestError(f"Document {id} does not retain historical content")
        if not _is_alive_at(record, at):
            return None

        if at == "current":
            revisions = await tx.all(
                f"SELECT seq, kind, version, content FROM {self._t['document_revisions']} "
                "WHERE document_id = ? ORDER BY seq ASC",
                id,
            )
        else:
            revisions = await tx.all(
                f"SELECT seq, kind, version, content FROM {self._t['document_revisions']} "
                "WHERE document_id = ? AND seq <= ? ORDER BY seq ASC",
                id,
                int(at),
            )
        if not revisions:
            raise RuntimeError(f"Document {id} is missing a required base")
        base_index = len(revisions) - 1
        while base_index >= 0 and revisions[base_index]["kind"] != "base":
            base_index -= 1
        if base_index < 0:
            raise RuntimeError(f"Document {id} is missing a required base")
        base_content = _parse_json(revisions[base_index]["content"])
        version = int(base_content["version"])
        value = apply_immutable_batches(
            base_content["value"],
            _delta_batches(id, version, revisions, base_index + 1),
        )
        return {
            "record": record,
            "version": version,
            "value": value,
            "deltasSinceBase": len(revisions) - base_index - 1,
        }


def _delta_batches(
    id: int, version: int, revisions: Sequence[dict[str, object]], start: int
) -> list[list[Op]]:
    """基点之后的增量序列；跨过版本边界而没有基点时报错（``memory.ts:52-65``）。"""
    batches: list[list[Op]] = []
    for index in range(start, len(revisions)):
        revision = revisions[index]
        if revision["kind"] != "delta" or int(str(revision["version"])) != version:
            raise RuntimeError(f"Document {id} crosses a stored version boundary without a base")
        content = _parse_json(revision["content"])
        batches.append(content["ops"])
    return batches
