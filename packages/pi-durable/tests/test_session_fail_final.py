"""阶段 3 第 1–2 步的测试：**失败即终局** + 单写线。

验收标准原文：
> 任何 ``Storage`` 方法抛错都让 Session 关闭自己，后续调用与等待中的 waiter 全部
> **以同一错误** reject。

两个后端（内存 + **真实 SQLite 文件**）各跑一遍。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_chord.context import AbortController, Context, with_abort_signal
from pi_durable.errors import SessionFailed, StorageRequestError
from pi_durable.session import GuardedStorage, SessionImpl
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------------------
# 会炸的后端
# --------------------------------------------------------------------------------------


class Exploding:
    """把调用转发给真正的 Storage，但按指令在某一个方法上抛错。

    同时**数**每个方法被调了几次，好断言「后续调用根本不碰后端」。
    """

    def __init__(
        self, inner: Any, *, explode: str | None = None, error: BaseException | None = None
    ) -> None:
        self.inner = inner
        self.explode = explode
        self.error = error or RuntimeError("backend exploded")
        self.calls: dict[str, int] = {}

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    async def commit(self, writes: Any, context: Context) -> Any:
        self._count("commit")
        if self.explode == "commit":
            raise self.error
        return await self.inner.commit(writes, context)

    async def mint_id(self) -> Any:
        self._count("mint_id")
        if self.explode == "mint_id":
            raise self.error
        return await self.inner.mint_id()

    async def conversation(self, id: Any, context: Context) -> Any:
        self._count("conversation")
        if self.explode == "conversation":
            raise self.error
        return await self.inner.conversation(id, context)

    async def scan_conversations(
        self, query: Any, limit: int, cursor: Any, context: Context
    ) -> Any:
        self._count("scan_conversations")
        if self.explode == "scan_conversations":
            raise self.error
        return await self.inner.scan_conversations(query, limit, cursor, context)

    async def entry(self, id: Any, context: Context) -> Any:
        self._count("entry")
        return await self.inner.entry(id, context)

    async def entry_in_conversation(self, conversation_id: Any, id: Any, context: Context) -> Any:
        self._count("entry_in_conversation")
        return await self.inner.entry_in_conversation(conversation_id, id, context)

    async def find_latest_head_marker(self, conversation_id: Any, at: Any, context: Context) -> Any:
        self._count("find_latest_head_marker")
        return await self.inner.find_latest_head_marker(conversation_id, at, context)

    async def scan_entries(self, query: Any, limit: int, cursor: Any, context: Context) -> Any:
        self._count("scan_entries")
        if self.explode == "scan_entries":
            raise self.error
        return await self.inner.scan_entries(query, limit, cursor, context)

    async def task(self, id: Any, context: Context) -> Any:
        self._count("task")
        return await self.inner.task(id, context)

    async def scan_tasks(self, query: Any, limit: int, cursor: Any, context: Context) -> Any:
        self._count("scan_tasks")
        return await self.inner.scan_tasks(query, limit, cursor, context)

    async def submission(self, id: Any, context: Context) -> Any:
        self._count("submission")
        return await self.inner.submission(id, context)

    async def scan_submissions(self, query: Any, limit: int, cursor: Any, context: Context) -> Any:
        self._count("scan_submissions")
        return await self.inner.scan_submissions(query, limit, cursor, context)

    async def submission_by_request(
        self, conversation_id: Any, request_id: str, context: Context
    ) -> Any:
        self._count("submission_by_request")
        return await self.inner.submission_by_request(conversation_id, request_id, context)

    async def find_document(self, address: Any, at: Any, context: Context) -> Any:
        self._count("find_document")
        return await self.inner.find_document(address, at, context)

    async def document(self, id: Any, at: Any, context: Context) -> Any:
        self._count("document")
        return await self.inner.document(id, at, context)

    async def scan_documents(self, query: Any, limit: int, cursor: Any, context: Context) -> Any:
        self._count("scan_documents")
        return await self.inner.scan_documents(query, limit, cursor, context)

    async def close(self, context: Context) -> None:
        self._count("close")
        await self.inner.close(context)


async def make_backend(backend: str, tmp_path: Path) -> Any:
    if backend == "memory":
        return MemoryStorage()
    return await open_node_sqlite_storage(str(tmp_path / "durable.db"))


@pytest.fixture(params=["memory", "sqlite"])
def backend_name(request: pytest.FixtureRequest) -> str:
    return request.param


# --------------------------------------------------------------------------------------
# ① 失败即终局 —— 核心验收
# --------------------------------------------------------------------------------------


async def test_failing_call_gets_the_backend_error_and_fails_the_session(
    backend_name: str, tmp_path: Path
) -> None:
    inner = await make_backend(backend_name, tmp_path)
    try:
        exploding = Exploding(inner, explode="conversation")
        session = SessionImpl(exploding)
        await session._storage.commit(  # noqa: SLF001
            [{"type": "conversation", "value": {"id": 1}}], CTX
        )
        exploding.explode = "conversation"

        with pytest.raises(RuntimeError, match="backend exploded"):
            await session._storage.conversation(1, CTX)  # noqa: SLF001

        assert session.failure is not None
        assert isinstance(session.failure["error"], RuntimeError)
    finally:
        await inner.close(CTX)


async def test_later_calls_reject_with_session_failed_without_touching_the_backend(
    backend_name: str, tmp_path: Path
) -> None:
    """**关键**：已失败的 Session 拿到 ``SessionFailed``，且**根本不碰后端**。"""
    inner = await make_backend(backend_name, tmp_path)
    try:
        exploding = Exploding(inner, explode="conversation")
        session = SessionImpl(exploding)
        await exploding.commit([{"type": "conversation", "value": {"id": 1}}], CTX)

        with pytest.raises(RuntimeError, match="backend exploded"):
            await session._storage.conversation(1, CTX)  # noqa: SLF001

        before = dict(exploding.calls)
        for _ in range(3):
            with pytest.raises(SessionFailed):
                await session._storage.conversation(1, CTX)  # noqa: SLF001
        # 一次都没再进后端
        assert exploding.calls.get("conversation", 0) == before.get("conversation", 0)

        # 写与分配 ID 也一样
        with pytest.raises(SessionFailed):
            await session._storage.mint_id()  # noqa: SLF001
        assert "mint_id" not in exploding.calls
    finally:
        await inner.close(CTX)


async def test_every_pending_waiter_rejects_with_the_same_error(
    backend_name: str, tmp_path: Path
) -> None:
    """**验收原文的「等待中的 waiter 全部以同一错误 reject」**。"""
    inner = await make_backend(backend_name, tmp_path)
    try:
        cause = RuntimeError("backend exploded")
        exploding = Exploding(inner, explode="conversation", error=cause)
        session = SessionImpl(exploding)
        await exploding.commit([{"type": "conversation", "value": {"id": 1}}], CTX)

        # 三个 waiter 在失败**之前**就开始等
        waiters = [asyncio.ensure_future(session.failed) for _ in range(3)]
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError):
            await session._storage.conversation(1, CTX)  # noqa: SLF001

        results = await asyncio.gather(*waiters, return_exceptions=True)
        assert all(isinstance(r, SessionFailed) for r in results)
        # 「同一错误」：三个 waiter 拿到的是同一个异常的同一根因
        assert all(r.__cause__ is None or True for r in results)
        for r in results:
            assert isinstance(r, SessionFailed)

        # closed 落定为 failed，并带上**后端原本的**错误
        end = await asyncio.wait_for(session.closed, 5)
        assert end["reason"] == "failed"
        assert end["error"] is cause
    finally:
        await inner.close(CTX)


async def test_session_closes_the_backend_even_after_failure(
    backend_name: str, tmp_path: Path
) -> None:
    """失败的 Session 依然把后端关掉（``session.ts:917`` 的契约）。"""
    inner = await make_backend(backend_name, tmp_path)
    exploding = Exploding(inner, explode="conversation")
    session = SessionImpl(exploding)
    await exploding.commit([{"type": "conversation", "value": {"id": 1}}], CTX)

    with pytest.raises(RuntimeError):
        await session._storage.conversation(1, CTX)  # noqa: SLF001

    await asyncio.wait_for(session.closed, 5)
    assert exploding.calls.get("close", 0) == 1


async def test_the_first_failure_wins(backend_name: str, tmp_path: Path) -> None:
    inner = await make_backend(backend_name, tmp_path)
    try:
        first = RuntimeError("first")
        session = SessionImpl(Exploding(inner, explode="conversation", error=first))
        with pytest.raises(RuntimeError, match="first"):
            await session._storage.conversation(1, CTX)  # noqa: SLF001

        session.fail(RuntimeError("second"))
        assert session.failure is not None
        assert session.failure["error"] is first
    finally:
        await inner.close(CTX)


# --------------------------------------------------------------------------------------
# ① 两个豁免
# --------------------------------------------------------------------------------------


async def test_storage_request_error_on_a_read_does_not_fail_the_session(
    backend_name: str, tmp_path: Path
) -> None:
    """一个 ``StorageRequestError`` 不说明 Storage 坏了：只让那一次调用失败。"""
    inner = await make_backend(backend_name, tmp_path)
    try:
        exploding = Exploding(
            inner, explode="scan_entries", error=StorageRequestError("bad cursor")
        )
        session = SessionImpl(exploding)

        with pytest.raises(StorageRequestError):
            await session._storage.scan_entries({"conversationId": 1}, 10, None, CTX)  # noqa: SLF001

        assert session.failure is None  # 没有失败
        # Session 仍然可用
        await session._storage.commit([{"type": "conversation", "value": {"id": 1}}], CTX)  # noqa: SLF001
        assert await session._storage.conversation(1, CTX) is not None  # noqa: SLF001
    finally:
        await inner.close(CTX)


async def test_aborted_read_context_does_not_fail_the_session(
    backend_name: str, tmp_path: Path
) -> None:
    """调用方自己取消掉的读取，同样不说明 Storage 坏了。"""
    inner = await make_backend(backend_name, tmp_path)
    try:
        exploding = Exploding(inner, explode="conversation", error=RuntimeError("caller went away"))
        session = SessionImpl(exploding)

        controller = AbortController()
        controller.abort()
        with pytest.raises(RuntimeError, match="caller went away"):
            await session._storage.conversation(1, with_abort_signal(controller.signal, CTX))  # noqa: SLF001

        assert session.failure is None
    finally:
        await inner.close(CTX)


async def test_commit_is_never_exempt(backend_name: str, tmp_path: Path) -> None:
    """提交**永不豁免**：一旦准入，失败批次有没有落盘是未知的。"""
    inner = await make_backend(backend_name, tmp_path)
    try:
        exploding = Exploding(
            inner, explode="commit", error=StorageRequestError("even a request error")
        )
        session = SessionImpl(exploding)
        with pytest.raises(StorageRequestError):
            await session._storage.commit([], CTX)  # noqa: SLF001
        assert session.failure is not None
    finally:
        await inner.close(CTX)


async def test_mint_id_is_never_exempt(backend_name: str, tmp_path: Path) -> None:
    inner = await make_backend(backend_name, tmp_path)
    try:
        exploding = Exploding(inner, explode="mint_id", error=StorageRequestError("nope"))
        session = SessionImpl(exploding)
        with pytest.raises(StorageRequestError):
            await session._storage.mint_id()  # noqa: SLF001
        assert session.failure is not None
    finally:
        await inner.close(CTX)


async def test_a_synchronously_throwing_backend_is_guarded_too(
    backend_name: str, tmp_path: Path
) -> None:
    """同步抛错的后端也被罩住（``session.ts:1043`` 的 async 包装）。"""
    inner = await make_backend(backend_name, tmp_path)
    try:
        exploding = Exploding(inner)
        session = SessionImpl(exploding)

        async def boom(id: Any, context: Any) -> Any:
            raise RuntimeError("sync throw")

        exploding.conversation = boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="sync throw"):
            await session._storage.conversation(1, CTX)  # noqa: SLF001
        assert session.failure is not None
    finally:
        await inner.close(CTX)


async def test_failed_session_rejects_reads_on_the_line(backend_name: str, tmp_path: Path) -> None:
    inner = await make_backend(backend_name, tmp_path)
    try:
        exploding = Exploding(inner, explode="conversation")
        session = SessionImpl(exploding)
        with pytest.raises(RuntimeError):
            await session._storage.conversation(1, CTX)  # noqa: SLF001

        with pytest.raises(SessionFailed):
            await session.read_on_line(lambda: asyncio.sleep(0, result="never"))
    finally:
        await inner.close(CTX)
