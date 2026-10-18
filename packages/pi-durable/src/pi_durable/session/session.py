"""Session 内核 —— 移植 ``durable/src/session/session.ts``（1,081 行）。

⚠️ **本模块分步交付，目前只到位阶段 3 的第 1–2 步**（见 ``PORTING.md``）：

* ✅ **第 1 步**：:class:`GuardedStorage` + **失败即终局**（:meth:`SessionImpl.fail`）
* ✅ **第 2 步**：**单写线** :meth:`SessionImpl.read_on_line` / ``_enqueue``
* ✅ **第 3 步**：:meth:`SessionImpl.commit` 与提交发布（``subscribe_commits`` / ``subscribe_close``）
* ✅ **第 4 步**：文档缓存逐出 + ``snapshot`` / ``document_state`` / ``watch_doc``

``snapshot_as_of`` **仍未做**（Session 接口里多出来的那一个方法），它不属于本轮的三个名字。

失败即终局（``session.ts:96-110`` 的契约）
------------------------------------------

**任何** ``Storage`` 方法抛错都让 Session 关闭自己：

1. 那次调用拿到**后端原本的错误**；
2. Session 记下第一个错误（**先到先得**，后续错误不再覆盖）；
3. :attr:`SessionImpl.failed` 立刻以 :class:`~pi_durable.errors.SessionFailed` 拒绝，
   所有 await 它的 waiter 都拿到**同一个** :class:`SessionFailed`；
4. Session 随即关闭自己（**包括关闭后端**），:attr:`SessionImpl.closed` 落定为
   ``{"reason": "failed", "error": ...}``；
5. 之后的每一次调用**不碰后端**，直接以 :class:`SessionFailed` 拒绝；
6. **不重试任何东西**——瞬时错误是 Storage 自己的事，重开时从已提交状态恢复。

**两个豁免**（``session.ts:1041-1046``）：一个 ``StorageRequestError``，或一个
**调用方上下文已被取消**的读取，都不说明 Storage 坏了，所以只让那一次调用失败。

单写线的 Python 等价设计（**含语义差异**）
--------------------------------------------

上游 ``#enqueue``（``session.ts:854-861``）是一条 promise 链::

    const run = this.#tail.then(job);
    this.#tail = run.then(() => undefined, () => undefined);
    return run;

本包用**串行尾链**（不是 ``asyncio.Lock``），理由是它保留上游的两条性质：

* **顺序**：每个作业等前一个落定才跑，严格按提交顺序。
* **失败隔离且不断链**：作业的失败只回给它的调用方；``_tail`` 永远是一个**会成功**的
  Future（上游 ``run.then(ok, err)`` 两边都吞）。
* **急切调度**：作业在 ``_enqueue`` 返回时就**已经被排上**，即使调用方不 await 它也会跑。
  这一点是 ``asyncio.Lock`` **做不到**的——用锁的话，调用方不 await 作业就永远不会开始。
  上游依赖急切调度（``close()`` 里 ``this.close(...).catch(() => {})`` 就是「发了不管」）。

**语义差异**（逐条）：

1. ``asyncio.ensure_future`` 需要一个**正在运行的事件循环**，所以
   :meth:`SessionImpl.commit` / :meth:`SessionImpl.read_on_line` **必须在协程里调用**。
   上游的 ``#enqueue`` 只做 ``.then``，同步上下文里也能调。这是真实差异，见 PORTING.md R15。
2. **不可重入**：作业内部再 ``_enqueue`` 会自锁（内层等外层、外层等内层）。
   上游的 promise 链**有同样的性质**——所以这不是移植引入的，是设计约束。
   上游的文档也明说：「回调里只通过 ``tx`` 读」（``types.ts:946-948``）。
3. **取消**：Python 的 Task 可被取消。这里把 ``CancelledError`` 也当作链的终止信号吞掉
   （``_settle`` 兜住 ``BaseException``），以保证**链不断**——与上游 ``.then(ok, err)`` 一致。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar, cast

from pi_chord.context import Context, without_abort_signal
from pi_chord.delta import track
from pi_chord.state import ReplicatedStateSourceOptions, replicated_state
from pi_durable.documents import (
    address_id,
    check_record_scope,
    check_record_version,
    materialize_document,
    resolve_address,
)
from pi_durable.errors import SessionFailed, StorageRequestError
from pi_durable.session.observation import (
    RETIREMENT_OPERATIONS,
    CommittedStateSource,
    CommittedWatch,
)
from pi_durable.session.transaction import (
    LoadedDocument,
    Transaction,
    TransactionHost,
    TransactionScope,
)
from pi_durable.types import (
    ConversationId,
    ConversationQuery,
    ConversationRecord,
    Cursor,
    DocumentAddress,
    DocumentId,
    DocumentPoint,
    DocumentQuery,
    DocumentRecord,
    EntryId,
    EntryQuery,
    EntryRecord,
    Page,
    Seq,
    SessionEnd,
    Storage,
    StorageWrite,
    StoredDocument,
    SubmissionId,
    SubmissionQuery,
    SubmissionRecord,
    TaskId,
    TaskQuery,
    TaskRecord,
)

__all__ = [
    "MAX_TIMER_DELAY",
    "FailureLatch",
    "GuardedStorage",
    "SessionImpl",
    "document_retention",
    "set_default_document_retention",
]

#: ``setTimeout`` / 定时器支持的最长延迟；更长的保留期要分几步等（``session.ts:94``）。
MAX_TIMER_DELAY = 2_147_483_647

#: 默认文档保留期：十分钟，与「被保留的上下文」一致（``session.ts:78``）。
_default_retention = 600_000.0


def set_default_document_retention(ms: float) -> None:
    """内部，**给测试用**：把「所有使用默认保留期的 Session」的保留期一次改掉，
    好让一套用例立刻触发逐出（``session.ts:80-83``）。

    ⚠️ 这是**进程级**可变状态（与上游的模块级 ``let defaultRetention`` 一致），
    所以用它做测试隔离时要还原。本包自己的测试都走**每实例**的
    ``SessionImpl(storage, document_retention_ms=...)``，不用这个全局开关。
    """
    global _default_retention
    _default_retention = ms


def _default_clock() -> float:
    """默认墙钟：毫秒（与 JS ``Date.now()`` 同一单位，保留期就是毫秒）。"""
    import time

    return time.time() * 1000.0


def _safe_retention(setting: float | None) -> float:
    """缓存记账**绝不能改变**它所跟随的那次操作的结果：保留期设置抛错就用默认值
    （``session.ts:169-175``）。"""
    try:
        return document_retention(setting)
    except Exception:
        return document_retention(None)


def document_retention(ms: float | None) -> float:
    """给定保留期的合法化（``session.ts:85-91``）。

    是**非负数字**就用它（``0`` 表示「提交一结束就逐出」，``math.inf`` 表示永不舍弃）；
    否则用默认值。
    """
    # 上游是 `typeof ms === "number" && ms >= 0`——**先判类型再比较**。
    # 顺序反了的话，一个字符串会在 `>=` 上抛 TypeError，而它本该只是「用默认值」。
    if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms >= 0:
        return float(ms)
    return _default_retention


_T = TypeVar("_T")

#: 读取调用可被豁免的两类错误：``StorageRequestError``，或调用方上下文已被取消。
_EXEMPT_FROM_FAILURE = (StorageRequestError,)


class FailureLatch(Protocol):
    """Session 的失败闩（``session.ts:907-910`` 的 ``FailureLatch``）。"""

    @property
    def failure(self) -> dict[str, Any] | None:
        """第一个失败，如果有过。"""
        ...

    def fail(self, error: BaseException) -> None:
        """以 ``error`` 让 Session 失败；第一个生效。"""
        ...


class GuardedStorage:
    """Session 的 Storage 外层的**失败守卫**（``session.ts:919-…``）。

    守卫之前先看闩：已失败的 Session 拿到 :class:`SessionFailed`，**根本不碰后端**。
    抛错的调用让 Session 失败，并把后端原本的错误**原样抛回**——除非它是
    ``StorageRequestError`` 或一个调用方上下文已被取消的读取：两者都不说明 Storage 坏了。

    ``close()`` **总是**到达后端，所以失败的 Session 依然能把它关掉。
    """

    def __init__(self, storage: Storage, latch: FailureLatch) -> None:
        self._storage = storage
        self._latch = latch
        #: 正在进行的后端调用，**含不在写线上的那些**；close 会在关后端之前等它们。
        self._underway: set[asyncio.Future[Any]] = set()
        self._closing = False

    def begin_closing(self) -> None:
        """让守卫开始拒绝新调用（``session.ts:1041`` 的 ``#closing``）。"""
        self._closing = True

    @property
    def underway(self) -> set[asyncio.Future[Any]]:
        """仍在进行的后端调用；``close`` 用它们决定何时关后端。"""
        return self._underway

    # ── Storage 的 16 个方法，全部过守卫 ────────────────────────────────────────

    async def commit(self, writes: list[StorageWrite], context: Context) -> Seq:
        """提交整批写入。**永不豁免**：一旦准入，失败批次有没有落盘是未知的。"""
        return await self._call(None, lambda: self._storage.commit(writes, context))

    async def mint_id(self) -> int:
        """分配一个新记录 ID。**永不豁免**。"""
        return await self._call(None, lambda: self._storage.mint_id())

    async def conversation(self, id: ConversationId, context: Context) -> ConversationRecord | None:
        return await self._call(context, lambda: self._storage.conversation(id, context))

    async def scan_conversations(
        self, query: ConversationQuery, limit: int, cursor: Cursor | None, context: Context
    ) -> Page:
        return await self._call(
            context, lambda: self._storage.scan_conversations(query, limit, cursor, context)
        )

    async def entry(self, id: EntryId, context: Context) -> dict[str, Any] | None:
        return await self._call(context, lambda: self._storage.entry(id, context))

    async def entry_in_conversation(
        self, conversation_id: ConversationId, id: EntryId, context: Context
    ) -> dict[str, Any] | None:
        return await self._call(
            context, lambda: self._storage.entry_in_conversation(conversation_id, id, context)
        )

    async def find_latest_head_marker(
        self, conversation_id: ConversationId, at_or_before: EntryId | None, context: Context
    ) -> EntryRecord | None:
        return await self._call(
            context,
            lambda: self._storage.find_latest_head_marker(conversation_id, at_or_before, context),
        )

    async def scan_entries(
        self, query: EntryQuery, limit: int, cursor: Cursor | None, context: Context
    ) -> Page:
        return await self._call(
            context, lambda: self._storage.scan_entries(query, limit, cursor, context)
        )

    async def task(self, id: TaskId, context: Context) -> TaskRecord | None:
        return await self._call(context, lambda: self._storage.task(id, context))

    async def scan_tasks(
        self, query: TaskQuery, limit: int, cursor: Cursor | None, context: Context
    ) -> Page:
        return await self._call(
            context, lambda: self._storage.scan_tasks(query, limit, cursor, context)
        )

    async def submission(self, id: SubmissionId, context: Context) -> SubmissionRecord | None:
        return await self._call(context, lambda: self._storage.submission(id, context))

    async def scan_submissions(
        self, query: SubmissionQuery, limit: int, cursor: Cursor | None, context: Context
    ) -> Page:
        return await self._call(
            context, lambda: self._storage.scan_submissions(query, limit, cursor, context)
        )

    async def submission_by_request(
        self, conversation_id: ConversationId, request_id: str, context: Context
    ) -> SubmissionRecord | None:
        return await self._call(
            context,
            lambda: self._storage.submission_by_request(conversation_id, request_id, context),
        )

    async def find_document(
        self, address: DocumentAddress, at: DocumentPoint, context: Context
    ) -> DocumentRecord | None:
        return await self._call(context, lambda: self._storage.find_document(address, at, context))

    async def document(
        self, id: DocumentId, at: DocumentPoint, context: Context
    ) -> StoredDocument | None:
        return await self._call(context, lambda: self._storage.document(id, at, context))

    async def scan_documents(
        self, query: DocumentQuery, limit: int, cursor: Cursor | None, context: Context
    ) -> Page:
        return await self._call(
            context, lambda: self._storage.scan_documents(query, limit, cursor, context)
        )

    async def close(self, context: Context) -> None:
        """关掉后端。**总是**到达后端（``session.ts:917``）。"""
        await self._storage.close(context)

    # ── 守卫本体 ──────────────────────────────────────────────────────────────

    async def _call(self, read: Context | None, run: Callable[[], Awaitable[_T]]) -> _T:
        """一次受守卫的后端调用（``session.ts:1037-1058``）。

        ``read`` 为 ``None`` 表示「写」或「分配 ID」——**永不豁免**。
        """
        self._assert_healthy()
        if self._closing:
            raise RuntimeError("Session is closed")

        # 包成 Task：这样**同步**抛错的后端也被守卫罩住。
        call = asyncio.ensure_future(_invoke(run))
        self._underway.add(call)
        try:
            result = await call
        except BaseException as error:
            self._assert_healthy()
            # 一个无效读取、或调用方已取消的读取，不说明 Storage 坏了：只让那一次调用失败。
            exempt = read is not None and (
                isinstance(error, _EXEMPT_FROM_FAILURE)
                or read.abort_signal is not None
                and read.abort_signal.aborted
            )
            if not exempt:
                self._latch.fail(error)
            raise
        finally:
            self._underway.discard(call)
        self._assert_healthy()
        return result

    def _assert_healthy(self) -> None:
        failure = self._latch.failure
        if failure is not None:
            raise SessionFailed(failure["error"])


async def _invoke(run: Callable[[], Awaitable[_T]]) -> _T:
    """异步包装，好让同步抛错的后端也走同一条路径（``session.ts:1043``）。"""
    return await run()


class SessionImpl:
    """Session 内核（``session.ts:112-…``）。

    ⚠️ 本类目前只实现了**失败守卫**与**单写线**；``commit`` 与提交发布属于第 3 步。
    """

    def __init__(
        self,
        storage: Storage,
        clock: Callable[[], float] | None = None,
        document_retention_ms: float | None = None,
    ) -> None:
        self._storage = GuardedStorage(storage, self)
        self._failure: dict[str, Any] | None = None
        #: R3：不在构造器里建 Future——Python 的 Future 必须绑定运行循环。
        self._failed: asyncio.Future[Any] | None = None
        self._closed: asyncio.Future[SessionEnd] | None = None
        self._closing: asyncio.Future[None] | None = None
        #: 单写线的尾部；**永远是一个会成功的 Future**。
        self._tail: asyncio.Future[Any] | None = None
        #: 提交监听器（宿主侧）。加同步跑，抛出只被报告，**不让提交失败**。
        self._commit_listeners: list[Callable[[dict[str, Any], Context], None]] = []
        #: Session **自己组件**的提交监听器。它们的抛出会让 Session 失败——它们的状态跟着每次提交走，
        #: 抛出就意味着内存落在存储后面。它们**先于**宿主监听器跑。
        self._internal_listeners: list[Callable[[dict[str, Any], Context], None]] = []
        #: 关闭监听器：关闭一开始（含失败导致的关闭）就**同步**跑。
        self._close_listeners: list[Callable[[], None]] = []
        #: 地址 → 已加载化身。
        self._documents: dict[str, Any] = {}
        #: 没有观察者附着的缓存文档，按地址，**最久未用在前**，值是那次使用的时间。
        self._idle_documents: dict[str, float] = {}
        #: 每个文档化身（记录 ID）上的观察者数；有观察者的缓存化身**永不被逐出**。
        self._attached_documents: dict[int, int] = {}
        #: 墙钟与保留期。缓存记账**绝不能改变**它所跟随的那次操作的结果——
        #: 时钟或保留期设置抛错时当作「就是现在」和「用默认值」（``session.ts:160-175``）。
        self._clock: Callable[[], float] = clock if clock is not None else _default_clock
        self._document_retention_ms: Callable[[], float] = lambda: _safe_retention(
            document_retention_ms
        )
        #: 待办逐出：最旧空闲文档的到期时刻 + 它的定时器句柄。
        self._document_sweep: dict[str, Any] | None = None
        self._sweep_jobs = 0
        #: 一旦「定时器不可用」就置位：此后不保留定时器，改为每次提交结束时逐出过期项。
        self._document_timers_unavailable = False
        #: 事务宿主。它拿的是**受守卫的** storage，所以事务里每一次读写都过守卫。
        self._host = TransactionHost(
            self._storage,
            self._clock,
            load=self._load_document,
            cached=self._cached_document,
            install=self._install_document,
            evict=self._evict_document,
        )

    # ── 可观察状态 ────────────────────────────────────────────────────────────

    @property
    def failure(self) -> dict[str, Any] | None:
        """让 Session 失败的那个错误，如果有过（``session.ts:143-145``）。"""
        return self._failure

    @property
    def failed(self) -> asyncio.Future[Any]:
        """Session 一失败就拒绝的 Future；**永不 resolve**（``session.ts:148-150``）。

        所有等待中的 waiter 都 await 它，于是拿到**同一个** :class:`SessionFailed`。
        """
        if self._failed is None:
            self._failed = asyncio.get_running_loop().create_future()
            if self._failure is not None:
                self._failed.set_exception(SessionFailed(self._failure["error"]))
                self._failed.exception()  # 标记为已取，避免「never retrieved」警告
        return self._failed

    @property
    def closed(self) -> asyncio.Future[SessionEnd]:
        """Session 关闭（含 Storage）后落定（``session.ts:138-140``）。"""
        if self._closed is None:
            self._closed = asyncio.get_running_loop().create_future()
            if self._closing is not None and self._closing.done():
                self._closed.set_result(self._end())
        return self._closed

    def _end(self) -> SessionEnd:
        if self._failure is None:
            return {"reason": "closed"}
        return {"reason": "failed", "error": self._failure["error"]}

    # ── 失败即终局（第 1 步） ────────────────────────────────────────────────

    def fail(self, error: BaseException) -> None:
        """以 ``error`` 让 Session 失败——**第一个生效**，且只报告一次（``session.ts:210-218``）。

        准入就此结束，关闭监听器**立刻**跑；它们从 :attr:`failure` 读错误。
        守卫调它；Harness 也会为它自己调度器提交里的抛错调它。
        """
        if self._failure is not None:
            return
        self._failure = {"error": error}
        if self._failed is not None and not self._failed.done():
            self._failed.set_exception(SessionFailed(error))
            self._failed.exception()
        # 发了不管：关不掉的失败属于调用方要看的东西，这里没人等。
        _fire_and_forget(self.close(without_abort_signal(_background_context())))

    def assert_usable(self) -> None:
        """拒绝「已失败」或「正在关闭」的 Session（``session.ts:864-867``）。"""
        self._assert_healthy()
        if self._closing is not None:
            raise RuntimeError("Session is closed")

    def _assert_healthy(self) -> None:
        if self._failure is not None:
            raise SessionFailed(self._failure["error"])

    # ── 第 4 步：三个只读观察者 ──────────────────────────────────────────────

    async def snapshot(self, token: Any, *args: Any) -> Any:
        """读一个已提交文档的值；**不存在返回 ``None``**（``session.ts:293-311``）。

        ⚠️ **命中缓存时不走写线**——上游就是对 ``cached?.valueVersion === definition.version``
        直接取用（``session.ts:298-306``）。这是刻意的：``snapshot`` 不改变任何状态，
        没必要为它排队。

        上游这个方法声明成 ``async``，所以 Python 侧也用 ``async def``——
        同步抛错自然变成拒绝（与 R14 的情形不同，见那条通则）。
        """
        self.assert_usable()
        definition = token.definition
        resolved = resolve_address(definition, args)
        context = cast(Context, args[resolved.next_argument])
        cached = self._documents.get(resolved.id)
        if cached is not None and cached.value_version == definition.get("version"):
            self._touch_document(resolved.id)

        if cached is not None and cached.value_version == definition.get("version"):
            loaded = cached
        else:

            async def load() -> Any:
                self._assert_healthy()
                return await self._load_document(definition, resolved.id, resolved.address, context)

            loaded = await self._enqueue(load)
        if loaded is None:
            return None
        check_record_scope(definition, loaded.record)
        check_record_version(definition, loaded.record, loaded.stored_version)
        return loaded.tracker.value

    def document_state(self, token: Any, *args: Any) -> asyncio.Future[Any]:
        """把一个**可释放的只读 chord 状态**绑到一个已提交的文档化身上
        （``session.ts:331-364``）。

        ⚠️ 上游这个方法**不是** ``async``——它直接返回 ``#enqueue(...)``，
        同步错误转成被拒绝的 Promise（**R14**）。
        """
        try:
            self.assert_usable()
            definition = token.definition
            resolved = resolve_address(definition, args)
            context = cast(Context, args[resolved.next_argument])

            async def run() -> Any:
                self._assert_healthy()
                loaded = await self._load_document(
                    definition, resolved.id, resolved.address, context
                )
                if loaded is None:
                    return None
                source, detach = self._attach_document(
                    definition,
                    loaded,
                    lambda value, release: CommittedStateSource(value, release),
                )
                try:
                    return replicated_state(
                        source, ReplicatedStateSourceOptions(on_error=self.report)
                    )
                except BaseException:
                    detach()
                    raise

            return self._enqueue(run)
        except Exception as error:
            return _failed_future(error)

    async def watch_doc(self, token: Any, *args: Any) -> Any:
        """取得一个文档化身的观察；**不存在返回 ``None``**（``session.ts:395-448``）。

        调用方上下文的取消信号会转给观察，所以取消上下文就取消这次观察。
        """
        self.assert_usable()
        definition = token.definition
        resolved = resolve_address(definition, args)
        context = cast(Context, args[resolved.next_argument])
        signal = context.abort_signal
        cancelled = signal is not None and signal.aborted

        def mark_cancelled() -> None:
            nonlocal cancelled
            cancelled = True

        if signal is not None:
            signal.add_listener(mark_cancelled)
        try:

            async def run() -> Any:
                self._assert_healthy()
                if cancelled:
                    _throw_cancellation(signal)
                loaded = await self._load_document(
                    definition, resolved.id, resolved.address, context
                )
                if cancelled:
                    _throw_cancellation(signal)
                if loaded is None:
                    return None
                watch, _detach = self._attach_document(
                    definition,
                    loaded,
                    lambda value, release: CommittedWatch(value, release, self.report),
                )
                return watch

            watch = await self._enqueue(run)
            if watch is None:
                return None
            if cancelled:
                watch.cancel()
                _throw_cancellation(signal)
            if signal is not None:
                watch.observe_cancellation(signal)
            return watch
        finally:
            if signal is not None:
                signal.remove_listener(mark_cancelled)

    def _attach_document(
        self,
        definition: Any,
        loaded: LoadedDocument,
        create: Callable[[Any, Callable[[], None]], Any],
    ) -> tuple[Any, Callable[[], None]]:
        """把一个观察者附到一个已提交的化身上：先校验定义，再转发这个化身的提交变更与关闭
        （``session.ts:780-817``）。

        ``detach`` 同时撤掉两个订阅。
        """
        check_record_scope(definition, loaded.record)
        check_record_version(definition, loaded.record, loaded.stored_version)
        slots: dict[str, Callable[[], None]] = {"commit": lambda: None, "close": lambda: None}
        # 附着之后这个化身留在缓存里；最后一个观察者解开、或它退休之后，才重新变空闲。
        unpin = self._pin_document(loaded.address_id, loaded.record["id"])

        def detach() -> None:
            slots["commit"]()
            slots["close"]()
            unpin()

        try:
            observer = create(loaded.tracker.value, detach)
        except BaseException:
            unpin()
            raise

        observed = {"version": loaded.value_version}
        record_id = loaded.record["id"]

        def on_commit(publication: dict[str, Any], context: Context) -> None:
            for change in publication["changes"]:
                if change["type"] != "document" or change["record"]["id"] != record_id:
                    continue
                # 文档状态的帧不带调用方取消；观察者自己管自己的取消。
                frame_context = (
                    without_abort_signal(context)
                    if isinstance(observer, CommittedStateSource)
                    else context
                )
                ops = _observed_operations(observed, change)
                # 「只为迁移而写的 base」对新版本的观察者什么都没改。
                if not ops:
                    continue
                # 退休的化身不会再变：它的观察者保留最后的值，但不再钉住任何东西。
                if change["value"] is None:
                    unpin()
                observer.advance(change["value"], list(ops), frame_context)

        slots["commit"] = self.observe_commits(on_commit)
        slots["close"] = self.subscribe_close(lambda: observer.close_session(self._failure))
        return observer, detach

    # ── 提交（第 3 步） ─────────────────────────────────────────────────────

    def commit(self, change: Callable[[Any], Any], context: Context) -> asyncio.Future[Any]:
        """在 Session 的写线上跑一次原子事务（``session.ts:201-203``）。

        ⚠️ 回调里**只通过 ``tx`` 读**：``snapshot()`` / ``document_state()`` / ``watch_doc()`` /
        ``get_task()`` 会排到回调正持有的那条线上，然后永不 resolve。
        """
        return self.commit_with(change, context)

    def commit_with(
        self,
        change: Callable[[Any], Any],
        context: Context,
        scope: TransactionScope | None = None,
    ) -> asyncio.Future[Any]:
        """内部提交，暴露具体事务（``session.ts:225-232``）。"""
        try:
            self.assert_usable()
        except Exception as error:
            return _failed_future(error)
        return self._enqueue(lambda: self._run_commit(change, context, scope))

    async def _run_commit(
        self,
        change: Callable[[Any], Any],
        context: Context,
        scope: TransactionScope | None,
    ) -> Any:
        """一次提交的完整链路（``session.ts:690-742``）。"""
        self._assert_healthy()
        if context.abort_signal is not None:
            context.abort_signal.throw_if_aborted()
        tx = Transaction(self._host, context, scope)
        result: Any
        try:
            result = await change(tx)
            # 回调**吞掉了**一个失败的读取时，它既不能提交，也不能装作成功。
            self._assert_healthy()
        except BaseException:
            await tx.settle_failure()
            self._sweep_documents()
            raise

        try:
            writes = await tx.settle_success()
        except BaseException:
            # 结算已经中止了每个变更。
            self._sweep_documents()
            raise

        if len(writes) == 0:
            tx.discard()
            self._sweep_documents()
            return result

        try:
            # 一旦准入，调用方的取消**不再打断**存储结算。
            seq = await self._storage.commit(writes, without_abort_signal(context))
        except BaseException:
            # 守卫已经让 Session 失败了。
            tx.discard()
            raise

        try:
            documents = tx.adopt(seq)
        except BaseException as error:
            # 存储已经提交了；adoption 失败意味着**内存落在持久状态后面**——那是一个失败的 Session。
            self.fail(error)
            raise

        for document in documents:
            self._touch_document(_address_of(document["record"]))
        self._publish(seq, writes, documents, context)
        self._sweep_documents()
        return result

    def _publish(
        self,
        seq: int,
        writes: list[Any],
        documents: list[dict[str, Any]],
        context: Context,
    ) -> None:
        """把一次提交发布给监听器（``session.ts:744-776``）。"""
        if not self._commit_listeners and not self._internal_listeners:
            return
        changes: list[dict[str, Any]] = [
            write
            for write in writes
            if write["type"] in ("conversation", "entry", "task", "submission")
        ]
        changes.extend(documents)
        publication = {"seq": seq, "changes": changes}
        # 提交已经持久化了：监听器失败**既不让提交失败，也不跳过其它监听器**。
        for internal in list(self._internal_listeners):
            # 内部监听器抛出意味着它的状态落在存储后面 → 让 Session 失败。
            contained(_listener_call(internal, publication, context), self.fail)
        for host_listener in list(self._commit_listeners):
            contained(_listener_call(host_listener, publication, context), self.report)

    def subscribe_commits(
        self, listener: Callable[[dict[str, Any], Context], None]
    ) -> Callable[[], None]:
        """在**采纳之后**同步观察每一次完整提交（``session.ts:544-550``）。

        监听器**不得阻塞、也不得调用 Session API**；抛出只被报告。
        """
        self.assert_usable()
        self._commit_listeners.append(listener)
        return lambda: (
            self._commit_listeners.remove(listener) if listener in self._commit_listeners else None
        )

    def observe_commits(
        self, listener: Callable[[dict[str, Any], Context], None]
    ) -> Callable[[], None]:
        """内部：给 Session **自己的组件**用的 ``subscribe_commits``（``session.ts:552-558``）。

        它们的状态跟着每次提交走，所以抛出会**让 Session 失败**，而不是只被报告。它们先于宿主监听器跑。
        """
        self.assert_usable()
        self._internal_listeners.append(listener)
        return lambda: (
            self._internal_listeners.remove(listener)
            if listener in self._internal_listeners
            else None
        )

    def subscribe_close(self, listener: Callable[[], None]) -> Callable[[], None]:
        """注册一个在**关闭开始时**同步调用的监听器（``session.ts:560-566``）。

        失败导致的关闭也会调用它（此时 :attr:`failure` 已设）。抛出只被报告。
        """
        self.assert_usable()
        self._close_listeners.append(listener)
        return lambda: (
            self._close_listeners.remove(listener) if listener in self._close_listeners else None
        )

    def report(self, error: BaseException) -> None:
        """内部：报告一个监听器错误。**普通 Session 丢掉它**（``session.ts:529``）。

        Harness 会覆盖它，把错误接到自己的日志上。
        """
        del error
        return None

    def before_close(self) -> None:
        """子类钩子：在封口之后、清缓存之前跑（``session.ts:540-542``）。"""
        return None

    # ── 第 4 步：文档缓存逐出 ────────────────────────────────────────────────

    def cache_stats(self) -> dict[str, int]:
        """缓存记账的快照，给测试与诊断用（``session.ts:580-595``）。"""
        return {
            "cached": len(self._documents),
            "idle": len(self._idle_documents),
            "attached": len(self._attached_documents),
            "sweeps": self._sweep_jobs,
        }

    def _touch_document(self, address_id: str) -> None:
        """记一次缓存文档的使用：**没有附着**的那个成为最新的空闲项，并排一次逐出
        （``session.ts:598-612``）。

        「热」文档每个**粒度**最多往后挪一次（粒度 = 保留期的十分之一，上限 1 秒）：
        它可能因此早走最多一个粒度，代价是一次重新加载，换来的是提交密集的负载不必反复重排。
        """
        cached = self._documents.get(address_id)
        if cached is None or cached.record["id"] in self._attached_documents:
            return
        now = self._clock()
        since = self._idle_documents.get(address_id)
        if since is not None and now - since < min(1_000, self._document_retention_ms() / 10):
            return
        self._idle_documents.pop(address_id, None)
        self._idle_documents[address_id] = now
        # 最旧文档的待办逐出优先，除非此后保留期被调短了——那就把它提前。
        sweep = self._document_sweep
        if sweep is None or sweep["at"] > now + self._document_retention_ms():
            self._schedule_document_sweep()

    def _drop_document(self, address_id: str) -> None:
        self._documents.pop(address_id, None)
        self._idle_documents.pop(address_id, None)

    def _schedule_document_sweep(self) -> None:
        """排一次「最旧空闲文档」的逐出（``session.ts:614-658``）。

        保留期为 0 时立刻排到写线上；否则在到期时由一个**不会吊住进程**的定时器触发。

        ⚠️ **上游的降级路径与 Python 的对应**
        --------------------------------------------------
        上游：``setTimeout`` 在 Node 里会**吊住进程**，所以要先 ``unref()``；
        「定时器无法 unref 时（如 Cloudflare Workers）就不保留定时器，改为每次提交结束时逐出过期项」。

        Python 的 ``asyncio.TimerHandle`` **没有** ``unref``——但也不需要：
        ``asyncio.run()`` 在主协程结束后就关循环，挂起的定时器**不会**吊住进程。
        所以那条降级在这里**没有对应的触发条件**。

        本包保留同一个形状，但把判据换成 Python 侧真实存在的那一个：
        **当前没有正在运行的事件循环**（``RuntimeError``）→ 定时器排不上 → 走降级
        （不保留定时器，改由每次提交结束时的 ``_sweep_documents`` 逐出）。
        **后果**：在没有事件循环的地方建 Session 时，过期文档会在**下一次提交结束**才被逐出，
        而不是按时逐出。状态始终正确，只是内存回收晚一步。
        """
        if self._closing is not None:
            return
        if not self._idle_documents:
            return
        oldest_address, oldest_since = next(iter(self._idle_documents.items()))
        at = oldest_since + self._document_retention_ms()
        sweep = self._document_sweep
        if sweep is not None and sweep["at"] <= at:
            return
        if sweep is not None and sweep.get("timer") is not None:
            sweep["timer"].cancel()

        # 待办逐出在它的作业跑之前**保持设置**，所以写线忙的时候再多的 touch 也不会排更多。
        holder: dict[str, Any] = {"at": at, "timer": None, "address": oldest_address}

        def run() -> None:
            self._sweep_jobs += 1

            async def job() -> None:
                if self._document_sweep is holder:
                    self._document_sweep = None
                if self._closing is None:
                    self._sweep_documents()

            self._enqueue(job)

        delay = at - self._clock()
        self._document_sweep = holder
        if self._document_timers_unavailable:
            self._document_sweep = None
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Python 侧的「定时器不可用」：没有运行中的事件循环。
            # ⚠️ 两条路都要过这里——「立刻排」用 ``call_soon`` 同样需要循环。
            self._document_timers_unavailable = True
            self._document_sweep = None
            return
        if delay <= 0:
            # 立刻：放到 ``call_soon`` 而不是内联，好让调用点的行为可预期。
            loop.call_soon(run)
            return
        holder["timer"] = loop.call_later(min(delay, MAX_TIMER_DELAY), run)

    def _sweep_documents(self) -> None:
        """在写线上：按「最久在前」逐出保留期已过的空闲文档，然后排下一次（``session.ts:663-670``）。"""
        expired = self._clock() - self._document_retention_ms()
        for cached_address, since in list(self._idle_documents.items()):
            if since > expired:
                break
            self._drop_document(cached_address)
        self._schedule_document_sweep()

    def _pin_document(self, address_id: str, record_id: int) -> Callable[[], None]:
        """把一个缓存化身钉在一个观察者上；返回的释放函数**只解一次**，最后一个解开后重新变空闲
        （``session.ts:673-690``）。"""
        self._attached_documents[record_id] = self._attached_documents.get(record_id, 0) + 1
        self._idle_documents.pop(address_id, None)
        pinned = True

        def unpin() -> None:
            nonlocal pinned
            if not pinned:
                return
            pinned = False
            count = self._attached_documents.get(record_id, 1) - 1
            if count > 0:
                self._attached_documents[record_id] = count
                return
            self._attached_documents.pop(record_id, None)
            cached = self._documents.get(address_id)
            if cached is not None and cached.record["id"] == record_id:
                self._touch_document(address_id)

        return unpin

    # ── 第 4 步：文档缓存与观察者 ────────────────────────────────────────────

    def _cached_document(self, address_id: str) -> Any:
        cached = self._documents.get(address_id)
        if cached is not None:
            self._touch_document(address_id)
        return cached

    async def _load_document(
        self, definition: Any, address_id: str, address: Any, context: Context
    ) -> Any:
        """冷加载一个文档化身（``session.ts:823-852``）。"""
        cached = self._documents.get(address_id)
        # 一个 tracker 只服务**它那份值被物化时的版本**的令牌；别的版本要重新从存储加载。
        if cached is not None and cached.value_version == definition.get("version"):
            self._touch_document(address_id)
            return cached
        if cached is not None:
            self._drop_document(address_id)
        record = await self._storage.find_document(address, "current", context)
        if record is None:
            return None
        stored = await self._storage.document(record["id"], "current", context)
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
        self._touch_document(address_id)
        return loaded

    def _install_document(self, document: LoadedDocument) -> None:
        self._documents[document.address_id] = document
        self._touch_document(document.address_id)

    def _evict_document(self, address_id: str, record_id: int) -> None:
        cached = self._documents.get(address_id)
        if cached is not None and cached.record["id"] == record_id:
            self._drop_document(address_id)

    # ── 单写线（第 2 步） ───────────────────────────────────────────────────

    def read_on_line(self, job: Callable[[], Awaitable[_T]]) -> asyncio.Future[_T]:
        """在写线上跑一个只读作业，好让多读推导观察到**同一个**已提交状态
        （``session.ts:235-245``）。

        ``commit`` 会走同一条线（第 3 步）。
        """
        try:
            self.assert_usable()
        except Exception as error:
            return _failed_future(error)

        async def run() -> _T:
            self._assert_healthy()
            return await job()

        return self._enqueue(run)

    def _tail_future(self) -> asyncio.Future[Any]:
        if self._tail is None:
            self._tail = asyncio.get_running_loop().create_future()
            self._tail.set_result(None)
        return self._tail

    def _enqueue(self, job: Callable[[], Awaitable[_T]]) -> asyncio.Future[_T]:
        """把 ``job`` 排到写线上（``session.ts:854-861``）。

        **急切调度**：返回时作业已经被排上，即使调用方不 await 它也会跑。
        链**永不断**：``_tail`` 始终是一个会成功的 Future。
        """
        loop = asyncio.get_running_loop()
        previous = self._tail_future()
        run: asyncio.Future[_T] = loop.create_task(_run_after(previous, job))
        self._tail = loop.create_task(_settle(run))
        return run

    # ── 关闭 ────────────────────────────────────────────────────────────────

    def close(self, context: Context) -> Awaitable[None]:
        """封住准入、结算已准入的工作，然后关闭 Storage（``session.ts:482-520``）。

        ⚠️ 第 3 步的「关闭监听器」与「发布」还没做，所以这里不清监听器集合
        （它们还不存在）。其余语义一致：**失败**的 Session 也会关掉后端，
        且 ``closed`` 落定为 ``{"reason": "failed", "error": ...}``。
        """
        if self._closing is None:
            cleanup = without_abort_signal(context)
            self._storage.begin_closing()
            closed_future = asyncio.get_running_loop().create_future()
            self._closing = closed_future

            async def run() -> None:
                self._commit_listeners.clear()
                self._internal_listeners.clear()
                try:
                    await self._storage.close(cleanup)
                except BaseException as error:
                    # 关不掉的 Storage 就是一个失败的 Storage；更早的失败仍是根因。
                    if self._failure is None:
                        self._failure = {"error": error}
                        self.report(error)
                    raise

            async def finish() -> None:
                try:
                    self.before_close()
                    await self._enqueue(run)
                except BaseException as error:
                    # 关不掉的 Storage 就是一个失败的 Storage；更早的失败仍是根因。
                    if self._failure is None:
                        self._failure = {"error": error}
                    raise
                finally:
                    # 只在还没落定时落定：`closed` 可能被调用方取消过（事件循环收尾时就会），
                    # 对它 set_result 会抛 InvalidStateError 并把关闭任务变成「未处理异常」。
                    if not closed_future.done():
                        closed_future.set_result(None)
                    if self._closed is not None and not self._closed.done():
                        self._closed.set_result(self._end())

            _fire_and_forget(finish())
            self._notify_close()
        return self._await_closing()

    def _notify_close(self) -> None:
        """把关闭监听器各跑一次，每个独立：一个失败只被报告，其余照跑（``session.ts:522-527``）。"""
        listeners = list(self._close_listeners)
        self._close_listeners.clear()
        for listener in listeners:
            contained(listener, self.report)

    async def _await_closing(self) -> None:
        assert self._closing is not None
        await self._closing


def _background_context() -> Context:
    from pi_chord.context import BACKGROUND_CONTEXT

    return BACKGROUND_CONTEXT


def _failed_future(error: BaseException) -> asyncio.Future[Any]:
    """一个已带异常的 Future（对应上游 ``Promise.reject``）——见 PORTING.md R14。"""
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_exception(error)
    future.exception()
    return future


def _fire_and_forget(awaitable: Awaitable[Any]) -> None:
    """跑一个**没人等**的协程（对应上游 ````.catch(() => {})`` 的「发了不管」）。

    收尾回调必须容忍**已取消**的 task：事件循环收尾时关闭任务会被取消，
    对已取消的 task 调 ``exception()`` 会再抛 ``CancelledError``，变成「未处理异常」噪音。
    """

    def swallow(finished: asyncio.Future[Any]) -> None:
        if finished.cancelled():
            return
        finished.exception()

    task = asyncio.ensure_future(awaitable)
    task.add_done_callback(swallow)


async def _run_after(previous: asyncio.Future[Any], job: Callable[[], Awaitable[_T]]) -> _T:
    """等 ``previous`` 落定（**它的错误不影响我**），再跑作业。"""
    with contextlib.suppress(BaseException):
        await asyncio.shield(previous)
    return await job()


async def _settle(run: asyncio.Future[Any]) -> None:
    """把链的尾部收成一个**永远成功**的 Future（上游 ``run.then(ok, err)`` 两边都吞）。"""
    with contextlib.suppress(BaseException):
        await asyncio.shield(run)


def contained(callback: Callable[[], Any], on_error: Callable[[BaseException], None]) -> None:
    """跑一个**不得让调用方失败**的宿主回调（``session.ts:878-888``）。

    抛出、或它返回的 awaitable 拒绝，都交给 ``on_error``，且**不 await** 它。
    """
    try:
        result = callback()
    except BaseException as error:
        on_error(error)
        return
    if inspect.isawaitable(result):
        task = asyncio.ensure_future(result)

        def report_exception(finished: asyncio.Future[Any]) -> None:
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                on_error(error)

        task.add_done_callback(report_exception)


def _listener_call(
    listener: Callable[[dict[str, Any], Context], None],
    publication: dict[str, Any],
    context: Context,
) -> Callable[[], None]:
    """把一个监听器调用包成无参回调（``contained`` 的入参形状）。"""

    def call() -> None:
        listener(publication, context)

    return call


def _address_of(record: dict[str, Any]) -> str:
    """一条记录的逻辑地址身份（``addressId(record)``）。"""
    return address_id(cast(Any, record))


def _observed_operations(
    observed: dict[str, int], change: dict[str, Any]
) -> tuple[Any, ...] | list[Any]:
    """一个已提交变更要让观察者套用的操作（``session.ts:890-901``）。

    在**别的定义版本**下水合的观察者持有的是另一个形状的值，所以它收到的是
    一次**根替换**，而不是针对那个形状的操作。
    """
    if change["value"] is None:
        return RETIREMENT_OPERATIONS
    if change["version"] == observed["version"]:
        return cast(list[Any], change["ops"])
    observed["version"] = int(change["version"])
    return (("r", change["value"]),)


def _throw_cancellation(signal: Any) -> None:
    """把调用方上下文的取消转成一次抛出（``session.ts:907-909`` 的 ``cancellationError``）。"""
    reason = getattr(signal, "reason", None)
    if isinstance(reason, BaseException):
        raise reason
    from pi_chord.context import AbortError

    raise AbortError("The operation was aborted")
