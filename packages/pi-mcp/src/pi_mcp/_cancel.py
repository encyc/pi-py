"""取消辅助：把「等待一个可等待对象」与「等待取消信号」赛跑。

上游把 ``AbortSignal`` 直接交给 ``fetch``，因此**在途请求**会被中止。Python 的
``asyncio.Event`` 没有这个能力，只能显式竞速 —— 本模块提供这件事的单一实现，
供 OAuth 发现/流程复用（见 ``PORTING.md`` 关于取消的偏离说明）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar

from .protocol.jsonrpc import McpAbortError

T = TypeVar("T")


async def await_or_abort(awaitable: Awaitable[T], signal: asyncio.Event | None) -> T:
    """等待 ``awaitable``；``signal`` 一旦置位就中止它并抛 ``McpAbortError``。

    ``signal`` 为 ``None`` 时等价于直接 ``await``。被中止的请求会被 ``cancel()``，
    交由底层（httpx / asyncio）自行清理连接。
    """
    if signal is None:
        return await awaitable
    if signal.is_set():
        raise McpAbortError()
    task: asyncio.Future[T] = asyncio.ensure_future(awaitable)
    # 显式标注成同构的 ``Future[Any]`` 集合：``asyncio.wait`` 的 stub 要求同类型，
    # 而这里两个 future 的结果类型本就不同（``T`` 与 ``bool``）。
    waiters: set[asyncio.Future[Any]] = {task, asyncio.ensure_future(signal.wait())}
    try:
        done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        task.cancel()
        raise McpAbortError()
    finally:
        for waiter in waiters:
            waiter.cancel()
        if not task.done():
            task.cancel()


__all__ = ["await_or_abort"]
