"""Session ↔ chord 的提交观察桥 —— 移植 ``durable/src/session/observation.ts``（292 行）。

两个对象，各由**一个**附着的 chord 状态一对一拥有：

* :class:`CommittedStateSource` —— 权威修订源，挂在文档或会话视图上；``None`` 值表示退休。
* :class:`CommittedWatch` —— 串行化的精确帧观察者。

JS 特有机制在 Python 侧的等价设计（**都有语义差异，逐条列出**）
------------------------------------------------------------------

1. **``queueMicrotask`` → :meth:`asyncio.AbstractEventLoop.call_soon`**

   上游用 ``queueMicrotask`` 保证「投递永远不内联发生在 :meth:`publish` 里」。
   Python 用 ``loop.call_soon``：回调在同一次事件循环迭代里、下一个检查点执行。
   **差异**：JS 的微任务队列只要求脚本块结束就会排空；``call_soon`` 需要事件循环真的转起来。
   两者在本模块的用法下等价——``publish`` 的调用点全都在事件循环里。
   ``call_soon`` **不允许传协程**，所以协程体（:meth:`_CommittedWatch._drain`）用
   :func:`asyncio.ensure_future` 包成任务；其余排空是同步的。

2. **``Promise.withResolvers<T>()`` → 惰性 :class:`asyncio.Future`**

   ``Promise.withResolvers`` 在**任何**地方都能建，Python 的 ``Future`` 必须绑定事件循环。
   所以 :class:`CommittedWatch` **不在构造器里建 Future**：它先把终态记在 ``_end`` 里，
   第一次取 :attr:`~CommittedWatch.closed` 时才在**当时的**运行循环上建 Future；
   若此前已经终结，就直接返回一个已完成（或已带异常）的 Future。
   **差异**：JS 拿到 Promise 的时机不影响结果；Python 侧「谁先 await 谁建 Future」，
   但可观察结果相同——终结状态是单次的，且先于任何 await。

3. **``AbortSignal`` → :class:`pi_chord.context.AbortSignal`**

   同名同义（``add_listener`` / ``remove_listener`` / ``aborted``），无需适配。

4. **``#drain`` 的「重入保护」直接照搬**：``_running`` / ``_scheduled`` 两个标志
   在 Python 里语义相同，因为事件循环同样不会在同步段中间插入另一个任务。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeVar, cast

from pi_chord.context import AbortSignal, Context, without_abort_signal
from pi_chord.delta import Op
from pi_chord.state import (
    ReplicatedStateSnapshot,
    ReplicatedStateSource,
    ReplicatedStateSourceAttachment,
    ReplicatedStateSourceFrame,
)
from pi_durable.errors import error_message
from pi_durable.types import WatchEnd

__all__ = [
    "MAX_PENDING_WATCH_FRAMES",
    "RETIREMENT_OPERATIONS",
    "CommittedStateSource",
    "CommittedWatch",
    "ObservedDocumentValue",
]

_T = TypeVar("_T")

#: 一个不可用的观察监听器身后保留的精确提交帧上限（``observation.ts:16``）。
MAX_PENDING_WATCH_FRAMES = 100

#: 已退休文档化身的规范终态更新（``observation.ts:19``）。
RETIREMENT_OPERATIONS: tuple[Op, ...] = (("r", None),)

#: 一个被观察的文档值：``None`` 表示退休。
ObservedDocumentValue = dict[str, Any] | None


class _SessionSourceAttachment(ReplicatedStateSourceAttachment[_T]):
    """一次附着：缓冲提交帧，并按序投递给唯一的监听器（``observation.ts:80-145``）。"""

    __slots__ = (
        "_activated",
        "_delivering",
        "_disposed",
        "_frames",
        "_listener",
        "_release",
        "_scheduled",
        "_snapshot",
    )

    _release: Callable[[], None] | None

    def __init__(
        self,
        snapshot: ReplicatedStateSnapshot[_T],
        release: Callable[[], None],
    ) -> None:
        self._snapshot = snapshot
        self._release = release
        self._frames: list[ReplicatedStateSourceFrame[_T]] = []
        self._listener: Callable[[ReplicatedStateSourceFrame[_T]], None] | None = None
        self._activated = False
        self._disposed = False
        self._scheduled = False
        self._delivering = False

    @property
    def snapshot(self) -> ReplicatedStateSnapshot[_T]:
        return self._snapshot

    def activate(self, listener: Callable[[ReplicatedStateSourceFrame[_T]], None]) -> None:
        """安装唯一监听器并**同步**排空已缓冲的帧（``state.py`` 的契约要求同步）。"""
        if self._activated:
            raise RuntimeError("State attachment is already active")
        if self._disposed:
            raise RuntimeError("State attachment is disposed")
        self._activated = True
        self._listener = listener
        self._drain()

    def publish(self, frame: ReplicatedStateSourceFrame[_T]) -> None:
        if self._disposed:
            return
        self._frames.append(frame)
        if not self._activated or self._delivering or self._scheduled:
            return
        self._scheduled = True
        loop = asyncio.get_running_loop()
        loop.call_soon(self._scheduled_drain)

    def _scheduled_drain(self) -> None:
        self._scheduled = False
        if self._disposed:
            return
        try:
            self._drain()
        except Exception:
            # chord 的帧监听器自己会兜住源契约失败；这里把「直连监听器意外抛出」
            # 也隔离在这一个附着上（``observation.ts:113-118``）。
            self.dispose()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._frames.clear()
        self._listener = None
        release = self._release
        self._release = None
        if release is not None:
            release()

    def _drain(self) -> None:
        listener = self._listener
        if listener is None or self._delivering or self._disposed:
            return
        self._delivering = True
        try:
            while not self._disposed:
                if not self._frames:
                    break
                listener(self._frames.pop(0))
        finally:
            self._delivering = False


class CommittedStateSource(Generic[_T]):
    """挂在文档或会话视图上的权威修订源（``observation.ts:25-78``）。

    ``advance()`` 推一帧；``None`` 值让源进入**退休**态，此后不再推。所有附着都释放后
    调一次 ``release``（上游用它把文档从 Session 缓存里摘掉）。
    """

    __slots__ = ("_attachments", "_closed", "_cursor", "_release", "_retired", "_value")

    _release: Callable[[], None] | None

    def __init__(self, value: _T, release: Callable[[], None]) -> None:
        self._value: _T | None = value
        self._release = release
        self._attachments: set[_SessionSourceAttachment[_T]] = set()
        self._cursor = 0
        self._retired = False
        self._closed = False

    def attach(self) -> ReplicatedStateSourceAttachment[_T]:
        if self._closed:
            raise RuntimeError("State source is closed")
        snapshot = ReplicatedStateSnapshot(value=cast(_T, self._value), cursor=self._cursor)

        def release() -> None:
            self._attachments.discard(attachment)
            if not self._attachments:
                self._finish_disposal()

        attachment = _SessionSourceAttachment(snapshot, release)
        self._attachments.add(attachment)
        return attachment

    def advance(self, value: _T, ops: list[Op], context: Context) -> None:
        if self._closed or self._retired:
            return
        self._value = value
        self._cursor += 1
        if value is None:
            self._retired = True
        frame: ReplicatedStateSourceFrame[_T] = ReplicatedStateSourceFrame(
            cursor=self._cursor, value=value, ops=list(ops), context=context
        )
        for attachment in list(self._attachments):
            attachment.publish(frame)

    def close_session(self) -> None:
        """停止发布；已附着的状态保留最后的值。失败也以同样方式结束它们（``observation.ts:64-68``）。"""
        if self._closed:
            return
        for attachment in list(self._attachments):
            attachment.dispose()
        self._finish_disposal()

    def _finish_disposal(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._value = None
        release = self._release
        self._release = None
        if release is not None:
            release()


class _WatchFrame(Generic[_T]):
    __slots__ = ("context", "ops", "value")

    def __init__(self, value: _T, ops: list[Op], context: Context) -> None:
        self.value = value
        self.ops = ops
        self.context = context


class CommittedWatch(Generic[_T]):
    """绑定到一个文档化身／会话视图的串行化精确帧观察（``observation.ts:157-288``）。

    ``None`` 值让它退休。监听器是协程——与上游的 ``async`` 监听器一致。
    """

    __slots__ = (
        "_cancellation_listener",
        "_cancellation_signal",
        "_closed_future",
        "_detach",
        "_end",
        "_listener",
        "_pending",
        "_replace",
        "_report",
        "_retired",
        "_running",
        "_scheduled",
        "_started",
        "_value",
    )

    def __init__(
        self,
        value: _T,
        detach: Callable[[], None],
        report: Callable[[Any], None],
        replace: Callable[[], _T] | None = None,
    ) -> None:
        self._value = value
        self._detach = detach
        self._report = report
        self._replace = replace
        self._pending: list[_WatchFrame[_T]] = []
        self._listener: Callable[[_T, list[Op], Context], Awaitable[None]] | None = None
        self._started = False
        self._scheduled = False
        self._running = False
        self._retired = False
        self._end: WatchEnd | None = None
        self._closed_future: asyncio.Future[WatchEnd] | None = None
        self._cancellation_signal: AbortSignal | None = None
        self._cancellation_listener: Callable[[], None] | None = None

    @property
    def value(self) -> _T:
        return self._value

    @property
    def closed(self) -> Awaitable[WatchEnd]:
        """终结时落定（``observation.ts:189-191``）。

        ``Promise.withResolvers`` → 惰性 Future：终结可能早于第一次 await，
        所以这里若已终结就直接返回已落定的 Future。
        """
        if self._closed_future is None:
            self._closed_future = asyncio.get_running_loop().create_future()
            if self._end is not None:
                self._closed_future.set_result(self._end)
        return self._closed_future

    def start(self, listener: Callable[[_T, list[Op], Context], Awaitable[None]]) -> None:
        """安装唯一的异步监听器；**绝不内联调用**（``observation.ts:193-199``）。"""
        if self._started:
            raise RuntimeError("Watch is already started")
        if self._end is not None:
            raise RuntimeError("Watch is stopped")
        self._started = True
        self._listener = listener
        if self._pending:
            self._schedule()

    async def stop(self) -> WatchEnd:
        self._terminate({"reason": "stopped"})
        return await self.closed

    def observe_cancellation(self, signal: AbortSignal) -> None:
        if self._cancellation_signal is not None:
            raise RuntimeError("Watch cancellation is already installed")
        if self._end is not None:
            return
        self._cancellation_signal = signal

        def listener() -> None:
            self.cancel()

        self._cancellation_listener = listener
        signal.add_listener(listener)
        if signal.aborted:
            self.cancel()

    def cancel(self) -> None:
        self._terminate({"reason": "cancelled"})

    def close_session(self, failure: Any = None) -> None:
        """以 ``session_closed`` 结束；Session 失败时以 ``session_failed`` 带上原错误（``observation.ts:220-224``）。

        ⚠️ ``failure`` 是 Session 的**失败记录**（``{"error": ...}``），不是错误本身——
        上游这里写的是 ``error: failure.error``。直接把它当 ``error`` 传出去会让观察者拿到一个
        包裹字典而不是根因（本包在阶段 3 第 4 步的测试里抓到过）。
        """
        if failure is None:
            self._terminate({"reason": "session_closed"})
        else:
            error = failure["error"] if isinstance(failure, dict) else failure
            self._terminate({"reason": "session_failed", "error": error})

    def advance(self, value: _T, ops: list[Op], context: Context) -> None:
        if self._end is not None or self._retired:
            return
        if value is None:
            self._retired = True
        if len(self._pending) >= MAX_PENDING_WATCH_FRAMES:
            # 溢出：丢掉全部待投递帧，改投一帧「替换」——默认是最新值。
            self._pending.clear()
            replacement = self._replace() if self._replace is not None else value
            self._pending.append(_WatchFrame(replacement, [("r", cast(Any, replacement))], context))
        else:
            self._pending.append(_WatchFrame(value, list(ops), context))
        if self._started:
            self._schedule()

    def _schedule(self) -> None:
        if self._scheduled or self._running or self._end is not None:
            return
        self._scheduled = True
        loop = asyncio.get_running_loop()
        loop.call_soon(self._scheduled_drain)

    def _scheduled_drain(self) -> None:
        self._scheduled = False
        if self._end is not None or self._running or not self._started:
            return
        task = asyncio.ensure_future(self._drain())
        # ``_drain`` 自己吞掉监听器异常（转成 report + terminate），所以这里只需防止
        # 「任务被 GC 时打印未取异常」。
        task.add_done_callback(lambda finished: finished.exception())

    async def _drain(self) -> None:
        if self._running or self._end is not None or not self._started:
            return
        self._running = True
        try:
            while self._end is None:
                if not self._pending:
                    break
                frame = self._pending.pop(0)
                self._value = frame.value
                # 投递用的上下文必须**不带** abort signal：监听器不该被调用方的取消打断。
                delivery_context = without_abort_signal(frame.context)
                assert self._listener is not None
                try:
                    await self._listener(frame.value, frame.ops, delivery_context)
                except Exception as error:
                    if self._end is None:
                        self._report(error)
                        self._terminate({"reason": "listener_error", "error": _to_error(error)})
                    break
                if frame.value is None:
                    self._terminate({"reason": "retired"})
                    break
        finally:
            self._running = False
            if self._end is None and self._pending:
                self._schedule()

    def _terminate(self, end: WatchEnd) -> None:
        """结束观察，只一次：解绑、丢帧、落定 ``closed``。已在跑的监听器继续跑（``observation.ts:278-287``）。"""
        if self._end is not None:
            return
        self._end = end
        self._detach()
        self._pending.clear()
        if self._cancellation_listener is not None and self._cancellation_signal is not None:
            self._cancellation_signal.remove_listener(self._cancellation_listener)
        if self._closed_future is None:
            self._closed_future = asyncio.get_running_loop().create_future()
        if not self._closed_future.done():
            self._closed_future.set_result(end)


def _to_error(error: Any) -> BaseException:
    """上游 ``toError``：非 ``Error`` 的值包成一个 ``Error``（``observation.ts:290-292``）。"""
    if isinstance(error, BaseException):
        return error
    return RuntimeError(error_message(error))


# 静态检查用：本模块实现了 chord 的两个源侧协议。
_SOURCE_PROTOCOL_CHECK: type[ReplicatedStateSource[Any]] | None = None
