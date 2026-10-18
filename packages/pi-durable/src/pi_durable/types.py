"""durable 的记录与存储契约 —— 移植 ``durable/src/types.ts``（1,154 行）的**存储子集**。

上游 ``types.ts`` 有 46 个导出类型，覆盖 records / 文档 / Session / Tx / Harness 全部契约。
本模块只取**存储内核**需要的那些：records、查询、``StorageWrite``、以及 :class:`Storage`
协议本身。``Session`` / ``Tx`` / ``TaskRuntime`` 属于 Session 内核，见 ``PORTING.md`` 的
「批次 B 仍未做」。

为什么记录用 :class:`~typing.TypedDict` 而不是 dataclass
--------------------------------------------------------

上游的记录是 TS 对象类型，**持久化形式就是 JSON**（``storage/sqlite/migrations.ts:28`` 的
``record TEXT NOT NULL CHECK (json_valid(record))``）。用 ``TypedDict`` 有三个直接好处：

1. 运行时就是普通 ``dict``——与「记录即 JSON」的事实一致，也与一致性套件里
   ``toEqual({...})`` 的深比较方式一致；
2. 「返回的记录必须与写入的解除关联」这条语义（一致性用例 *detaches retained writes and every
   returned record*）用 ``copy.deepcopy`` 天然表达；
3. ``mypy --strict`` 对 ``TypedDict`` 的检查足够强，且可判别联合（``SubmissionRecord`` 的 7 种、
   ``TaskState`` 的 5 种）用 ``Union`` 就能表达。

这是相对 PORTING.md 偏离 #6（对象字面量 → 冻结 dataclass）的**有意例外**：那条针对的是
*选项对象*，记录是*持久化数据*。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import (
    Any,
    Literal,
    NamedTuple,
    NotRequired,
    Protocol,
    Required,
    TypeAlias,
    TypedDict,
)

from pi_ai import Message
from pi_chord import JsonValue
from pi_chord.context import Context
from pi_chord.delta import Op

__all__ = [
    "CommitChange",
    "CommitPublication",
    "ContextEdit",
    "ConversationId",
    "ConversationOwnership",
    "ConversationQuery",
    "ConversationRecord",
    "Cursor",
    "DocumentAddress",
    "DocumentCommitChange",
    "DocumentContent",
    "DocumentCopySource",
    "DocumentCreate",
    "DocumentId",
    "DocumentPoint",
    "DocumentQuery",
    "DocumentRecord",
    "DocumentSemantics",
    "DocumentWatch",
    "EntryDraft",
    "EntryId",
    "EntryQuery",
    "EntryRecord",
    "Id",
    "JoinPolicy",
    "JsonObject",
    "NextTaskState",
    "Page",
    "ROOT_CONVERSATION_ID",
    "RunningTask",
    "ScanOrder",
    "Seq",
    "SessionEnd",
    "Storage",
    "StorageWrite",
    "StoredDocument",
    "SubmissionCreate",
    "SubmissionId",
    "SubmissionQuery",
    "SubmissionRecord",
    "SubmissionSettlement",
    "Task",
    "TaskDefinition",
    "TaskId",
    "TaskOptions",
    "TaskOutcome",
    "TaskOutcomeError",
    "TaskOwnership",
    "TaskQuery",
    "TaskRecord",
    "TaskState",
    "TypedEntry",
    "TypedEntryDraft",
    "WatchEnd",
    "WatchHandle",
]

#: 上游 ``types.ts:15``：每个 durable 文档的根都是 JSON 对象。
JsonObject: TypeAlias = dict[str, JsonValue]

#: 上游的 ``Id<Kind>`` 是**被擦除的**数字品牌（``types.ts:20-25``）：编译期区分种类，
#: 运行期就是 ``number``。Python 用 ``NewType`` 对应——运行期同样是裸 ``int``。
Id: TypeAlias = int
ConversationId = Id
EntryId = Id
SubmissionId = Id
DocumentId = Id
#: ``TaskId<Result>`` 在 TS 里带一个结果类型参数；Python 的 ``NewType`` 不支持泛型，
#: 这里退化成裸别名（见 PORTING.md）。
TaskId = Id

#: 上游 ``types.ts:36``：一次原子提交得到的、严格递增的序列号；允许有空洞。
Seq = int

#: 上游 ``types.ts:39``：根会话永远用这个保留 ID。
ROOT_CONVERSATION_ID: ConversationId = 1

#: 上游 ``types.ts:277``：``failFast`` 或 ``allSettled``。
JoinPolicy = Literal["failFast", "allSettled"]


# --------------------------------------------------------------------------------------
# 会话与条目（types.ts:302-365）
# --------------------------------------------------------------------------------------


class _ForkSource(TypedDict):
    conversationId: ConversationId
    at: EntryId


class _Owner(TypedDict):
    conversationId: ConversationId
    taskId: TaskId


class ConversationRecord(TypedDict, total=False):
    """不可变的身份、历史血缘与任务归属（``types.ts:302-316``）。"""

    id: ConversationId
    parent: _ForkSource
    owner: _Owner


class _ContextEditOmit(TypedDict):
    target: EntryId
    action: Literal["omit"]


class _ContextEditReplace(TypedDict):
    target: EntryId
    action: Literal["replace"]
    messages: list[Message]


#: 对某个可见条目在模型上下文里贡献的不可变覆盖（``types.ts:317-332``）。
ContextEdit: TypeAlias = _ContextEditOmit | _ContextEditReplace


class EntryRecord(TypedDict, total=False):
    """不可变的转录事件（``types.ts:333-350``）。``id`` / ``conversationId`` / ``kind`` 必有。"""

    id: EntryId
    conversationId: ConversationId
    kind: str
    model: list[Message]
    data: JsonValue
    head: EntryId
    edits: list[ContextEdit]
    byTaskId: TaskId


class EntryDraft(TypedDict, total=False):
    """身份与归属确定前的条目内容（``types.ts:351-356``）。

    ``head`` 给 ``"self"`` 表示「活动上下文从这个新条目的 ID 开始」。
    """

    kind: str
    model: list[Message]
    data: JsonValue
    head: EntryId | Literal["self"]
    edits: list[ContextEdit]


# --------------------------------------------------------------------------------------
# 提交（submission）（types.ts:379-456）
# --------------------------------------------------------------------------------------


class _SubmissionBase(TypedDict, total=False):
    id: SubmissionId
    conversationId: ConversationId
    requestId: str


class SubmissionRecordBase(_SubmissionBase):
    """每种提交状态共有的身份字段（``types.ts:372-378``）。"""


class InputQueued(_SubmissionBase):
    type: Literal["input"]
    status: Literal["queued"]


class InputPlaced(_SubmissionBase):
    type: Literal["input"]
    status: Literal["placed"]
    entry: EntryId


class InputDone(_SubmissionBase):
    type: Literal["input"]
    status: Literal["done"]
    entry: EntryId
    answer: EntryId


class InputUnanswered(_SubmissionBase):
    """上游 ``types.ts:414-420``：``entry?: EntryId``——**可选**，所以这里 ``NotRequired``。"""

    type: Literal["input"]
    status: Literal["unanswered"]
    reason: str
    detail: NotRequired[JsonValue]
    entry: NotRequired[EntryId]


class WriteQueued(_SubmissionBase):
    type: Literal["write"]
    status: Literal["queued"]


class WriteDone(_SubmissionBase):
    type: Literal["write"]
    status: Literal["done"]
    entry: EntryId


class WriteUnanswered(_SubmissionBase):
    type: Literal["write"]
    status: Literal["unanswered"]
    reason: str
    detail: NotRequired[JsonValue]


#: 一次已受理的用户输入或被动写入的持久生命周期（``types.ts:379-444``）。
SubmissionRecord: TypeAlias = (
    InputQueued
    | InputPlaced
    | InputDone
    | InputUnanswered
    | WriteQueued
    | WriteDone
    | WriteUnanswered
)


class _SettlementDone(TypedDict):
    status: Literal["done"]
    answer: EntryId


class _SettlementUnanswered(TypedDict, total=False):
    status: Literal["unanswered"]
    reason: str
    detail: JsonValue


#: 为某次提交准备的终局状态（``types.ts:446-449``）。
SubmissionSettlement: TypeAlias = _SettlementDone | _SettlementUnanswered

#: 身份确定前的提交字段（``types.ts:451-456``）。
SubmissionCreate: TypeAlias = SubmissionRecord


# --------------------------------------------------------------------------------------
# 任务（types.ts:458-585）
# --------------------------------------------------------------------------------------


class TaskOutcomeError(TypedDict, total=False):
    """持久化的 JSON 安全错误快照，替代运行时的 ``Error`` 对象（``types.ts:458-463``）。"""

    message: str
    detail: JsonValue


class _OutcomeCompleted(TypedDict):
    status: Literal["completed"]
    result: JsonValue


class _OutcomeFailed(TypedDict):
    status: Literal["failed"]
    error: TaskOutcomeError


class _OutcomeAborted(TypedDict):
    status: Literal["aborted"]
    reason: str


class _OutcomeFaulted(TypedDict):
    status: Literal["faulted"]
    error: TaskOutcomeError


class _OutcomeOrphaned(TypedDict):
    status: Literal["orphaned"]
    reason: str


#: 任务转为终局时记录的持久原因与可选结果（``types.ts:465-501``）。
TaskOutcome: TypeAlias = (
    _OutcomeCompleted | _OutcomeFailed | _OutcomeAborted | _OutcomeFaulted | _OutcomeOrphaned
)


class _StatePending(TypedDict):
    status: Literal["pending"]
    checkpoint: JsonValue


class _StateRunning(TypedDict):
    status: Literal["running"]
    checkpoint: JsonValue


class _StateWaiting(TypedDict):
    status: Literal["waiting"]
    checkpoint: JsonValue
    on: list[TaskId]
    policy: JoinPolicy


class _StateCompleting(TypedDict):
    status: Literal["completing"]
    outcome: TaskOutcome


class _StateTerminal(TypedDict):
    status: Literal["terminal"]
    outcome: TaskOutcome


#: 一个任务状态机的完整状态（``types.ts:502-553``）。5 种，按 ``status`` 判别。
TaskState: TypeAlias = (
    _StatePending | _StateRunning | _StateWaiting | _StateCompleting | _StateTerminal
)


class TaskRecord(TypedDict, total=False):
    """一个持久任务状态机的完整替换记录（``types.ts:573-585``）。

    ``id`` / ``conversationId`` / ``kind`` / ``version`` / ``input`` / ``background`` /
    ``abortRequested`` / ``state`` 必有。
    """

    id: TaskId
    conversationId: ConversationId
    kind: str
    version: int
    input: JsonValue
    owner: TaskId
    background: bool
    abortRequested: bool
    abortReason: Literal["restart"]
    abandonOnRestart: Literal[True]
    startedAt: int
    endedAt: int
    memos: dict[str, JsonValue]
    state: TaskState


class _OwnershipConversation(TypedDict):
    kind: Literal["conversation"]


class _OwnershipTask(TypedDict):
    kind: Literal["task"]
    taskId: TaskId


#: 任务归属（``types.ts:274-275``）。
TaskOwnership: TypeAlias = _OwnershipConversation | _OwnershipTask

#: 会话归属（``types.ts:299-300``）。
ConversationOwnership: TypeAlias = _OwnershipConversation | _OwnershipTask


class TaskOptions(TypedDict, total=False):
    """创建任务的选项（``types.ts:280-298``）。"""

    ownership: TaskOwnership
    conversationId: ConversationId
    background: bool
    abandonOnRestart: bool


# --------------------------------------------------------------------------------------
# 文档（types.ts:587-731）
# --------------------------------------------------------------------------------------


class _ScopeSession(TypedDict):
    kind: Literal["session"]


class _ScopeConversation(TypedDict):
    kind: Literal["conversation"]
    conversationId: ConversationId


class _ScopeTask(TypedDict):
    kind: Literal["task"]
    taskId: TaskId


DocumentScope: TypeAlias = _ScopeSession | _ScopeConversation | _ScopeTask


class DocumentSemantics(TypedDict, total=False):
    """文档的历史与分叉语义（``types.ts:56-62`` 的 ``DocumentSemantics``）。"""

    history: Literal["latest", "rewindable"]
    fork: Literal["current", "initial", "asOf"]


class DocumentRecord(TypedDict, total=False):
    """一次 create-to-retire 的文档化身记录（``types.ts:587-625``）。

    ``id`` / ``kind`` / ``createdAt`` / ``scope`` 必有。
    """

    id: DocumentId
    kind: str
    key: str
    createdAt: Seq
    retiredAt: Seq
    scope: DocumentScope
    history: Literal["latest", "rewindable"]
    fork: Literal["current", "initial", "asOf"]


#: 存储创建并盖章新 ``DocumentRecord`` 时提供的字段（``types.ts:626-631``）。
DocumentCreate: TypeAlias = DocumentRecord


class Page(TypedDict, total=False):
    """一页有序扫描结果与可选续扫状态（``types.ts:633-636``）。"""

    items: list[Any]
    next: Cursor


#: 后端自有的 JSON 续扫状态；调用方只把它原样回传给同一个扫描（``types.ts:642``）。
Cursor: TypeAlias = dict[str, JsonValue]

#: 扫描的 ID 方向（``types.ts:645``）。
ScanOrder = Literal["ascending", "descending"]


class ConversationQuery(TypedDict, total=False):
    """有序会话扫描的可选过滤（``types.ts:648-655``）。"""

    ownerConversationId: ConversationId
    ownerTaskId: TaskId
    order: ScanOrder


class EntryQuery(TypedDict, total=False):
    """单个会话的 fork 感知历史的闭区间 ID 边界（``types.ts:656-666``）。"""

    conversationId: ConversationId
    minEntryId: EntryId
    maxEntryId: EntryId
    order: ScanOrder


class TaskQuery(TypedDict, total=False):
    """有序任务扫描的可选过滤（``types.ts:667-677``）。"""

    conversationId: ConversationId
    kind: str
    status: str
    abortRequested: bool
    background: bool
    order: ScanOrder


class SubmissionQuery(TypedDict, total=False):
    """有序提交扫描的可选过滤（``types.ts:678-685``）。"""

    conversationId: ConversationId
    status: str
    order: ScanOrder


#: 文档成员与内容读取所用的时点：当前，或某一个历史提交序号（``types.ts:686``）。
DocumentPoint: TypeAlias = Seq | Literal["current"]


class DocumentAddress(TypedDict, total=False):
    """单例或某个具名家族成员的精确逻辑身份（``types.ts:689-696``）。"""

    kind: str
    scope: DocumentScope
    key: str


class DocumentQuery(TypedDict, total=False):
    """在某个精确 scope、某个时点上存活的文档化身的有序扫描（``types.ts:697-703``）。"""

    scope: DocumentScope
    at: DocumentPoint
    kind: str


class _ContentBase(TypedDict):
    version: int
    kind: Literal["base"]
    value: JsonObject


class _ContentDelta(TypedDict):
    version: int
    kind: Literal["delta"]
    ops: list[Op]


#: 完整检查点，或由所属 Session 选出的 Chord 操作批次（``types.ts:704-716``）。
DocumentContent: TypeAlias = _ContentBase | _ContentDelta


class DocumentCopySource(TypedDict):
    """无定义文档复制所选中的精确持久源（``types.ts:717-722``）。"""

    id: DocumentId
    at: DocumentPoint


class StoredDocument(TypedDict):
    """某个时点上物化出的、已解除关联的值与存储的定义版本（``types.ts:723-731``）。"""

    record: DocumentRecord
    version: int
    value: JsonObject
    deltasSinceBase: int


# --------------------------------------------------------------------------------------
# StorageWrite 与提交发布（types.ts:732-789）
# --------------------------------------------------------------------------------------


class _WriteConversation(TypedDict):
    type: Literal["conversation"]
    value: ConversationRecord


class _WriteEntry(TypedDict):
    type: Literal["entry"]
    value: EntryRecord


class _WriteTask(TypedDict):
    type: Literal["task"]
    value: TaskRecord


class _WriteSubmission(TypedDict):
    type: Literal["submission"]
    value: SubmissionRecord


class _WriteDocumentCreate(TypedDict):
    type: Literal["document.create"]
    record: DocumentCreate
    content: _ContentBase


class _WriteDocumentCopy(TypedDict):
    type: Literal["document.copy"]
    record: DocumentCreate
    source: DocumentCopySource


class _WriteDocumentChange(TypedDict):
    type: Literal["document.change"]
    id: DocumentId
    content: DocumentContent


class _WriteDocumentRetire(TypedDict):
    type: Literal["document.retire"]
    id: DocumentId


#: 一次原子存储提交里的一次记录或文档变更（``types.ts:732-750``）。
StorageWrite: TypeAlias = (
    _WriteConversation
    | _WriteEntry
    | _WriteTask
    | _WriteSubmission
    | _WriteDocumentCreate
    | _WriteDocumentCopy
    | _WriteDocumentChange
    | _WriteDocumentRetire
)

#: 一次提交里非文档（表）记录的变更类型（``types.ts:773-777``）。
TableCommitChange: TypeAlias = Literal["conversation", "entry", "task", "submission"]


class DocumentCommitChange(TypedDict, total=False):
    """某个文档化身已提交的变更（``types.ts:751-772``）。"""

    type: Literal["document"]
    record: DocumentRecord
    conversationId: Any
    version: Any
    value: Any
    deltasSinceBase: int


#: 一次提交发布的变更（``types.ts:778-779``）。
CommitChange: TypeAlias = DocumentCommitChange | TableCommitChange


class CommitPublication(TypedDict, total=False):
    """一次已提交的发布：序号与它改变了什么（``types.ts:781-789``）。"""

    seq: Seq
    tables: list[Any]
    documents: list[DocumentCommitChange]
    records: list[Any]


# --------------------------------------------------------------------------------------
# Storage 契约（types.ts:1059-1139）
# --------------------------------------------------------------------------------------


class Storage(Protocol):
    """``Session`` 记录的原子持久化边界（``types.ts:1059-1139``）。

    它**信任**所属 Session 提供语义有效的记录、引用、血缘与状态迁移；自身负责原子性、
    全局 ID 归属、会话/条目的不可变创建、文档记录一致性，以及返回值解除关联。
    Session 负责把提交串行化。

    一个方法抛出的错误是**终局**的：它会让 Session 失败，之后什么都不重试，所以瞬时故障要在
    方法内部重试。读操作有两种错误只让那一次读失败：``StorageRequestError``（请求无效，
    必须无持久副作用）与「调用方上下文被中止」。``commit()`` / ``mint_id()`` 的错误一律致命。
    """

    async def commit(self, writes: list[StorageWrite], context: Context) -> Seq: ...

    async def mint_id(self) -> Id: ...

    async def conversation(
        self, id: ConversationId, context: Context
    ) -> ConversationRecord | None: ...

    async def scan_conversations(
        self,
        query: ConversationQuery,
        limit: int,
        cursor: Cursor | None,
        context: Context,
    ) -> Page: ...

    async def entry(self, id: EntryId, context: Context) -> dict[str, Any] | None: ...

    async def entry_in_conversation(
        self, conversation_id: ConversationId, id: EntryId, context: Context
    ) -> dict[str, Any] | None: ...

    async def find_latest_head_marker(
        self,
        conversation_id: ConversationId,
        at_or_before_entry_id: EntryId | None,
        context: Context,
    ) -> EntryRecord | None: ...

    async def scan_entries(
        self,
        query: EntryQuery,
        limit: int,
        cursor: Cursor | None,
        context: Context,
    ) -> Page: ...

    async def task(self, id: TaskId, context: Context) -> TaskRecord | None: ...

    async def scan_tasks(
        self,
        query: TaskQuery,
        limit: int,
        cursor: Cursor | None,
        context: Context,
    ) -> Page: ...

    async def submission(self, id: SubmissionId, context: Context) -> SubmissionRecord | None: ...

    async def scan_submissions(
        self,
        query: SubmissionQuery,
        limit: int,
        cursor: Cursor | None,
        context: Context,
    ) -> Page: ...

    async def submission_by_request(
        self, conversation_id: ConversationId, request_id: str, context: Context
    ) -> SubmissionRecord | None: ...

    async def find_document(
        self, address: DocumentAddress, at: DocumentPoint, context: Context
    ) -> DocumentRecord | None: ...

    async def document(
        self, id: DocumentId, at: DocumentPoint, context: Context
    ) -> StoredDocument | None: ...

    async def scan_documents(
        self,
        query: DocumentQuery,
        limit: int,
        cursor: Cursor | None,
        context: Context,
    ) -> Page: ...

    async def close(self, context: Context) -> None: ...


class TaskDefinition(Protocol):
    """任务定义的**窄提取**（``types.ts:243-270`` 的 ``TaskDefinition``）。

    完整的定义还带 ``phases`` / ``abort`` / ``migrate`` / ``hooks``——那些属于**批次 D**
    （scheduler 运行时）。事务侧只需要这三样：把它写进 ``TaskRecord.kind`` / ``version``，
    以及取第一个持久化检查点。
    """

    @property
    def name(self) -> str:
        """注册的任务种类，持久化进 ``TaskRecord.kind``。"""
        ...

    @property
    def version(self) -> int:
        """随活输入与检查点一起持久化的定义版本。"""
        ...

    def initial(self, input: JsonValue) -> dict[str, Any]:
        """新建任务的第一个持久化检查点。"""
        ...


class Task(Protocol):
    """一个类型化的可执行任务定义（``types.ts:272-274``）。"""

    @property
    def definition(self) -> TaskDefinition: ...


# --------------------------------------------------------------------------------------
# 文档定义层（documents.ts 的契约部分）
# --------------------------------------------------------------------------------------


class LatestConversationSemantics(TypedDict):
    """只保留当前状态的会话文档（``types.ts:41-45``）。"""

    scope: Literal["conversation"]
    history: Literal["latest"]
    fork: Literal["current", "initial"]


class RewindableConversationSemantics(TypedDict):
    """历史仍可被 as-of 读取寻址的会话文档（``types.ts:48-52``）。"""

    scope: Literal["conversation"]
    history: Literal["rewindable"]
    fork: Literal["asOf", "current", "initial"]


class SessionScopeSemantics(TypedDict):
    """会话级（Session-scoped）语义：不声明 history / fork（``types.ts:57``）。"""

    scope: Literal["session"]


class TaskScopeSemantics(TypedDict):
    """任务级语义：不声明 history / fork（``types.ts:60``）。"""

    scope: Literal["task"]


class CheckpointInfo(TypedDict):
    """交给文档 checkpoint 判定的已存重放状态（``types.ts:65-69``）。"""

    deltasSinceBase: int


class CommonDocDefinition(TypedDict, total=False):
    """单例文档与文档族共有的定义字段（``types.ts:72-…``）。

    ``kind`` / ``version`` 是必填；``initial`` / ``migrate`` / ``checkpointWhen`` 由
    :func:`pi_durable.documents.define_doc` 在**运行期**校验存在性，因此这里用 ``total=False``
    配合 ``Required``。
    """

    kind: Required[str]
    version: Required[int]
    initial: NotRequired[Callable[[JsonValue], JsonObject]]
    migrate: NotRequired[Callable[[JsonObject, int], JsonObject]]
    checkpointWhen: NotRequired[
        Callable[[Mapping[str, JsonValue], Sequence[Any], CheckpointInfo], bool]
    ]


#: 定义对象在 Session 里的**擦除后形状**（``documents.ts:88-96`` 的 ``AnyDocDefinition``）。
#: 运行期就是普通 dict；这里给出静态形状，便于 :mod:`pi_durable.documents` 的助手签名。
DocDefinition: TypeAlias = dict[str, Any]


class ResolvedAddress(NamedTuple):
    """逻辑地址 + 它在 map 里的字符串身份 + 下一个参数下标（``documents.ts:101-106``）。"""

    address: DocumentAddress
    id: str
    next_argument: int


class WatchEnd(TypedDict, total=False):
    """一次文档观察的终态（``types.ts`` 的 ``WatchEnd``）。

    ``reason`` 取 ``stopped`` / ``cancelled`` / ``session_closed`` / ``retired`` 之一时没有
    ``error``；``listener_error`` 与 ``session_failed`` 带 ``error``。
    """

    reason: Required[str]
    error: NotRequired[Any]


class WatchHandle(Protocol):
    """对一份不可变值的精确帧观察，带界待投递（``types.ts`` 的 ``WatchHandle``）。"""

    @property
    def value(self) -> Any: ...

    @property
    def closed(self) -> Any: ...

    def start(self, listener: Any) -> None: ...

    async def stop(self) -> WatchEnd: ...


class SessionEnd(TypedDict, total=False):
    """一个 Session 为何结束（``types.ts:947`` 的 ``SessionEnd``）。

    ``reason`` 是 ``"closed"``（主动关闭）或 ``"failed"``（存储失败，带 ``error``）。
    """

    reason: Required[str]
    error: NotRequired[Any]


# --------------------------------------------------------------------------------------
# 批次 D：任务运行态与文档观察的契约（``types.ts:137-145, 357, 901-910``）
# --------------------------------------------------------------------------------------


class RunningTask(Protocol):
    """一条**正在跑**的任务记录（``types.ts:137-139``）。

    上游是 ``TaskRecord<I,S,R> & { state: Extract<TaskState<S,R>, {status:"running"}> }``
    —— 也就是把 ``TaskRecord`` 的 ``state`` **收窄**到 running。
    Python 没法在运行期表达那个交叉类型，所以这里是**契约上的收窄**：
    ``state["status"]`` 一定是 ``"running"``，由 :meth:`TaskRuntime.commit` 的调用点保证。
    """

    id: int
    kind: str
    conversation_id: int
    state: dict[str, Any]


#: 一个任务为**自己**提交的下一个状态：替换检查点、等待、或它的结果（``types.ts:142-145``）。
#:
#: 上游是 ``Extract<TaskState<S,R>, {status: "running" | "waiting" | "terminal"}>``。
#: ⚠️ **返回 ``terminal`` 时，只要该任务下面还有普通的、它拥有的活在跑，
#: 存储的就是 ``completing``**（spec §5.5）——所以要读回来看，不能假定存进去的就是终局。
NextTaskState = dict[str, Any]

#: 条目里 ``data`` 的类型为 ``D``；``Never`` 表示这个种类不带数据（``types.ts:356-358``）。
#:
#: ⚠️ 上游是 ``Omit<EntryRecord, "data"> & ({data: D} | {data?: never})``。
#: **Python 里类型参数被擦除**，所以本包给的是**别名 + 说明**，
#: 收窄由 :func:`~pi_durable.entries.as_typed` / ``EntryToken.is_()`` 在运行期承担。
TypedEntry = EntryRecord

#: 一个类型化种类的内容；``kind`` 由令牌提供（``types.ts:360-362``）。
TypedEntryDraft = EntryDraft

#: 一个已提交文档的非创建式观察，**在值不可变期间**（``types.ts:901-907``）。
#:
#: 上游是 ``WatchHandle<Readonly<T> | null>``：文档**不存在时值是 ``None``**
#: （这与「值是空对象」不同——那是存在但为空的文档）。
DocumentWatch = WatchHandle
