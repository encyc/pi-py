"""``harness/provider.py`` 的测试 —— 移植 ``harness/provider.ts``（39 行）。

这是批次 D 里第一个**真正接上 Session 运行时**的模块，所以测试同时覆盖
「身份稳定」与「分叉重新开始」两条语义，并跑内存与真实 SQLite 两个后端。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.harness.provider import ProviderDoc, ensure_provider_session_id
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage


class Runtime:
    """``ProviderRuntime`` 的最小实现：直接转发给 Session。"""

    def __init__(self, session: SessionImpl, conversation_id: int) -> None:
        self._session = session
        self.conversation_id = conversation_id

    async def snapshot(self, token: Any, *args: Any) -> Any:
        return await self._session.snapshot(token, *args)

    async def commit(self, change: Any, context: Any) -> Any:
        return await self._session.commit(change, context)


async def make_session(backend: str, tmp_path: Path) -> SessionImpl:
    storage = (
        MemoryStorage()
        if backend == "memory"
        else await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    )
    session = SessionImpl(storage)
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    return session


# --------------------------------------------------------------------------------------
# 定义
# --------------------------------------------------------------------------------------


def test_provider_doc_semantics() -> None:
    """身份必须是**当前值**，而且分叉要**重新初始化**。

    沿用父会话的 session ID 会让两次会话在提供方那边被认成同一个 ——
    这就是 ``history: "latest"`` + ``fork: "initial"`` 的理由。
    """
    definition = ProviderDoc.definition
    assert definition["kind"] == "pi.provider"
    assert definition["version"] == 1
    assert definition["scope"] == "conversation"
    assert definition["history"] == "latest"
    assert definition["fork"] == "initial"


def test_provider_doc_is_not_a_family() -> None:
    assert "family" not in ProviderDoc.definition or ProviderDoc.definition["family"] is not True


def test_provider_initial_generates_a_session_id() -> None:
    initial = ProviderDoc.definition["initial"]
    first = initial(None)
    second = initial(None)
    assert isinstance(first["sessionId"], str) and first["sessionId"]
    assert first["sessionId"] != second["sessionId"]  # 每次都是新身份


def test_provider_checkpoint_when_always_stores_a_base() -> None:
    """``checkpointWhen: () => true``：每次都写 base，不做增量。"""
    predicate = ProviderDoc.definition["checkpointWhen"]
    assert predicate({"x": 1}, [], {"deltasSinceBase": 0}) is True
    assert predicate({"x": 1}, [], {"deltasSinceBase": 99}) is True


# --------------------------------------------------------------------------------------
# ensure_provider_session_id
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_creates_then_reuses_the_identity(backend: str, tmp_path: Path) -> None:
    session = await make_session(backend, tmp_path)
    try:
        runtime = Runtime(session, 1)

        created = await ensure_provider_session_id(runtime, CTX)
        assert created
        # 第二次走 snapshot 路径，**不写任何东西**
        again = await ensure_provider_session_id(runtime, CTX)
        assert again == created
    finally:
        await session.close(CTX)


async def test_the_second_call_does_not_commit(
    backend: str | None = None, tmp_path: Path | None = None
) -> None:
    """正常路径**不写**：上游注释明说 "without writing in the normal path"。"""
    session = SessionImpl(MemoryStorage())
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    runtime = Runtime(session, 1)

    await ensure_provider_session_id(runtime, CTX)

    commits: list[int] = []
    session.subscribe_commits(lambda pub, ctx: commits.append(pub["seq"]))
    await ensure_provider_session_id(runtime, CTX)
    assert commits == []  # 第二次一次提交都没有


async def test_persisted_across_reopen(tmp_path: Path) -> None:
    """身份是**持久化**的：关掉再打开还是同一个。"""
    path = str(tmp_path / "durable.db")

    first = await open_node_sqlite_storage(path)
    session = SessionImpl(first)
    try:
        await session.commit(lambda tx: tx.create_root_conversation(), CTX)
        created = await ensure_provider_session_id(Runtime(session, 1), CTX)
    finally:
        await session.close(CTX)

    second = await open_node_sqlite_storage(path)
    reopened = SessionImpl(second)
    try:
        assert await ensure_provider_session_id(Runtime(reopened, 1), CTX) == created
    finally:
        await reopened.close(CTX)


async def test_each_conversation_gets_its_own_identity() -> None:
    session = SessionImpl(MemoryStorage())
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    await session.commit(lambda tx: tx.create_conversation({"kind": "ownerless"}), CTX)

    root_id = await ensure_provider_session_id(Runtime(session, 1), CTX)
    other_id = await ensure_provider_session_id(Runtime(session, 2), CTX)
    assert root_id != other_id

    # 再取一次都稳定
    assert await ensure_provider_session_id(Runtime(session, 1), CTX) == root_id
    assert await ensure_provider_session_id(Runtime(session, 2), CTX) == other_id


async def test_a_fork_starts_with_a_fresh_identity() -> None:
    """**关键语义**：``fork: "initial"`` —— 分叉拿到的是**新**身份，不是父的。

    沿用父的 session ID 会让提供方把两次会话认成同一个。
    """
    session = SessionImpl(MemoryStorage())
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    parent_id = await ensure_provider_session_id(Runtime(session, 1), CTX)

    entry = await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.user"}), CTX)
    forked: dict[str, Any] = {}

    async def fork(tx: Any) -> None:
        forked.update(await tx.fork_conversation(1, entry["id"], {"kind": "ownerless"}))

    await session.commit(fork, CTX)
    child = forked["id"]  # ⚠️ 不要硬编码：条目已经用掉了一个 ID

    child_id = await ensure_provider_session_id(Runtime(session, child), CTX)
    assert child_id != parent_id
    # 父的保持不变
    assert await ensure_provider_session_id(Runtime(session, 1), CTX) == parent_id


async def test_identity_survives_an_unrelated_commit() -> None:
    session = SessionImpl(MemoryStorage())
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    created = await ensure_provider_session_id(Runtime(session, 1), CTX)

    await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.user"}), CTX)
    assert await ensure_provider_session_id(Runtime(session, 1), CTX) == created


async def test_exception_when_the_commit_did_not_produce_an_id() -> None:
    """提交没有产生身份时**报错**，而不是返回一个空值（``provider.ts:37``）。"""

    class BrokenRuntime:
        conversation_id = 1

        async def snapshot(self, token: Any, *args: Any) -> Any:
            return None  # 没有已存身份

        async def commit(self, change: Any, context: Any) -> Any:
            return None  # 也不写

    with pytest.raises(RuntimeError, match="has no provider session ID"):
        await ensure_provider_session_id(BrokenRuntime(), CTX)  # type: ignore[arg-type]
