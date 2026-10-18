"""``pi_durable.tools.file_mutation_queue`` 的单元测试。

上游 ``durable/src/tools/file-mutation-queue.ts``（55 行）。
关注三件事：**同文件串行**、**异文件不互相等待**、**失败不卡住后来者**。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from pi_chord.context import BACKGROUND_CONTEXT
from pi_durable.env.node import NodeExecutionEnv, NodeExecutionEnvOptions
from pi_durable.tools.file_mutation_queue import _queues, with_file_mutation_queue


@pytest.fixture
def env(tmp_path: Path) -> Iterator[NodeExecutionEnv]:
    yield NodeExecutionEnv(NodeExecutionEnvOptions(cwd=str(tmp_path)))


@pytest.fixture(autouse=True)
def _clear_queues() -> Iterator[None]:
    """每个用例前后清空模块级队列，避免用例之间互相影响。"""
    _queues.clear()
    yield
    _queues.clear()


async def test_same_file_is_serialized(env: NodeExecutionEnv) -> None:
    """同一文件的两次调用必须**依次**执行，不能交错。"""
    trace: list[str] = []

    async def first() -> None:
        trace.append("first:start")
        await asyncio.sleep(0.02)
        trace.append("first:end")

    async def second() -> None:
        trace.append("second:start")
        await asyncio.sleep(0)
        trace.append("second:end")

    await asyncio.gather(
        with_file_mutation_queue(env, "a.txt", first, BACKGROUND_CONTEXT),
        with_file_mutation_queue(env, "a.txt", second, BACKGROUND_CONTEXT),
    )

    assert trace == ["first:start", "first:end", "second:start", "second:end"]


async def test_different_files_do_not_wait(env: NodeExecutionEnv) -> None:
    """不同文件共用事件循环，谁也不挡谁（``file-mutation-queue.ts:30-31``）。"""
    both_started = asyncio.Event()
    started = 0

    async def fn() -> None:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        # 若被串行化，第一个会在这里超时。
        await asyncio.wait_for(both_started.wait(), timeout=1)

    await asyncio.gather(
        with_file_mutation_queue(env, "a.txt", fn, BACKGROUND_CONTEXT),
        with_file_mutation_queue(env, "b.txt", fn, BACKGROUND_CONTEXT),
    )

    assert started == 2


async def test_returns_the_callback_result(env: NodeExecutionEnv) -> None:
    async def fn() -> str:
        return "done"

    assert await with_file_mutation_queue(env, "a.txt", fn, BACKGROUND_CONTEXT) == "done"


async def test_error_propagates_and_does_not_block_later_callers(
    env: NodeExecutionEnv,
) -> None:
    """``fn`` 抛出时异常照常上抛，且**不会**让后续调用永远等下去。

    上游把 ``release()`` 放在 ``finally``（``file-mutation-queue.ts:51-53``），
    所以链尾一定会 resolve。
    """

    async def failing() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await with_file_mutation_queue(env, "a.txt", failing, BACKGROUND_CONTEXT)

    async def ok() -> str:
        return "after"

    assert await with_file_mutation_queue(env, "a.txt", ok, BACKGROUND_CONTEXT) == "after"


async def test_queue_entry_is_removed_when_idle(env: NodeExecutionEnv) -> None:
    """最后一个持有者退出后不留残留条目（``file-mutation-queue.ts:53``）。"""

    async def fn() -> None:
        return None

    await with_file_mutation_queue(env, "a.txt", fn, BACKGROUND_CONTEXT)
    assert _queues == {}


async def test_queue_entry_stays_while_a_caller_is_waiting(env: NodeExecutionEnv) -> None:
    """还有等待者时不能把槽位删掉，否则第三个调用会插队。"""
    gate = asyncio.Event()

    async def slow() -> None:
        await gate.wait()

    async def quick() -> None:
        return None

    first = asyncio.create_task(with_file_mutation_queue(env, "a.txt", slow, BACKGROUND_CONTEXT))
    await asyncio.sleep(0)  # 让 first 拿到槽位
    second = asyncio.create_task(with_file_mutation_queue(env, "a.txt", quick, BACKGROUND_CONTEXT))
    await asyncio.sleep(0)

    assert len(_queues) == 1  # 槽位仍在，second 排在 first 后面

    gate.set()
    await asyncio.gather(first, second)
    assert _queues == {}


async def test_symlinked_directory_shares_one_key(env: NodeExecutionEnv, tmp_path: Path) -> None:
    """``write`` 新建文件、之后经符号链接目录改动它，必须落到同一个键上。

    这正是 ``canonical()`` 存在的理由（``file-mutation-queue.ts:12-15``）：
    ``canonical()`` 拿不到 ``canonicalPath`` 时会用 ``join_path([path, ".."])`` 逐级向上
    找存在的祖先，再把名字接回去。这条路要求 ``join_path`` **做词法归一化**
    （上游用 ``node:path`` 的 ``join``，``env/node.ts:648-650``），否则
    ``!absolutePath.startsWith(parent)`` 恒为真，函数会直接放弃。
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)

    trace: list[str] = []

    async def slow() -> None:
        trace.append("slow:start")
        await asyncio.sleep(0.02)
        trace.append("slow:end")

    async def quick() -> None:
        trace.append("quick:start")

    await asyncio.gather(
        with_file_mutation_queue(env, "real/f.txt", slow, BACKGROUND_CONTEXT),
        with_file_mutation_queue(env, "link/f.txt", quick, BACKGROUND_CONTEXT),
    )

    assert trace == ["slow:start", "slow:end", "quick:start"]


async def test_different_environments_are_independent(tmp_path: Path) -> None:
    """键里含文件系统 id（``file-mutation-queue.ts:9``）；不同 env 实例互不影响。"""
    first_env = NodeExecutionEnv(NodeExecutionEnvOptions(cwd=str(tmp_path)))
    second_env = NodeExecutionEnv(NodeExecutionEnvOptions(cwd=str(tmp_path)))
    # 本地环境共享同一个 id（``node.py`` 里 `self.id = "node:local"`），
    # 所以同路径的两次调用仍然会串行——这是上游刻意的语义。
    assert first_env.id == second_env.id

    trace: list[str] = []

    async def slow() -> None:
        trace.append("slow:start")
        await asyncio.sleep(0.02)
        trace.append("slow:end")

    async def quick() -> None:
        trace.append("quick:start")

    await asyncio.gather(
        with_file_mutation_queue(first_env, "f.txt", slow, BACKGROUND_CONTEXT),
        with_file_mutation_queue(second_env, "f.txt", quick, BACKGROUND_CONTEXT),
    )

    assert trace == ["slow:start", "slow:end", "quick:start"]


async def test_many_concurrent_callers_run_in_order(env: NodeExecutionEnv) -> None:
    """多个并发调用按提交顺序执行，且全部完成。"""
    trace: list[int] = []

    def operation(index: int):
        async def fn() -> None:
            await asyncio.sleep(0.001)
            trace.append(index)

        return with_file_mutation_queue(env, "a.txt", fn, BACKGROUND_CONTEXT)

    await asyncio.gather(*(operation(i) for i in range(10)))

    assert trace == list(range(10))
