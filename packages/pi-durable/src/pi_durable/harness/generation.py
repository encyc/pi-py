"""生成任务的**叶子层** —— 移植 ``harness/generation.ts``(715) 的 **Slice A**（约 180 行）。

按**可测性**切（与 `tool.py` / `compaction.py` 同一取舍）：``GenerationTask`` 的 phase 处理器
都收 ``runtime: TaskRuntime``，而它的实现在 `scheduler.py`。本片只收**不碰 ``TaskRuntime``** 的部分。

⚠️ **本片最要紧的是 :func:`convert_partial`** —— 它是 ``live.py`` 里
:func:`~pi_durable.harness.live.settle_scheduler_outcome` 那条**环上回边**的**另一端**。
SCC-1 当时留了 ``type: ignore`` 与一条「运行任务分支测不了」的说明；**本片一到就把它补上**
（见 ``tests/test_harness_live.py`` 里那条测试）。

**Slice B**（``GenerationTask`` 本体 + ``read_calls`` / ``create_tool_task`` /
``fail_model_error`` / ``fail_no_model`` / ``stream_response`` / ``classify`` / ``answer`` /
``start_tool_round`` / ``finish_tool_round``，以及要 ``GenerationTask`` 的
``start_run`` / ``create_generation``）在 `scheduler.py` 到位后做。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, cast

from pi_ai import (
    AssistantMessage,
    is_context_overflow,
    is_retryable_assistant_error,
    retry_delay_ms,
)
from pi_ai.transcript import get_current_tools
from pi_chord.json import CopyJsonOptions, copy_json
from pi_durable.entries import AssistantEntry, ResetEntry, SystemEntry, UserEntry
from pi_durable.harness.agent import add_tools
from pi_durable.harness.compaction import create_compaction, estimate_context, select_cut
from pi_durable.harness.inbox import apply_boundary, prepare_boundary
from pi_durable.harness.json import assign_json
from pi_durable.harness.live import LiveDoc, end_run
from pi_durable.harness.prompt import plan_system_entries, render_sections, replay_sections
from pi_durable.harness.provider import ensure_provider_session_id
from pi_durable.harness.tool import ToolTask, append_tool_result, harness_error
from pi_durable.harness.usage import record_usage
from pi_durable.tasks import define_task

__all__ = [
    "DEFAULT_POLL_AFTER_MS",
    "GenerationCheckpoint",
    "GenerationInput",
    "GenerationResult",
    "Request",
    "append_assistant",
    "compaction_failure",
    "convert_partial",
    "GenerationTask",
    "classify",
    "answer",
    "finish_tool_round",
    "start_run",
    "start_tool_round",
    "create_generation",
    "stream_response",
    "read_calls",
    "fail_no_model",
    "fail_model_error",
    "create_tool_task",
    "hand_over",
    "threshold_compaction",
]

#: 轮询一个「提供方侧延迟响应」的默认间隔（``generation.ts:112``）。
DEFAULT_POLL_AFTER_MS = 5000

#: 一次生成的输入——**空**：运行的控制信息在 ``pi.live.run`` 里（``generation.ts:50``）。
GenerationInput = dict[str, Any]

#: 一次生成的检查点（``generation.ts:52-93``）。
#:
#: ⚠️ 这是**持久化的 wire record**，所以键名保持 **camelCase**。
#: 五个 phase：``prepare`` / ``request`` / ``retry`` / ``poll`` / ``tools``。
GenerationCheckpoint = dict[str, Any]

#: 一次生成的结果（``generation.ts:95``）。
GenerationResult = dict[str, Any]

#: 分类一条消息时，从**产生它的那次请求**需要的东西（``generation.ts:101-110``）。
Request = dict[str, Any]


def threshold_compaction(
    view: Any,
    planned: list[Any],
    context_window: float,
    policy: dict[str, Any],
) -> str | None:
    """准备阶段在发起请求**之前**要启动哪种压缩（spec §8.3）（``generation.ts:322-340``）。

    三种结果：

    * ``"blocking"`` —— 超过 ``contextWindow - reserveTokens``；
    * ``"background"`` —— 超过 ``blocking - backgroundTokens``（且 ``backgroundTokens > 0``）；
    * ``None`` —— 都不用。

    ⚠️ **两个前置条件是独立的**（有一个专门的测试钉住）：
    ① ``enabled`` 为假、或 ``context_window <= 0`` → 直接不用；
    ② **即使超了阈值**，若 ``select_cut()`` **找不到切点**（没有东西可压）→ 也**不用**。
    少了 ② 会启动一个注定什么都不做的压缩任务。

    调用方只在 ``pi.live`` 里**没有列出任何压缩**时才启动后台那个。
    """
    if not policy.get("enabled") or context_window <= 0:
        return None
    tokens = estimate_context(view, [m for entry in planned for m in (entry.get("model") or [])])
    blocking = context_window - policy["reserveTokens"]
    background = blocking - policy["backgroundTokens"]
    if tokens > blocking:
        over = "blocking"
    elif policy["backgroundTokens"] > 0 and tokens > background:
        over = "background"
    else:
        over = None
    if over is None or select_cut(view, policy["keepRecentTokens"]) is None:
        return None
    return str(over)


def compaction_failure(outcome: dict[str, Any]) -> str:
    """一次**没有完成**的阻塞压缩的错误文本（``generation.ts:342-353``）。

    上游的 ``switch`` 是**穷尽**的（``Exclude<TaskOutcome, {status:"completed"}>``），
    所以四种状态都有对应文案——**别把 ``aborted`` 漏成默认分支**。
    """
    status = outcome["status"]
    if status in ("failed", "faulted"):
        return f"Compaction failed: {outcome['error']['message']}"
    if status == "orphaned":
        return f"Compaction failed: {outcome['reason']}"
    return "Compaction was aborted"


async def append_assistant(tx: Any, conversation_id: int, message: Any) -> Any:
    """追加一条 ``pi.assistant`` 条目，并把用量计入 ``pi.usage``（``generation.ts:688-696``）。

    用量按 ``models`` 归类、键是 ``provider/model``——**与工具的 ``tools`` 那条路分开**。
    """
    usage = _field(message, "usage")
    provider = _field(message, "provider") or ""
    model = _field(message, "model") or ""
    if usage is not None:
        await record_usage(tx, conversation_id, "models", f"{provider}/{model}", usage)
    # ⚠️ 存储要序列化，所以消息必须是**严格 JSON**；本包 `append_entry` 的参数顺序是
    # ``(conversation_id, value, token)``（偏离 #48 子项）。
    return await tx.append_entry(conversation_id, {"model": [to_json(message)]}, AssistantEntry)


async def convert_partial(tx: Any, live: Any, conversation_id: int) -> None:
    """把一次**被打断 / 中止 / 出错 / 孤立**的尝试留下的已提交部分，
    作为一条 **aborted 助手条目**追加进去（``generation.ts:381-388``）。

    调用方随后会替换或移除 ``generation``。

    ⚠️ **这是环上那条回边的另一端**：``live.settle_scheduler_outcome`` 在运行任务分支上
    函数内延迟导入的就是它。上游靠 ESM 提升与活绑定容忍这个环，Python 不行——
    见 PORTING.md〈SCC 四文件的拆分方案〉。

    ⚠️ **为什么必须留下这条**：它让 transcript **保留模型产出过的东西**、
    ``pi.usage`` 也**计入它的花费**——丢了它，一次崩溃就会让用户白花一笔钱且看不到半句输出。
    """
    generation = live.get("generation")
    partial = None if generation is None else generation.get("message")
    if partial is None:
        return
    # ⚠️ **R24**：`live` 是 chord 的 `Draft`，**两层下标**读出来的还是 Draft，
    # 而 `copy_json` 要**严格 JSON**、**不会**替你展开。
    # 症状按后端不同：sqlite 抛 `Value contains a non-JSON Draft`，**内存后端一路通过**。
    message = cast(dict[str, Any], copy_json(_plain(partial)))
    message["stop_reason"] = "aborted"
    await append_assistant(tx, conversation_id, message)


def hand_over(live: Any, from_: int, to: int) -> None:
    """把运行控制从 ``from_`` 交给 ``to``；**运行的输入跟着一起走**（``generation.ts:713-715``）。

    只有当前持有者确实是 ``from_`` 时才改——否则一个过期的移交会把别人的运行抢走。
    """
    run = live.get("run")
    if run is not None and run["taskId"] == from_:
        run["taskId"] = to


def to_json(message: Any) -> Any:
    """把一条消息转成**严格 JSON**（``dict`` 就原样返回）。

    ⚠️ **R24 同族**：``AssistantMessage`` 是 pydantic 对象，而存储要序列化。
    **内存后端不会报**，只有 sqlite 的 ``json.dumps`` 会抛。
    """
    if isinstance(message, dict):
        return message
    return copy_json(message)


def _plain(value: Any) -> Any:
    """有 ``to_json`` 就调，否则原样返回（**R24**）。

    与 :func:`~pi_durable.harness.inbox._plain` 同一个助手、同一个理由：
    chord 的 ``Draft`` **递归**包装每个嵌套容器，而 ``copy_json`` 拒绝 Draft。
    """
    to_json = getattr(value, "to_json", None)
    return value if to_json is None else to_json()


def _field(message: Any, name: str, default: Any = None) -> Any:
    """取一条消息的字段，**同时**接受 JSON dict 与 pydantic 模型（**R26**）。"""
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


# --------------------------------------------------------------------------------------
# Slice B（第一批）：不依赖 `GenerationTask` 的四个助手（``generation.ts:297-370``）
# --------------------------------------------------------------------------------------


async def read_calls(runtime: Any, assistant: int, call_ids: list[str], context: Any) -> list[Any]:
    """助手条目里的那几条工具调用，**按 ``call_ids`` 的顺序**（``generation.ts:297-307``）。

    ⚠️ **顺序由 ``call_ids`` 决定、不是由条目里的顺序决定**——因为 ``tools`` 阶段是
    **顺序执行**的，而待办队列就是调用发生的顺序。搞反会让工具**按错的次第跑**。

    ⚠️ ``slice(0, 1)``：**重复的 callId 只取第一条**（模型重复给同一个 id 时不该跑两次）。
    """
    entry = await runtime.entry(assistant, context, AssistantEntry)
    messages = (entry or {}).get("model") or []
    message = messages[0] if messages else None
    role = _field(message, "role")
    if role != "assistant":
        return []
    calls = [
        content
        for content in (_field(message, "content") or [])
        if _field(content, "type") == "toolCall"
    ]
    found: list[Any] = []
    for id in call_ids:
        for call in calls:
            if _field(call, "id") == id:
                found.append(call)
                break  # ⚠️ `slice(0, 1)`——重复 id 只取第一条
    return found


async def create_tool_task(tx: Any, runtime: Any, assistant: int, call_id: str) -> int:
    """为一次调用建一个**由本生成任务拥有**的工具任务（``generation.ts:309-315``）。

    ⚠️ ``ownership: {kind: "task", taskId: runtime.taskId}``——**不是** ``conversation``：
    它让生成任务成为工具任务的拥有者，于是

    * 生成任务**不会在工具跑完之前终局**（它保持 ``completing``）；
    * 生成被中止时，**工具任务跟着被中止**（取消沿拥有关系向下传播）。
    """
    return int(
        await tx.create_task(
            ToolTask,
            {"kind": "model", "assistant": assistant, "callId": call_id},
            {"ownership": {"kind": "task", "taskId": runtime.task_id}},
        )
    )


async def fail_model_error(runtime: Any, text: str, context: Any) -> None:
    """把这次运行的输入结算成 ``unanswered``（``model_error``）**并以 ``text`` 失败**
    （``generation.ts:355-365``）。

    ⚠️ **两件事在同一次提交里**：结算输入 + 写终局。分开的话，
    输入会停在「已应答」而任务已经失败——**UI 与状态机对不上**。
    """

    async def change(tx: Any, _current: Any = None) -> Any:
        live = await tx.doc(LiveDoc, runtime.conversation_id)
        end_run(
            tx,
            live,
            runtime.task_id,
            {"status": "unanswered", "reason": "model_error", "detail": text},
        )
        return {
            "status": "terminal",
            "outcome": {
                "status": "failed",
                "error": {"message": text, "detail": {"reason": "model_error"}},
            },
        }

    await runtime.commit(change, context)


async def fail_no_model(runtime: Any, ref: Any, context: Any) -> None:
    """把输入结算成 ``unanswered``（``no_model``）并失败（``generation.ts:367-377``）。

    ⚠️ 两句文案由 ``ref`` 是否存在决定——**「没配置模型」与「配的那个不可用」是两回事**，
    而用户要做的事也不同（去配置 vs 去查 provider）。
    """

    message = (
        "No model is configured"
        if ref is None
        else f"Model {ref['provider']}/{ref['modelId']} is not available"
    )

    async def change(tx: Any, _current: Any = None) -> Any:
        live = await tx.doc(LiveDoc, runtime.conversation_id)
        end_run(tx, live, runtime.task_id, {"status": "unanswered", "reason": "no_model"})
        return {
            "status": "terminal",
            "outcome": {
                "status": "failed",
                "error": {"message": message, "detail": {"reason": "no_model"}},
            },
        }

    await runtime.commit(change, context)


async def stream_response(
    runtime: Any,
    model: Any,
    messages: list[Any],
    options: Any,
    attempt: int,
    context: Any,
) -> Any:
    """把模型输出流式地跑完，**顺带节流地把部分消息提交进 `live.generation.message`**
    （``generation.ts:393-447``）。

    三道关键设计：

    1. ⚠️ **同步拷贝那份 partial**：provider **还在改它自己那一份**。
       直接提交引用会让「已提交的消息」在之后**继续变化**——
       而它已经写进存储了（**R24** 同族）。
    2. ⚠️ **节流**：只有「没有计时器**且**没有在途提交」时才排下一次。
       两个条件都要——只查计时器会让**上一次提交还没落地时又排一次**。
    3. ⚠️ **跳过空内容的 partial**：pi-ai 开头的 ``start`` 事件什么都还没有；
       而一个 **deferred 响应永远不会越过它**，所以它**从不留下 partial**。

    ``finally`` 里 ``stopped = True`` **然后再 await 在途提交**——
    不置位的话，那个提交的 ``finally`` 会**再排一次**计时器（永远停不下来）。
    """
    interval = runtime.settings["progress"]["partialIntervalMs"]
    state: dict[str, Any] = {"pending": None, "timer": None, "in_flight": None, "stopped": False}

    def flush() -> None:
        state["timer"] = None
        partial = state["pending"]
        state["pending"] = None
        if partial is None or state["stopped"]:
            return

        async def push() -> None:
            # ⚠️ **同步拷贝**：provider 还在改它自己那份。
            # ⚠️ 本端 `copy_json` 的选项是 `CopyJsonOptions` 对象（上游是字面量对象）。
            message = copy_json(partial, CopyJsonOptions(omit_undefined_properties=True))

            async def change(tx: Any, _current: Any = None) -> None:
                live = await tx.doc(LiveDoc, runtime.conversation_id)
                if live.get("generation") is None:
                    live["generation"] = {"attempt": attempt}
                assign_json(live["generation"], "message", message)
                return None

            await runtime.commit(change, context)

        async def run_push() -> None:
            try:
                await push()
            except BaseException as error:  # noqa: BLE001 - 上游同样捕获一切
                # ⚠️ 中止标记或关闭**之后**的拒绝是预期的；已提交的状态仍然一致。
                if not runtime.signal.aborted:
                    runtime.report(error)
            finally:
                state["in_flight"] = None
                if state["pending"] is not None and not state["stopped"]:
                    state["timer"] = _schedule(flush, interval)

        state["in_flight"] = asyncio.ensure_future(run_push())

    try:
        # ⚠️ `EventStream` **本身就是 `AsyncIterator`**（`pi_ai.event_stream`）——
        # 直接 `async for` 它，不是 `async for ... in stream.events`
        # （`.events` 是个普通 list，那样写会报 `'async for' requires an object with __aiter__`）。
        events = runtime.models.stream_simple(model, {"messages": list(messages)}, options)
        async for event in events:
            kind = _field(event, "type")
            partial = _field(event, "partial")
            # ⚠️ **一个没有内容的 partial 什么都显示不出来**——而 deferred 响应永远不会越过它。
            if kind in ("done", "error") or partial is None or not _field(partial, "content"):
                continue
            state["pending"] = partial
            if state["timer"] is None and state["in_flight"] is None:
                state["timer"] = _schedule(flush, interval)
        return await events.result()
    finally:
        state["stopped"] = True
        _cancel(state["timer"])
        in_flight = state["in_flight"]
        if in_flight is not None:
            await asyncio.gather(in_flight, return_exceptions=True)


def _schedule(fn: Any, delay_ms: float) -> Any:
    """一个**真的**计时器（上游用 ``setTimeout``）。"""
    return asyncio.get_running_loop().call_later(max(delay_ms, 0) / 1000, fn)


def _cancel(handle: Any) -> None:
    if handle is not None:
        handle.cancel()


# --------------------------------------------------------------------------------------
# Slice B：生成任务的环（按**定义顺序**排成 DAG —— 每个函数只引用它上面的）
# --------------------------------------------------------------------------------------


async def create_generation(tx: Any, conversation_id: int) -> int:
    """在会话的**行尾**开一次生成（``generation.ts:700-711``）。

    ⚠️ **本文件唯一的前向引用**：`GenerationTask` 定义在文件末尾，而这里要用它。

    **TS 的 ``function`` 声明会提升，Python 没有** —— 所以这里必须显式加一句静态说明。
    **这是唯一能表达「互相引用」的方式**：运行期的环是完整的
    （`GenerationTask` 的 phase → `answer` → 本函数 → `GenerationTask`），
    只是静态检查器需要被告知「这个名字稍后会有」。

    ⚠️ **这条 ignore 的双向验证**在 `test_harness_generation_helpers.py` 里：
    - `answer` → 本函数（交付答案后开新生成）；
    - 本函数 → `GenerationTask`（**新建的生成真的跑到终局**）。
    **只做静态说明就是绕过；必须有运行期测试证明它闭嘴是对的。**
    """
    return int(
        await tx.create_task(
            # ⚠️ **本文件里那处 `type: ignore[name-defined]` 已经删掉了** ——
            # `GenerationTask` 现在就在本文件末尾，名字解析得到。
            # （mypy 在它落地的那一刻报了 `unused-ignore`，正是"会过期的 ignore"第 3 次兑现。）
            GenerationTask,
            {},
            {"ownership": {"kind": "conversation"}, "conversationId": conversation_id},
        )
    )


async def start_tool_round(
    runtime: Any,
    request: dict[str, Any],
    message: Any,
    calls: list[Any],
    context: Any,
) -> None:
    """追加**会用工具的**助手回复，并在**同一次提交里**起它的工具轮（spec §8.3）
    （``generation.ts:574-620``）。

    **没被这次请求提供的工具**在这里**直接拿到一个 ``tool_unavailable`` 结果条目**；
    其余的每一个都建一个**由本次生成拥有**的工具任务，而**顺序模式下现在只起第一个**。
    然后生成在 ``tools`` 阶段等它们，**并保住这次运行**。

    ⚠️ **两处最容易写错**：

    1. **``tools`` 这个名字在源码里被遮蔽了两次**：外层是**工具定义**（`agent.tools`），
       内层是**任务 id 列表**。JS 的块级作用域让同名合法，**Python 同名赋值就是覆盖** ——
       本端改名为 ``definitions`` / ``task_ids``。
    2. **``offered`` 来自 ``request.messages``**（有就用），否则**按 ``cutoff`` 重取上下文**
       —— 因为钩子可能改过消息，而 `toolUse` 的元数据是以**真正发出去的那份**为准的。
    """
    conversation_id = runtime.conversation_id
    messages = request.get("messages")
    if messages is None:
        view = await runtime.context(conversation_id, context, {"at": request.get("cutoff")})
        messages = view["messages"]
    # ⚠️ 外层：**工具定义**（不是任务 id）。
    offered = {tool.name for tool in get_current_tools(messages)}
    definitions = (await runtime.agent(context))["tools"]
    settings = runtime.settings
    sequential = settings.get("toolExecution") == "sequential" or any(
        call["name"] in offered
        and any(d.name == call["name"] and d.execution_mode == "sequential" for d in definitions)
        for call in calls
    )

    async def change(tx: Any, _current: Any = None) -> Any:
        live = await tx.doc(LiveDoc, conversation_id)
        entry = await append_assistant(tx, conversation_id, message)
        slots: list[dict[str, Any]] = []
        # ⚠️ 内层：**任务 id**（源码里与外层同名，本端改名）。
        task_ids: list[int] = []
        pending: list[str] = []
        for call in calls:
            if call["name"] not in offered:
                # ⚠️ **没被提供的工具**：立刻写一条结果条目，**不进 pending**。
                unavailable = harness_error(
                    "tool_unavailable", f"Tool {call['name']} is not available"
                )
                result = await append_tool_result(
                    tx, conversation_id, call, unavailable, {"timestamp": runtime.now()}
                )
                slots.append(
                    {
                        "callId": call["id"],
                        "name": call["name"],
                        "status": "done",
                        "entry": result["id"],
                    }
                )
                continue
            if sequential and task_ids:
                # ⚠️ 顺序模式下**后续的**调用只进 pending——现在只起第一个。
                pending.append(call["id"])
                slots.append({"callId": call["id"], "name": call["name"], "status": "pending"})
                continue
            task_id = await create_tool_task(tx, runtime, entry["id"], call["id"])
            task_ids.append(task_id)
            slots.append(
                {
                    "callId": call["id"],
                    "name": call["name"],
                    "taskId": task_id,
                    "status": "pending",
                }
            )
        # ⚠️ 工具轮开始了：`live.generation` 的**部分消息**到此为止。
        del live["generation"]
        live["tools"] = slots
        checkpoint = {
            "phase": "tools",
            "assistant": entry["id"],
            "tools": task_ids,
            "pending": pending,
        }
        return {
            "status": "waiting",
            "checkpoint": checkpoint,
            "on": task_ids,
            "policy": "allSettled",
        }

    await runtime.commit(change, context)


async def start_run(tx: Any, conversation_id: int, live: Any, inputs: list[int]) -> None:
    """起一次运行：**建一次生成**并把输入挂到它下面（``generation.ts:698-704``）。

    ⚠️ `live.run` 是「这次运行归谁管」的**唯一**记录 ——
    而 `inputs` 必须**同时**写进去：不然那些输入会在下一次边界时**被当成孤儿**重新投递。
    """
    live["run"] = {"taskId": await create_generation(tx, conversation_id), "inputs": inputs}


async def finish_tool_round(
    runtime: Any,
    assistant: int,
    task_ids: list[int],
    context: Any,
) -> None:
    """工具轮的工具都终局了：施加它们的 control，然后在**某个边界**结束或交接这次运行
    （``generation.ts:622-688``）。

    ⚠️ **三处最容易写错**（按风险）：

    1. **``terminate`` 是三重**全称**条件**，不是存在条件：
       ``len(slots) > 0`` **且** 每个 slot 都有 ``taskId`` **且** 那些 taskId 的 control **都** ``terminate``。
       > **「每一轮调用（含没有任务的）都必须要求终止」——不是「任一」。**
       写错的症状是「偶尔该停没停」，而**不报错**。
    2. **``controls`` 的版本 1 兼容**：上游是 ``outcome.result.kind !== "nested"``。
       ⚠️ **版本 1 的结果没有 `kind`** —— 而 `undefined !== "nested"` 是 **true**，
       所以**缺失即按模型发起的调用处理**（**读它的 control**），不是跳过。
       本端 ``result.get("kind") != "nested"` 在缺失时同样为 True ✓
       —— **这不是"默认值"，是"缺失时的语义本来如此"**。
    3. **``handoff`` 是 ``findLast``**（调用顺序里**最后一个**赢）。
       测它**必须用"有多个 handoff"的输入**——只有一个时"最后赢"与"第一个赢"结果相同。
    """
    conversation_id = runtime.conversation_id
    controls: dict[int, Any] = {}
    outcomes = await runtime.outcomes(task_ids, context)
    for index, id in enumerate(task_ids):
        outcome = outcomes[index]
        # ⚠️ 版本 1 的结果没有 `kind` —— 而 `None != "nested"` 为 True，
        # 所以**缺失即按模型发起的调用处理**（读它的 control）。
        controls[id] = (
            outcome["result"].get("control")
            if outcome["status"] == "completed" and outcome["result"].get("kind") != "nested"
            else None
        )

    live = await runtime.snapshot(LiveDoc, conversation_id, context)
    slots = (live or {}).get("tools") or []
    results = [slot["entry"] for slot in slots if slot.get("entry") is not None]
    await runtime.hooks.each("afterTools", lambda hook: hook(assistant, results, runtime, context))

    # ⚠️ **三重全称条件**：见 docstring 第 1 条。
    terminate = len(slots) > 0 and all(
        slot.get("taskId") is not None
        and (controls.get(slot["taskId"]) or {}).get("terminate") is True
        for slot in slots
    )
    added = [
        name for control in controls.values() for name in ((control or {}).get("addTools") or [])
    ]
    # ⚠️ **最后一个赢**（`findLast`）。
    handoff = next(
        (
            control["handoff"]
            for control in reversed(list(controls.values()))
            if (control or {}).get("handoff") is not None
        ),
        None,
    )

    async def change(tx: Any, _current: Any = None) -> Any:
        boundary = await prepare_boundary(tx, conversation_id, runtime.settings)
        if added:
            await add_tools(tx, conversation_id, added)
        live = await tx.doc(LiveDoc, conversation_id)
        now = runtime.now()
        if terminate or handoff is not None:
            if handoff is not None:
                # ⚠️ 交接先写一条 `ResetEntry`，并**把边界头挪到它**。
                reset_entry = await tx.append_entry(
                    conversation_id,
                    {
                        "head": "self",
                        "model": [{"role": "user", "content": handoff, "timestamp": now}],
                    },
                    ResetEntry,
                )
                # ⚠️ `Boundary` 是 **dataclass**（不是 dict）——按属性写。
                boundary.head = reset_entry["id"]
            applied = await apply_boundary(tx, boundary, "final", now)
            end_run(tx, live, runtime.task_id, {"status": "done", "answer": assistant})
            if applied["users"]:
                await start_run(tx, conversation_id, live, applied["users"])
        else:
            applied = await apply_boundary(tx, boundary, "postTools", now)
            if applied["reset"]:
                # ⚠️ 排队的重置在**答案之前**切掉了运行的上下文。
                end_run(tx, live, runtime.task_id, {"status": "unanswered", "reason": "reset"})
                if applied["users"]:
                    await start_run(tx, conversation_id, live, applied["users"])
            else:
                __del_key(live, "tools")
                __del_key(live, "nestedTools")
                run = live.get("run")
                if run is not None and run.get("taskId") == runtime.task_id:
                    run["inputs"] = [*(run.get("inputs") or []), *applied["users"]]
                # ⚠️ **工具轮跑完 → 交接给下一次生成** —— 这就是那条逻辑环的另一半。
                hand_over(live, runtime.task_id, await create_generation(tx, conversation_id))
        return {
            "status": "terminal",
            "outcome": {"status": "completed", "result": {"entryId": assistant}},
        }

    await runtime.commit(change, context)


async def answer(runtime: Any, message: Any, context: Any) -> None:
    """一次**最终答复**；最终边界负责放置排队项（spec §6）（``generation.ts:531-573``）。

    第一个 ``onYield`` 续写会**追加一条用户消息并把运行交给一次后继生成** ——
    但**只在**边界既没选到用户项、也没触发重置时。否则这次运行的输入结算为 ``done``，
    而选到的用户项**开始下一次运行**。

    ⚠️ **本函数是那条逻辑环的闭合点**：它 `hand_over` 给 `create_generation`，
    而后者建的 `GenerationTask` 的 phases 又会回到这里。
    **`create_generation` 上那句 `type: ignore` 的意义就在这一处**——
    **轮 5 的双向测试要走的就是这条路。**
    """
    continuation: Any = None

    async def on_yield(hook: Any) -> None:
        nonlocal continuation
        # ⚠️ **第一个续写赢**（`if (continuation !== undefined) return`）——
        # 后面的钩子**不再被调用**去产生续写（但钩子本身仍然跑，只是不看结果）。
        if continuation is not None:
            return
        returned = hook(message, runtime, context)
        if inspect.isawaitable(returned):
            returned = await returned
        if returned is not None:
            continuation = returned.get("continue")

    await runtime.hooks.each("onYield", on_yield)
    conversation_id = runtime.conversation_id

    async def change(tx: Any, _current: Any = None) -> Any:
        # ⚠️ 队列模式**在会话线上读**——边界是在这一刻决定的。
        boundary = await prepare_boundary(tx, conversation_id, runtime.settings)
        live = await tx.doc(LiveDoc, conversation_id)
        entry = await append_assistant(tx, conversation_id, message)
        result = {
            "status": "terminal",
            "outcome": {"status": "completed", "result": {"entryId": entry["id"]}},
        }
        applied = await apply_boundary(tx, boundary, "final", runtime.now())
        # ⚠️ **续写只在边界既没选到用户项、也没重置时**才交给后继生成。
        if continuation is not None and not applied["users"] and not applied["reset"]:
            user = {"role": "user", "content": continuation, "timestamp": runtime.now()}
            await tx.append_entry(conversation_id, {"model": [user]}, UserEntry)
            hand_over(live, runtime.task_id, await create_generation(tx, conversation_id))
            __del_key(live, "generation")
            return result
        end_run(tx, live, runtime.task_id, {"status": "done", "answer": entry["id"]})
        if applied["users"]:
            await start_run(tx, conversation_id, live, applied["users"])
        return result

    await runtime.commit(change, context)


async def classify(runtime: Any, request: dict[str, Any], message: Any, context: Any) -> None:
    """把一条**终局**的 provider 消息分类，**并在同一次提交里清掉 partial**
    （``generation.ts:449-542``）。

    ⚠️ **顺序即优先级，七处**：

    1. **`signal.throw_if_aborted()` 是第一句** —— 中止标记或关闭时，
       **由中止调用或重开的那次运行处理已提交状态**，这里**什么都不碰**；
    2. **deferred**：`pollAt = max(now + (pollAfterMs ?? 5000), request.pollAt + 1)`
       —— ⚠️ **第二个分支是"单调推进"**：保证**不会用同一个时刻再 poll 一次**；
    3. **`afterResponse` 钩子**在 deferred 之后、分派之前；
    4. ⚠️ **`toolUse` 在两个分派里都出现**，所以第一个必须带 **`calls` 非空**
       —— 否则一个「说要用工具、但一个 call 都没有」的响应会**去跑一个空工具轮**；
    5. **overflow**：`is_context_overflow(message)` —— ⚠️ **只传消息**。
       `context_window` 由 pi_ai 从**消息的 provider 元数据**取，而它**用真值判断**；
       **一旦这里自己从 settings 拼一个传进去，就绕过了那条判断**，
       症状是「窗口为 0 的模型**不触发**溢出压缩」；
    6. ⚠️ **overflow 永不重试** —— **只有 compaction 能让下一次请求装得下**。
       （「重试只在环境可能变化时才有意义」；上下文溢出**不重试也不会变小**。）
    7. ⚠️ **`settings` 是这一刻读的**，不是准备时钉住的 ——
       上游注释明说「retry 与 compaction 策略**管的是下一次尝试**」。
    """
    # 1. 中止/关闭：由别人处理。
    runtime.signal.throw_if_aborted()
    conversation_id = runtime.conversation_id
    attempt = request["attempt"]
    compacted = request.get("compacted")
    ref = request["model"]
    cutoff = request["cutoff"]

    # 2. deferred：转 `poll`。
    if message.get("stop_reason") == "deferred" and message.get("deferred") is not None:
        handle = message["deferred"]
        # ⚠️ **单调推进**：不会用同一个时刻再 poll 一次。
        previous = request.get("pollAt")
        poll_at = max(
            runtime.now() + (handle.get("pollAfterMs") or DEFAULT_POLL_AFTER_MS),
            float("-inf") if previous is None else previous + 1,
        )

        async def to_poll(tx: Any, _current: Any = None) -> Any:
            live = await tx.doc(LiveDoc, conversation_id)
            live["generation"] = {"attempt": attempt, "deferred": {"pollAt": poll_at}}
            checkpoint = {
                "phase": "poll",
                "attempt": attempt,
                "model": ref,
                "cutoff": cutoff,
                "handle": handle,
                "pollAt": poll_at,
            }
            if compacted is not None:
                checkpoint["compacted"] = compacted
            return {"status": "running", "checkpoint": checkpoint}

        await runtime.commit(to_poll, context)
        return

    # 3. `afterResponse` 钩子（在 deferred 之后、分派之前）。
    await runtime.hooks.each("afterResponse", lambda hook: hook(message, runtime, context))

    calls = [c for c in (message.get("content") or []) if _field(c, "type") == "toolCall"]
    stop = message.get("stop_reason")
    # 4. ⚠️ `calls` 非空 —— 否则会去跑一个**空工具轮**。
    if stop == "toolUse" and calls:
        return await start_tool_round(runtime, request, message, calls, context)
    if stop in ("stop", "length", "toolUse"):
        return await answer(runtime, message, context)

    # 5/7. 策略**现在**读（它们管的是**下一次尝试**）。
    settings = runtime.settings
    # ⚠️ **`is_context_overflow` / `is_retryable_assistant_error` 收的是 pydantic 的
    # `AssistantMessage`**，而这里拿到的 `message` 是**从 provider 流里来的 JSON dict**。
    # 直接传 dict 会报 `'dict' object has no attribute 'stop_reason'`（**R26**）——
    # 这是端到端才发现的（单测里我一直传的是构造好的对象）。
    pydantic_message = _as_assistant_message(message)
    overflow = stop == "error" and is_context_overflow(pydantic_message)
    if overflow and compacted is None and settings["compaction"]["enabled"]:
        policy = settings["compaction"]
        view = await runtime.context(conversation_id, context, {"at": cutoff})
        if select_cut(view, policy["keepRecentTokens"]) is not None:
            text = message.get("error_message") or "Context overflow"

            async def to_compact(tx: Any, _current: Any = None) -> Any:
                live = await tx.doc(LiveDoc, conversation_id)
                await append_assistant(tx, conversation_id, message)
                __del_key(live, "generation")
                # ⚠️ `owner` 给了 → **阻塞式**（生成在等它）。
                child = await create_compaction(
                    tx, conversation_id, {"reason": "overflow"}, runtime.task_id
                )
                checkpoint = {
                    "phase": "prepare",
                    "attempt": attempt,
                    "compacted": child,
                    "overflow": text,
                }
                return {
                    "status": "waiting",
                    "checkpoint": checkpoint,
                    "on": [child],
                    "policy": "allSettled",
                }

            await runtime.commit(to_compact, context)
            return

    # 6. ⚠️ **overflow 永不重试**（只有 compaction 能让下次装得下）。
    policy = settings["retry"]
    retry = (
        stop == "error"
        and not overflow
        and is_retryable_assistant_error(pydantic_message)
        and policy["enabled"]
        and attempt <= policy["maxRetries"]
    )
    until = runtime.now() + retry_delay_ms(_retry_policy(settings), attempt) if retry else 0

    async def settle(tx: Any, _current: Any = None) -> Any:
        live = await tx.doc(LiveDoc, conversation_id)
        await append_assistant(tx, conversation_id, message)
        if retry:
            live["generation"] = {
                "attempt": attempt,
                "retry": {"at": until, "error": message.get("error_message") or ""},
            }
            checkpoint = {"phase": "retry", "attempt": attempt, "until": until}
            if compacted is not None:
                checkpoint["compacted"] = compacted
            return {"status": "running", "checkpoint": checkpoint}
        text = message.get("error_message") or f"Model response ended with stop reason {stop}"
        end_run(
            tx,
            live,
            runtime.task_id,
            {"status": "unanswered", "reason": "model_error", "detail": text},
        )
        return {
            "status": "terminal",
            "outcome": {
                "status": "failed",
                "error": {"message": text, "detail": {"reason": "model_error"}},
            },
        }

    await runtime.commit(settle, context)


# --------------------------------------------------------------------------------------
# `GenerationTask` 本体（``generation.ts:118-295``）
# --------------------------------------------------------------------------------------


async def _prepare_phase(task: Any, runtime: Any, context: Any) -> None:
    """`prepare`：渲染系统提示与工具清单、追加它们需要的 `pi.system` 条目，然后转 `request`
    （``generation.ts:127-176``）。

    ⚠️ **这里解析出的 agent 与 settings 对这次请求是固定的**。
    ⚠️「**只有 Harness 会往忙碌的会话里写模型上下文**」——所以这里读到的上下文
    **在那次提交时仍然是最新的**（期间只可能有**仅用于显示**的条目落进来）。

    ⚠️ **阈值压缩是阻塞式的**（`create_compaction` 给了 `owner`）：
    它先压缩、**再重新 prepare**；而**在压缩追加之前，transcript 是不变的**。
    """
    conversation_id = runtime.conversation_id
    agent = await runtime.agent(context)
    settings = runtime.settings
    model = _field(agent, "model")
    thinking_level = _field(agent, "thinking_level")
    # ⚠️ **`runtime.models.get_model` 是契约方法**，不是 `pi_ai.get_model`（注册表函数）——同名不同物。
    resolved = (
        None
        if model is None
        else runtime.models.get_model(_field(model, "provider"), _field(model, "model_id"))
    )
    if model is None or resolved is None:
        return await fail_no_model(runtime, model, context)

    checkpoint = task["state"]["checkpoint"]
    attempt = checkpoint["attempt"]
    compacted = checkpoint.get("compacted")
    overflow = checkpoint.get("overflow")
    blocked = checkpoint.get("threshold")

    if compacted is not None and overflow is not None:
        outcome = (await runtime.outcomes([compacted], context))[0]
        if outcome["status"] != "completed" or outcome["result"].get("entryId") is None:
            return await fail_model_error(runtime, overflow, context)
    # ⚠️ **阈值压缩「什么都没压」或「被拒绝」时，那次请求可能还装得下**；
    # 而**失败**的那次留下了一个它判定为过大的上下文：发出去**至多是没有空间放答案**。
    if compacted is not None and blocked is True:
        outcome = (await runtime.outcomes([compacted], context))[0]
        if outcome["status"] != "completed":
            return await fail_model_error(runtime, compaction_failure(outcome), context)

    view = await runtime.context(conversation_id, context)
    shown = replay_sections(view["messages"])
    report = runtime.report
    env: Any = None
    try:
        env = await runtime.env(context)
    except BaseException as error:  # noqa: BLE001 - 上游同样捕获一切
        signal = getattr(context, "abort_signal", None)
        if signal is not None and signal.aborted:
            raise
        report(error)
    prompt_input: dict[str, Any] = {
        "conversationId": conversation_id,
        "agent": agent,
        "env": env,
        "shown": shown,
        "read": runtime,
    }
    # ⚠️ `PromptInput` 是本端的 **TypedDict**（键是 camelCase 的上游名）——
    # 用 `cast` 过掉那个名义类型（本端的 `_RuntimeView` 不实现它，只按形状给）。
    desired = await render_sections(
        _field(agent, "sections") or [],
        cast("Any", prompt_input),
        shown,
        report,
        context,
    )
    entries = plan_system_entries(view, desired, _field(agent, "tools") or [], runtime.now())
    threshold = (
        None
        if compacted is not None
        else threshold_compaction(view, entries, resolved.context_window, settings["compaction"])
    )

    if threshold == "blocking":
        # ⚠️ **阻塞式**：先压缩、再重新 prepare。transcript 在压缩追加之前不变。
        async def to_compact(tx: Any, _current: Any = None) -> Any:
            child = await create_compaction(
                tx, conversation_id, {"reason": "threshold"}, runtime.task_id
            )
            return {
                "status": "waiting",
                "checkpoint": {
                    "phase": "prepare",
                    "attempt": attempt,
                    "compacted": child,
                    "threshold": True,
                },
                "on": [child],
                "policy": "allSettled",
            }

        await runtime.commit(to_compact, context)
        return

    async def change(tx: Any, _current: Any = None) -> Any:
        page = await tx.scan_entries({"conversationId": conversation_id}, 1)
        cutoff = page["items"][0]["id"] if page["items"] else None
        for entry in entries:
            cutoff = (await tx.append_entry(conversation_id, entry, SystemEntry))["id"]
        if cutoff is None:
            raise RuntimeError(f"Conversation {conversation_id} has no entries to send")
        # ⚠️ **在这次提交里查** —— 于是准备期间被准入的一次压缩**也算数**。
        if threshold == "background":
            live_doc = await tx.doc(LiveDoc, conversation_id)
            if live_doc.get("compactions") is None:
                await create_compaction(tx, conversation_id, {"reason": "threshold"})
        request: dict[str, Any] = {
            "attempt": attempt,
            "model": model,
            "thinkingLevel": thinking_level,
            "streamOptions": settings["stream"],
            "cutoff": cutoff,
        }
        if compacted is not None:
            request["compacted"] = compacted
        return {"status": "running", "checkpoint": {"phase": "request", **request}}

    await runtime.commit(change, context)


async def _request_phase(task: Any, runtime: Any, context: Any) -> None:
    """`request`：发一次请求并把结果交给 `classify`（``generation.ts:177-211``）。"""
    checkpoint = task["state"]["checkpoint"]
    attempt = checkpoint["attempt"]
    compacted = checkpoint.get("compacted")
    ref = checkpoint["model"]
    thinking_level = checkpoint["thinkingLevel"]
    stream_options = checkpoint["streamOptions"]
    cutoff = checkpoint["cutoff"]
    conversation_id = runtime.conversation_id

    async def start(tx: Any, _current: Any = None) -> None:
        live = await tx.doc(LiveDoc, conversation_id)
        # ⚠️ 被中断的那次尝试留下的 partial **先落成一条被中止的助手条目**，再开新的。
        await convert_partial(tx, live, conversation_id)
        live["generation"] = {"attempt": attempt}
        return None

    await runtime.commit(start, context)
    model = runtime.models.get_model(_field(ref, "provider"), _field(ref, "model_id"))
    if model is None:
        return await fail_no_model(runtime, ref, context)
    view = await runtime.context(conversation_id, context, {"at": cutoff})
    messages = view["messages"]

    async def before_request(hook: Any) -> None:
        nonlocal messages
        replaced = hook({"messages": messages}, runtime, context)
        if inspect.isawaitable(replaced):
            replaced = await replaced
        if replaced is not None:
            messages = replaced["messages"]

    await runtime.hooks.each("beforeRequest", before_request)
    options: dict[str, Any] = {
        **stream_options,
        "signal": runtime.signal,
        "sessionId": await ensure_provider_session_id(runtime, context),
    }
    if thinking_level != "off":
        options["reasoning"] = thinking_level
    message = await stream_response(runtime, model, messages, options, attempt, context)
    request = {
        "attempt": attempt,
        "model": ref,
        "cutoff": cutoff,
        "messages": view["messages"],
    }
    if compacted is not None:
        request["compacted"] = compacted
    await classify(runtime, request, message, context)


async def _retry_phase(task: Any, runtime: Any, context: Any) -> None:
    """`retry`：等到 `until`，然后把尝试数 +1 并**回到 `prepare`**（``generation.ts:212-227``）。

    ⚠️ **`runtime.sleep` 同时被调用自己的信号与调用方的 context 取消**（`_sleep` 的语义），
    所以一次中止/关闭会**立刻**把这个等待拒掉，而不是等满 `until`。
    """
    checkpoint = task["state"]["checkpoint"]
    attempt = checkpoint["attempt"]
    compacted = checkpoint.get("compacted")
    until = checkpoint["until"]
    await runtime.sleep(until, context)

    async def change(tx: Any, _current: Any = None) -> Any:
        live = await tx.doc(LiveDoc, runtime.conversation_id)
        live["generation"] = {"attempt": attempt + 1}
        next_checkpoint: dict[str, Any] = {"phase": "prepare", "attempt": attempt + 1}
        if compacted is not None:
            next_checkpoint["compacted"] = compacted
        return {"status": "running", "checkpoint": next_checkpoint}

    await runtime.commit(change, context)


async def _poll_phase(task: Any, runtime: Any, context: Any) -> None:
    """`poll`：等到 `pollAt`，取 deferred 结果并交给 `classify`（``generation.ts:228-240``）。"""
    checkpoint = task["state"]["checkpoint"]
    ref = checkpoint["model"]
    model = runtime.models.get_model(_field(ref, "provider"), _field(ref, "model_id"))
    if model is None:
        return await fail_no_model(runtime, ref, context)
    await runtime.sleep(checkpoint["pollAt"], context)
    message = await runtime.models.fetch_deferred(
        model, checkpoint["handle"], {"signal": runtime.signal}
    )
    request = {
        "attempt": checkpoint["attempt"],
        "model": ref,
        "cutoff": checkpoint["cutoff"],
        "pollAt": checkpoint["pollAt"],
    }
    if checkpoint.get("compacted") is not None:
        request["compacted"] = checkpoint["compacted"]
    await classify(runtime, request, message, context)


async def _tools_phase(task: Any, runtime: Any, context: Any) -> None:
    """`tools`：**顺序**起这次工具轮的调用（``generation.ts:241-263``）。

    ⚠️ **顺序执行**：``pending`` 是队列，每次只起**队首**那一个，然后**等它**
    （检查点转 ``waiting``，``on: [taskId]``，策略 ``allSettled``）。
    队列空了才 `finish_tool_round`。

    ⚠️ **`pending` 的顺序由 `read_calls` 决定**（也就是由 `call_ids` 决定），
    **不是由助手条目里的顺序决定** —— 搞反会让工具**按错的次第跑**。
    """
    checkpoint = task["state"]["checkpoint"]
    assistant = checkpoint["assistant"]
    # ⚠️ 源码里这个键也叫 `tools`（**任务 id 列表**）——与外层的"工具定义"同名，本端读作 `task_ids`。
    task_ids = checkpoint["tools"]
    pending = list(checkpoint["pending"])
    if not pending:
        return await finish_tool_round(runtime, assistant, task_ids, context)
    next_call, rest = pending[0], pending[1:]

    async def change(tx: Any, _current: Any = None) -> Any:
        live = await tx.doc(LiveDoc, runtime.conversation_id)
        task_id = await create_tool_task(tx, runtime, assistant, next_call)
        # ⚠️ 把刚建的任务 id 补进**还没建任务的那个槽位**（`taskId` 为空的那条）。
        slot = next(
            (
                s
                for s in (live.get("tools") or [])
                if s["callId"] == next_call and s.get("taskId") is None
            ),
            None,
        )
        if slot is not None:
            slot["taskId"] = task_id
        return {
            "status": "waiting",
            "checkpoint": {
                "phase": "tools",
                "assistant": assistant,
                "tools": [*task_ids, task_id],
                "pending": rest,
            },
            "on": [task_id],
            "policy": "allSettled",
        }

    await runtime.commit(change, context)


async def _generation_abort_phase(task: Any, runtime: Any, context: Any) -> None:
    """`abort`（``generation.ts:265-290``）。

    ⚠️ **与 `ToolTask` 的 `abort` 不同：这里没有三态文案**（那条区分是**工具**的，
    因为工具可能"已经跑了一半"而生成不会）。生成的 abort 只有一种：
    **未开始的调用拿到 `aborted` 结果 + 运行结算为 `unanswered/aborted`**。

    两件事：

    1. **`poll` 阶段要取消那个 deferred 句柄** —— ⚠️ **取消失败只报告、不抛**：
       我们已经在中止路径上，**没有任何上层能处理它**（关闭路径通则）；
    2. **`tools` 阶段要把"还没开始的调用"写成 `aborted` 结果** ——
       它们**永远不会跑**，而调用方在等它们的结果。
    """
    checkpoint = task["state"]["checkpoint"]
    if checkpoint["phase"] == "poll":
        ref = checkpoint["model"]
        model = runtime.models.get_model(_field(ref, "provider"), _field(ref, "model_id"))
        if model is not None:
            try:
                await runtime.models.cancel_deferred(
                    model, checkpoint["handle"], {"signal": runtime.signal}
                )
            except BaseException as error:  # noqa: BLE001 - 中止路径：只报告，不抛
                runtime.report(error)

    conversation_id = runtime.conversation_id
    # ⚠️ 它跑在**这一轮的工具任务都终局之后**；**没开始的那些调用拿到 `aborted` 结果**（spec §8.5）。
    unstarted = (
        await read_calls(runtime, checkpoint["assistant"], checkpoint["pending"], context)
        if checkpoint["phase"] == "tools"
        else []
    )

    async def change(tx: Any, _current: Any = None) -> Any:
        live = await tx.doc(LiveDoc, conversation_id)
        await convert_partial(tx, live, conversation_id)
        for call in unstarted:
            result = harness_error("aborted", f"Tool {call['name']} was aborted")
            await append_tool_result(
                tx, conversation_id, call, result, {"timestamp": runtime.now()}
            )
        end_run(tx, live, runtime.task_id, {"status": "unanswered", "reason": "aborted"})
        return {"status": "terminal", "outcome": {"status": "aborted"}}

    await runtime.commit(change, context)


GenerationTask = define_task(
    {
        "name": "pi.generation",
        "version": 1,
        "initial": lambda input_=None: {"phase": "prepare", "attempt": 1},
        "phases": {
            "prepare": _prepare_phase,
            "request": _request_phase,
            "retry": _retry_phase,
            "poll": _poll_phase,
            "tools": _tools_phase,
        },
        "abort": _generation_abort_phase,
    }
)


def __del_key(target: Any, key: str) -> None:
    """删掉一个 ``Draft`` / dict 上的键（**存在才删**）。

    ⚠️ **`Draft.pop` 不是 dict 的 pop** —— 它的签名是 ``pop(index=-1)``，
    是**列表**语义！传 ``pop("k", None)`` 会报
    ``TypeError: Draft.pop() takes from 1 to 2 positional arguments but 3 were given``。

    正确写法是 ``if key in target: del target[key]``（`Draft` 两个 dunder 都有）。
    ⚠️ 这是〈本端把上游的字面量/对象做成了 dataclass〉那一族的**反面**：
    这里不是"上游是对象、本端是 dataclass"，而是**本端的 `Draft` 长得像 dict、实际不是**。
    """
    if key in target:
        del target[key]


def _as_assistant_message(message: Any) -> Any:
    """把一个**从 provider 来的 JSON dict** 转成 pydantic 的 `AssistantMessage`。

    ⚠️ **pi_ai 的判别函数收的是模型、不是 dict**（`is_context_overflow` /
    `is_retryable_assistant_error` 都读属性）。**R26 的反方向**：
    前面几处是"实现按对象写、调用方给 dict"，这一处是**"框架函数按对象写、我们给 dict"**。

    ⚠️ 已经是模型时**原样返回** —— 不要重复构造（那会丢掉 provider 的元数据）。
    """
    if isinstance(message, AssistantMessage):
        return message
    return AssistantMessage(**message)


def _retry_policy(settings: dict[str, Any]) -> Any:
    """把 settings 里的**会话重试策略**转成 `pi_ai.RetryPolicy`。

    ⚠️ **两处不匹配，都要处理**：

    1. **形状**：settings 给的是 **TypedDict（dict）**，而 `pi_ai.retry_delay_ms` 收的是
       **dataclass 对象**（它读 `policy.base_delay_ms`）—— 传 dict 会报
       `'dict' object has no attribute 'base_delay_ms'`（**R26**，端到端才发现）；
    2. **命名**：settings 是 **camelCase**（`maxRetries` / `baseDelayMs` / `maxAgentDelayMs`），
       而 `RetryPolicy` 是 **snake_case**。

    ⚠️ `max_agent_delay_ms` **必须传** —— 它有默认值 60 秒，而**漏传会让退避被静默封顶在 60 秒**
    （那与"用户没设"是两回事）。
    """
    from pi_ai import RetryPolicy

    policy = settings.get("retry") or {}
    return RetryPolicy(
        enabled=bool(policy.get("enabled")),
        max_retries=int(policy.get("maxRetries") or 0),
        base_delay_ms=int(policy.get("baseDelayMs") or 1000),
        max_agent_delay_ms=int(policy.get("maxAgentDelayMs") or 60_000),
    )
