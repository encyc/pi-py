"""可观察的复制状态：不可变 JSON 值 + 有序、串行化的变更通知。

对应上游 ``chord/src/state.ts``（423 行）与 ``chord/src/delta/README.md``。

两种形态（上游 ``state.ts:1-5``）:

- **权威且可变**：``replicated_state(initial)``
- **只发布的视图**：``replicated_state(source, options)`` —— 一个外部修订源的视图

``ReplicatedStateSubscriber`` 的投递语义（上游 ``state.ts:20-27``）:

- 每个订阅**串行化**回调，并在更新之前等待水合（hydration）。
- 运行中的回调之后最多排队 **100** 次投递；溢出时只保留最新的那个
  「值 / 上下文 / delivery」，因此**更新序号可能跳跃**。
- 失败被**隔离**上报，投递继续。
- 退订会丢弃挂起的工作，但**不中止也不等待**正在跑的回调。

Python 侧的 async 映射见 :class:`_StateSubscriber`。
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, Literal, Protocol, TypeVar, cast

from .context import BACKGROUND_CONTEXT, Context
from .delta import Op, Tracker, track

__all__ = [
    "AttachedReplicatedState",
    "MutableReplicatedState",
    "ReplicatedState",
    "ReplicatedStateDelivery",
    "ReplicatedStateSnapshot",
    "ReplicatedStateSource",
    "ReplicatedStateSourceAttachment",
    "ReplicatedStateSourceFrame",
    "ReplicatedStateSourceOptions",
    "replicated_state",
]

T = TypeVar("T")

ErrorReporter = Callable[[BaseException], None]


def _to_error(error: object) -> BaseException:
    """上游 ``toError``（``state.ts:400-402``）。"""
    return error if isinstance(error, BaseException) else Exception(str(error))


def _report_error_async(error: BaseException) -> None:
    """上游 ``reportErrorAsync``（``state.ts:394-398``）：在微任务里抛出。

    Python 没有微任务队列：有运行中的事件循环时用 ``call_soon`` 抛出（由循环的异常
    处理器上报，不打断投递）；没有循环时写 stderr —— 同样是「让错误可见但不打断
    调用方」。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        return

    def reraise() -> None:
        raise error

    loop.call_soon(reraise)


# ─── 公开类型 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReplicatedStateDelivery:
    """一次投递的元信息（上游 ``state.ts:9-12``）。"""

    kind: Literal["hydrate", "update"]
    sequence: int


StateListener = Callable[[T, Context, ReplicatedStateDelivery], Any]
"""订阅回调。

可以返回 awaitable：上游允许监听器返回 Promise 并在其落定后继续排空
（``state.ts:133-141``）。Python 侧用**运行中的事件循环**调度它。
"""


class ReplicatedState(Generic[T], ABC):
    """只读的复制状态（上游 ``ReplicatedState``, ``state.ts:14-28``）。

    ``value`` 是**契约上不可变**的：不冻结，且可能与进程内提供者共享容器；
    消费者不得修改它。后续更新不会改动此前返回过的值。
    """

    @property
    @abstractmethod
    def value(self) -> T | None:
        """当前值；尚未水合时为 ``None``。"""

    @abstractmethod
    def subscribe(self, listener: StateListener[T]) -> Callable[[], None]:
        """订阅变更，返回退订函数。"""


class MutableReplicatedState(ReplicatedState[T], ABC):
    """权威且可变的复制状态（上游 ``state.ts:30-42``）。"""

    @property
    @abstractmethod
    def value(self) -> T: ...

    @abstractmethod
    def change(self, context: Context, mutate: Callable[[Any], Any]) -> None:
        """原子地发布一次同步的 overlay 变更。

        回调返回后 draft 句柄即不可用。放置进 draft 的值按值拷贝；给对象属性赋
        ``None`` 是写入 JSON ``null``（Python 没有 ``undefined``）。

        回调**必须同步**：返回 awaitable 会抛 ``TypeError``（上游 ``state.ts:263-266``）。
        因此返回类型是 ``Any`` 而不是 ``None`` —— 返回值需要被检查。
        """

    @abstractmethod
    def replace(self, context: Context, value: T) -> None:
        """原子地取走一个无别名严格 JSON 替换值的不可变所有权。"""


@dataclass(frozen=True)
class ReplicatedStateSourceFrame(Generic[T]):
    """快照之后提交的一个不可变权威修订（上游 ``state.ts:44-53``）。"""

    cursor: int
    """单调的源游标；快照后的第一帧必须是 ``snapshot.cursor + 1``。"""

    value: T
    ops: list[Op]
    context: Context


@dataclass(frozen=True)
class ReplicatedStateSnapshot(Generic[T]):
    """``attach()`` 在原子边界上捕获的固定不可变快照（上游 ``state.ts:57``）。"""

    value: T
    cursor: int


class ReplicatedStateSourceAttachment(Generic[T], ABC):
    """一次源附着（上游 ``state.ts:55-66``）。"""

    @property
    @abstractmethod
    def snapshot(self) -> ReplicatedStateSnapshot[T]: ...

    @abstractmethod
    def activate(self, listener: Callable[[ReplicatedStateSourceFrame[T]], None]) -> None:
        """安装唯一的监听器，并**同步**按提交顺序排空所有已缓冲的帧。

        单次使用。开始之后，每一个新提交的帧也必须按序投递直到 dispose，
        包括在前一帧投递期间**重入**产生的提交。
        """

    @abstractmethod
    def dispose(self) -> None:
        """停止投递并释放源资源。必须幂等。"""


class ReplicatedStateSource(Generic[T], Protocol):
    """一个权威的不可变修订源（上游 ``state.ts:68-78``）。

    ``attach()`` 必须**同步且原子地**捕获一个快照，并登记返回的 attachment 以缓冲
    此后每一个已提交的帧。快照必须包含该边界之前的每一次提交；缓冲的帧必须包含
    该边界之后的每一次提交，**不重不漏**。
    """

    def attach(self) -> ReplicatedStateSourceAttachment[T]: ...


@dataclass(frozen=True)
class ReplicatedStateSourceOptions:
    """上游 ``state.ts:80-83``。"""

    on_error: ErrorReporter | None = None
    """接收源契约与发布监听器的失败，而不把它们抛回源。"""


class AttachedReplicatedState(ReplicatedState[T], ABC):
    """由一次源附着支撑的、同步水合的只发布状态（上游 ``state.ts:85-90``）。"""

    @property
    @abstractmethod
    def value(self) -> T: ...

    @abstractmethod
    def dispose(self) -> None:
        """幂等地释放源附着；最后发布的值仍然可读。"""


# ─── 内部：投递 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Delivery(Generic[T]):
    value: T
    context: Context
    delivery: ReplicatedStateDelivery


def _optional_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _close_awaitable(value: object) -> None:
    """丢掉一个不会被 await 的协程，避免 "never awaited" 警告。"""
    close = getattr(value, "close", None)
    if callable(close):
        close()


class _StateSubscriber(Generic[T]):
    """一次公开订阅，独立于生产者与其他订阅者的进度（上游 ``state.ts:101-171``）。"""

    def __init__(self, listener: StateListener[T], report_error: ErrorReporter) -> None:
        self._listener = listener
        self._report_error = report_error
        self._pending: list[_Delivery[T]] = []
        self._running = False
        self._started = False
        self._closed = False

    def push(self, frame: _Delivery[T]) -> None:
        if self._closed:
            return
        if len(self._pending) == 100:
            # 冷副本可能在自己的首次水合开始前**重入**地排入更新。
            hydration = None if self._started else self._pending[0]
            self._pending.clear()
            if hydration is not None:
                self._pending.append(hydration)
        self._pending.append(frame)

    def drain(self) -> None:
        if self._running or self._closed:
            return
        self._running = True
        while self._pending:
            frame = self._pending.pop(0)
            self._started = True
            try:
                result = self._listener(frame.value, frame.context, frame.delivery)
            except BaseException as error:  # noqa: BLE001 - 与上游一致，监听器失败被隔离
                self._report(error)
                continue
            if inspect.isawaitable(result):
                self._await_result(result)
                return
        self._running = False

    def clear(self) -> None:
        self._pending.clear()

    def close(self) -> None:
        self._closed = True
        self.clear()

    def _await_result(self, result: Any) -> None:
        """异步监听器：在事件循环上跑完再继续排空（上游 ``state.ts:133-141``）。"""
        loop = _optional_loop()
        if loop is None:
            _close_awaitable(result)
            self._report(
                RuntimeError("an async replicated-state listener requires a running event loop")
            )
            self._resume()
            return

        async def wait() -> None:
            try:
                await result
            except asyncio.CancelledError:
                # 事件循环收尾时的取消不是监听器失败，不该当错误上报。
                raise
            except BaseException as error:  # noqa: BLE001
                self._report(error)

        task = loop.create_task(wait())
        task.add_done_callback(lambda _task: self._resume())

    def _resume(self) -> None:
        self._running = False
        self.drain()

    def _report(self, error: object) -> None:
        try:
            self._report_error(_to_error(error))
        except BaseException as report_error:  # noqa: BLE001 - 上报器自己失败时退到异步上报
            _report_error_async(_to_error(report_error))


@dataclass(frozen=True)
class _Publication(Generic[T]):
    value: T
    sequence: int
    context: Context


class _ReplicatedStatePublisher(Generic[T]):
    """维护本地发布顺序，独立于修订如何产生（上游 ``state.ts:179-239``）。"""

    def __init__(self, initial: T, report_error: ErrorReporter | None = None) -> None:
        self._value = initial
        self._report_error: ErrorReporter = report_error or _report_error_async
        self._listeners: dict[_StateSubscriber[T], int] = {}
        self._publications: list[_Publication[T]] = []
        self._sequence = 0
        self._delivering = False

    @property
    def value(self) -> T:
        return self._value

    def snapshot(self) -> tuple[T, int]:
        return (self._value, self._sequence)

    def subscribe(self, listener: StateListener[T]) -> Callable[[], None]:
        value, sequence = self.snapshot()
        subscriber: _StateSubscriber[T] = _StateSubscriber(listener, self._report_error)
        self._listeners[subscriber] = sequence
        # 水合没有调用方；它用后台上下文投递（上游 state.ts:205）。
        subscriber.push(
            _Delivery(value, BACKGROUND_CONTEXT, ReplicatedStateDelivery("hydrate", sequence))
        )
        subscriber.drain()

        def unsubscribe() -> None:
            subscriber.close()
            self._listeners.pop(subscriber, None)

        return unsubscribe

    def publish(self, value: T, context: Context) -> None:
        """发布一个已准备好的不可变修订。订阅者的失败只上报，绝不抛出
        （上游 ``state.ts:214-238``）。"""
        self._value = value
        self._sequence += 1
        self._publications.append(_Publication(value, self._sequence, context))
        if self._delivering:
            return
        self._delivering = True
        try:
            while self._publications:
                publication = self._publications.pop(0)
                delivery = ReplicatedStateDelivery("update", publication.sequence)
                for subscriber, hydrated_sequence in list(self._listeners.items()):
                    if publication.sequence <= hydrated_sequence:
                        continue
                    subscriber.push(_Delivery(publication.value, publication.context, delivery))
                    subscriber.drain()
        finally:
            self._delivering = False


# ─── 内部：两种实现 ──────────────────────────────────────────────────────────


class _MutableReplicatedState(MutableReplicatedState[T]):
    """上游 ``MutableReplicatedStateImpl``（``state.ts:241-290``）。"""

    def __init__(self, initial: T) -> None:
        self._tracker: Tracker[T] = track(initial)
        self._publisher: _ReplicatedStatePublisher[T] = _ReplicatedStatePublisher(
            self._tracker.value
        )
        self._changing = False

    @property
    def value(self) -> T:
        return self._tracker.value

    def change(self, context: Context, mutate: Callable[[Any], Any]) -> None:
        if self._changing:
            raise RuntimeError(
                "Replicated state cannot be changed reentrantly from a change callback"
            )
        self._changing = True
        prepared: Any
        try:
            change = self._tracker.begin_change()
            try:
                outcome = mutate(change.state)
                if inspect.isawaitable(outcome):
                    # 上游先把 promise 的失败吞掉再抛类型错误（state.ts:263-266）。
                    _close_awaitable(outcome)
                    raise TypeError("Replicated state change callbacks must be synchronous")
                prepared = change.prepare()
            except BaseException:
                change.abort()
                raise
        finally:
            self._changing = False

        self._tracker.adopt(prepared)
        if len(prepared.ops) > 0:
            self._publisher.publish(prepared.value, context)

    def replace(self, context: Context, value: T) -> None:
        if self._changing:
            raise RuntimeError("Replicated state cannot be replaced from a change callback")
        prepared = self._tracker.prepare_replace(value)
        self._tracker.adopt(prepared)
        if len(prepared.ops) > 0:
            self._publisher.publish(prepared.value, context)

    def subscribe(self, listener: StateListener[T]) -> Callable[[], None]:
        return self._publisher.subscribe(listener)


class _AttachedReplicatedState(AttachedReplicatedState[T]):
    """上游 ``AttachedReplicatedStateImpl``（``state.ts:292-360``）。"""

    def __init__(
        self,
        attachment: ReplicatedStateSourceAttachment[T],
        options: ReplicatedStateSourceOptions,
    ) -> None:
        snapshot = attachment.snapshot
        _assert_cursor(snapshot.cursor, "snapshot")
        self._attachment = attachment
        self._cursor = snapshot.cursor
        self._report_error: ErrorReporter = options.on_error or _report_error_async
        self._publisher: _ReplicatedStatePublisher[T] = _ReplicatedStatePublisher(
            snapshot.value, lambda error: self._report(error)
        )
        self._disposed = False

    @property
    def value(self) -> T:
        return self._publisher.value

    def subscribe(self, listener: StateListener[T]) -> Callable[[], None]:
        return self._publisher.subscribe(listener)

    def activate(self) -> None:
        self._attachment.activate(self._receive)

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._attachment.dispose()

    def _receive(self, frame: ReplicatedStateSourceFrame[T]) -> None:
        if self._disposed:
            return
        try:
            _assert_cursor(frame.cursor, "frame")
            expected = self._cursor + 1
            if frame.cursor != expected:
                raise RuntimeError(
                    f"Replicated state source cursor has a gap: expected {expected}, received {frame.cursor}"
                )
            self._cursor = frame.cursor
            self._publisher.publish(frame.value, frame.context)
        except BaseException as error:  # noqa: BLE001 - 源契约失败 → 上报并停止
            self._fail(_to_error(error))

    def _fail(self, error: BaseException) -> None:
        if self._disposed:
            return
        self._disposed = True
        try:
            self._attachment.dispose()
        except BaseException as dispose_error:  # noqa: BLE001
            self._report(
                BaseExceptionGroup(
                    "Replicated state source contract failed", [error, dispose_error]
                )
            )
            return
        self._report(error)

    def _report(self, error: object) -> None:
        try:
            self._report_error(_to_error(error))
        except BaseException as report_error:  # noqa: BLE001
            _report_error_async(_to_error(report_error))


_MAX_SAFE_INTEGER = 2**53 - 1


def _assert_cursor(cursor: object, kind: str) -> None:
    """上游 ``assertCursor``（``state.ts:382-385``）：必须是 **safe integer**。

    JS 的 ``Number.isSafeInteger`` 有上界 ``2**53 - 1``；Python 的 ``int`` 无界，
    所以要显式补上，并排除 ``bool``（它是 ``int`` 子类）。
    """
    if isinstance(cursor, bool) or not isinstance(cursor, int) or abs(cursor) > _MAX_SAFE_INTEGER:
        raise TypeError(f"Replicated state source {kind} cursor must be a safe integer")


def _attach_source(
    source: ReplicatedStateSource[T],
    options: ReplicatedStateSourceOptions,
) -> AttachedReplicatedState[T]:
    """上游 ``attachReplicatedStateSource``（``state.ts:362-380``）。"""
    attachment = source.attach()
    try:
        state = _AttachedReplicatedState(attachment, options)
        state.activate()
        return state
    except BaseException as error:  # noqa: BLE001
        try:
            attachment.dispose()
        except BaseException as dispose_error:  # noqa: BLE001
            raise BaseExceptionGroup(
                "Failed to attach replicated state source", [error, dispose_error]
            ) from None
        raise


def _is_source(value: object) -> bool:
    """上游 ``isReplicatedStateSource``（``state.ts:421-423``）。"""
    return callable(getattr(value, "attach", None))


def replicated_state(
    initial_or_source: T | ReplicatedStateSource[T],
    options: ReplicatedStateSourceOptions | None = None,
) -> MutableReplicatedState[T] | AttachedReplicatedState[T]:
    """创建复制状态（上游 ``replicatedState``, ``state.ts:404-419``）。

    - 传入一个 :class:`ReplicatedStateSource`（有可调用的 ``attach``）→ 附着到它，
      得到只发布的 :class:`AttachedReplicatedState`。
    - 否则取走 ``initial_or_source`` 的不可变所有权（无别名严格 JSON 根），
      得到权威且可变的 :class:`MutableReplicatedState`。

    **调用方在此之后不得再修改 ``initial_or_source``。**
    """
    if _is_source(initial_or_source):
        return _attach_source(
            cast(ReplicatedStateSource[T], initial_or_source),
            options or ReplicatedStateSourceOptions(),
        )
    return _MutableReplicatedState(cast(T, initial_or_source))
