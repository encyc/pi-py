"""异步小工具。

上游 TypeScript 的 ``await`` 对同步值与 Promise 一视同仁，Python 需要显式区分。
provider 的可选钩子（``on_provider_stream_event`` 等）允许调用方传同步函数或
协程函数，故统一经 :func:`maybe_await` 调用。
"""

from __future__ import annotations

import asyncio
from typing import Any

__all__ = ["maybe_await"]


async def maybe_await(value: Any) -> None:
    """值为可等待对象时等待它；``None`` 与其他值直接忽略。"""
    if value is None:
        return
    if isinstance(value, (asyncio.Future,)) or asyncio.iscoroutine(value):
        await value
