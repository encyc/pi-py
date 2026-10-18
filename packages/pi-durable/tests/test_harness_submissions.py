"""``harness/submissions.py``(241) 的测试 —— **SCC 的最后一块**。

⚠️ 本片是 SCC 里**唯一不碰 `TaskRuntime`** 的一块：``admit_submission`` 只收 ``tx``，
``Submissions`` 只收 ``SessionImpl`` + ``Storage``——所以**两者都能完整验收**。
（唯一要到 `generation.py` **Slice B** 的是「空闲输入落地后启动运行」那条分支，
它走延迟导入；本文件用 monkeypatch 替掉它来测**准入决策**，**端到端排到 Slice B**。）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.errors import ConversationBusy
from pi_durable.harness.inbox import InboxDoc
from pi_durable.harness.live import LiveDoc
from pi_durable.harness.submissions import (
    Submissions,
    admit_submission,
    check_input_entry,
    is_settled,
)
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage

ONE_AT_A_TIME: dict[str, Any] = {"steering": "one-at-a-time", "followUp": "one-at-a-time"}


async def make_session(backend: str, tmp_path: Path) -> SessionImpl:
    storage = (
        MemoryStorage()
        if backend == "memory"
        else await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    )
    session = SessionImpl(storage)
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    return session


def input_draft(text: str = "hi", **over: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "type": "input",
        "content": {"parts": [{"type": "text", "text": text}]},
    }
    draft.update(over)
    return draft


def write_draft(entry: dict[str, Any]) -> dict[str, Any]:
    return {"type": "write", "entry": entry}


@pytest.fixture
def no_start_run(monkeypatch: pytest.MonkeyPatch) -> list[list[int]]:
    """替掉 ``start_run``，好单测**准入决策**。

    ⚠️ 测的是**准入**（谁排队、谁落地、谁被拒），**不是**「运行真的起来了」——
    后者要 `generation.py` 的 Slice B，端到端测试排在那里。
    """
    started: list[list[int]] = []

    async def fake_start_run(tx: Any, conversation_id: int, live: Any, users: list[int]) -> None:
        started.append(list(users))

    import pi_durable.harness.generation as generation

    monkeypatch.setattr(generation, "start_run", fake_start_run, raising=False)
    return started


# --------------------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------------------


def test_is_settled_covers_only_done_and_unanswered() -> None:
    assert is_settled({"status": "done"}) is True
    assert is_settled({"status": "unanswered"}) is True
    assert is_settled({"status": "queued"}) is False
    assert is_settled({"status": "placed"}) is False


@pytest.mark.parametrize("kind", ["", "pi.user", "pi.anything"])
def test_check_input_entry_rejects_reserved_or_empty_kinds(kind: str) -> None:
    """``pi.`` 前缀是 Harness 内建种类的**保留区**——无类型调用方不能占用它。"""
    with pytest.raises(TypeError):
        check_input_entry({"kind": kind})


def test_check_input_entry_rejects_a_non_string_kind() -> None:
    with pytest.raises(TypeError) as caught:
        check_input_entry({"kind": 5})
    assert "got 5" in str(caught.value)


def test_check_input_entry_uses_js_string_conversion() -> None:
    """⚠️ 错误里的 ``got ${String(kind)}`` 是 **JS 的 ``String()`` 语义**：
    ``str(True)`` 在 Python 是 ``"True"``、JS 是 ``"true"``。照 JS 转，
    否则后来的人拿上游报错文本搜不到。"""
    with pytest.raises(TypeError) as caught:
        check_input_entry({"kind": True})
    assert "got true" in str(caught.value)


def test_check_input_entry_accepts_a_plain_kind() -> None:
    check_input_entry({"kind": "app.note"})


# --------------------------------------------------------------------------------------
# admit_submission —— 空闲路径（真实 tx，内存 + SQLite）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_an_idle_input_places_its_entry_and_starts_a_run(
    backend: str, tmp_path: Path, no_start_run: list[list[int]]
) -> None:
    session = await make_session(backend, tmp_path)
    try:
        captured: list[int] = []

        async def change(tx: Any) -> None:
            captured.append(await admit_submission(tx, 1, input_draft(), 1.0, ONE_AT_A_TIME))

        await session.commit(change, CTX)
        record = await session._storage.submission(captured[0], CTX)  # noqa: SLF001
        assert record["status"] == "placed"
        assert record["type"] == "input"
        assert record["entry"] is not None
        assert no_start_run == [[captured[0]]]  # 启动了一次运行，带这个提交
    finally:
        await session.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_an_idle_write_appends_and_settles_done(
    backend: str, tmp_path: Path, no_start_run: list[list[int]]
) -> None:
    session = await make_session(backend, tmp_path)
    try:
        captured: list[int] = []

        async def change(tx: Any) -> None:
            captured.append(
                await admit_submission(
                    tx, 1, write_draft({"kind": "app.note", "data": {"x": 1}}), 1.0, ONE_AT_A_TIME
                )
            )

        await session.commit(change, CTX)
        record = await session._storage.submission(captured[0], CTX)  # noqa: SLF001
        assert record["status"] == "done"
        assert "entry" in record
        assert no_start_run == []  # 写入不启动运行
    finally:
        await session.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_a_display_only_write_settles_done_even_when_busy(
    backend: str, tmp_path: Path, no_start_run: list[list[int]]
) -> None:
    """⚠️ **纯展示写入**（只有 ``kind`` 与 ``data``）**忙不忙都立刻落地**——
    它不改变模型看得见的东西，所以没有理由排队。"""
    session = await make_session(backend, tmp_path)
    try:

        async def busy(tx: Any) -> None:
            live = await tx.doc(LiveDoc, 1)
            live["run"] = {"taskId": 9, "inputs": []}

        await session.commit(busy, CTX)

        captured: list[int] = []

        async def change(tx: Any) -> None:
            captured.append(
                await admit_submission(
                    tx, 1, write_draft({"kind": "app.note", "data": {"x": 1}}), 1.0, ONE_AT_A_TIME
                )
            )

        await session.commit(change, CTX)
        record = await session._storage.submission(captured[0], CTX)  # noqa: SLF001
        assert record["status"] == "done"  # 没有排队
    finally:
        await session.close(CTX)


async def test_a_display_only_write_queues_behind_a_queued_reset(
    no_start_run: list[list[int]],
) -> None:
    """⚠️ **唯一例外**：排在**已排队的 reset** 之后时要**排队**。

    那样它会落在 reset **之后**——而不是在「reset 结束的那段上下文」**之前**。
    这条规则很容易被当成多余的分支删掉，但删掉它会让展示写入**跑到 reset 前面去**。

    ⚠️ 这里让对话**忙碌**：那样 `admit_submission` **不跑边界**，
    于是我们能干净地观察到「它进了队」而不是「它立刻 done」。
    （空闲时边界会**消费**掉这条队列，观察不到中间态。）
    """
    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:

        async def seed(tx: Any) -> None:
            live = await tx.doc(LiveDoc, 1)
            live["run"] = {"taskId": 9, "inputs": []}
            draft = await tx.doc(InboxDoc, 1)
            # ⚠️ `has_queued_reset` 的判据是 **`entry.head == "self"`**——
            # 只看 `kind == "pi.reset"` 是不够的（那是**已落地**的 reset 的形状）。
            draft["items"] = [
                {"id": 900, "mode": "write", "entry": {"kind": "pi.reset", "head": "self"}}
            ]

        await session.commit(seed, CTX)

        captured: list[int] = []

        async def change(tx: Any) -> None:
            captured.append(
                await admit_submission(
                    tx, 1, write_draft({"kind": "app.note", "data": {}}), 1.0, ONE_AT_A_TIME
                )
            )

        await session.commit(change, CTX)

        items = (await session.snapshot(InboxDoc, 1, CTX))["items"]
        assert [item["id"] for item in items] == [900, captured[0]]  # 排到 reset **后面**
        record = await session._storage.submission(captured[0], CTX)  # noqa: SLF001
        assert record["status"] == "queued"  # **没有**立刻 done
    finally:
        await session.close(CTX)


async def test_a_busy_input_queues_in_the_inbox(
    no_start_run: list[list[int]],
) -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:

        async def busy(tx: Any) -> None:
            live = await tx.doc(LiveDoc, 1)
            live["run"] = {"taskId": 9, "inputs": []}

        await session.commit(busy, CTX)

        captured: list[int] = []

        async def change(tx: Any) -> None:
            captured.append(await admit_submission(tx, 1, input_draft(), 1.0, ONE_AT_A_TIME))

        await session.commit(change, CTX)
        record = await session._storage.submission(captured[0], CTX)  # noqa: SLF001
        assert record["status"] == "queued"

        items = (await session.snapshot(InboxDoc, 1, CTX))["items"]
        assert [item["id"] for item in items] == [captured[0]]
        assert items[0]["mode"] == "followUp"
    finally:
        await session.close(CTX)


async def test_a_busy_steer_is_marked_as_steer(
    no_start_run: list[list[int]],
) -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:

        async def busy(tx: Any) -> None:
            live = await tx.doc(LiveDoc, 1)
            live["run"] = {"taskId": 9, "inputs": []}

        await session.commit(busy, CTX)

        async def change(tx: Any) -> None:
            await admit_submission(tx, 1, input_draft(whenBusy="steer"), 1.0, ONE_AT_A_TIME)

        await session.commit(change, CTX)
        items = (await session.snapshot(InboxDoc, 1, CTX))["items"]
        assert items[0]["mode"] == "steer"
    finally:
        await session.close(CTX)


async def test_a_rejected_busy_input_raises_conversation_busy(
    no_start_run: list[list[int]],
) -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:

        async def busy(tx: Any) -> None:
            live = await tx.doc(LiveDoc, 1)
            live["run"] = {"taskId": 9, "inputs": []}

        await session.commit(busy, CTX)

        async def change(tx: Any) -> None:
            with pytest.raises(ConversationBusy):
                await admit_submission(tx, 1, input_draft(whenBusy="reject"), 1.0, ONE_AT_A_TIME)

        await session.commit(change, CTX)
    finally:
        await session.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_a_known_request_id_is_idempotent(
    backend: str, tmp_path: Path, no_start_run: list[list[int]]
) -> None:
    """⚠️ **已知的请求 ID 直接返回它已有的提交且不写任何东西**——这是幂等重试的根据。"""
    session = await make_session(backend, tmp_path)
    try:
        # ⚠️ **两次准入必须在不同的提交里**：`submissionByRequest()` 是**读表**，
        # 同一次提交里第一次写之后就 `ReadAfterWrite` 了。
        # 幂等本来就是为**重试**设计的——那是两次提交，不是一次里的两次调用。
        first = await session.commit(
            lambda tx: admit_submission(tx, 1, input_draft(requestId="r1"), 1.0, ONE_AT_A_TIME),
            CTX,
        )
        second = await session.commit(
            lambda tx: admit_submission(
                tx, 1, input_draft("again", requestId="r1"), 1.0, ONE_AT_A_TIME
            ),
            CTX,
        )
        assert first == second
    finally:
        await session.close(CTX)


async def test_a_request_id_reused_for_another_type_is_rejected(
    no_start_run: list[list[int]],
) -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:
        await session.commit(
            lambda tx: admit_submission(tx, 1, input_draft(requestId="r1"), 1.0, ONE_AT_A_TIME),
            CTX,
        )

        async def change(tx: Any) -> None:
            with pytest.raises(RuntimeError, match="already identifies a submission of type input"):
                await admit_submission(
                    tx,
                    1,
                    write_draft({"kind": "app.note", "data": {}}) | {"requestId": "r1"},
                    1.0,
                    ONE_AT_A_TIME,
                )

        await session.commit(change, CTX)
    finally:
        await session.close(CTX)


async def test_an_input_entry_kind_is_checked_before_anything_is_written(
    no_start_run: list[list[int]],
) -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:

        async def change(tx: Any) -> None:
            with pytest.raises(TypeError):
                await admit_submission(
                    tx, 1, input_draft(entry={"kind": "pi.user"}), 1.0, ONE_AT_A_TIME
                )

        await session.commit(change, CTX)
        entries = await session._storage.scan_entries({"conversationId": 1}, 50, None, CTX)  # noqa: SLF001
        assert entries["items"] == []  # 校验在读/写之前
    finally:
        await session.close(CTX)


# --------------------------------------------------------------------------------------
# Submissions 类
# --------------------------------------------------------------------------------------


def make_submissions(session: SessionImpl) -> Submissions:
    return Submissions(
        session,
        session._storage,  # noqa: SLF001
        lambda: 1.0,
        lambda: ONE_AT_A_TIME,
        lambda: None,
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_submit_returns_a_handle_that_reports_status(
    backend: str, tmp_path: Path, no_start_run: list[list[int]]
) -> None:
    session = await make_session(backend, tmp_path)
    try:
        submissions = make_submissions(session)
        handle = await submissions.submit(1, input_draft(), CTX)
        record = await handle.status(CTX)
        assert record["id"] == handle.id
        assert record["status"] == "placed"
    finally:
        await session.close(CTX)


async def test_abort_withdraws_a_queued_submission(
    no_start_run: list[list[int]],
) -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:

        async def busy(tx: Any) -> None:
            live = await tx.doc(LiveDoc, 1)
            live["run"] = {"taskId": 9, "inputs": []}

        await session.commit(busy, CTX)
        submissions = make_submissions(session)

        # 在总线里准入，好让撤回看到它
        id = await session.commit(
            lambda tx: admit_submission(tx, 1, input_draft(), 1.0, ONE_AT_A_TIME), CTX
        )
        result = await submissions.abort(id, CTX)
        assert result == "aborted"

        record = await submissions.status(id, CTX)
        assert record["status"] == "unanswered"
        assert record["reason"] == "aborted"
        # 收件箱项也移除了
        items = (await session.snapshot(InboxDoc, 1, CTX)).get("items") or []
        assert [item["id"] for item in items] == []
    finally:
        await session.close(CTX)


async def test_abort_reports_a_placed_submission_honestly(
    no_start_run: list[list[int]],
) -> None:
    """⚠️ 已落地的输入**撤回不了**——如实报告 ``already_placed``，**不假装成功**。"""
    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:
        submissions = make_submissions(session)
        handle = await submissions.submit(1, input_draft(), CTX)
        assert await submissions.abort(handle.id, CTX) == "already_placed"
    finally:
        await session.close(CTX)


async def test_abort_reports_settled_and_not_found(
    no_start_run: list[list[int]],
) -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:
        submissions = make_submissions(session)
        id = await session.commit(
            lambda tx: admit_submission(
                tx, 1, write_draft({"kind": "app.note", "data": {}}), 1.0, ONE_AT_A_TIME
            ),
            CTX,
        )
        assert await submissions.abort(id, CTX) == "settled"
        assert await submissions.abort(999, CTX) == "not_found"
    finally:
        await session.close(CTX)


async def test_abort_honours_the_conversation_scope(
    no_start_run: list[list[int]],
) -> None:
    """给了 ``conversation_id`` 时，**别的对话的提交一律 not_found**。"""
    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:
        submissions = make_submissions(session)
        handle = await submissions.submit(1, input_draft(), CTX)
        assert await submissions.abort(handle.id, CTX, conversation_id=999) == "not_found"
    finally:
        await session.close(CTX)


async def test_get_is_none_for_a_missing_submission(
    no_start_run: list[list[int]],
) -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:
        assert await make_submissions(session).get(999, CTX) is None
    finally:
        await session.close(CTX)


async def test_status_throws_for_a_missing_submission() -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:
        with pytest.raises(RuntimeError, match="Submission 999 does not exist"):
            await make_submissions(session).status(999, CTX)
    finally:
        await session.close(CTX)


async def test_handle_abort_throws_for_a_missing_submission(
    no_start_run: list[list[int]],
) -> None:
    """句柄的 ``abort()`` 把 ``not_found`` 转成**抛错**——调用方拿到的句柄一定存在。"""
    from pi_durable.harness.submissions import SubmissionHandle

    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:
        with pytest.raises(RuntimeError, match="Submission 999 does not exist"):
            await SubmissionHandle(999, make_submissions(session)).abort(CTX)
    finally:
        await session.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_wait_returns_at_once_for_a_settled_submission(
    backend: str, tmp_path: Path, no_start_run: list[list[int]]
) -> None:
    session = await make_session(backend, tmp_path)
    try:
        submissions = make_submissions(session)
        id = await session.commit(
            lambda tx: admit_submission(
                tx, 1, write_draft({"kind": "app.note", "data": {}}), 1.0, ONE_AT_A_TIME
            ),
            CTX,
        )
        settled = await submissions.wait(id, CTX)
        assert settled["status"] == "done"
    finally:
        await session.close(CTX)


async def test_wait_is_woken_by_a_settling_publication(
    no_start_run: list[list[int]],
) -> None:
    """⚠️ **查与登记在同一条会话线上完成**：否则一次结算发布可能正好落在两者之间，
    于是这个等待**永远**不会被唤醒。

    这里让 ``abort()`` 去结算它——走的是 ``#observe`` 那条唤醒路径。
    """
    import asyncio

    session = await make_session("memory", None)  # type: ignore[arg-type]
    try:

        async def busy(tx: Any) -> None:
            live = await tx.doc(LiveDoc, 1)
            live["run"] = {"taskId": 9, "inputs": []}

        await session.commit(busy, CTX)
        submissions = make_submissions(session)
        id = await session.commit(
            lambda tx: admit_submission(tx, 1, input_draft(), 1.0, ONE_AT_A_TIME), CTX
        )

        waiting = asyncio.ensure_future(submissions.wait(id, CTX))
        await asyncio.sleep(0)
        assert not waiting.done()

        assert await submissions.abort(id, CTX) == "aborted"
        record = await asyncio.wait_for(waiting, 2)
        assert record["status"] == "unanswered"
        assert record["reason"] == "aborted"
    finally:
        await session.close(CTX)


async def test_closing_the_session_rejects_registered_waiters(
    no_start_run: list[list[int]],
) -> None:
    import asyncio

    session = await make_session("memory", None)  # type: ignore[arg-type]

    async def busy(tx: Any) -> None:
        live = await tx.doc(LiveDoc, 1)
        live["run"] = {"taskId": 9, "inputs": []}

    await session.commit(busy, CTX)
    submissions = make_submissions(session)
    id = await session.commit(
        lambda tx: admit_submission(tx, 1, input_draft(), 1.0, ONE_AT_A_TIME), CTX
    )

    waiting = asyncio.ensure_future(submissions.wait(id, CTX))
    await asyncio.sleep(0)

    await session.close(CTX)
    with pytest.raises(Exception, match="closed"):
        await asyncio.wait_for(waiting, 2)
