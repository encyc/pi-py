"""阶段 3 第 2 步的测试：**单写线**。

上游 ``#enqueue``（``session.ts:854-861``）是一条 promise 链::

    const run = this.#tail.then(job);
    this.#tail = run.then(() => undefined, () => undefined);

本包用**串行尾链**（不是 ``asyncio.Lock``）以保留三条性质：顺序、失败隔离且不断链、
**急切调度**。这三条各有测试。
"""

from __future__ import annotations

import asyncio

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage

pytestmark = pytest.mark.asyncio


def make_session() -> SessionImpl:
    return SessionImpl(MemoryStorage())


# --------------------------------------------------------------------------------------
# 顺序
# --------------------------------------------------------------------------------------


async def test_jobs_run_in_submission_order() -> None:
    session = make_session()
    order: list[int] = []

    async def job(index: int, delay: float) -> int:
        await asyncio.sleep(delay)
        order.append(index)
        return index

    # 故意让**先提交的**慢、后提交的快：串行线必须仍然按提交顺序完成
    futures = [
        session.read_on_line(lambda i=i, d=d: job(i, d))  # type: ignore[misc]
        for i, d in enumerate([0.03, 0.02, 0.01, 0.0])
    ]
    results = await asyncio.gather(*futures)

    assert order == [0, 1, 2, 3]
    assert results == [0, 1, 2, 3]


async def test_jobs_do_not_overlap() -> None:
    """串行线的核心保证：同一时刻只有一个作业在跑。"""
    session = make_session()
    active = 0
    max_active = 0

    async def job() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1

    await asyncio.gather(*[session.read_on_line(job) for _ in range(5)])
    assert max_active == 1


# --------------------------------------------------------------------------------------
# 失败隔离且不断链
# --------------------------------------------------------------------------------------


async def test_a_failing_job_does_not_break_the_chain() -> None:
    """作业失败只回给它的调用方；后面的作业照跑（上游 ``run.then(ok, err)`` 两边都吞）。"""
    session = make_session()
    ran: list[int] = []

    async def boom() -> None:
        raise RuntimeError("job exploded")

    async def ok(index: int) -> int:
        ran.append(index)
        return index

    bad = session.read_on_line(boom)
    good1 = session.read_on_line(lambda: ok(1))
    good2 = session.read_on_line(lambda: ok(2))

    with pytest.raises(RuntimeError, match="job exploded"):
        await bad
    assert await good1 == 1
    assert await good2 == 2
    assert ran == [1, 2]
    # 一个作业失败**不**让 Session 失败（它不是 Storage 的错误）
    assert session.failure is None


async def test_each_caller_gets_its_own_outcome() -> None:
    session = make_session()

    async def boom() -> None:
        raise ValueError("mine")

    bad = session.read_on_line(boom)
    good = session.read_on_line(lambda: asyncio.sleep(0, result="fine"))

    assert await good == "fine"
    with pytest.raises(ValueError, match="mine"):
        await bad


# --------------------------------------------------------------------------------------
# 急切调度（这是选「串行尾链」而不是 asyncio.Lock 的理由）
# --------------------------------------------------------------------------------------


async def test_jobs_are_scheduled_eagerly() -> None:
    """**关键差异**：作业在 ``read_on_line`` 返回时就已经排上，调用方不 await 也会跑。

    上游依赖这一点——``close()`` 里就是「发了不管」（``this.close(...).catch(() => {})``）。
    用 ``asyncio.Lock`` 的话，调用方不 await 作业永远不会开始。
    """
    session = make_session()
    ran: list[str] = []

    async def job() -> None:
        ran.append("ran")

    pending = session.read_on_line(job)
    # 还没 await 它，先让事件循环转两轮
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ran == ["ran"], "作业应当已被急切调度"
    await pending


async def test_fire_and_forget_job_still_runs() -> None:
    session = make_session()
    done = asyncio.Event()

    async def job() -> None:
        done.set()

    session.read_on_line(job)  # 返回值丢掉
    await asyncio.wait_for(done.wait(), 1)


# --------------------------------------------------------------------------------------
# 与失败守卫的交互
# --------------------------------------------------------------------------------------


async def test_read_on_line_rejects_on_a_failed_session() -> None:
    from pi_durable.errors import SessionFailed

    session = make_session()
    session.fail(RuntimeError("storage died"))
    with pytest.raises(SessionFailed):
        await session.read_on_line(lambda: asyncio.sleep(0, result=1))


async def test_read_on_line_rejects_on_a_closed_session() -> None:
    session = make_session()
    await session.close(CTX)
    with pytest.raises(RuntimeError, match="Session is closed"):
        await session.read_on_line(lambda: asyncio.sleep(0, result=1))


async def test_queued_job_is_rechecked_when_it_starts() -> None:
    """作业真正开始前会再查一次健康状态——排队期间 Session 可能已经失败了。

    这是 ``read_on_line`` 里 ``_assert_healthy()`` 的作用：**准入时**查一次不够，
    因为作业可能排在队列里等很久。
    """
    from pi_durable.errors import SessionFailed

    session = make_session()
    gate = asyncio.Event()
    started: list[str] = []

    async def blocking() -> None:
        started.append("blocking")
        await gate.wait()

    async def queued() -> None:
        started.append("queued")

    first = session.read_on_line(blocking)
    second = session.read_on_line(queued)

    # 让第一个作业真正开始跑（它在队列头部）
    await asyncio.sleep(0)
    assert started == ["blocking"]

    # 它还没结束，第二个还在排队；此时让 Session 失败
    session.fail(RuntimeError("died while queued"))
    gate.set()

    # 第一个作业已经跑起来了，会正常结束
    await asyncio.wait_for(first, 1)
    # 第二个作业开始前被拦下
    with pytest.raises(SessionFailed):
        await second
    assert "queued" not in started
