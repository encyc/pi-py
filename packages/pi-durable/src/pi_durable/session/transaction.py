"""一次 Session 提交回调的事务面 ``Tx`` —— 移植 ``durable/src/session/transaction.ts``（1,043 行）。

⚠️ **本模块分步交付，目前只到位第 1–2 步**（见 ``PORTING.md`` 的〈Session 运行时〉）：

* ✅ **第 1 步**：:func:`apply_submission_change` + :meth:`Transaction.settle_submission` /
  :meth:`Transaction.place_submission`
* ✅ **第 2 步**：表读取（``conversation`` / ``entry`` / ``task`` / ``scan_*`` /
  ``latest_head_marker`` / ``submission`` / ``submission_by_request``）
* ✅ **第 3 步**：写入暂存与装配（``create_conversation`` / ``create_root_conversation`` /
  ``fork_conversation`` / ``append_entry`` / ``create_submission`` /
  :meth:`Transaction.settle_success` / ``staged_conversations`` / ``staged_tasks``）
* ✅ **第 4 步**：任务状态机（``create_task`` / ``set_task`` / ``_stamp_times`` / ``staged_tasks``）
* ✅ **第 5 步**：文档（``doc()`` / ``retire_doc()`` / ``DocumentPlan`` / ``adopt``）—— 阶段 2 完结

**没有留桩**：上面三组方法**不存在**，而不是「存在但抛 NotImplementedError」——
后者会让人以为能用。

两条已经能完整验收的语义
------------------------

1. **读后写被拒**：``#read`` 在 :class:`~pi_durable.errors.ReadAfterWrite` 之后拒绝
   （``transaction.ts:922-930``）。这是 :class:`Transaction` 的核心不变量之一：
   一个提交回调一旦写了表，就不能再按「已提交状态」去读，否则读到的是**不含自己写入**的
   旧状态。第 1 步的 ``settle_submission`` / ``place_submission`` 就会置位这个标志。
2. **候选任务记录优先于已提交记录**：``task()`` 走 :meth:`Transaction._committed_task`，
   它**记忆化**已提交读取（``transaction.ts:980-984``），这样同一事务里多次读同一个任务
   只碰一次存储。第 4 步的写入候选会插在它前面。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from pi_chord.context import Context
from pi_chord.delta import Draft, Tracker, track
from pi_chord.json import copy_json
from pi_durable.documents import (
    address_id,
    check_record_scope,
    check_record_version,
    document_create,
    materialize_document,
    materialize_document_value,
    resolve_address,
)
from pi_durable.entries import EntryToken
from pi_durable.errors import ReadAfterWrite
from pi_durable.session.forks import prepare_fork_document_copies
from pi_durable.types import (
    ROOT_CONVERSATION_ID,
    ConversationId,
    ConversationOwnership,
    ConversationQuery,
    ConversationRecord,
    Cursor,
    DocumentAddress,
    DocumentId,
    EntryId,
    EntryQuery,
    EntryRecord,
    JsonObject,
    Page,
    Storage,
    StorageWrite,
    SubmissionCreate,
    SubmissionId,
    SubmissionRecord,
    SubmissionSettlement,
    TaskId,
    TaskOptions,
    TaskQuery,
    TaskRecord,
)

_T = TypeVar("_T")

__all__ = [
    "INTERNAL_SCAN_PAGE_SIZE",
    "LoadedDocument",
    "Transaction",
    "TransactionHost",
    "TransactionScope",
    "apply_submission_change",
]

#: 内部扫描的分页大小（``transaction.ts:81``）。
INTERNAL_SCAN_PAGE_SIZE = 256


# --------------------------------------------------------------------------------------
# 第 1 步：提交变更的纯函数（``transaction.ts:61-79``）
# --------------------------------------------------------------------------------------

#: 一次暂存的提交变更：一次结算，或把一个排队中的提交放到它的条目上。
SubmissionChange = dict[str, Any]


def apply_submission_change(
    current: SubmissionRecord, change: SubmissionChange
) -> SubmissionRecord:
    """应用一次变更后的**完整**记录（``transaction.ts:67-79``）。

    规则：

    * 已经 ``done`` / ``unanswered`` 的记录**不再变**——已结算的提交保持原样。
    * ``placed``：只有 ``queued`` 能被放置；输入型变 ``placed``，写入型直接变 ``done``，
      并带上 ``entry``。
    * ``done``：只有 ``placed`` 的**输入**能被回答。
    * 其余（``unanswered``）：整体并入。队里与已放置的记录不带 answer / reason / detail；
      ``unanswered`` 的输入**保留自己的 entry**。
    """
    status = current["status"]
    if status == "done" or status == "unanswered":
        return current

    if change["status"] == "placed":
        if status != "queued":
            raise RuntimeError(f"Submission {current['id']} is not queued")
        placed_status = "placed" if current["type"] == "input" else "done"
        return {**current, "status": placed_status, "entry": change["entry"]}  # type: ignore[return-value]

    if change["status"] == "done" and status != "placed":
        raise RuntimeError(f"Submission {current['id']} is not a placed input")

    # 队里与已放置的记录不带 answer / reason / detail；unanswered 的输入保留它的 entry。
    return {**current, **change}  # type: ignore[return-value]


def _resolved_future(value: _T) -> asyncio.Future[_T]:
    """一个已落定的 Future（对应上游直接 ``Promise.resolve(x)``）。"""
    future: asyncio.Future[_T] = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


def _failed_future(error: Exception) -> asyncio.Future[Any]:
    """一个已带异常的 Future（对应上游 ``Promise.reject(error)``）。

    ⚠️ 不能直接 ``raise``：``doc()`` / ``retire_doc()`` 是**同步**方法（上游返回 Promise），
    它们把同步错误转成一个被拒绝的 Promise，而不是抛出去。
    """
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_exception(error)
    future.exception()  # 标记为已取，免得「未被 await」时打警告
    return future


async def _await_none(awaitable: Awaitable[Any]) -> None:
    await awaitable


# --------------------------------------------------------------------------------------
# 宿主（``transaction.ts:86-119``）
# --------------------------------------------------------------------------------------


@dataclass
class LoadedDocument:
    """Session tracker 缓存拥有的**一个已提交文档化身**（``transaction.ts:86-97``）。

    字段名用 snake_case：这是**本包自己的**内存结构，不是持久化记录。上游的
    ``addressId`` / ``storedVersion`` / ``valueVersion`` / ``deltasSinceBase``
    依次对应下面的 ``address_id`` / ``stored_version`` / ``value_version`` /
    ``deltas_since_base``（与 ``EnvConformanceOptions`` 同一条约定）。
    """

    address_id: str
    record: dict[str, Any]
    #: 已持久化的定义版本；tracked 值只在内存里迁移时它会更旧。
    stored_version: int
    #: tracked 值**形状所属**的定义版本；用别的版本访问会重新从存储加载。
    value_version: int
    #: 最新基点之后的已存增量数；被 adoption 推进，好让下次谓词调用不必再读。
    deltas_since_base: int
    tracker: Tracker[JsonObject]


class TransactionHost:
    """事务在持有写线期间使用的 Session 服务（``transaction.ts:99-119``）。

    第 5 步（文档）会用到 ``cached`` / ``load`` / ``install`` / ``evict``；本步只要求
    ``storage`` 与 ``now``。
    """

    def __init__(
        self,
        storage: Storage,
        now: Callable[[], float] | None = None,
        conversation_created: Callable[[Any, Any], Awaitable[None]] | None = None,
        *,
        load: Callable[[Any, str, Any, Context], Awaitable[LoadedDocument | None]] | None = None,
        cached: Callable[[str], LoadedDocument | None] | None = None,
        install: Callable[[LoadedDocument], None] | None = None,
        evict: Callable[[str, int], None] | None = None,
    ) -> None:
        self.storage = storage
        self._now = now
        self._conversation_created = conversation_created
        #: Session 提供的文档缓存回调。上游 ``TransactionHost`` 是**接口**，由 ``SessionImpl`` 实现；
        #: 这里允许注入，好让 Session 把**它自己的**（带逐出的）缓存接进来。
        self._load_callback = load
        self._cached_callback = cached
        self._install_callback = install
        self._evict_callback = evict
        #: 地址 → 已加载化身。**这是无界缓存**：阶段 3 的 Session 会用带回逐出的缓存覆盖它
        #: （上游 ``session.ts:96-104`` 的保留期与定时器）。这里只保证**语义正确**。
        self._documents: dict[str, LoadedDocument] = {}

    async def conversation_created(self, tx: Any, record: Any) -> None:
        """暂存「属于每个新建/分叉会话」的写入（``transaction.ts:117``）。

        上游由 Session 提供：分叉一个会话时，子会话还要额外落一批文档。
        第 5 步落位后这里会真的用到；现在默认是**不做任何事**。
        """
        if self._conversation_created is not None:
            await self._conversation_created(tx, record)

    # ── 文档缓存与冷加载（上游由 SessionImpl 提供，``session.ts:823-852``） ──────────

    def cached(self, address_id: str) -> LoadedDocument | None:
        """返回缓存的当前化身，不加载（``transaction.ts:104``）。"""
        if self._cached_callback is not None:
            return self._cached_callback(address_id)
        return self._documents.get(address_id)

    async def load(
        self,
        definition: Any,
        address_id: str,
        address: DocumentAddress,
        context: Context,
    ) -> LoadedDocument | None:
        """返回缓存的当前化身，必要时冷加载并按需迁移（``session.ts:823-852``）。

        ⚠️ 一个 tracker 只服务**它那份值被物化时的版本**的令牌；别的版本要重新从存储加载。
        """
        if self._load_callback is not None:
            return await self._load_callback(definition, address_id, address, context)
        cached = self._documents.get(address_id)
        if cached is not None and cached.value_version == definition.get("version"):
            return cached
        if cached is not None:
            self._documents.pop(address_id, None)

        record = await self.storage.find_document(address, "current", context)
        if record is None:
            return None
        stored = await self.storage.document(record["id"], "current", context)
        if stored is None:
            raise RuntimeError(f"Current document {record['id']} ({record['kind']}) cannot be read")
        value = materialize_document(definition, stored)
        loaded = LoadedDocument(
            address_id=address_id,
            record=cast(dict[str, Any], stored["record"]),
            stored_version=stored["version"],
            value_version=int(definition.get("version", 0)),
            deltas_since_base=stored["deltasSinceBase"],
            tracker=track(value),
        )
        self._documents[address_id] = loaded
        return loaded

    def install(self, document: LoadedDocument) -> None:
        """装入一个新提交的化身（``transaction.ts:112``）。"""
        if self._install_callback is not None:
            self._install_callback(document)
            return
        self._documents[document.address_id] = document

    def evict(self, address_id: str, record_id: DocumentId) -> None:
        """退休化身时把它从缓存摘掉——**仅当**它仍是该地址的占用者（``transaction.ts:114``）。"""
        if self._evict_callback is not None:
            self._evict_callback(address_id, record_id)
            return
        current = self._documents.get(address_id)
        if current is not None and current.record["id"] == record_id:
            self._documents.pop(address_id, None)

    def now(self) -> float:
        """任务生命周期时间用的墙钟（``transaction.ts:102``）。"""
        return self._now() if self._now is not None else _default_now()


def _default_now() -> float:
    import time

    return time.time() * 1000.0


@dataclass
class TransactionScope:
    """一次提交绑定到的默认值（``transaction.ts:128-134``）。"""

    conversation_id: ConversationId | None = None
    """``tx.create_task()`` 用的会话。"""

    task_id: TaskId | None = None
    """本次运行时提交所属的任务；会盖在追加条目的 ``byTaskId`` 上。"""


@dataclass
class _DocumentTarget:
    """一个已暂存文档化身的存储/缓存来源（``transaction.ts:135-149`` 的 ``DocumentTarget``）。"""

    kind: str  # "loaded" | "created" | "fork-copy" | "retire-only"
    document: LoadedDocument | None = None
    record: Any = None
    version: int | None = None
    tracker: Tracker[JsonObject] | None = None
    source: Any = None


@dataclass
class _DocumentEntry:
    """本事务取得、创建或退休的**一个**文档化身（``transaction.ts:151-167``）。"""

    address_id: str
    address: Any
    #: 无定义的 fork 副本与终局任务扫描发现的退休条目没有 definition。
    definition: Any = None
    #: 记忆化的公开获取；纯元数据退休没有它。
    #: ⚠️ 存的是 **Task** 不是协程——见 PORTING.md R1。
    draft_promise: asyncio.Future[Any] | None = None
    target: _DocumentTarget | None = None
    change: Any = None
    prepared: Any = None
    retire_on_commit: bool = False


@dataclass
class _DocumentPlan:
    """一个已暂存化身要写什么、要发布什么——在存储准入**之前**定好，好让 adoption 只是套用
    （``transaction.ts:169-190``）。"""

    address_id: str
    record: Any
    retire: bool = False
    content: dict[str, Any] | None = None
    change: Any = None
    conversation_id: ConversationId | None = None


def _publishes(plan: _DocumentPlan) -> bool:
    """adoption 是否发布这个计划（``transaction.ts:1041-1043``）。

    每一次创建、复制、退休都发布；已加载的化身只在**写了内容**时发布——
    其中包含「只为迁移而写的 base」，好让旧形状的观察者收到新值。
    """
    return (
        plan.retire
        or plan.change is None
        or plan.change["loaded"] is None
        or plan.content is not None
    )


@dataclass
class _TransactionTask:
    """本事务碰过的**一个**任务的已提交与候选状态（``transaction.ts:121-126``）。"""

    committed_read: asyncio.Future[TaskRecord | None] | None = None
    #: 第 4 步落位：``{"kind": "create" | "replace", "record": ...}``。
    write: dict[str, Any] | None = None
    publication_conversation_id: ConversationId | None = None


# --------------------------------------------------------------------------------------
# 事务
# --------------------------------------------------------------------------------------


class Transaction:
    """一次 Session 提交回调的事务（``transaction.ts:194-…``）。

    每个异步操作都被跟踪，好让回调结算时能拒绝并排空未完成的工作。
    """

    def __init__(
        self,
        host: TransactionHost,
        context: Context,
        scope: TransactionScope | None = None,
    ) -> None:
        self._host = host
        self._context = context
        self._scope = scope or TransactionScope()
        self._pending_operations: set[asyncio.Future[Any]] = set()
        self._sealed = False
        self._has_table_write = False
        # ── 第 3 步：写入暂存 ──────────────────────────────────────────────
        #: 原子批；会话与条目**立刻**入队（上游 ``transaction.ts:203``）。
        self._writes: list[StorageWrite] = []
        self._created_conversation_ids: set[ConversationId] = set()
        self._fork_source_conversation_ids: set[ConversationId] = set()
        self._fork_source_document_ids: set[DocumentId] = set()
        #: 本事务创建的提交记录，按 ID。
        self._submissions: dict[SubmissionId, SubmissionRecord] = {}
        #: 提交结算与放置，按暂存顺序；装配期针对最新候选记录解析。
        self._submission_changes: list[tuple[SubmissionId, SubmissionChange]] = []
        #: 第 1 步入队、第 3 步装配时解析。
        self._tasks_by_id: dict[TaskId, _TransactionTask] = {}

        # ── 第 5 步：文档 ──────────────────────────────────────────────────
        #: 每个已暂存化身的写入与发布计划，在装配期构建。
        self._plans: list[_DocumentPlan] = []
        #: 每一个文档获取或退休标记，按暂存顺序。
        self._documents: list[_DocumentEntry] = []
        #: 每个逻辑地址上最新的**事务本地**化身或退休标记。
        self._latest_document_by_address: dict[str, _DocumentEntry] = {}
        #: 分叉要复制的文档，按暂存顺序——第 5 步折成 ``DocumentEntry``。
        self._fork_document_entries: list[tuple[Any, Any]] = []

    # ─── 表读取（第 2 步） ────────────────────────────────────────────────────

    async def conversation(self, id: ConversationId) -> ConversationRecord | None:
        return await self._read(
            "conversation", lambda: self._host.storage.conversation(id, self._context)
        )

    async def entry(self, id: EntryId, token: EntryToken | None = None) -> EntryRecord | None:
        """读一个条目；给了 ``token`` 就按它的 ``kind`` 收窄（``transaction.ts:233-243``）。

        上游是重载 ``entry(id)`` / ``entry(token, id)``；Python 没有重载，所以令牌是
        **第二个可选参数**（PORTING.md 偏离 #48 的同一取舍）。
        """

        async def read() -> EntryRecord | None:
            found = await self._host.storage.entry(id, self._context)
            if found is None:
                return None
            record = cast(EntryRecord, found["entry"])
            if token is None or token.is_(record):
                return record
            return None

        return await self._read("entry", read)

    async def task(self, id: TaskId) -> TaskRecord | None:
        return await self._read("task", lambda: self._committed_task(id))

    async def scan_conversations(
        self, query: ConversationQuery, limit: int, cursor: Cursor | None = None
    ) -> Page:
        return await self._read(
            "scanConversations",
            lambda: self._host.storage.scan_conversations(query, limit, cursor, self._context),
        )

    async def scan_entries(
        self, query: EntryQuery, limit: int, cursor: Cursor | None = None
    ) -> Page:
        return await self._read(
            "scanEntries",
            lambda: self._host.storage.scan_entries(query, limit, cursor, self._context),
        )

    async def latest_head_marker(self, conversation_id: ConversationId) -> EntryRecord | None:
        return await self._read(
            "latestHeadMarker",
            lambda: self._host.storage.find_latest_head_marker(
                conversation_id, None, self._context
            ),
        )

    async def scan_tasks(self, query: TaskQuery, limit: int, cursor: Cursor | None = None) -> Page:
        return await self._read(
            "scanTasks",
            lambda: self._host.storage.scan_tasks(query, limit, cursor, self._context),
        )

    async def submission(self, id: SubmissionId) -> SubmissionRecord | None:
        """内部：已提交的提交记录（``transaction.ts:269-272``）。"""
        return await self._read(
            "submission", lambda: self._host.storage.submission(id, self._context)
        )

    async def submission_by_request(
        self, conversation_id: ConversationId, request_id: str
    ) -> SubmissionRecord | None:
        """带会话级 request ID 的已提交提交记录（``transaction.ts:274-279``）。"""
        return await self._read(
            "submissionByRequest",
            lambda: self._host.storage.submission_by_request(
                conversation_id, request_id, self._context
            ),
        )

    # ─── 第 1 步：提交结算与放置 ──────────────────────────────────────────────

    def settle_submission(self, id: SubmissionId, settlement: SubmissionSettlement) -> None:
        """结算一个提交（``transaction.ts:446-450``）。

        在**装配期**针对本事务最新的候选记录解析，回退到已提交状态——所以它**不是**调用方的
        表读取，并且可以在第一次表写入之后调用。
        """
        self._assert_open()
        self._has_table_write = True
        # 上游是 `copyJson(settlement)`：拿一份解除关联的副本，免得调用方之后改它。
        staged = cast(SubmissionChange, copy_json(settlement))
        self._submission_changes.append((id, staged))

    def place_submission(self, id: SubmissionId, entry: EntryId) -> None:
        """把一个排队中的提交放到 ``entry``；与 :meth:`settle_submission` 一样在装配期解析
        （``transaction.ts:452-457``）。"""
        self._assert_open()
        self._has_table_write = True
        self._submission_changes.append((id, {"status": "placed", "entry": entry}))

    def staged_submission_changes(self) -> Sequence[tuple[SubmissionId, SubmissionChange]]:
        """内部：按暂存顺序给出提交变更。第 3 步的装配按这个顺序解析它们。"""
        return tuple(self._submission_changes)

    # ─── 第 3 步：写入暂存 ───────────────────────────────────────────────────

    async def create_conversation(self, ownership: ConversationOwnership) -> ConversationRecord:
        """创建一个显式选定归属的会话（``transaction.ts:282-284``）。"""
        return await self._write(lambda: self._stage_conversation(None, ownership))

    async def create_root_conversation(self) -> ConversationRecord:
        """内部：保留的根身份的最终形态引导路径（``transaction.ts:287-289``）。

        与 :meth:`create_conversation` 共用同一条私有路径，只是把 ``reserved_id`` 定成
        :data:`~pi_durable.types.ROOT_CONVERSATION_ID`。
        """
        ownerless = cast(ConversationOwnership, {"kind": "ownerless"})
        return await self._write(
            lambda: self._stage_conversation(None, ownerless, ROOT_CONVERSATION_ID)
        )

    async def fork_conversation(
        self,
        parent_conversation_id: ConversationId,
        at: EntryId,
        ownership: ConversationOwnership,
    ) -> ConversationRecord:
        """在一个具体的可见条目上创建历史分叉（``transaction.ts:291-298``）。"""
        return await self._write(
            lambda: self._stage_conversation(
                {"conversationId": parent_conversation_id, "at": at}, ownership
            )
        )

    async def _stage_conversation(
        self,
        parent: dict[str, Any] | None,
        ownership: ConversationOwnership,
        reserved_id: ConversationId | None = None,
    ) -> ConversationRecord:
        owner_task_id = ownership["taskId"] if ownership["kind"] == "task" else None
        id = reserved_id
        if id is None:
            id = await self._host.storage.mint_id()
        self._assert_open()

        owner: dict[str, Any] | None = None
        if owner_task_id is not None:
            task = await self._current_task(owner_task_id)
            self._assert_open()
            if task is None:
                raise RuntimeError(f"Conversation owner task {owner_task_id} does not exist")
            owner = {"conversationId": task["conversationId"], "taskId": owner_task_id}

        record: dict[str, Any] = {"id": id}
        if parent is not None:
            record["parent"] = parent
        if owner is not None:
            record["owner"] = owner

        copies: list[Any] = []
        if parent is not None:
            copies = await prepare_fork_document_copies(
                self._host.storage, parent["conversationId"], parent["at"], id, self._context
            )
            self._assert_open()
        for copy in copies:
            self._fork_source_document_ids.add(copy.source["id"])
            entry = _DocumentEntry(
                address_id=address_id(copy.record),
                address=copy.record,
                target=_DocumentTarget(kind="fork-copy", record=copy.record, source=copy.source),
            )
            self._documents.append(entry)
            self._latest_document_by_address[entry.address_id] = entry
        if parent is not None:
            self._fork_source_conversation_ids.add(parent["conversationId"])
        self._created_conversation_ids.add(id)
        self._writes.append(cast(StorageWrite, {"type": "conversation", "value": record}))
        await self._host.conversation_created(self, record)
        self._assert_open()
        return cast(ConversationRecord, record)

    async def append_entry(
        self,
        conversation_id: ConversationId,
        value: dict[str, Any],
        token: EntryToken | None = None,
    ) -> EntryRecord:
        """追加一个条目（``transaction.ts:345-381``）。

        上游是重载 ``appendEntry(conversationId, value)`` /
        ``appendEntry(token, conversationId, value)``；Python 没有重载，令牌做成**第三可选参数**，
        由它提供 ``kind``（PORTING.md 偏离 #48 的同一取舍）。

        ``head: "self"`` 会被换成新条目自己的 ID；``byTaskId`` 来自事务作用域（未设则整个键省略）。
        """
        staged = await self._write(lambda: self._append_entry(conversation_id, value, token))
        return staged

    async def _append_entry(
        self,
        conversation_id: ConversationId,
        value: dict[str, Any],
        token: EntryToken | None,
    ) -> EntryRecord:
        await self._require_conversation(conversation_id)
        self._assert_open()
        id = await self._host.storage.mint_id()
        self._assert_open()

        draft = dict(value)
        if token is not None:
            draft["kind"] = token.kind
        head = draft.pop("head", None)
        record: dict[str, Any] = {**draft, "id": id, "conversationId": conversation_id}
        if head is not None:
            record["head"] = id if head == "self" else head
        # 上游用 `copyJson(..., {omitUndefinedProperties: true})`：未定义的 `head` / `byTaskId`
        # 整个键都被省掉，而不是留一个 null。
        if self._scope.task_id is not None:
            record["byTaskId"] = self._scope.task_id
        self._writes.append(cast(StorageWrite, {"type": "entry", "value": record}))
        return cast(EntryRecord, record)

    async def create_submission(self, create: SubmissionCreate) -> SubmissionRecord:
        """创建一个带全新 ID 的**裸**提交记录；不施加任何准入规则（``transaction.ts:430-438``）。

        没有 busy 检查、没有收件箱排队、没有放置——要那些语义请走 ``Conversation.submit()``
        或会话句柄。
        """
        return await self._write(lambda: self._create_submission(create))

    async def _create_submission(self, create: SubmissionCreate) -> SubmissionRecord:
        await self._require_conversation(create["conversationId"])
        self._assert_open()
        id = await self._host.storage.mint_id()
        self._assert_open()
        record = cast(SubmissionRecord, {**create, "id": id})
        self._submissions[id] = record
        return record

    def staged_conversations(self) -> list[ConversationRecord]:
        """内部：本事务目前创建或分叉的会话（``transaction.ts:485-490``）。"""
        return [write["value"] for write in self._writes if write["type"] == "conversation"]

    def staged_tasks(self) -> list[TaskRecord]:
        """内部：本事务目前创建或替换的任务候选记录（``transaction.ts:478-482``）。

        ⚠️ **第 4 步落位**：任务**写入**（``set_task`` / ``create_task``）还没做，所以现在恒为
        空列表。方法本身现在就完整——它读的是第 4 步会填充的那个 map。
        """
        return [
            task.write["record"] for task in self._tasks_by_id.values() if task.write is not None
        ]

    # ─── 第 4 步：任务状态机 ─────────────────────────────────────────────────

    async def create_task(
        self,
        task: Any,
        input: Any,
        options: TaskOptions,
    ) -> TaskId:
        """创建一个持久任务并返回它的 ID（``transaction.ts:383-429``）。

        ``ownership`` 是必填的——任务总要指明它的拥有者（spec §5.5）。子任务的会话被强制为
        拥有者的会话，且**不能**是后台任务。
        """
        return await self._write(lambda: self._create_task(task, input, options))

    async def _create_task(self, task: Any, input: Any, options: TaskOptions) -> TaskId:
        ownership = options["ownership"]
        owner: TaskRecord | None = None
        if ownership["kind"] == "task":
            # 装配期还会按拥有者的**最终候选**再校验一次。
            owner = await self._current_task(ownership["taskId"])
            self._assert_open()
            if owner is None:
                raise RuntimeError(f"Task owner {ownership['taskId']} does not exist")
            if options.get("background") is True:
                raise TypeError("A child task cannot be background")
            declared = options.get("conversationId")
            if declared is not None and declared != owner["conversationId"]:
                raise RuntimeError(
                    f"A child task lives in its owner's conversation {owner['conversationId']}"
                )

        conversation_id = (
            owner["conversationId"]
            if owner is not None
            else options.get("conversationId", self._scope.conversation_id)
        )
        if conversation_id is None:
            raise TypeError("Tx.createTask() requires options.conversationId")
        await self._require_conversation(conversation_id)
        self._assert_open()

        definition = task.definition
        checkpoint = definition.initial(input)
        id = await self._host.storage.mint_id()
        self._assert_open()

        record: dict[str, Any] = {
            "id": id,
            "conversationId": conversation_id,
            "kind": definition.name,
            "version": definition.version,
            "input": input,
            "background": options.get("background", False),
            "abortRequested": False,
            "state": {"status": "pending", "checkpoint": checkpoint},
        }
        if owner is not None:
            record["owner"] = owner["id"]
        if options.get("abandonOnRestart") is True:
            record["abandonOnRestart"] = True

        self._tasks_by_id[id] = _TransactionTask(write={"kind": "create", "record": record})
        return id

    def set_task(self, value: TaskRecord) -> None:
        """内部：**整体替换**一个任务记录（``transaction.ts:459-475``）。

        任务只通过自己的运行时改变状态。终局候选不能再改；任务也不能换会话。
        """
        self._assert_open()
        self._has_table_write = True
        task = self._task_entry(value["id"])
        candidate = task.write["record"] if task.write is not None else None
        if candidate is not None and candidate["state"]["status"] == "terminal":
            raise RuntimeError(f"Task {value['id']} already has a terminal candidate")
        if candidate is not None and candidate["conversationId"] != value["conversationId"]:
            raise RuntimeError(f"Task {value['id']} cannot change conversations")
        kind = (
            "create" if task.write is not None and task.write.get("kind") == "create" else "replace"
        )
        task.write = {"kind": kind, "record": self._stamp_times(value, candidate)}

    def _stamp_times(self, value: TaskRecord, candidate: TaskRecord | None) -> TaskRecord:
        """生命周期时间：第一次变 ``running`` 盖 ``startedAt``，变 ``terminal`` 盖 ``endedAt``；
        一旦盖上就不再改（``transaction.ts:950-964``）。

        ⚠️ 上游用的是 ``??``（只跳过 ``null``/``undefined``），所以这里**必须显式判 ``None``**：
        用 Python 的 ``or`` 会把时间戳 ``0`` 当成「没值」而重新盖章。
        """
        status = value["state"]["status"]

        def stamp(flag: bool) -> float | None:
            return self._host.now() if flag else None

        prior = candidate or {}
        started_at: float | None = prior.get("startedAt")
        if started_at is None:
            started_at = value.get("startedAt")
        if started_at is None:
            started_at = stamp(status == "running")

        ended_at: float | None = prior.get("endedAt")
        if ended_at is None:
            ended_at = value.get("endedAt")
        if ended_at is None:
            ended_at = stamp(status == "terminal")

        if started_at == value.get("startedAt") and ended_at == value.get("endedAt"):
            return value
        stamped = dict(value)
        if started_at is not None:
            stamped["startedAt"] = started_at
        if ended_at is not None:
            stamped["endedAt"] = ended_at
        return cast(TaskRecord, stamped)

    # ─── 第 5 步：文档 ───────────────────────────────────────────────────────

    def doc(self, token: Any, *args: Any) -> Awaitable[Draft]:
        """取得（必要时创建）一个文档草稿（``transaction.ts:492-541``）。

        上游有 6 个重载按作用域分派；Python 侧统一成 ``doc(token, *args)``，
        参数布局由 :func:`~pi_durable.documents.resolve_address` 按定义的
        ``scope`` / ``family`` 解析（偏离 #48 的同一取舍）。
        """
        try:
            self._assert_open()
            definition = token.definition
            resolved = resolve_address(definition, args)
            self._assert_task_documents_open(resolved)
            latest = self._latest_document_by_address.get(resolved.id)
            if latest is not None and not latest.retire_on_commit:
                if latest.draft_promise is not None:
                    return latest.draft_promise
                if latest.target is not None and latest.target.kind == "fork-copy":
                    latest.draft_promise = self._ensure_future(
                        self._acquire_fork_copy(latest, definition, latest.target)
                    )
                    return latest.draft_promise
            seed = (
                copy_json(args[resolved.next_argument])
                if definition.get("family") is True
                else None
            )
            entry = _DocumentEntry(
                address_id=resolved.id,
                address=resolved.address,
                definition=definition,
                retire_on_commit=False,
            )
            self._documents.append(entry)
            self._latest_document_by_address[entry.address_id] = entry
            # 在 await 之前先抓住退休状态，好让「旧的未决获取」与它的替代者保持区分。
            skip_load = latest is not None and latest.retire_on_commit
            entry.draft_promise = self._ensure_future(self._acquire(entry, seed, skip_load))
            return entry.draft_promise
        except Exception as error:
            return _failed_future(error)

    def retire_doc(self, token: Any, *args: Any) -> Awaitable[None]:
        """退休一个文档化身（``transaction.ts:543-586``）。"""
        try:
            self._assert_open()
            definition = token.definition
            resolved = resolve_address(definition, args)
            latest = self._latest_document_by_address.get(resolved.id)
            if latest is not None and latest.retire_on_commit:
                return _resolved_future(None)
            if (
                latest is not None
                and latest.target is not None
                and latest.target.kind == "fork-copy"
            ):
                check_record_scope(definition, latest.target.record)
                latest.retire_on_commit = True
                return _resolved_future(None)
            if latest is not None and latest.draft_promise is not None:
                # 退休一个已取得的草稿：先把它的最终内容持久化，再退休。
                latest.retire_on_commit = True
                return self._ensure_future(_await_none(latest.draft_promise))
            entry = _DocumentEntry(
                address_id=resolved.id,
                address=resolved.address,
                definition=definition,
                retire_on_commit=True,
            )
            self._documents.append(entry)
            self._latest_document_by_address[entry.address_id] = entry
            return self._ensure_future(self._find_retirement(entry))
        except Exception as error:
            return _failed_future(error)

    def _ensure_future(self, awaitable: Awaitable[_T]) -> asyncio.Future[_T]:
        """把一个协程包成可**重复 await** 的 Task 并登记进未决操作。

        ⚠️ R1：上游缓存的是 Promise，可以重复 await；缓存裸协程会在第二次 await 时炸。
        """
        future = asyncio.ensure_future(awaitable)
        self._pending_operations.add(future)
        future.add_done_callback(self._pending_operations.discard)
        return future

    def _assert_task_documents_open(self, resolved: Any) -> None:
        """任务级文档：任务一旦终局就不能再拿它的文档（``transaction.ts:907-912``）。"""
        scope = resolved.address["scope"]
        if scope["kind"] != "task":
            return
        staged = self._tasks_by_id.get(scope["taskId"])
        if (
            staged is not None
            and staged.write is not None
            and staged.write["record"]["state"]["status"] == "terminal"
        ):
            raise RuntimeError(f"Task {scope['taskId']} is terminal")

    async def _acquire(self, entry: _DocumentEntry, seed: Any, skip_load: bool) -> Draft:
        """取得一个文档草稿：先试缓存/存储，没有再按定义创建（``transaction.ts:588-623``）。"""
        definition = entry.definition
        loaded = (
            None
            if skip_load
            else await self._host.load(definition, entry.address_id, entry.address, self._context)
        )
        self._assert_open()
        if loaded is not None:
            check_record_scope(definition, loaded.record)
            check_record_version(definition, loaded.record, loaded.stored_version)
            entry.target = _DocumentTarget(kind="loaded", document=loaded)
            entry.change = loaded.tracker.begin_change()
            return cast(Draft, entry.change.state)

        scope = entry.address["scope"]
        if scope["kind"] == "conversation":
            await self._require_conversation(scope["conversationId"])
        if scope["kind"] == "task":
            task = await self._current_task(scope["taskId"])
            if task is None:
                raise RuntimeError(f"Task {scope['taskId']} does not exist")
            if task["state"]["status"] == "terminal":
                raise RuntimeError(f"Task {scope['taskId']} is terminal")
        self._assert_open()

        initial = definition["initial"]
        value = cast(
            JsonObject,
            copy_json(initial(seed) if definition.get("family") is True else initial(None)),
        )
        id = await self._host.storage.mint_id()
        self._assert_open()
        tracker: Tracker[JsonObject] = track(value)
        entry.target = _DocumentTarget(
            kind="created",
            record=document_create(definition, entry.address, id),
            version=definition.get("version"),
            tracker=tracker,
        )
        entry.change = tracker.begin_change()
        return cast(Draft, entry.change.state)

    async def _acquire_fork_copy(
        self, entry: _DocumentEntry, definition: Any, target: _DocumentTarget
    ) -> Draft:
        """取得一个分叉副本的草稿（``transaction.ts:625-650``）。"""
        stored = await self._host.storage.document(
            target.source["id"], target.source["at"], self._context
        )
        self._assert_open()
        if stored is None:
            raise RuntimeError(f"Fork source document {target.source['id']} cannot be read")
        record = stored["record"]
        copy_record = target.record
        if (
            record["scope"]["kind"] != "conversation"
            or record["kind"] != copy_record["kind"]
            or record.get("key") != copy_record.get("key")
            or record.get("history") != copy_record.get("history")
            or record.get("fork") != copy_record.get("fork")
        ):
            raise RuntimeError(
                f"Fork source document {target.source['id']} does not match the copied record"
            )
        value = materialize_document_value(
            definition, copy_record, stored["version"], stored["value"]
        )
        tracker: Tracker[JsonObject] = track(value)
        entry.definition = definition
        entry.target = _DocumentTarget(
            kind="created", record=copy_record, version=definition.get("version"), tracker=tracker
        )
        entry.change = tracker.begin_change()
        return cast(Draft, entry.change.state)

    async def _find_retirement(self, entry: _DocumentEntry) -> None:
        """找出要退休的当前化身（``transaction.ts:652-660``）。"""
        cached = self._host.cached(entry.address_id)
        record = (
            cached.record
            if cached is not None
            else await self._host.storage.find_document(entry.address, "current", self._context)
        )
        self._assert_open()
        if record is None:
            return
        check_record_scope(entry.definition, record)
        entry.target = _DocumentTarget(kind="retire-only", record=record)

    # ─── 装配与结算（第 3 步） ───────────────────────────────────────────────

    async def settle_success(self) -> list[StorageWrite]:
        """回调成功后封口、准备每个未决变更，并装配原子批（``transaction.ts:675-698``）。

        任何失败都会在存储准入之前中止每个变更。

        ⚠️ **本步只装配第 1–3 步能产生的写入**（会话 / 条目 / 提交）。第 4–5 步的容器
        （任务写入、文档计划）现在必然为空；万一非空，这里会**直接报错**而不是静默丢掉它们——
        静默丢写入比报错危险得多。
        """
        self._sealed = True
        if self._pending_operations:
            await asyncio.gather(*list(self._pending_operations), return_exceptions=True)
            raise RuntimeError("Session commit callback settled before its pending Tx operations")
        try:
            # 同步准备每个未决变更；这会吊销每个草稿。
            for document in self._documents:
                if document.change is not None:
                    document.prepared = document.change.prepare()
            return await self._assemble()
        except Exception:
            self.discard()
            raise

    async def _assemble(self) -> list[StorageWrite]:
        """装配这次的原子批（``transaction.ts:745-850`` 的第 1–3 步子集）。"""
        storage = self._host.storage
        for entry in self._documents:
            plan = _plan_document(entry)
            if plan is not None:
                self._plans.append(plan)
        self._reject_fork_source_writes(self._plans)

        await self._validate_owners()
        await self._validate_task_replacements()

        # 终局结算会退休该任务的**每一个**文档，包括本事务刚创建的。
        terminal_task_ids = {
            staged.write["record"]["id"]
            for staged in self._tasks_by_id.values()
            if staged.write is not None and staged.write["record"]["state"]["status"] == "terminal"
        }
        if terminal_task_ids:
            retiring: set[DocumentId] = set()
            for plan in self._plans:
                scope = plan.record["scope"]
                if scope["kind"] != "task" or scope["taskId"] not in terminal_task_ids:
                    continue
                plan.retire = True
                retiring.add(plan.record["id"])
            for task_id in terminal_task_ids:
                staged = self._tasks_by_id.get(task_id)
                if (
                    staged is not None
                    and staged.write is not None
                    and staged.write.get("kind") == "create"
                ):
                    continue
                cursor: Cursor | None = None
                while True:
                    page = await storage.scan_documents(
                        {"scope": {"kind": "task", "taskId": task_id}, "at": "current"},
                        INTERNAL_SCAN_PAGE_SIZE,
                        cursor,
                        self._context,
                    )
                    for record in page["items"]:
                        if record["id"] in retiring:
                            continue
                        self._plans.append(
                            _DocumentPlan(address_id=address_id(record), record=record, retire=True)
                        )
                        retiring.add(record["id"])
                    cursor = page.get("next")
                    if cursor is None:
                        break

        # 发布归属在存储准入**之前**解析好，这样 adoption 全程同步。
        for plan in self._plans:
            if not _publishes(plan):
                continue
            scope = plan.record["scope"]
            if scope["kind"] == "conversation":
                plan.conversation_id = int(scope["conversationId"])
            if scope["kind"] != "task":
                continue
            staged_task = self._task_entry(int(scope["taskId"]))
            if staged_task.publication_conversation_id is None:
                owner_task = await self._current_task(int(scope["taskId"]))
                if owner_task is not None:
                    staged_task.publication_conversation_id = int(owner_task["conversationId"])
            plan.conversation_id = staged_task.publication_conversation_id

        # 提交变更在装配期针对「本事务最新的候选记录」解析，回退到已提交状态。
        for id, change in self._submission_changes:
            current_record = self._submissions.get(id)
            if current_record is None:
                current_record = await storage.submission(id, self._context)
            if current_record is None:
                raise RuntimeError(f"Submission {id} does not exist")
            next_record = apply_submission_change(current_record, change)
            if next_record is not current_record:
                self._submissions[id] = next_record

        writes = list(self._writes)
        writes.extend(
            cast(StorageWrite, {"type": "submission", "value": value})
            for value in self._submissions.values()
        )
        for staged in self._tasks_by_id.values():
            if staged.write is not None:
                writes.append(cast(StorageWrite, {"type": "task", "value": staged.write["record"]}))

        for plan in self._plans:
            change = plan.change
            # checkpoint 谓词**最后**跑，在所有校验之后。
            content = plan.content
            if (
                content is not None
                and content["type"] == "document.change"
                and content["content"]["kind"] == "delta"
                and change is not None
                and change["loaded"] is not None
            ):
                prepared = change["prepared"]
                info = {"deltasSinceBase": change["loaded"].deltas_since_base}
                predicate = (change["definition"] or {}).get("checkpointWhen")
                if predicate is not None and predicate(prepared.value, prepared.ops, info):
                    plan.content = {
                        **content,
                        "content": {
                            "version": change["version"],
                            "kind": "base",
                            "value": prepared.value,
                        },
                    }
            if plan.content is not None:
                writes.append(cast(StorageWrite, plan.content))
            if plan.retire:
                writes.append(
                    cast(StorageWrite, {"type": "document.retire", "id": plan.record["id"]})
                )
        return writes

    def _reject_fork_source_writes(self, plans: list[_DocumentPlan]) -> None:
        """分叉事务里不能改「分叉来源」的文档或会话（``transaction.ts:882-901``）。"""
        for plan in plans:
            if plan.content is None and not plan.retire:
                continue
            record = plan.record
            if record["id"] in self._fork_source_document_ids:
                raise RuntimeError(
                    f"Cannot change fork source document {record['id']} in the fork transaction"
                )
            scope = record["scope"]
            if (
                scope["kind"] == "conversation"
                and scope["conversationId"] in self._fork_source_conversation_ids
                and record.get("fork") == "current"
            ):
                raise RuntimeError(
                    f"Cannot fork conversation {scope['conversationId']} while changing its "
                    "current-policy documents"
                )

    def _abort_changes(self) -> None:
        """中止每个已暂存的变更（``transaction.ts:903-905``）。"""
        for document in self._documents:
            if document.change is not None:
                document.change.abort()

    def adopt(self, seq: int) -> list[dict[str, Any]]:
        """存储成功后**按指针交换**套用每个已准备的变更，并描述发布（``transaction.ts:700-742``）。"""
        publications: list[dict[str, Any]] = []
        for plan in self._plans:
            committed = "createdAt" in plan.record
            record = dict(plan.record) if committed else {**plan.record, "createdAt": seq}
            if plan.retire:
                record = {**record, "retiredAt": seq}
            change = plan.change
            if change is not None:
                tracker = change["tracker"]
                prepared = change["prepared"]
                loaded = change["loaded"]
                version = change["version"]
                # 新化身会被采纳，除非它在同一次提交里就退休；已加载的只在**真改了**时采纳。
                if (not plan.retire) if loaded is None else bool(prepared.ops):
                    tracker.adopt(prepared)
                else:
                    prepared.abort()
                if loaded is not None:
                    if loaded.stored_version < version:
                        loaded.stored_version = version
                    content = plan.content
                    if content is not None and content["type"] == "document.change":
                        if content["content"]["kind"] == "base":
                            loaded.deltas_since_base = 0
                        else:
                            loaded.deltas_since_base += 1
                elif not plan.retire:
                    self._host.install(
                        LoadedDocument(
                            address_id=plan.address_id,
                            record=record,
                            stored_version=version,
                            value_version=version,
                            deltas_since_base=0,
                            tracker=tracker,
                        )
                    )
            conversation_id = plan.conversation_id
            if plan.retire:
                if committed:
                    self._host.evict(plan.address_id, record["id"])
                publications.append(
                    {
                        "type": "document",
                        "record": record,
                        "conversationId": conversation_id,
                        "version": None,
                        "value": None,
                        "ops": [],
                    }
                )
            elif plan.content is not None and plan.content["type"] == "document.copy":
                publications.append(
                    {
                        "type": "document.copy",
                        "record": record,
                        "conversationId": conversation_id,
                        "source": plan.content["source"],
                    }
                )
            elif change is not None and _publishes(plan):
                ops = [] if change["loaded"] is None else list(change["prepared"].ops)
                publications.append(
                    {
                        "type": "document",
                        "record": record,
                        "conversationId": conversation_id,
                        "version": change["version"],
                        "value": change["prepared"].value,
                        "ops": ops,
                    }
                )
        return publications

    async def _validate_task_replacements(self) -> None:
        """替换（非创建）的任务必须在存储里存在、未终局、且不换会话（``transaction.ts:786-796``）。"""
        for id, staged in self._tasks_by_id.items():
            if staged.write is None or staged.write.get("kind") != "replace":
                continue
            record = staged.write["record"]
            committed = await self._committed_task(id)
            if committed is None:
                raise RuntimeError(f"Task {id} does not exist")
            if committed["state"]["status"] == "terminal":
                raise RuntimeError(f"Task {id} is already terminal")
            if committed["conversationId"] != record["conversationId"]:
                raise RuntimeError(f"Task {id} cannot change conversations")

    async def _validate_owners(self) -> None:
        """新的被拥有工作需要一个**活着的**拥有者，按拥有者的最终候选判定（``transaction.ts:855-880``）。

        ``completing`` / 已终局 / 已标记 abort 的任务都不能再创建被拥有的工作——
        所以一个任务**不能**在结束自己的那次提交里创建被拥有的工作（spec §5.5）。
        """
        owners: list[tuple[str, TaskId]] = []
        for write in self._writes:
            if write["type"] != "conversation":
                continue
            owner = write["value"].get("owner")
            if owner is not None:
                owners.append(("Conversation owner task", int(owner["taskId"])))
        for staged in self._tasks_by_id.values():
            if staged.write is not None and staged.write.get("kind") == "create":
                record = staged.write["record"]
                if record.get("owner") is not None:
                    owners.append(("Task owner", int(record["owner"])))

        for what, task_id in owners:
            owner_record = await self._current_task(task_id)
            if owner_record is None:
                raise RuntimeError(f"{what} {task_id} does not exist")
            status = owner_record["state"]["status"]
            if status == "terminal" or status == "completing":
                raise RuntimeError(f"{what} {task_id} is {status}")
            if owner_record["abortRequested"]:
                raise RuntimeError(f"{what} {task_id} is abort-marked")

    async def _require_conversation(self, id: ConversationId) -> None:
        """确认会话存在，或由本事务创建（``transaction.ts:942-947``）。"""
        if id in self._created_conversation_ids:
            return
        if await self._host.storage.conversation(id, self._context) is None:
            raise RuntimeError(f"Conversation {id} does not exist")

    async def _current_task(self, id: TaskId) -> TaskRecord | None:
        """最新候选任务记录，回退到已提交状态；**不是**调用方的表读取（``transaction.ts:976-978``）。"""
        task = self._tasks_by_id.get(id)
        if task is not None and task.write is not None:
            return cast(TaskRecord, task.write["record"])
        return await self._committed_task(id)

    async def _write(self, write: Callable[[], Awaitable[_T]]) -> _T:
        """登记一次表写入（``transaction.ts:932-940``）——它会置位「读过表就不能再写」的反面：
        写过表之后**读取**才被拒（见 :meth:`_read`）。"""
        try:
            self._assert_open()
            self._has_table_write = True
        except Exception:
            raise
        return await self._track(write())

    # ─── 生命周期（第 1–2 步只用得到这两条） ─────────────────────────────────

    async def settle_failure(self) -> None:
        """回调失败后封口：中止每个变更并观察每个未完成操作（``transaction.ts:665-669``）。"""
        self._sealed = True
        self._abort_changes()
        await asyncio.gather(*list(self._pending_operations), return_exceptions=True)

    def discard(self) -> None:
        """存储失败后（或不需要写入时）丢弃每个已准备的变更（``transaction.ts:699-701``）。

        第 5 步已落位，所以这里真的会中止文档变更。
        """
        self._abort_changes()

    # ─── 内部 ────────────────────────────────────────────────────────────────

    def _assert_open(self) -> None:
        if self._sealed:
            raise RuntimeError("Transaction has settled")

    def _task_entry(self, id: TaskId) -> _TransactionTask:
        """给出（必要时新建）某个任务在本事务里的条目（``transaction.ts:966-974``）。"""
        task = self._tasks_by_id.get(id)
        if task is None:
            task = _TransactionTask()
            self._tasks_by_id[id] = task
        return task

    def _committed_task(self, id: TaskId) -> Awaitable[TaskRecord | None]:
        """最新候选任务记录，回退到已提交状态；**不是**调用方的表读取（``transaction.ts:980-984``）。

        已提交读取是**记忆化**的：同一事务里多次读同一个任务只碰一次存储。
        """
        task = self._task_entry(id)
        pending = task.committed_read
        if pending is None:
            # ⚠️ 必须包成 **Task**，不能直接存协程：JS 的 Promise 可以重复 await，
            # Python 的协程 await 第二次会 RuntimeError("cannot reuse already awaited
            # coroutine")。上游 ``task.committedRead ??= storage.task(...)`` 靠的就是
            # Promise 可重复等待这一点。
            pending = asyncio.ensure_future(self._host.storage.task(id, self._context))
            task.committed_read = pending
        return pending

    async def _track(self, operation: Awaitable[_T]) -> _T:
        """登记一个操作，好让回调结算能拒绝并排空它（``transaction.ts:915-921``）。"""
        future = asyncio.ensure_future(operation)
        self._pending_operations.add(future)
        future.add_done_callback(self._pending_operations.discard)
        return await future

    async def _read(self, method: str, read: Callable[[], Awaitable[_T]]) -> _T:
        """一次**表读取**（``transaction.ts:922-930``）。

        一旦本事务写过表，就读不到「已提交状态」了：``ReadAfterWrite`` 拒绝它，
        因为此时已提交状态**不含**本事务自己的写入。
        """
        try:
            self._assert_open()
            if self._has_table_write:
                raise ReadAfterWrite(method)
        except Exception as error:
            raise error
        return await self._track(read())


def _plan_document(entry: _DocumentEntry) -> _DocumentPlan | None:
    """一个已暂存化身要写什么、要发布什么（``transaction.ts:990-1043`` 的 ``planDocument``）。"""
    target = entry.target
    if target is None:
        return None
    plan = _DocumentPlan(address_id=entry.address_id, record=None, retire=entry.retire_on_commit)

    if target.kind == "created":
        prepared = entry.prepared
        content = {"version": target.version, "kind": "base", "value": prepared.value}
        plan.record = target.record
        plan.content = {"type": "document.create", "record": target.record, "content": content}
        plan.change = {
            "tracker": target.tracker,
            "prepared": prepared,
            "version": target.version,
            "loaded": None,
            "definition": None,
        }
        return plan

    if target.kind == "fork-copy":
        plan.record = target.record
        plan.content = {"type": "document.copy", "record": target.record, "source": target.source}
        return plan

    if target.kind == "retire-only":
        plan.record = target.record
        return plan

    # loaded
    assert target.document is not None
    loaded = target.document
    definition = entry.definition
    prepared = entry.prepared
    version = int(definition.get("version", 0))
    id = loaded.record["id"]
    # 版本变了就必须存 base（哪怕没有操作）；否则只在真改了东西时存 delta。
    loaded_content: dict[str, Any] | None
    if loaded.stored_version < version:
        loaded_content = {
            "type": "document.change",
            "id": id,
            "content": {"version": version, "kind": "base", "value": prepared.value},
        }
    elif prepared.ops:
        loaded_content = {
            "type": "document.change",
            "id": id,
            "content": {"version": version, "kind": "delta", "ops": prepared.ops},
        }
    else:
        loaded_content = None
    plan.record = loaded.record
    plan.change = {
        "tracker": loaded.tracker,
        "prepared": prepared,
        "version": version,
        "loaded": loaded,
        "definition": definition,
    }
    plan.content = loaded_content
    return plan
