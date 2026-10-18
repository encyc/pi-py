"""阶段 3 第 3 步的测试：``commit`` 与提交发布。

链路：事务 → settle → 存储准入 → ``adopt`` → ``_publish`` 给 ``subscribe_commits`` /
``subscribe_close``。内存与真实 SQLite 各跑一遍关键路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_chord.context import AbortController, with_abort_signal
from pi_durable.errors import SessionFailed
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage

pytestmark = pytest.mark.asyncio


def make_session(storage: Any = None) -> SessionImpl:
    return SessionImpl(storage if storage is not None else MemoryStorage())


async def seed_root(session: SessionImpl) -> None:
    async def change(tx: Any) -> None:
        await tx.create_root_conversation()

    await session.commit(change, CTX)


# --------------------------------------------------------------------------------------
# 基本链路
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_commit_runs_the_callback_and_returns_its_result(
    backend: str, tmp_path: Path
) -> None:
    storage = (
        MemoryStorage()
        if backend == "memory"
        else await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    )
    session = make_session(storage)
    try:

        async def change(tx: Any) -> str:
            await tx.create_root_conversation()
            entry = await tx.append_entry(1, {"kind": "pi.user", "data": {"text": "hi"}})
            return f"entry-{entry['id']}"

        assert await session.commit(change, CTX) == "entry-2"

        # 真的落到了后端
        stored = await storage.entry(2, CTX)
        assert stored is not None
        assert stored["entry"]["data"] == {"text": "hi"}
    finally:
        await session.close(CTX)
        if backend == "sqlite":
            pass


async def test_commit_publishes_every_change_kind() -> None:
    session = make_session()
    published: list[dict[str, Any]] = []
    session.subscribe_commits(lambda pub, ctx: published.append(pub))

    async def change(tx: Any) -> None:
        await tx.create_root_conversation()
        await tx.append_entry(1, {"kind": "pi.user"})
        await tx.create_submission(
            {"conversationId": 1, "requestId": "r", "type": "input", "status": "queued"}
        )

    await session.commit(change, CTX)

    assert len(published) == 1
    assert published[0]["seq"] == 1
    kinds = [c["type"] for c in published[0]["changes"]]
    assert kinds == ["conversation", "entry", "submission"]


async def test_commit_publishes_document_changes() -> None:
    from pi_durable.documents import define_doc

    session = make_session()
    published: list[dict[str, Any]] = []
    session.subscribe_commits(lambda pub, ctx: published.append(pub))
    token = define_doc(
        {
            "kind": "pi.notes",
            "version": 1,
            "scope": "conversation",
            "history": "rewindable",
            "fork": "asOf",
            "initial": lambda seed: {"n": 0},
        }
    )

    async def change(tx: Any) -> None:
        await tx.create_root_conversation()
        draft = await tx.doc(token, 1)
        draft["n"] = 1

    await session.commit(change, CTX)

    docs = [c for c in published[0]["changes"] if c["type"] == "document"]
    assert len(docs) == 1
    assert docs[0]["value"] == {"n": 1}
    assert docs[0]["conversationId"] == 1


async def test_commits_are_serialized_and_seq_increases() -> None:
    session = make_session()
    seqs: list[int] = []
    session.subscribe_commits(lambda pub, ctx: seqs.append(pub["seq"]))

    async def change(tx: Any) -> None:
        await tx.append_entry(1, {"kind": "pi.user"})

    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    await asyncio_gather_commits(session, change)
    assert seqs == [1, 2, 3]


async def asyncio_gather_commits(session: SessionImpl, change: Any) -> None:
    import asyncio

    await asyncio.gather(session.commit(change, CTX), session.commit(change, CTX))


async def test_no_writes_means_no_storage_commit_and_no_publication() -> None:
    session = make_session()
    published: list[Any] = []
    session.subscribe_commits(lambda pub, ctx: published.append(pub))

    async def read_only(tx: Any) -> str:
        await tx.create_root_conversation()
        return "done"

    # 只有会话写入 → 有发布；下面用**纯读**提交验证「零写入」
    assert await session.commit(read_only, CTX) == "done"
    assert len(published) == 1

    async def truly_empty(tx: Any) -> str:
        return "nothing"

    assert await session.commit(truly_empty, CTX) == "nothing"
    assert len(published) == 1  # 没有新发布


# --------------------------------------------------------------------------------------
# 失败路径
# --------------------------------------------------------------------------------------


async def test_callback_throw_propagates_and_commits_nothing() -> None:
    session = make_session()
    published: list[Any] = []
    session.subscribe_commits(lambda pub, ctx: published.append(pub))

    async def change(tx: Any) -> None:
        await tx.create_root_conversation()
        raise ValueError("callback exploded")

    with pytest.raises(ValueError, match="callback exploded"):
        await session.commit(change, CTX)

    assert published == []
    assert session.failure is None  # 回调的错误**不**让 Session 失败


async def test_swallowing_a_failed_read_still_does_not_commit() -> None:
    """回调吞掉了一个**失败的读取**时必须不提交，也不能装作成功（``session.ts:700-702``）。"""

    class Boom(MemoryStorage):
        async def conversation(self, id: Any, context: Any) -> Any:
            raise RuntimeError("read exploded")

    session = make_session(Boom())

    async def change(tx: Any) -> str:
        # 这条测试要的就是「回调**吞掉**了读错误」，所以这里必须 try/except/pass。
        try:  # noqa: SIM105
            await tx.conversation(1)
        except RuntimeError:
            pass
        return "pretended success"

    with pytest.raises(SessionFailed):
        await session.commit(change, CTX)
    assert session.failure is not None


async def test_storage_commit_failure_fails_the_session() -> None:
    class Boom(MemoryStorage):
        async def commit(self, writes: Any, context: Any) -> Any:
            raise RuntimeError("commit exploded")

    session = make_session(Boom())

    async def change(tx: Any) -> None:
        await tx.create_root_conversation()

    with pytest.raises(RuntimeError, match="commit exploded"):
        await session.commit(change, CTX)
    assert session.failure is not None


async def test_adoption_failure_fails_the_session() -> None:
    """adoption 失败意味着**内存落在持久状态后面**——必须让 Session 失败（``session.ts:734-739``）。"""
    session = make_session()
    await seed_root(session)

    from pi_durable.session.transaction import Transaction

    original_adopt = Transaction.adopt  # ⚠️ 必须**保存**原方法：`del Transaction.adopt`
    # 会把它从类上永久删掉，污染后面所有用例。

    def exploding_adopt(self: Any, seq: int) -> Any:
        raise RuntimeError("adopt exploded")

    Transaction.adopt = exploding_adopt  # type: ignore[method-assign]
    try:

        async def change(tx: Any) -> None:
            # 根会话已经由 seed_root 建过——这里只追加条目，好让链路真正走到 adopt
            await tx.append_entry(1, {"kind": "pi.user"})

        with pytest.raises(RuntimeError, match="adopt exploded"):
            await session.commit(change, CTX)
        assert session.failure is not None
    finally:
        Transaction.adopt = original_adopt  # type: ignore[method-assign]


async def test_aborted_context_is_rejected_before_the_callback_runs() -> None:
    session = make_session()
    ran: list[int] = []
    controller = AbortController()
    controller.abort()

    async def change(tx: Any) -> None:
        ran.append(1)

    from pi_chord.context import AbortError

    with pytest.raises(AbortError):
        await session.commit(change, with_abort_signal(controller.signal, CTX))
    assert ran == []


# --------------------------------------------------------------------------------------
# 提交发布
# --------------------------------------------------------------------------------------


async def test_unsubscribing_stops_publication() -> None:
    session = make_session()
    published: list[Any] = []
    unsubscribe = session.subscribe_commits(lambda pub, ctx: published.append(pub))

    async def change(tx: Any) -> None:
        await tx.create_root_conversation()

    async def append(tx: Any) -> None:
        await tx.append_entry(1, {"kind": "pi.user"})

    await session.commit(change, CTX)
    assert len(published) == 1
    unsubscribe()
    await session.commit(append, CTX)
    assert len(published) == 1


async def test_a_throwing_commit_listener_neither_fails_the_commit_nor_skips_others() -> None:
    """提交已经持久化了：监听器失败**既不让提交失败，也不跳过其它监听器**（``session.ts:761-775``）。"""
    session = make_session()
    seen: list[str] = []
    reported: list[BaseException] = []
    session.report = reported.append  # type: ignore[method-assign]
    session.subscribe_commits(
        lambda pub, ctx: (_ for _ in ()).throw(RuntimeError("listener exploded"))
    )
    session.subscribe_commits(lambda pub, ctx: seen.append("second"))

    async def change(tx: Any) -> str:
        await tx.create_root_conversation()
        return "ok"

    assert await session.commit(change, CTX) == "ok"
    assert seen == ["second"]  # 第二个监听器照跑
    assert len(reported) == 1
    assert session.failure is None  # 提交没失败


async def test_a_throwing_internal_listener_fails_the_session() -> None:
    """内部监听器（``observe_commits``）的状态跟着提交走，所以抛出**让 Session 失败**。

    ⚠️ 但**那一次提交照常返回它的结果**——上游 ``fail()`` 只记录失败、不抛
    （``session.ts:210-218``），而 ``#publish`` 跑在存储提交**之后**。
    也就是说：提交已经持久化了，内部监听器的失败只是**把 Session 标记为失败**。
    这个区分很重要——「提交成功」与「Session 仍健康」是两件事。
    """
    session = make_session()
    session.observe_commits(
        lambda pub, ctx: (_ for _ in ()).throw(RuntimeError("internal exploded"))
    )

    async def change(tx: Any) -> str:
        await tx.create_root_conversation()
        return "committed"

    assert await session.commit(change, CTX) == "committed"  # 提交本身成功
    assert session.failure is not None  # 但 Session 已经失败了
    assert isinstance(session.failure["error"], RuntimeError)
    end = await session.closed
    assert end["reason"] == "failed"


# --------------------------------------------------------------------------------------
# 关闭
# --------------------------------------------------------------------------------------


async def test_close_listener_runs_on_close() -> None:
    session = make_session()
    closed: list[int] = []
    session.subscribe_close(lambda: closed.append(1))
    await session.close(CTX)
    assert closed == [1]
    assert await session.closed == {"reason": "closed"}


async def test_close_listener_runs_on_failure_too() -> None:
    session = make_session()
    closed: list[int] = []
    session.subscribe_close(lambda: closed.append(1))
    session.fail(RuntimeError("died"))
    assert closed == [1]
    end = await session.closed
    assert end["reason"] == "failed"


async def test_a_throwing_close_listener_is_reported_and_the_rest_still_run() -> None:
    session = make_session()
    reported: list[BaseException] = []
    session.report = reported.append  # type: ignore[method-assign]
    session.subscribe_close(lambda: (_ for _ in ()).throw(RuntimeError("close listener exploded")))
    session.subscribe_close(lambda: reported.append(ValueError("second ran")))  # type: ignore[arg-type]

    await session.close(CTX)
    assert len(reported) == 2


# --------------------------------------------------------------------------------------
# 与守卫的交互
# --------------------------------------------------------------------------------------


async def test_commit_after_close_is_rejected() -> None:
    session = make_session()
    await session.close(CTX)
    with pytest.raises(RuntimeError, match="Session is closed"):
        await session.commit(lambda tx: None, CTX)


async def test_commit_after_failure_rejects_with_session_failed() -> None:
    session = make_session()
    session.fail(RuntimeError("died"))
    with pytest.raises(SessionFailed):
        await session.commit(lambda tx: None, CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_commit_end_to_end_with_documents(backend: str, tmp_path: Path) -> None:
    from pi_durable.documents import define_doc

    storage = (
        MemoryStorage()
        if backend == "memory"
        else await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    )
    session = make_session(storage)
    try:
        token = define_doc(
            {
                "kind": "pi.notes",
                "version": 1,
                "scope": "conversation",
                "history": "rewindable",
                "fork": "asOf",
                "initial": lambda seed: {"items": [], "n": 0},
            }
        )
        published: list[Any] = []
        session.subscribe_commits(lambda pub, ctx: published.append(pub))

        async def create(tx: Any) -> None:
            await tx.create_root_conversation()
            draft = await tx.doc(token, 1)
            draft["items"].append("a")

        async def update(tx: Any) -> None:
            draft = await tx.doc(token, 1)
            draft["n"] = 5

        await session.commit(create, CTX)
        await session.commit(update, CTX)

        assert [p["seq"] for p in published] == [1, 2]
        docs = [c for c in published[1]["changes"] if c["type"] == "document"]
        assert docs[0]["value"] == {"items": ["a"], "n": 5}
        assert docs[0]["ops"]  # 第二次是增量
    finally:
        await session.close(CTX)
