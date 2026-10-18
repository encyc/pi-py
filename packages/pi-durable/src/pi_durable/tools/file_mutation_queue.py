"""同一文件上的写操作串行化 —— 移植 ``durable/src/tools/file-mutation-queue.ts``（55 行）。

``edit`` 与 ``write`` 对**同一个文件**的改动必须按调用顺序依次落盘，否则两次读-改-写
会互相覆盖。这个模块把「同一文件系统 id + 同一规范化路径」的调用串成一条链，
不同文件、不同文件系统互不等待（``file-mutation-queue.ts:28-32``）。

明确**不是**锁：它挡不住 ``bash`` 里的命令，也挡不住其它进程
（上游注释原话，``file-mutation-queue.ts:31``）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pi_chord.context import Context
from pi_durable.env import ExecutionEnv, FileError, Result, get_or_throw

__all__ = ["with_file_mutation_queue"]

_T = TypeVar("_T")

#: 每个文件改动链的尾部，键是「文件系统 id + 规范化路径」（``file-mutation-queue.ts:5``）。
_queues: dict[str, asyncio.Future[None]] = {}


async def _mutation_key(env: ExecutionEnv, path: str, context: Context) -> str:
    absolute_path = get_or_throw(await env.absolute_path(path, context))
    return f"{env.id}\0{await _canonical(env, absolute_path, context)}"


async def _canonical(env: ExecutionEnv, absolute_path: str, context: Context) -> str:
    """规范化路径；文件还不存在时用「规范化后的父目录 + 文件名」（``file-mutation-queue.ts:16-26``）。

    这样「``write`` 新建一个文件」与「之后改动它」在符号链接目录下也会落到同一个键上。
    """
    result: Result[str, FileError] = await env.canonical_path(absolute_path, context)
    if result.ok:
        return result.value
    if result.error.code == "not_supported":
        return absolute_path
    if result.error.code != "not_found":
        raise result.error
    # 文件系统自己拆路径，所以一个文件名里可能含有在别处是分隔符的字符。
    parent = get_or_throw(await env.join_path([absolute_path, ".."], context))
    if parent == absolute_path or not absolute_path.startswith(parent):
        return absolute_path
    name = absolute_path[len(parent) + (0 if parent.endswith(("/", "\\")) else 1) :]
    return get_or_throw(
        await env.join_path([await _canonical(env, parent, context), name], context)
    )


async def with_file_mutation_queue(
    env: ExecutionEnv,
    path: str,
    fn: Callable[[], Awaitable[_T]],
    context: Context,
) -> _T:
    """把 ``fn`` 排进 ``path`` 的改动链，返回它的结果（``file-mutation-queue.ts:33-55``）。

    并发调用按「键解析完成的顺序」执行。``fn`` 抛出的异常照常向上传播，
    且**不会**卡住后来者：``done`` 在 ``finally`` 里一定被置位。
    """
    key = await _mutation_key(env, path, context)
    loop = asyncio.get_running_loop()

    previous = _queues.get(key)
    done: asyncio.Future[None] = loop.create_future()
    # 先占住槽位再 await，中间没有挂起点，所以没有别的调用能插进来
    # （对应上游「Take the slot without awaiting」，``file-mutation-queue.ts:40-47``）。
    _queues[key] = done

    try:
        if previous is not None:
            # 上游链条里 `done` 总会 resolve（release 在 finally 里），所以这里不会抛。
            await previous
        return await fn()
    finally:
        if not done.done():
            done.set_result(None)
        if _queues.get(key) is done:
            del _queues[key]
