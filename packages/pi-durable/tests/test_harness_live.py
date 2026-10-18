"""``harness/live.py`` 的测试 —— 移植 ``harness/live.ts``（268 行），**SCC-1**。

``pi.live`` 是 UI 看的那一面：当前 generation 尝试、工具槽位、嵌套调用、正在跑的压缩。
本文件覆盖它的全部**纯函数**，以及 ``settle_scheduler_outcome`` 的**工具/压缩两条分支**
（真实 Session，内存 + SQLite）。

⚠️ **SCC-1 当时的缺口已经补上**（本条留档）：``settle_scheduler_outcome`` 的「运行任务」
分支要 ``generation.convert_partial``，而那是 SCC 的另一块。当时留了延迟导入 +
``type: ignore`` 与一句「运行任务分支测不了」。**`generation.py` 的 Slice A 一到，
``type: ignore`` 已删除、该分支的测试已补齐**（见文件末尾）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.harness.live import (
    LiveDoc,
    add_compaction_status,
    clear_progress,
    compaction_status,
    end_run,
    fail_compaction_status,
    finish_slot,
    remove_compaction_status,
    remove_nested_slots,
    settle_scheduler_outcome,
    tool_slot,
)
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage


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
# 文档定义
# --------------------------------------------------------------------------------------


def test_live_doc_semantics() -> None:
    definition = LiveDoc.definition
    assert definition["kind"] == "pi.live"
    assert definition["scope"] == "conversation"
    assert definition["history"] == "latest"
    assert definition["fork"] == "initial"  # 运行中状态不该被分叉复制
    assert definition["initial"](None) == {}


def test_checkpoint_when_writes_a_base_only_when_nothing_runs() -> None:
    """**spec §8.2**：什么都不跑的时候写完整 base。

    ⚠️ 上游的 REMINDER 明说**不要加增量计数上限**——工具输出基准测试就是查这条规则。
    """
    predicate = LiveDoc.definition["checkpointWhen"]

    assert predicate({}, [], {"deltasSinceBase": 99}) is True  # 空闲
    assert predicate({"generation": {"attempt": 1}}, [], {"deltasSinceBase": 0}) is False
    assert predicate({"tools": [{"status": "running"}]}, [], {"deltasSinceBase": 0}) is False
    assert predicate({"tools": [{"status": "done"}]}, [], {"deltasSinceBase": 5}) is True
    assert predicate({"nestedTools": [{"status": "running"}]}, [], {"deltasSinceBase": 0}) is False
    # **没有**增量上限：不管攒了多少增量，空闲就是 base
    assert predicate({}, [], {"deltasSinceBase": 10_000}) is True


# --------------------------------------------------------------------------------------
# end_run
# --------------------------------------------------------------------------------------


def test_end_run_settles_inputs_and_clears_presentation() -> None:
    settled: list[tuple[Any, Any]] = []

    class Tx:
        def settle_submission(self, id: Any, settlement: Any) -> None:
            settled.append((id, settlement))

    live: dict[str, Any] = {
        "run": {"taskId": 7, "inputs": [1, 2]},
        "generation": {"attempt": 1},
        "tools": [{"taskId": 5}],
        "nestedTools": [{"taskId": 6}],
    }
    end_run(Tx(), live, 7, {"status": "unanswered", "reason": "aborted"})

    assert settled == [
        (1, {"status": "unanswered", "reason": "aborted"}),
        (2, {"status": "unanswered", "reason": "aborted"}),
    ]
    assert "run" not in live
    assert "generation" not in live
    assert "tools" not in live
    assert "nestedTools" not in live


def test_end_run_clears_presentation_even_when_the_task_does_not_own_the_run() -> None:
    """**总是**清掉 generation/tools/nestedTools——它们的呈现属于**正在结束的**那次运行。"""
    live: dict[str, Any] = {
        "run": {"taskId": 7, "inputs": [1]},
        "generation": {"attempt": 1},
    }
    end_run(Any, live, 999, {"status": "unanswered", "reason": "x"})  # type: ignore[arg-type]

    assert live["run"] == {"taskId": 7, "inputs": [1]}  # 不是它的运行，留着
    assert "generation" not in live  # 但呈现被清了


# --------------------------------------------------------------------------------------
# 压缩状态
# --------------------------------------------------------------------------------------


def test_compaction_statuses_stay_in_task_id_order() -> None:
    live: dict[str, Any] = {}
    add_compaction_status(live, {"taskId": 3, "reason": "manual", "blocking": False, "attempt": 1})
    add_compaction_status(
        live, {"taskId": 5, "reason": "threshold", "blocking": True, "attempt": 1}
    )

    assert [s["taskId"] for s in live["compactions"]] == [3, 5]
    assert compaction_status(live, 5)["reason"] == "threshold"
    assert compaction_status(live, 9) is None


def test_remove_compaction_status_drops_the_list_when_empty() -> None:
    live: dict[str, Any] = {}
    add_compaction_status(live, {"taskId": 3, "reason": "manual", "blocking": False, "attempt": 1})
    remove_compaction_status(live, 3)
    assert "compactions" not in live  # 列表空了就一并移除


def test_fail_compaction_status_records_the_failure_and_removes_the_status() -> None:
    live: dict[str, Any] = {}
    add_compaction_status(live, {"taskId": 3, "reason": "overflow", "blocking": True, "attempt": 2})
    fail_compaction_status(live, 3, "summarization failed")

    assert "compactions" not in live
    assert live["compactionFailure"] == {
        "taskId": 3,
        "reason": "overflow",
        "error": "summarization failed",
    }


def test_fail_compaction_status_for_an_unknown_task_records_nothing() -> None:
    live: dict[str, Any] = {}
    fail_compaction_status(live, 9, "boom")
    assert "compactionFailure" not in live


# --------------------------------------------------------------------------------------
# 槽位查找
# --------------------------------------------------------------------------------------


def test_tool_slot_finds_a_model_issued_slot() -> None:
    live: dict[str, Any] = {"tools": [{"taskId": 1}, {"taskId": 2}]}
    assert tool_slot(live, 2) == {"taskId": 2}
    assert tool_slot(live, 9) is None


def test_tool_slot_binary_searches_nested_slots() -> None:
    """嵌套槽位**按任务 ID 递增**，所以能二分——一个工具可能发起上千次嵌套调用。"""
    live: dict[str, Any] = {"nestedTools": [{"taskId": t} for t in (10, 20, 30, 40, 50)]}
    for wanted in (10, 20, 30, 40, 50):
        assert tool_slot(live, wanted) == {"taskId": wanted}
    assert tool_slot(live, 25) is None
    assert tool_slot(live, 5) is None
    assert tool_slot(live, 99) is None


def test_tool_slot_prefers_a_model_issued_slot() -> None:
    live: dict[str, Any] = {"tools": [{"taskId": 7}], "nestedTools": [{"taskId": 7}]}
    assert tool_slot(live, 7) is live["tools"][0]


def test_tool_slot_without_nested_slots_returns_none() -> None:
    assert tool_slot({}, 1) is None
    assert tool_slot({"tools": []}, 1) is None


# --------------------------------------------------------------------------------------
# finish / clear / remove
# --------------------------------------------------------------------------------------


def test_finish_slot_marks_done_and_clears_progress() -> None:
    """标记 done 并清掉运行中输出；调用方再设 ``entry``（模型发起）或 ``summary``（嵌套）。"""
    slot: dict[str, Any] = {
        "callId": "c1",
        "name": "read",
        "status": "running",
        "output": "partial",
        "droppedBytes": 3,
        "droppedLines": 1,
        "details": {"a": 1},
        "diagnostics": [{"severity": "warn"}],
    }
    finish_slot(slot)

    assert slot["status"] == "done"
    assert slot["callId"] == "c1"  # 身份字段留着
    assert slot["name"] == "read"
    for key in ("output", "droppedBytes", "droppedLines", "details", "diagnostics"):
        assert key not in slot


def test_finish_slot_does_not_set_an_entry() -> None:
    """工具任务**出错或被孤立**时 ``entry`` 缺失——由上下文派生合成那条结果。"""
    slot: dict[str, Any] = {"status": "running"}
    finish_slot(slot)
    assert "entry" not in slot


def test_clear_progress_only_touches_progress_fields() -> None:
    slot: dict[str, Any] = {"status": "running", "output": "x", "name": "read"}
    clear_progress(slot)
    assert slot == {"status": "running", "name": "read"}


def test_remove_nested_slots_is_transitive() -> None:
    """父在子之前，所以一遍扫描就能传递地移除。"""
    live: dict[str, Any] = {
        "nestedTools": [
            {"taskId": 1, "parentTaskId": 9},  # 直接子
            {"taskId": 2, "parentTaskId": 1},  # 孙
            {"taskId": 3, "parentTaskId": 8},  # 无关
        ]
    }
    remove_nested_slots(live, 9)
    assert [s["taskId"] for s in live["nestedTools"]] == [3]


def test_remove_nested_slots_drops_the_list_when_empty() -> None:
    live: dict[str, Any] = {"nestedTools": [{"taskId": 1, "parentTaskId": 9}]}
    remove_nested_slots(live, 9)
    assert "nestedTools" not in live


def test_remove_nested_slots_on_a_missing_list_is_a_noop() -> None:
    live: dict[str, Any] = {}
    remove_nested_slots(live, 1)
    assert live == {}


# --------------------------------------------------------------------------------------
# settle_scheduler_outcome（工具 / 压缩两条分支，接真实存储）
# --------------------------------------------------------------------------------------


async def seed_tool_slot(session: SessionImpl, task_id: int) -> None:
    async def change(tx: Any) -> None:
        state = await tx.doc(LiveDoc, 1)
        state["tools"] = [{"taskId": task_id, "status": "running", "output": "partial"}]
        state["nestedTools"] = [
            {"taskId": task_id + 100, "parentTaskId": task_id, "status": "running"},
            {"taskId": task_id + 200, "parentTaskId": 999, "status": "running"},
        ]

    await session.commit(change, CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_tool_task_fault_finishes_the_slot_without_an_entry(
    backend: str, tmp_path: Path
) -> None:
    session = await make_session(backend, tmp_path)
    try:
        await seed_tool_slot(session, 42)

        async def change(tx: Any) -> None:
            await settle_scheduler_outcome(
                tx,
                {"id": 42, "conversationId": 1, "kind": "pi.tool"},
                {"status": "faulted", "error": {"message": "boom"}},
            )

        await session.commit(change, CTX)
        live = await session.snapshot(LiveDoc, 1, CTX)

        slot = live["tools"][0]
        assert slot["status"] == "done"
        assert "entry" not in slot  # 上下文派生会合成那条缺失的结果
        assert "output" not in slot  # 运行中输出被清掉
        # 它自己的嵌套槽位被移除，别人的留着
        assert [s["taskId"] for s in live["nestedTools"]] == [242]
    finally:
        await session.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_compaction_fault_records_the_error_message(backend: str, tmp_path: Path) -> None:
    session = await make_session(backend, tmp_path)
    try:

        async def seed(tx: Any) -> None:
            state = await tx.doc(LiveDoc, 1)
            state["compactions"] = [
                {"taskId": 7, "reason": "overflow", "blocking": True, "attempt": 1}
            ]

        await session.commit(seed, CTX)

        async def change(tx: Any) -> None:
            await settle_scheduler_outcome(
                tx,
                {"id": 7, "conversationId": 1, "kind": "pi.compaction"},
                {"status": "faulted", "error": {"message": "provider refused"}},
            )

        await session.commit(change, CTX)
        live = await session.snapshot(LiveDoc, 1, CTX)

        assert "compactions" not in live
        assert live["compactionFailure"] == {
            "taskId": 7,
            "reason": "overflow",
            "error": "provider refused",
        }
    finally:
        await session.close(CTX)


async def test_compaction_orphan_records_the_reason() -> None:
    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]

    async def seed(tx: Any) -> None:
        state = await tx.doc(LiveDoc, 1)
        state["compactions"] = [{"taskId": 7, "reason": "manual", "blocking": False, "attempt": 1}]

    await session.commit(seed, CTX)

    async def change(tx: Any) -> None:
        await settle_scheduler_outcome(
            tx,
            {"id": 7, "conversationId": 1, "kind": "pi.compaction"},
            {"status": "orphaned", "reason": "owner ended"},
        )

    await session.commit(change, CTX)
    live = await session.snapshot(LiveDoc, 1, CTX)
    assert live["compactionFailure"]["error"] == "owner ended"


async def test_an_unrelated_task_kind_is_ignored() -> None:
    """**其它种类一律忽略**——所以它从不在此之外创建 ``pi.live``。"""
    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]

    async def change(tx: Any) -> None:
        await settle_scheduler_outcome(
            tx,
            {"id": 1, "conversationId": 1, "kind": "pi.other"},
            {"status": "faulted", "error": {"message": "x"}},
        )

    await session.commit(change, CTX)
    # 那个对话上**没有**创建 pi.live
    assert await session.snapshot(LiveDoc, 1, CTX) is None


# --------------------------------------------------------------------------------------
# SCC-1 留下的缺口：运行任务的终局清理（generation.convert_partial 到位后补齐）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_a_faulted_run_task_settles_its_inputs_and_converts_the_partial(
    backend: str, tmp_path: Path
) -> None:
    """⚠️ **这是 SCC-1 当时测不了的那条分支**（它要 `generation.convert_partial`）。

    它一次做三件事，缺一不可：

    1. 把已提交的**部分回答**作为一条 **aborted 助手条目**留下来
       ——丢了它，一次崩溃就让用户白花一笔钱且看不到半句输出；
    2. 把运行的**输入**结算成 ``unanswered``（``faulted`` 带上错误文本）；
    3. 移除 ``run`` 与那份呈现。
    """
    session = await make_session(backend, tmp_path)
    try:
        # 一次「正在跑」的生成：pi.live 上有 run 与部分回答
        async def seed(tx: Any) -> None:
            # ⚠️ `inputs` 必须是**真实的提交 ID**——`end_run` 会逐个结算它们，
            # 结算一个不存在的提交会报 `Submission N does not exist`。
            submission = await tx.create_submission(
                {
                    "conversationId": 1,
                    "requestId": "r1",
                    "type": "input",
                    "status": "queued",
                }
            )
            live = await tx.doc(LiveDoc, 1)
            live["run"] = {"taskId": 77, "inputs": [submission["id"]]}
            live["generation"] = {
                "attempt": 1,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "half an answer"}],
                    "provider": "p",
                    "model": "m",
                    "stop_reason": "stop",
                    "timestamp": 1,
                },
            }

        await session.commit(seed, CTX)

        async def change(tx: Any) -> None:
            await settle_scheduler_outcome(
                tx,
                {"id": 77, "conversationId": 1, "kind": "pi.generation"},
                {"status": "faulted", "error": {"message": "provider exploded"}},
            )

        await session.commit(change, CTX)
        live = await session.snapshot(LiveDoc, 1, CTX)

        assert "run" not in live  # 2 + 3：运行结束、呈现清掉
        assert "generation" not in live

        # 1：部分回答被留下来，且**标成 aborted**
        entries = await session._storage.scan_entries(  # noqa: SLF001
            {"conversationId": 1}, 50, None, CTX
        )
        assistants = [e for e in entries["items"] if e["kind"] == "pi.assistant"]
        assert assistants, "部分回答必须被留下来"
        message = assistants[-1]["model"][0]
        assert message["stop_reason"] == "aborted"
    finally:
        await session.close(CTX)


async def test_a_run_task_that_does_not_own_the_run_is_left_alone() -> None:
    """运行控制**不是**这个任务的时，什么都不做——否则一个过期的终局会抢走别人的运行。"""
    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]
    try:

        async def seed(tx: Any) -> None:
            live = await tx.doc(LiveDoc, 1)
            live["run"] = {"taskId": 99, "inputs": []}

        await session.commit(seed, CTX)

        async def change(tx: Any) -> None:
            await settle_scheduler_outcome(
                tx,
                {"id": 77, "conversationId": 1, "kind": "pi.generation"},
                {"status": "faulted", "error": {"message": "x"}},
            )

        await session.commit(change, CTX)
        live = await session.snapshot(LiveDoc, 1, CTX)
        assert live["run"] == {"taskId": 99, "inputs": []}
    finally:
        await session.close(CTX)
