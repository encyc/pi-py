"""``harness/generation.py`` 的 **Slice A**（叶子层）测试。

⚠️ 按**可测性**切：``GenerationTask`` 的 phase 处理器都收 ``TaskRuntime``，
而它的实现在 `scheduler.py`。本片只测不碰它的部分。

⚠️ **本片最要紧的验证**：``convert_partial`` 是 `live.settle_scheduler_outcome`
那条**环上回边**的另一端——`tests/test_harness_live.py` 里有它的端到端测试。
"""

from __future__ import annotations

from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.harness.generation import (
    DEFAULT_POLL_AFTER_MS,
    append_assistant,
    compaction_failure,
    convert_partial,
    hand_over,
    threshold_compaction,
)
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage


async def make_session(backend: str, tmp_path: Any) -> SessionImpl:
    storage = (
        MemoryStorage()
        if backend == "memory"
        else await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    )
    session = SessionImpl(storage)
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    return session


def view(contributions: list[list[dict[str, Any]]], **kwargs: Any) -> Any:
    return {
        "head": kwargs.get("head"),
        "entries": kwargs.get("entries", [{"id": i + 1} for i in range(len(contributions))]),
        "contributions": contributions,
        "messages": [m for b in contributions for m in b],
    }


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}], "stop_reason": "stop"}


def policy(**over: Any) -> dict[str, Any]:
    base = {"enabled": True, "reserveTokens": 100, "keepRecentTokens": 1, "backgroundTokens": 50}
    base.update(over)
    return base


def test_default_poll_after() -> None:
    assert DEFAULT_POLL_AFTER_MS == 5000


# --------------------------------------------------------------------------------------
# threshold_compaction
# --------------------------------------------------------------------------------------


def test_a_disabled_policy_never_compacts() -> None:
    contributions = [[user("x" * 5000)], [assistant("y" * 5000)]]
    assert threshold_compaction(view(contributions), [], 10_000, policy(enabled=False)) is None


def test_a_non_positive_context_window_never_compacts() -> None:
    contributions = [[user("x" * 5000)], [assistant("y" * 5000)]]
    assert threshold_compaction(view(contributions), [], 0, policy()) is None
    assert threshold_compaction(view(contributions), [], -1, policy()) is None


def test_no_compaction_below_every_threshold() -> None:
    contributions = [[user("small")], [assistant("small")]]
    assert threshold_compaction(view(contributions), [], 1_000_000, policy()) is None


def test_blocking_when_over_the_reserve_boundary() -> None:
    contributions = [[user("x" * 2000)], [assistant("y" * 2000)]]
    over = threshold_compaction(view(contributions), [], 500, policy(reserveTokens=100))
    assert over == "blocking"


def test_background_when_between_the_two_thresholds() -> None:
    contributions = [[user("x" * 2000)], [assistant("y" * 2000)]]
    over = threshold_compaction(
        view(contributions), [], 5000, policy(reserveTokens=100, backgroundTokens=4900)
    )
    assert over == "background"


def test_background_disabled_when_the_gap_is_zero() -> None:
    """``backgroundTokens == 0`` 表示**关掉**后台压缩——只留阻塞那条。"""
    contributions = [[user("x" * 2000)], [assistant("y" * 2000)]]
    over = threshold_compaction(
        view(contributions), [], 5000, policy(reserveTokens=100, backgroundTokens=0)
    )
    assert over is None


def test_no_compaction_when_no_cut_can_be_found() -> None:
    """⚠️ **第二个独立前置条件**：即使超了阈值，若 ``select_cut()`` **找不到切点**
    （没有东西可压），也**不用**压缩。

    少了这条会启动一个注定什么都不做的压缩任务——**白花一次模型调用**。
    构造：全部内容都在 ``keepRecentTokens`` 之内 → 尾部无法留够 → 找不到切点。
    """
    contributions = [[user("x")], [assistant("y")]]
    assert threshold_compaction(view(contributions), [], 1, policy(keepRecentTokens=10**9)) is None


def test_planned_entry_messages_count_toward_the_size() -> None:
    """``planned`` 是**本次准备要写入**的条目（比如 system 提示），它们也要算进大小。"""
    contributions = [[user("x" * 100)], [assistant("y" * 100)]]
    base = threshold_compaction(view(contributions), [], 1_000_000, policy())
    planned = [{"model": [{"role": "user", "content": [{"type": "text", "text": "z" * 5000}]}]}]
    assert base is None
    assert (
        threshold_compaction(view(contributions), planned, 200, policy(reserveTokens=100))
        == "blocking"
    )


# --------------------------------------------------------------------------------------
# compaction_failure
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "phrase"),
    [
        ({"status": "failed", "error": {"message": "boom"}}, "Compaction failed: boom"),
        ({"status": "faulted", "error": {"message": "boom"}}, "Compaction failed: boom"),
        ({"status": "orphaned", "reason": "owner ended"}, "Compaction failed: owner ended"),
        ({"status": "aborted"}, "Compaction was aborted"),
    ],
)
def test_compaction_failure_covers_all_four_non_completed_statuses(
    outcome: dict[str, Any], phrase: str
) -> None:
    """⚠️ 上游的 ``switch`` 在 ``Exclude<..., "completed">`` 上是**穷尽**的——
    四种状态都有对应文案，**别把 ``aborted`` 漏成默认分支**。"""
    assert compaction_failure(outcome) == phrase


# --------------------------------------------------------------------------------------
# hand_over
# --------------------------------------------------------------------------------------


def test_hand_over_moves_the_run_only_from_the_current_holder() -> None:
    live: dict[str, Any] = {"run": {"taskId": 5, "inputs": [1, 2]}}
    hand_over(live, 5, 9)
    assert live["run"]["taskId"] == 9
    assert live["run"]["inputs"] == [1, 2]  # **输入跟着一起走**


def test_hand_over_ignores_a_stale_holder() -> None:
    """⚠️ 只有当前持有者确实是 ``from`` 时才改——否则**一个过期的移交会把别人的运行抢走**。"""
    live: dict[str, Any] = {"run": {"taskId": 5, "inputs": []}}
    hand_over(live, 999, 9)
    assert live["run"]["taskId"] == 5


def test_hand_over_without_a_run_is_a_noop() -> None:
    live: dict[str, Any] = {}
    hand_over(live, 1, 2)
    assert live == {}


# --------------------------------------------------------------------------------------
# append_assistant（真实 tx，内存 + SQLite）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_append_assistant_records_usage_under_the_model_key(
    backend: str, tmp_path: Any
) -> None:
    """用量按 **``models``** 归类、键是 ``provider/model``——与工具的 ``tools`` 那条**分开**。"""
    from pi_durable.harness.usage import UsageDoc

    session = await make_session(backend, tmp_path)
    try:
        message = {
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "provider": "anthropic",
            "model": "claude",
            "stop_reason": "stop",
            "timestamp": 1,
            "usage": {"input": 5, "output": 7, "total_tokens": 12},
        }

        async def change(tx: Any) -> None:
            await append_assistant(tx, 1, message)

        await session.commit(change, CTX)
        usage = await session.snapshot(UsageDoc, 1, CTX)
        assert usage["models"]["anthropic/claude"]["input"] == 5

        entries = await session._storage.scan_entries({"conversationId": 1}, 50, None, CTX)  # noqa: SLF001
        kinds = [e["kind"] for e in entries["items"]]
        assert "pi.assistant" in kinds
    finally:
        await session.close(CTX)


# --------------------------------------------------------------------------------------
# convert_partial —— 环上回边的另一端
# --------------------------------------------------------------------------------------


async def test_convert_partial_is_a_noop_without_a_partial() -> None:
    session = await make_session("memory", None)  # type: ignore[arg-type]

    async def change(tx: Any) -> None:
        live = await tx.doc(__import__("pi_durable.harness.live", fromlist=["LiveDoc"]).LiveDoc, 1)
        await convert_partial(tx, live, 1)

    await session.commit(change, CTX)
    entries = await session._storage.scan_entries({"conversationId": 1}, 50, None, CTX)  # noqa: SLF001
    assert [e["kind"] for e in entries["items"]] == []


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_convert_partial_appends_an_aborted_assistant_entry(
    backend: str, tmp_path: Any
) -> None:
    """⚠️ **它让 transcript 保留模型产出过的东西、``pi.usage`` 也计入它的花费。**

    丢了它，一次崩溃就会让用户**白花一笔钱**且**看不到半句输出**。

    并且它把 ``stop_reason`` 改成 ``"aborted"``：那条回答**没有正常结束**，
    模型不该把它当成一次完整的回复来接着写。
    """
    from pi_durable.harness.live import LiveDoc

    session = await make_session(backend, tmp_path)
    try:
        chunk = {
            "role": "assistant",
            "content": [{"type": "text", "text": "half an answer"}],
            "provider": "p",
            "model": "m",
            "stop_reason": "stop",
            "timestamp": 1,
        }

        async def seed(tx: Any) -> None:
            live = await tx.doc(LiveDoc, 1)
            live["generation"] = {"attempt": 1, "message": chunk}

        await session.commit(seed, CTX)

        async def change(tx: Any) -> None:
            live = await tx.doc(LiveDoc, 1)
            await convert_partial(tx, live, 1)

        await session.commit(change, CTX)

        entries = await session._storage.scan_entries({"conversationId": 1}, 50, None, CTX)  # noqa: SLF001
        assistants = [e for e in entries["items"] if e["kind"] == "pi.assistant"]
        assert len(assistants) == 1
        assert assistants[0]["model"][0]["stop_reason"] == "aborted"
        assert assistants[0]["model"][0]["content"][0]["text"] == "half an answer"
    finally:
        await session.close(CTX)
