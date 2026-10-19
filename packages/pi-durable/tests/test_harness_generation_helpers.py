"""`harness/generation.py` 的 **Slice B 第一批**：四个不依赖 `GenerationTask` 的助手。

⚠️ **本文件用的是真基建**（真 Session + 真 `TaskScheduler` + **真 `_RuntimeView`**），
**没有假的 runtime 替身**。理由见下。

## ⚠️ 我第一版写了假 `_Runtime`，它失败了 —— 教训值得留

我写了一个「**能收能发但中间那步不做**」的替身：它的 `commit` **跑了回调、却丢掉了回调返回的状态**。
而真实 `_RuntimeView.commit` 会走 `_commit_state` 把 `{"status": "terminal", ...}` 写下去。

**危险在于**：被测代码看起来跑通了，而**实际的状态迁移没发生** ——
于是断言失败在**很远的地方**（`KeyError: 'outcome'`），而**不是**在「替身没做事」这一层。

**判据**：**写替身时，先问「被测代码依赖它做哪几件事」，而不是「它需要哪些字段」。**

**而这里的最优解不是"把替身写对"，是"别写替身"** —— 真 `_RuntimeView` 就在手边
（`scheduler._runtime(invocation, phase)`）。**形状对了不代表作用对了；能用真的，就不要用替代物。**
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.entries import AssistantEntry, UserEntry
from pi_durable.harness.generation import (
    GenerationTask,
    create_tool_task,
    fail_model_error,
    fail_no_model,
    read_calls,
)
from pi_durable.harness.live import LiveDoc
from pi_durable.harness.scheduler import Phase
from pi_durable.harness.tool import ToolTask
from pi_durable.testing.harness_e2e import FakeModels, make_harness, make_tool


class _RuntimeWithEntry:
    """真 `_RuntimeView` + 记一下那个助手条目 id（`read_calls` 要它）。

    ⚠️ **它不模拟任何行为**——所有调用**原样转发**给真的 `_RuntimeView`。
    这才是替身该有的样子：**加一个字段，不换一个行为**。
    """

    def __init__(self, inner: Any, entry_id: int) -> None:
        self._inner = inner
        self.entry_id = entry_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _real_runtime(session: Any, scheduler: Any, call_ids: list[str]) -> Any:
    """一个**真的** `_RuntimeView`（不是替身），跑在真的调度器上。

    ⚠️ 它替被测代码做**全部**的事：`commit` 走 `_commit_state`、
    `entry` 按本端契约把令牌收在**第三位**、`commit` 带真的 `TransactionScope`。
    """
    conversation = await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    conversation_id = conversation["id"]

    async def seed(tx: Any) -> dict[str, int]:
        entry = await tx.append_entry(
            conversation_id,
            {
                "kind": "pi.assistant",
                "model": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall", "id": i, "name": "read", "arguments": {}}
                            for i in call_ids
                        ],
                        "stop_reason": "toolUse",
                        "timestamp": 1,
                    }
                ],
            },
        )
        task_id = await tx.create_task(
            ToolTask,
            {"kind": "model", "assistant": entry["id"], "callId": call_ids[0]},
            {"ownership": {"kind": "conversation"}, "conversationId": conversation_id},
        )
        return {"entry": entry["id"], "task": task_id}

    made = await session.commit(seed, CTX)
    # ⚠️ 把任务放进调度器的 `#live` 并造一个**真的**调用——`_runtime` 要用它。
    record = await session._storage.task(made["task"], CTX)  # noqa: SLF001
    # ⚠️ 必须置成 `running`：`_gated` 的第 4 条前置只放行 `running` 的任务，
    # 否则会报 `Task N is pending`（我第一版就是这样）。
    record["state"]["status"] = "running"
    scheduler._live[record["id"]] = record  # noqa: SLF001
    invocation = scheduler._create_invocation(record, "run")  # noqa: SLF001
    snapshot = scheduler._registry.snapshot()  # noqa: SLF001
    phase = Phase(
        snapshot=lambda: snapshot,
        task=lambda: type("T", (), {"definition": {"name": "demo", "phases": {}}})(),
    )
    return _RuntimeWithEntry(scheduler._runtime(invocation, phase), made["entry"])  # noqa: SLF001


# --------------------------------------------------------------------------------------
# read_calls
# --------------------------------------------------------------------------------------


async def test_read_calls_returns_them_in_the_call_id_order(tmp_path: Path) -> None:
    """⚠️ **顺序由 ``call_ids`` 决定、不是条目里的顺序**——`tools` 阶段是**顺序执行**的，
    搞反会让工具**按错的次第跑**（而错法是**静默的**：工具都跑了，只是顺序错了）。"""
    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        runtime = await _real_runtime(session, scheduler, ["a", "b", "c"])
        found = await read_calls(runtime, runtime.entry_id, ["c", "a"], CTX)
        assert [c["id"] for c in found] == ["c", "a"]
    finally:
        await session.close(CTX)


async def test_read_calls_takes_only_the_first_of_a_duplicate_id(tmp_path: Path) -> None:
    """⚠️ ``slice(0, 1)``：模型重复给同一个 id 时**不该跑两次**。"""
    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        runtime = await _real_runtime(session, scheduler, ["dup", "dup"])
        found = await read_calls(runtime, runtime.entry_id, ["dup"], CTX)
        assert [c["id"] for c in found] == ["dup"]
    finally:
        await session.close(CTX)


async def test_read_calls_skips_ids_that_are_not_there(tmp_path: Path) -> None:
    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        runtime = await _real_runtime(session, scheduler, ["a"])
        assert await read_calls(runtime, runtime.entry_id, ["nope"], CTX) == []
    finally:
        await session.close(CTX)


# --------------------------------------------------------------------------------------
# create_tool_task
# --------------------------------------------------------------------------------------


async def test_create_tool_task_is_owned_by_the_generation(tmp_path: Path) -> None:
    """⚠️ ``ownership: {kind: "task", taskId}``——**不是** ``conversation``。

    **一个字段的选择同时决定了两个生命周期语义**：

    1. 生成任务**不会在工具跑完之前终局**（它保持 ``completing``）；
    2. 生成被中止时，**工具任务跟着被中止**（取消沿拥有关系向下传播）。
    """
    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        runtime = await _real_runtime(session, scheduler, ["a"])
        # ⚠️ `create_tool_task(tx, runtime, ...)` 的第一个参数是 **Tx**，不是 runtime。
        child = await session.commit(
            lambda tx: create_tool_task(tx, runtime, runtime.entry_id, "a"), CTX
        )

        record = await session._storage.task(child, CTX)  # noqa: SLF001
        assert record["kind"] == "pi.tool"
        assert record["owner"] == runtime.task_id  # ⚠️ 拥有者是**生成任务**
        assert record["input"] == {"kind": "model", "assistant": runtime.entry_id, "callId": "a"}
    finally:
        await session.close(CTX)


# --------------------------------------------------------------------------------------
# fail_model_error / fail_no_model
# --------------------------------------------------------------------------------------


async def test_fail_model_error_settles_the_inputs_and_fails(tmp_path: Path) -> None:
    """⚠️ **两件事在同一次提交里**：结算输入 + 写终局。

    分开的话，输入会停在「已应答」而任务已经失败——**UI 与状态机对不上**。
    """
    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        runtime = await _real_runtime(session, scheduler, ["a"])
        await fail_model_error(runtime, "the model exploded", CTX)

        record = await session._storage.task(runtime.task_id, CTX)  # noqa: SLF001
        assert record["state"]["status"] == "terminal"
        error = record["state"]["outcome"]["error"]
        assert error["message"] == "the model exploded"
        assert error["detail"] == {"reason": "model_error"}

        live = await session.snapshot(LiveDoc, runtime.conversation_id, CTX)
        assert live is not None  # `end_run` 跑了（它结算这次运行的输入）
    finally:
        await session.close(CTX)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (None, "No model is configured"),
        ({"provider": "anthropic", "modelId": "claude"}, "Model anthropic/claude is not available"),
    ],
)
async def test_fail_no_model_distinguishes_the_two_situations(
    tmp_path: Path, ref: Any, expected: str
) -> None:
    """⚠️ **「没配置模型」与「配的那个不可用」是两回事**——
    用户要做的事也不同（去配置 vs 去查 provider）。"""
    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        runtime = await _real_runtime(session, scheduler, ["a"])
        await fail_no_model(runtime, ref, CTX)

        record = await session._storage.task(runtime.task_id, CTX)  # noqa: SLF001
        error = record["state"]["outcome"]["error"]
        assert error["message"] == expected
        assert error["detail"] == {"reason": "no_model"}
    finally:
        await session.close(CTX)


# --------------------------------------------------------------------------------------
# stream_response：用**假 provider**（真 `EventStream`）+ **真 runtime**
# --------------------------------------------------------------------------------------


async def _streaming_runtime(tmp_path: Path) -> tuple[Any, Any, Any]:
    """真 session + 真调度器 + 真 `_RuntimeView`，并把 provider 换成 `FakeModels`。"""
    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    runtime = await _real_runtime(session, scheduler, ["a"])
    models = FakeModels()
    scheduler._models = models  # noqa: SLF001
    return session, scheduler, runtime


async def test_stream_response_commits_the_partial_and_returns_the_final_message(
    tmp_path: Path,
) -> None:
    from pi_durable.harness.generation import stream_response

    session, _scheduler, runtime = await _streaming_runtime(tmp_path)
    try:
        models = runtime.models
        task = asyncio.ensure_future(
            stream_response(runtime, {"provider": "p", "modelId": "m"}, [], {}, 1, CTX)
        )
        await asyncio.sleep(0)  # 让协程跑到 `stream_simple`
        assert models.calls, "provider 应该已经被调过"
        # ⚠️ 第二个参数是 **pi_ai 的 Context**（上游传 `{messages}`）
        assert "messages" in models.calls[0]["context"]

        models.push_partial("hello")
        # ⚠️ **必须等节流计时器落地**（`partialIntervalMs = 100`）——
        # 流一结束 `stream_response` 就会取消计时器，pending 的 partial **有意不提交**
        # （终局消息由 `classify` 提交，不是这条节流路径）。我第一版没等，于是什么也没写进去。
        await asyncio.sleep(0.25)
        models.finish(
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}], "timestamp": 1}
        )
        final = await asyncio.wait_for(task, 5)
        assert final["content"][0]["text"] == "hello"

        live = await session.snapshot(LiveDoc, runtime.conversation_id, CTX)
        assert live is not None
        # ⚠️ partial 被节流地提交进了 `live.generation.message`
        generation = live.get("generation") or {}
        assert generation.get("attempt") == 1
        assert generation.get("message", {}).get("content", [{}])[0].get("text") == "hello"
    finally:
        await session.close(CTX)


async def test_stream_response_skips_an_empty_partial(tmp_path: Path) -> None:
    """⚠️ **空内容的 partial 什么都不显示**——而 **deferred 响应永远不会越过它**，
    所以它**从不留下 partial**。"""
    from pi_durable.harness.generation import stream_response

    session, _scheduler, runtime = await _streaming_runtime(tmp_path)
    try:
        models = runtime.models
        task = asyncio.ensure_future(
            stream_response(runtime, {"provider": "p", "modelId": "m"}, [], {}, 1, CTX)
        )
        await asyncio.sleep(0)
        models.push_empty_partial()
        models.finish(
            {"role": "assistant", "content": [], "stop_reason": "deferred", "timestamp": 1}
        )
        await asyncio.wait_for(task, 5)

        live = await session.snapshot(LiveDoc, runtime.conversation_id, CTX)
        generation = (live or {}).get("generation") or {}
        # ⚠️ **没有留下 partial**——`message` 不该被写进去
        assert "message" not in generation
    finally:
        await session.close(CTX)


async def test_stream_response_copies_the_partial_instead_of_referencing_it(
    tmp_path: Path,
) -> None:
    """⚠️ **提交的是值，不是引用。**

    provider **还在改它自己那一份** —— 直接提交引用会让**已经写进存储的消息继续变化**，
    而你在别处读到的可能是**变了之后的**。**它不报错。**
    """
    from pi_durable.harness.generation import stream_response

    session, _scheduler, runtime = await _streaming_runtime(tmp_path)
    try:
        models = runtime.models
        task = asyncio.ensure_future(
            stream_response(runtime, {"provider": "p", "modelId": "m"}, [], {}, 1, CTX)
        )
        await asyncio.sleep(0)
        partial = models.push_partial("first")
        await asyncio.sleep(0.25)  # 等节流计时器落地（见上一条）
        models.finish(
            {"role": "assistant", "content": [{"type": "text", "text": "first"}], "timestamp": 1}
        )
        await asyncio.wait_for(task, 5)

        # ⚠️ **提交之后**再改 provider 那一份（模拟"它还在改"）
        partial["content"][0]["text"] = "MUTATED AFTER COMMIT"

        live = await session.snapshot(LiveDoc, runtime.conversation_id, CTX)
        stored = ((live or {}).get("generation") or {}).get("message") or {}
        # ⇒ 存下来的必须是**当时的快照**，而不是"被改过的引用"
        assert stored.get("content", [{}])[0].get("text") == "first"
    finally:
        await session.close(CTX)


# --------------------------------------------------------------------------------------
# 轮 5：**双向测试** —— 验证那句 `type: ignore` 的"闭嘴"是对的
# --------------------------------------------------------------------------------------


async def test_the_generation_ring_is_closed_at_runtime(tmp_path: Path) -> None:
    """⚠️ **环在运行期真的闭合** —— 这是 `create_generation` 上那句
    `type: ignore[name-defined]`（现已删除）**唯一合格的验证**。

    静态说明让检查器闭嘴；**只有"顺着环走一圈它确实解析到了"才能证明它闭嘴是对的**。
    **只做前者就是绕过。**

    两个方向：

    * **`create_generation` → `GenerationTask`**：这里建的任务**真的跑起来了**
      （走到 `prepare` 并因为没配模型而失败 —— **那也是"跑到了终局"**）；
    * **`answer` → `create_generation`**：见下一条测试。
    """
    from pi_durable.harness.generation import create_generation

    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        conversation = await session.commit(lambda tx: tx.create_root_conversation(), CTX)
        # ⚠️ 这一步会解析 `GenerationTask` 这个名字 —— 若环断了，这里就是 `NameError`。
        task_id = await session.commit(lambda tx: create_generation(tx, conversation["id"]), CTX)
        assert isinstance(task_id, int)

        # ⚠️ **`GenerationTask` 的名字真的解析到了**：任务的 kind 就是它的名字。
        record = await session._storage.task(task_id, CTX)  # noqa: SLF001
        assert record["kind"] == "pi.generation"
        assert record["version"] == 1

        # ⚠️ 而且它**真的能跑**：调度器把它带到终局（没配模型 → `fail_no_model`）。
        await scheduler.open(CTX)
        scheduler.resume()
        settled = await asyncio.wait_for(scheduler.wait_for_task(task_id, CTX), 5)
        assert settled["state"]["status"] == "terminal"
        outcome = settled["state"]["outcome"]
        # ⚠️ **`prepare` 真的跑了**（否则不会有这个 `no_model` 结果），
        # 这就是"名字解析到了且 phase 执行了"的证据。
        assert outcome["status"] == "failed"
        assert outcome["error"]["detail"]["reason"] == "no_model"
    finally:
        await session.close(CTX)


async def test_answer_hands_over_to_a_successor_generation(tmp_path: Path) -> None:
    """⚠️ **另一个方向**：`answer` 交付答复后，**`onYield` 的续写会开出一次后继生成**。

    这条走的是 `answer → create_generation` —— 环的另一半。

    构造：一个 `onYield` 钩子返回 `{"continue": ...}`，而**边界既没选到用户项、也没重置**
    （用一条普通答复，队列里没有待处理的输入）。
    """
    from pi_durable.harness.generation import answer

    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        # ⚠️ **不要另外再造一个根会话** —— `_real_runtime` 已经建了一个，
        # 而 `create_root_conversation` 的 id 是**全局唯一**的，再造会报
        # `ID 1 already belongs to conversation`（我的第一版就是这样）。
        runtime = await _real_runtime(session, scheduler, ["a"])
        conversation_id = runtime.conversation_id

        # 一个返回续写的 `onYield` 钩子。
        async def on_yield(message: Any, rt: Any, ctx: Any) -> dict[str, Any]:
            return {"continue": "and then keep going"}

        class _Hooks:
            async def each(self, name: str, invoke: Any) -> None:
                if name == "onYield":
                    await invoke(on_yield)

        runtime.hooks = _Hooks()  # type: ignore[attr-defined]

        message = {
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "stop",
            "timestamp": 1,
        }
        await answer(runtime, message, CTX)

        # ⚠️ **后台多了一次生成任务** —— 那就是 `create_generation` 被调到的证据。
        tasks = await session._storage.scan_tasks(  # noqa: SLF001
            {"conversationId": conversation_id}, 50, None, CTX
        )
        generations = [t for t in tasks["items"] if t["kind"] == "pi.generation"]
        assert len(generations) >= 1, [t["kind"] for t in tasks["items"]]
    finally:
        await session.close(CTX)


# --------------------------------------------------------------------------------------
# 轮 5：`GenerationTask` 的 **overflow → compaction 子任务 → 回 `prepare`**
# （把三个任务串起来的那条路径 —— 最有价值的一条）
# --------------------------------------------------------------------------------------


async def test_an_overflow_starts_a_blocking_compaction_and_goes_back_to_prepare(
    tmp_path: Path,
) -> None:
    """⚠️ **整条集成路径**：`classify` 认出溢出 → 建一个**阻塞式**压缩子任务 →
    检查点**回到 `prepare`**、带上 `{compacted, overflow}`。

    它把**三个任务**串起来了：生成 → 压缩 → （压缩完成后）回到生成。

    断言的是**可观察的结果**：

    1. **真的建了一个 `pi.compaction` 子任务**，而它的 **`owner` 是这次生成**
       （⇒ `blocking=True`、`background=False` —— 这是 `create_compaction` 那个三分支的验证点）；
    2. 生成任务**转成 `waiting` 且 `on: [child]`**、策略 `allSettled`；
    3. 检查点回到 **`prepare`** 且带 **`overflow` 文案**；
    4. ⚠️ **`live.generation` 被删掉**（partial 到此为止）；
    5. ⚠️ **助手条目被追加了**（那次失败的响应也要留在 transcript 里）。
    """
    from pi_durable.harness.generation import classify
    from pi_durable.harness.live import compaction_status

    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        runtime = await _real_runtime(session, scheduler, ["a"])
        conversation_id = runtime.conversation_id
        # ⚠️ 压缩需要 `settings.compaction.enabled` 与一个**找得到切点**的上下文。
        scheduler._settings = lambda: {  # noqa: SLF001
            "contextRetentionMs": 600_000,
            "progress": {"partialIntervalMs": 100, "outputIntervalMs": 100},
            "compaction": {
                "enabled": True,
                "reserveTokens": 1000,
                "backgroundTokens": 100,
                "keepRecentTokens": 1,
            },
            "retry": {"enabled": False, "maxRetries": 0},
            "stream": {},
            "toolExecution": "sequential",
        }

        # ⚠️ **先播一段够长的 transcript**：`select_cut` 需要一个**切点**，
        # 而它在"只有一个助手条目"的会话里返回 `None`（上游语义正是「没有切点就不走这条」）。
        # 我第一版就是那样，于是它落到 retry/fail 分支。**实现是对的，是夹具不够。**
        async def seed(tx: Any) -> None:
            for i in range(6):
                await tx.append_entry(
                    conversation_id,
                    {"model": [{"role": "user", "content": f"question {i}", "timestamp": i + 1}]},
                    UserEntry,
                )
                await tx.append_entry(
                    conversation_id,
                    {
                        "model": [
                            {
                                "role": "assistant",
                                "content": [{"type": "text", "text": f"answer {i} " * 200}],
                                "timestamp": i + 2,
                            }
                        ]
                    },
                    AssistantEntry,
                )

        await session.commit(seed, CTX)
        origin = await session._storage.scan_entries(  # noqa: SLF001
            {"conversationId": conversation_id}, 50, None, CTX
        )
        assert len(origin["items"]) >= 12, len(origin["items"])

        # ⚠️ 形态 1：**错误措辞**匹配（`prompt is too long`）。
        message = {
            "role": "assistant",
            "content": [],
            "stop_reason": "error",
            "error_message": "prompt is too long: 200000 tokens > 100000 maximum",
            "timestamp": 1,
        }
        # ⚠️ **`cutoff` 必须是"最后一条"** —— 这是诊断脚本给出的答案。
        #
        # 我此前用的是 `runtime.entry_id`（`_real_runtime` 建的那条**最早的**助手条目），
        # 而 12 条播种条目都在它**之后**：于是 `runtime.context(..., at=cutoff)`
        # 读回的视图**只有那一条条目** → `select_cut` 返回 `None` → 落到 retry/fail 分支。
        #
        # `scripts/diagnose_select_cut.py` 一次性排除了我猜了两轮的三个假设
        # （措辞 / enabled / 条目数），并直接显示出 `select_cut(keep=1) = 11` ——
        # **问题不在切点判据，在 cutoff。**
        # ⚠️ **`scan_entries` 是「最新在前」** —— 所以 `[-1]` 取到的是**最早**那条
        # （诊断脚本显示 `cutoff=2` 而实际有 13 条条目 → 视图只有 1 条 → 无切点）。
        # 用 `max(...)` 表达意图，**不依赖分页顺序**。
        last = max(e["id"] for e in origin["items"])
        request = {"attempt": 1, "model": {"provider": "p", "modelId": "m"}, "cutoff": last}
        await classify(runtime, request, message, CTX)

        record = await session._storage.task(runtime.task_id, CTX)  # noqa: SLF001
        state = record["state"]
        assert state["status"] == "waiting"
        assert state["policy"] == "allSettled"
        checkpoint = state["checkpoint"]
        assert checkpoint["phase"] == "prepare"
        assert "prompt is too long" in checkpoint["overflow"]
        child = checkpoint["compacted"]
        assert state["on"] == [child]

        # ⚠️ 子任务的归属：**生成拥有它** ⇒ 阻塞式（生成在等它）。
        child_record = await session._storage.task(child, CTX)  # noqa: SLF001
        assert child_record["kind"] == "pi.compaction"
        assert child_record["owner"] == runtime.task_id
        assert child_record["background"] is False
        assert child_record["input"]["reason"] == "overflow"

        # ⚠️ 状态挂上了 `pi.live`，而且是 **blocking**。
        live = await session.snapshot(LiveDoc, runtime.conversation_id, CTX)
        status = compaction_status(live, child)
        assert status is not None
        assert status["blocking"] is True
        assert status["reason"] == "overflow"
        # ⚠️ partial 到此为止。
        assert "generation" not in live

        # ⚠️ 那次失败的响应**要留在 transcript 里**。
        page = await session._storage.scan_entries(  # noqa: SLF001
            {"conversationId": runtime.conversation_id}, 50, None, CTX
        )
        assert any(e["kind"] == "pi.assistant" for e in page["items"])
    finally:
        await session.close(CTX)


async def test_a_retryable_error_schedules_a_retry(tmp_path: Path) -> None:
    """⚠️ **`retry` 分支**：`stop_reason == "error"` + 可重试 + 未超限 → 检查点转 `retry`。

    ⚠️ **`settings` 是这一刻读的**（不是准备时钉住的）—— 上游注释明说
    「retry 与 compaction 策略**管的是下一次尝试**」。
    **⇒ 所以在 `classify` 之前改设置就会生效**，这正是本测试要证明的。
    """
    from pi_durable.harness.generation import classify

    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        runtime = await _real_runtime(session, scheduler, ["a"])
        conversation_id = runtime.conversation_id
        # ⚠️ 在 `classify` **之前**设：重试开启、允许 3 次、退避 0。
        scheduler._settings = lambda: {  # noqa: SLF001
            "contextRetentionMs": 600_000,
            "progress": {"partialIntervalMs": 100, "outputIntervalMs": 100},
            "compaction": {
                "enabled": False,
                "reserveTokens": 1000,
                "backgroundTokens": 100,
                "keepRecentTokens": 1,
            },
            "retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 0, "maxDelayMs": 0},
            "stream": {},
            "toolExecution": "sequential",
        }
        message = {
            "role": "assistant",
            "content": [],
            "stop_reason": "error",
            "error_message": "overloaded_error: the server is busy",  # 可重试
            "timestamp": 1,
        }
        await classify(
            runtime,
            {"attempt": 1, "model": {"provider": "p", "modelId": "m"}, "cutoff": runtime.entry_id},
            message,
            CTX,
        )

        record = await session._storage.task(runtime.task_id, CTX)  # noqa: SLF001
        state = record["state"]
        assert state["status"] == "running"  # ⚠️ 不是 terminal
        assert state["checkpoint"]["phase"] == "retry"
        assert state["checkpoint"]["attempt"] == 1  # 下一次尝试是 2，由 retry phase +1
        assert state["checkpoint"]["until"] >= 0

        # ⚠️ `live.generation.retry` 也记上了（UI 要显示"正在重试"）。
        live = await session.snapshot(LiveDoc, conversation_id, CTX)
        generation = (live or {}).get("generation") or {}
        assert generation.get("attempt") == 1
        assert "overloaded" in (generation.get("retry") or {}).get("error", "")
    finally:
        await session.close(CTX)


async def test_an_overflow_is_never_retried(tmp_path: Path) -> None:
    """⚠️ **「overflow 永不重试」** —— **只有 compaction 能让下一次请求装得下**。

    **重试只有在"环境可能变化"时才有意义**；上下文溢出**不重试也不会变小**。

    构造：溢出措辞 + 重试**已启用**且未超限。若实现漏了 `not overflow`，这里会走 retry；
    而正确实现会落到 `failed`（`model_error`）。
    """
    from pi_durable.harness.generation import classify

    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        runtime = await _real_runtime(session, scheduler, ["a"])
        scheduler._settings = lambda: {  # noqa: SLF001
            "contextRetentionMs": 600_000,
            "progress": {"partialIntervalMs": 100, "outputIntervalMs": 100},
            # ⚠️ **compaction 关掉**：那样溢出分支不会走，于是只剩 `not overflow` 挡住 retry。
            "compaction": {
                "enabled": False,
                "reserveTokens": 1000,
                "backgroundTokens": 100,
                "keepRecentTokens": 1,
            },
            "retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 0, "maxDelayMs": 0},
            "stream": {},
            "toolExecution": "sequential",
        }
        message = {
            "role": "assistant",
            "content": [],
            "stop_reason": "error",
            "error_message": "prompt is too long: 200000 tokens > 100000 maximum",
            "timestamp": 1,
        }
        await classify(
            runtime,
            {"attempt": 1, "model": {"provider": "p", "modelId": "m"}, "cutoff": runtime.entry_id},
            message,
            CTX,
        )
        record = await session._storage.task(runtime.task_id, CTX)  # noqa: SLF001
        state = record["state"]
        assert state["status"] == "terminal"  # ⚠️ **没有**转 retry
        assert state["outcome"]["status"] == "failed"
        assert state["outcome"]["error"]["detail"]["reason"] == "model_error"
    finally:
        await session.close(CTX)


# --------------------------------------------------------------------------------------
# 最后两条：`CompactionTask` 的 abort 撤状态 + `blocking` 的"生成真的继续了"
# --------------------------------------------------------------------------------------


async def test_compaction_abort_removes_the_status_and_leaves_no_half_summary(
    tmp_path: Path,
) -> None:
    """⚠️ **`abort` 只做一件事：撤掉 `pi.live` 上的压缩状态。**

    理由：**半压缩的摘要根本不该存在**（摘要是一次性的，要么放要么不放）。

    三件事都要断言：

    1. `pi.live` 上的**压缩状态真被撤掉**（不是"标记成失败"）；
    2. **没有留下半压缩的摘要条目**（`pi.compaction` 条目数为 0）；
    3. 任务终局为 **`aborted`**。

    ⚠️ 本条此前挂 `xfail(strict=True)`，根因是**测试驱动错了调用者**（见下方「关键」注释）。
    """
    from pi_durable.harness.compaction import CompactionTask, _compaction_abort_phase
    from pi_durable.harness.live import add_compaction_status

    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        runtime = await _real_runtime(session, scheduler, ["a"])
        conversation_id = runtime.conversation_id
        assert conversation_id is not None

        # 先造一个压缩任务 + 它挂在 `live` 上的状态。
        async def setup(tx: Any) -> int:
            child = await tx.create_task(
                CompactionTask,
                {"reason": "manual"},
                {"ownership": {"kind": "conversation"}, "conversationId": conversation_id},
            )
            live = await tx.doc(LiveDoc, conversation_id)
            add_compaction_status(
                live,
                {"taskId": child, "reason": "manual", "blocking": False, "attempt": 1},
            )
            return int(child)

        child = await session.commit(setup, CTX)
        # ⚠️ **必须置成 `running`**：`_gated` 第 4 条只放行 `running` 的任务。
        child_record = await session._storage.task(child, CTX)  # noqa: SLF001
        child_record["state"]["status"] = "running"
        scheduler._live[child] = child_record  # noqa: SLF001

        live = await session.snapshot(LiveDoc, conversation_id, CTX)
        from pi_durable.harness.live import compaction_status

        assert compaction_status(live, child) is not None  # 先确认它真的挂上了

        # ⚠️ **关键：让这次 abort 跑在「子任务自己的 invocation」下。**
        #
        # `_compaction_abort_phase` 的提交走 `runtime.commit` → `_RuntimeView.commit` → `_gated`，
        # 而 **`_gated` 写的是 `invocation.task_id` 那个任务**。若复用生成任务的 runtime，
        # 撤除会**跑在生成任务上**，而**子任务永远停在 `pending`** —— 这正是本条此前 xfail 的原因。
        child_invocation = scheduler._create_invocation(child_record, "abort")  # noqa: SLF001
        child_runtime = scheduler._runtime(  # noqa: SLF001
            child_invocation,
            # `_compaction_abort_phase` **不用** phase（它的第一个参数被忽略），给个最小即可。
            Phase(
                snapshot=lambda: scheduler._registry.snapshot(),  # noqa: SLF001
                task=lambda: type(
                    "T", (), {"definition": {"name": "pi.compaction", "phases": {}}}
                )(),
            ),
        )
        await _compaction_abort_phase(None, child_runtime, CTX)

        live = await session.snapshot(LiveDoc, conversation_id, CTX)
        # ① 状态**真被撤掉**
        assert compaction_status(live, child) is None
        # ② **没有半压缩的摘要**
        page = await session._storage.scan_entries(  # noqa: SLF001
            {"conversationId": conversation_id}, 50, None, CTX
        )
        assert [e for e in page["items"] if e["kind"] == "pi.compaction"] == []
        # ③ 终局为 aborted
        record = await session._storage.task(child, CTX)  # noqa: SLF001
        assert record["state"]["status"] == "terminal"
        assert record["state"]["outcome"]["status"] == "aborted"
    finally:
        await session.close(CTX)


async def test_a_blocking_compaction_resumes_the_generation_that_waits_for_it(
    tmp_path: Path,
) -> None:
    """⚠️ **`blocking` 的语义真的兑现了没有** —— 这是它的**唯一验证点**。

    断言的是「**生成真的继续了**」，而不只是「子任务完成了」：

    1. 先造出真实关系：**一个 `pi.generation` 任务** `waiting` + `on: [child]`
       （用真的 `create_compaction`，且 `owner` 就是那个生成 ⇒ 阻塞式）；
    2. **让子任务终局**（模拟它跑完）；
    3. ⚠️ **然后跑调度器** —— 生成应当**从 `waiting` 恢复**，而不是永远停在那里。

    ⚠️ 这条与 :func:`test_an_overflow_starts_a_blocking_compaction_and_goes_back_to_prepare`
    的分工：那条验「生成进入等待」，这条验「**等待被解开之后它继续跑**」。

    ⚠️ 本条此前挂 `xfail(strict=True)`。它在修的过程中**暴露了两个真问题**：
    ① 「任务种类用错」（`pi.tool` 的检查点里写了生成的 `prepare`）——
    它触发了实现侧一个**真正的缺陷**（取 phase 处理器在 `try` 外面 → 无限自旋），**已修**；
    ② 事务边界（`ReadAfterWrite`）——**已按「拆两个事务」修**。
    """
    from pi_durable.harness.compaction import create_compaction
    from pi_durable.harness.live import add_compaction_status

    session, scheduler = await make_harness("memory", tmp_path, [make_tool()])
    try:
        conversation = await session.commit(lambda tx: tx.create_root_conversation(), CTX)
        conversation_id = conversation["id"]

        # ⚠️ **事务 A**：先建那个**生成任务**（`pi.generation`）。
        # ⚠️ 本测试此前用的是 `_real_runtime` 建的 **`pi.tool`** 任务，却在检查点里写了
        # **生成任务**的 `prepare` 阶段 —— 于是 `_run` 取处理器时 `KeyError: 'prepare'`，
        # 而那个 KeyError 当时在 `try` **外面**，导致**无限自旋 + 报告风暴**。
        # （实现侧已修：取处理器挪进了 `try`；这里则是把**任务种类**改对。）
        generation = await session.commit(
            lambda tx: tx.create_task(
                GenerationTask,
                {},
                {"ownership": {"kind": "conversation"}, "conversationId": conversation_id},
            ),
            CTX,
        )

        # ⚠️ **事务 B**：读旧记录 → 建压缩 → 写等待状态。
        # **必须分两个事务**：`create_compaction` 自己会写 `tx.doc(...)`
        # （`add_compaction_status`），所以「读旧记录」若与它在同一事务里就必然
        # 触发 `ReadAfterWrite`（一条本端明确的不变量）。
        async def setup(tx: Any) -> int:
            record = await tx.task(generation)  # ⚠️ 读必须在任何写之前
            child = await create_compaction(
                tx, conversation_id, {"reason": "threshold"}, generation
            )
            tx.set_task(
                {
                    **record,
                    "state": {
                        "status": "waiting",
                        "policy": "allSettled",
                        "on": [child],
                        "checkpoint": {
                            "phase": "prepare",
                            "attempt": 1,
                            "compacted": child,
                            "threshold": True,
                        },
                    },
                }
            )
            return child

        child = await session.commit(setup, CTX)
        record = await session._storage.task(generation, CTX)  # noqa: SLF001
        assert record["state"]["status"] == "waiting"
        assert record["state"]["on"] == [child]

        # ② 让子任务终局（模拟它跑完）——**保持 `blocking` 状态挂在那里**。
        async def finish(tx: Any) -> None:
            child_record = await tx.task(child)
            # ⚠️ **千万不要原地改 `tx.task()` 返回的那份记录！**
            #
            # `Transaction._committed_task` 是**记忆化**的（上游 `task.committedRead ??=` 同样），
            # 而 `tx.task(id)` 把**那个被记住的 dict 本身**交给你。原地改它之后，
            # `_validate_task_replacements` 再读同一个 id 时拿到的是**你改过的那份** ——
            # 于是它以为「存储里已经终局」，报 `Task 4 is already terminal`（**而存储里其实是 pending**）。
            #
            # ⇒ 本端照上游的 `withState(record, …)` 习惯：**建一份新的**（展开 + 覆盖）。
            tx.set_task(
                {
                    **child_record,
                    "state": {
                        "status": "terminal",
                        "outcome": {"status": "completed", "result": {"entryId": 1}},
                    },
                }
            )
            live = await tx.doc(LiveDoc, conversation_id)
            add_compaction_status(
                live, {"taskId": child, "reason": "threshold", "blocking": True, "attempt": 1}
            )

        await session.commit(finish, CTX)

        # ③ ⚠️ **跑调度器** —— 生成应当**从 `waiting` 恢复**。
        await scheduler.open(CTX)
        scheduler.resume()
        # 它恢复后会走到 `prepare`，因为没有模型而失败 —— **那也是"继续了"**。
        settled = await asyncio.wait_for(scheduler.wait_for_task(generation, CTX), 5)
        assert settled["state"]["status"] == "terminal"
        # ⚠️ **关键断言**：它**不是停在 `waiting`**，而且**真的执行了 phase**
        # （`fail_no_model` 是 `prepare` 的产物，所以它确实继续跑了）。
        assert settled["state"]["outcome"]["error"]["detail"]["reason"] in (
            "no_model",
            "model_error",
        )
    finally:
        await session.close(CTX)
