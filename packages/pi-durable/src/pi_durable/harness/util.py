"""harness 的小工具 —— 移植 ``harness/util.ts``（61 行）。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeVar

from pi_chord.context import Context
from pi_durable.errors import SessionFailed
from pi_durable.types import Cursor, Page

__all__ = ["Waiters", "closed_error", "scan_all"]

_K = TypeVar("_K")
_T = TypeVar("_T")


class Waiters(Generic[_K, _T]):
    """按 key 分组的待办等待；每一次等待**只落定一次**——通过 :meth:`resolve`、
    :meth:`reject_all`，或它自己上下文的取消（``util.ts:6-42``）。

    ⚠️ :meth:`add` 是**同步**方法却要返回一个可 await 的 Future（上游返回 Promise），
    所以它需要**正在运行的事件循环**——与 :meth:`~pi_durable.session.session.SessionImpl.commit`
    同一个约束（PORTING.md **R15**）。
    """

    def __init__(self) -> None:
        self._sets: dict[_K, set[asyncio.Future[_T]]] = {}

    def add(self, key: _K, context: Context) -> asyncio.Future[_T]:
        signal = context.abort_signal
        loop = asyncio.get_running_loop()
        if signal is not None and signal.aborted:
            future: asyncio.Future[_T] = loop.create_future()
            # 上游是 `Promise.reject(signal.reason)`——已取消的上下文一进来就拒绝。
            reason: Any = signal.reason
            future.set_exception(
                reason if isinstance(reason, BaseException) else RuntimeError(str(reason))
            )
            future.exception()  # 标记为已取，避免「never retrieved」警告
            return future

        waiter: asyncio.Future[_T] = loop.create_future()
        group = self._sets.setdefault(key, set())
        group.add(waiter)

        if signal is not None:

            def on_abort() -> None:
                group.discard(waiter)
                if not group and self._sets.get(key) is group:
                    del self._sets[key]
                if not waiter.done():
                    reason: Any = signal.reason
                    waiter.set_exception(
                        reason if isinstance(reason, BaseException) else RuntimeError(str(reason))
                    )

            signal.add_listener(on_abort)

            def detach(_finished: asyncio.Future[_T]) -> None:
                signal.remove_listener(on_abort)

            waiter.add_done_callback(detach)
        return waiter

    def keys(self) -> list[_K]:
        return list(self._sets)

    def resolve(self, key: _K, value: _T) -> None:
        """让 ``key`` 上的每个等待者拿到 ``value``；这一组就此消失。"""
        group = self._sets.pop(key, None)
        for waiter in group or ():
            if not waiter.done():
                waiter.set_result(value)

    def reject_all(self, error: BaseException) -> None:
        """让**所有**等待中的等待者以 ``error`` 拒绝，并清空。"""
        groups = list(self._sets.values())
        self._sets.clear()
        for group in groups:
            for waiter in group:
                if not waiter.done():
                    waiter.set_exception(error)


async def scan_all(scan: Callable[[Cursor | None], Awaitable[Page]]) -> list[Any]:
    """一次分页扫描的**全部**条目，按页序（``util.ts:45-53``）。"""
    items: list[Any] = []
    cursor: Cursor | None = None
    while True:
        page = await scan(cursor)
        items.extend(page["items"])
        cursor = page.get("next")
        if cursor is None:
            return items


def closed_error(session: Any) -> Exception:
    """一个已关闭的 Harness 以什么拒绝（``util.ts:56-60``）：

    Session 因失败而关闭时，是带着根因的 :class:`~pi_durable.errors.SessionFailed`。
    """
    failure = session.failure
    if failure is None:
        return RuntimeError("Harness is closed")
    return SessionFailed(failure["error"])
