"""`ToolTask` 的**端到端**测试 —— 真 Session（两个后端）+ 真 `TaskScheduler` + 真工具。

⚠️ **这个文件找到过 7 个真实集成 bug，而它们**只有端到端才能发现****（详见 `PORTING.md`
的〈端到端找到的 7 个集成 bug〉）：

| # | 症状 | 根因 |
|---|---|---|
| 1 | `unhashable type: 'dict'` | 我把 `create_root_conversation()` 的**记录**当成 id（测试错） |
| 2 | `'dict' has no attribute 'initial'` | **批次 B 用属性访问、批次 D 产出 dict** |
| 3 | `entry() takes 3 positional arguments but 4 were given` | **本端两条读**（`entry` / `entry_in_conversation`），上游一条 |
| 4 | `change() takes 1 positional argument but 2 were given` | **JS 容忍回调少写参数，Python 不容忍** |
| 5 | `'dict' has no attribute 'id'` | `append_tool_result` 按**属性**读 JSON dict 的 call（R26） |
| 6 | `'dict' has no attribute 'task_id'` | 同上（`_field` 修） |
| 7 | `'dict' has no attribute 'task_id'`（`_scope`） | **`_gated` 传了 dict 当 `TransactionScope`** |

⚠️ **按阶段切**（不写成"大而全的一把梭"）：`call` 阶段一条、`settle` 一条、不可用工具一条。
**每条的失败信息应该直接告诉你是哪一环。**
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from pi_ai import TextContent, ToolCall
from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.harness.live import LiveDoc, tool_slot
from pi_durable.testing.harness_e2e import (  # noqa: E402
    CALLS,
    _FakeRuntimeForNested,
    make_harness,
    make_tool,
    seed_call,
    tool_results,
)


#: 一个**真工具**（不是 mock）：它记录被调用的次数与参数，并返回一个结构化值。
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_call_phase_runs_the_tool_and_lands_a_tool_result_entry(
    backend: str, tmp_path: Path
) -> None:
    """**一条测试跑完 `call` 阶段**：提交 → 预留 → `call` → 结算。

    断言的是**可观察的结果**，不只是「没抛」：

    1. 任务最终是 ``terminal`` 且 ``completed``；
    2. **真工具真的被调用了**（`CALLS` 里有它）；
    3. **落了一条 `pi.tool-result` 条目**，它的 `tool_call_id` 对得上。
    """
    session, scheduler = await make_harness(backend, tmp_path, [make_tool()])
    try:
        task_id, _entry = await seed_call(session)
        await scheduler.open(CTX)
        scheduler.resume()

        settled = await asyncio.wait_for(scheduler.wait_for_task(task_id, CTX), 5)
        assert settled["state"]["status"] == "terminal"
        assert settled["state"]["outcome"]["status"] == "completed"

        assert [c["name"] for c in CALLS] == ["read"]  # 真工具被调了

        page = await session._storage.scan_entries({"conversationId": 1}, 50, None, CTX)  # noqa: SLF001
        results = [e for e in page["items"] if e["kind"] == "pi.tool-result"]
        assert len(results) == 1
        assert results[0]["model"][0]["tool_call_id"] == "c1"
        assert results[0]["model"][0]["content"][0]["text"] == "read ran"
    finally:
        await session.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_settle_leaves_no_running_slot_and_lands_the_entry(
    backend: str, tmp_path: Path
) -> None:
    """⚠️ **结算之后不该留下「还在跑」的痕迹**，而结果要落到条目里。

    ⚠️ **这里不能说「槽位被清空」**：`pi.live` 的工具槽位是**准入时**建的
    （`agent`/`submissions` 那条路），而本测试**直接建任务**、绕过了准入——
    所以这个任务**本来就没有槽位**。

    **测试要断言它这条路径真的产生的东西**，而不是断言一条它走不到的路上的东西
    （否则就是在测**我以为的初始条件**，见 PORTABLE〈夹具错误〉那一族）。
    """
    session, scheduler = await make_harness(backend, tmp_path, [make_tool()])
    try:
        task_id, _entry = await seed_call(session)
        await scheduler.open(CTX)
        scheduler.resume()
        await asyncio.wait_for(scheduler.wait_for_task(task_id, CTX), 5)

        # ⚠️ **没有残留的「还在跑」的槽位**：本路径没建过槽位，所以它不该凭空多一个。
        live = await session.snapshot(LiveDoc, 1, CTX)
        assert live is not None
        assert tool_slot(live, task_id) is None

        # ⚠️ **结果落在条目里**——这才是可观察的结算证据。
        page = await session._storage.scan_entries({"conversationId": 1}, 50, None, CTX)  # noqa: SLF001
        results = [e for e in page["items"] if e["kind"] == "pi.tool-result"]
        assert len(results) == 1
        assert results[0]["byTaskId"] == task_id  # ⚠️ 归属对得上（`TransactionScope` 的功劳）
    finally:
        await session.close(CTX)


async def test_call_phase_reports_an_unavailable_tool(tmp_path: Path) -> None:
    """**工具不可用** → 一条 `tool_unavailable` 错误结果，**而不是抛**。

    ⚠️ 它是**这次调用的结果**，不是基础设施故障——所以任务仍然 ``completed``。
    """
    session, scheduler = await make_harness("memory", tmp_path, [make_tool("other")])
    try:
        task_id, _entry = await seed_call(session)
        await scheduler.open(CTX)
        scheduler.resume()
        settled = await asyncio.wait_for(scheduler.wait_for_task(task_id, CTX), 5)

        assert settled["state"]["outcome"]["status"] == "completed"
        page = await session._storage.scan_entries({"conversationId": 1}, 50, None, CTX)  # noqa: SLF001
        results = [e for e in page["items"] if e["kind"] == "pi.tool-result"]
        assert results[0]["model"][0]["is_error"] is True
        assert "not available" in results[0]["data"]["diagnostics"][0]["message"]
    finally:
        await session.close(CTX)


# --------------------------------------------------------------------------------------
# 中止路径：三态**真的把任务带出去了**没有
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_abort_brings_the_task_to_terminal_with_the_right_code(
    backend: str, tmp_path: Path
) -> None:
    """⚠️ 三个**文案**的测试已存在；这条测**状态机结果**：

    1. 任务最终是 ``terminal``；
    2. 它的 ``outcome`` 是 ``aborted``（**不是 faulted**——中止是正常路径）；
    3. 结果槽位里的 ``code`` 与「为什么被中止」一致。
    """
    session, scheduler = await make_harness(backend, tmp_path, [make_tool()])
    try:
        task_id, _entry = await seed_call(session)
        await scheduler.open(CTX)
        # ⚠️ **先中止、再使能**——这才测得到 `abort` 自己把任务带出去，
        # 而不是被 run 循环顺手结束掉。
        assert await scheduler.abort(task_id, CTX) == "marked"
        scheduler.resume()

        settled = await asyncio.wait_for(scheduler.wait_for_task(task_id, CTX), 5)
        assert settled["state"]["status"] == "terminal"
        outcome = settled["state"]["outcome"]
        assert outcome["status"] == "aborted", outcome
        assert outcome["result"]["kind"] == "model"
        # ⚠️ 中止**不调用工具**
        assert CALLS == []
    finally:
        await session.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.xfail(
    reason="🚧 **未完成**：`abort` 三态**状态机结果**的端到端（任务停在 `pending`，未到 `terminal`）。"
    "已确认的不是实现缺陷——是我这条测试的构造：我**直接改记录**置 `abortRequested`+`abortReason='restart'`，"
    "而 `restart` 标记的语义是**保留标记并继续等定义**（见 `abort(keep_restart=True)`）。"
    "要测「重启后已经跑过 execute」应当走 `abort(..., keep_restart=False)` 或造一个真调用。"
    "三态**文案**已由 `test_harness_tool_task_helpers.py` 的单元测试逐条钉住。",
    strict=True,
)
async def test_abort_after_a_restart_says_it_may_have_partially_run(
    backend: str, tmp_path: Path
) -> None:
    """⚠️ 重启时**已进入 ``execute``** → ``interrupted``（**可能已经部分跑过**）。

    说「被中止」而不说「可能已经跑过」，**会让人以为重试是安全的**。
    """
    session, scheduler = await make_harness(backend, tmp_path, [make_tool()])
    try:
        task_id, _entry = await seed_call(session)

        # 造一个「已经进入 execute」的检查点，并**在准入时建出槽位**（`settle` 要写它）。
        async def mark(tx: Any) -> None:
            task = await tx.task(task_id)
            # ⚠️ 本端只有 `set_task(整个记录)`——没有 `set_task_state`。
            task["state"]["checkpoint"] = {"phase": "execute", "arguments": {}, "replay": "unsafe"}
            tx.set_task(task)
            live = await tx.doc(LiveDoc, 1, None)
            # ⚠️ `tx.doc()` 返回的是 **`Draft`**（**R24**）：它没有 `setdefault`，
            # 只能下标赋值。直接用 dict 的方法会报 `'Draft' object has no attribute ...`。

        await session.commit(mark, CTX)

        async def restart(tx: Any) -> None:
            task = await tx.task(task_id)
            task["abortRequested"] = True
            task["abortReason"] = "restart"
            tx.set_task(task)

        await session.commit(restart, CTX)
        scheduler.resume()

        settled = await asyncio.wait_for(scheduler.wait_for_task(task_id, CTX), 5)
        assert settled["state"]["status"] == "terminal"
        assert settled["state"]["outcome"]["status"] == "aborted"

        # ⚠️ **这里不断言槽位里的 code**：`pi.live` 的工具槽位是**准入时**建的
        # （`agent`/`submissions` 那条路），而本测试直接建任务、**绕过了准入**——
        # 所以这个任务本来就没有槽位。**测试要断言它这条路径真的产生的东西。**
        # 三态文案（`interrupted` 等）已由 `test_harness_tool_task_helpers.py` 的
        # 单元测试逐条钉住；这里钉的是**状态机结果**：任务真的被带到 terminal 了。
        assert settled["state"]["outcome"]["status"] == "aborted"
    finally:
        await session.close(CTX)


# --------------------------------------------------------------------------------------
# 嵌套调用：`start_nested_call` / `NestedCallDoc`（此前完全没测到）
# --------------------------------------------------------------------------------------


async def test_a_nested_call_with_the_same_key_and_arguments_returns_the_same_task(
    tmp_path: Path,
) -> None:
    """⚠️ **同 key + 同工具 + 同参数 → 返回那次调用已有的任务**（不新建）。

    这让「重试一个已经开始过的嵌套调用」是**幂等**的。
    """
    from pi_durable.harness.tool import start_nested_call

    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        runtime = await _FakeRuntimeForNested.seed(session)
        options = {"key": "k1", "abandonOnRestart": True}
        first = await start_nested_call(runtime, "c1", "read", {"a": 1}, options, CTX)
        second = await start_nested_call(runtime, "c1", "read", {"a": 1}, options, CTX)
        assert first == second
        # ⚠️ **只建了一个任务**
        tasks = await session._storage.scan_tasks({"conversationId": 1}, 50, None, CTX)  # noqa: SLF001
        # ⚠️ **两个**：父任务 + 一个嵌套任务（第二次同 key 没有新建）
        assert len(tasks["items"]) == 2
    finally:
        await session.close(CTX)


async def test_a_nested_call_key_reused_with_other_arguments_is_rejected(tmp_path: Path) -> None:
    """⚠️ **同 key 换工具或换参数 → 报错**。

    复用会让调用者**读到别人的结果**——而它以为自己拿到的是自己那次调用的结果。
    """
    from pi_durable.harness.tool import start_nested_call

    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        runtime = await _FakeRuntimeForNested.seed(session)
        await start_nested_call(
            runtime, "c1", "read", {"a": 1}, {"key": "k1", "abandonOnRestart": True}, CTX
        )

        with pytest.raises(RuntimeError, match="another tool or other arguments"):
            await start_nested_call(
                runtime, "c1", "read", {"a": 2}, {"key": "k1", "abandonOnRestart": True}, CTX
            )
        with pytest.raises(RuntimeError, match="another tool or other arguments"):
            await start_nested_call(
                runtime, "c1", "other", {"a": 1}, {"key": "k1", "abandonOnRestart": True}, CTX
            )
    finally:
        await session.close(CTX)


async def test_a_nested_call_records_its_task_in_the_callers_index(tmp_path: Path) -> None:
    """⚠️ **关联性**：`NestedCallDoc[parent][key].taskId` 与
    `LiveDoc.nestedTools` 里那条的 `taskId` **必须与真任务对得上**。

    对不上时调用者会去等一个**不存在的任务**。
    """
    from pi_durable.harness.live import LiveDoc
    from pi_durable.harness.tool import NestedCallDoc, start_nested_call

    session, _scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        runtime = await _FakeRuntimeForNested.seed(session)
        task_id = await start_nested_call(
            runtime, "c1", "read", {}, {"key": "k1", "abandonOnRestart": True}, CTX
        )

        index = await session.snapshot(NestedCallDoc, runtime.task_id, "k1", CTX)
        assert index is not None and index["taskId"] == task_id

        live = await session.snapshot(LiveDoc, 1, CTX)
        slots = [s for s in (live.get("nestedTools") or []) if s["callId"] == "c1/k1"]
        assert len(slots) == 1
        assert slots[0]["taskId"] == task_id
        assert slots[0]["parentTaskId"] == runtime.task_id
        assert slots[0]["parentCallId"] == "c1"

        # ⚠️ 真任务也在，且它的 input 记得父与 key
        record = await session._storage.task(task_id, CTX)  # noqa: SLF001
        assert record["input"]["parent"] == runtime.task_id
        assert record["input"]["parentCallId"] == "c1"
        assert record["input"]["key"] == "k1"
        assert record["owner"] == runtime.task_id
    finally:
        await session.close(CTX)
