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

from typing import Any, cast

from pi_chord.json import copy_json
from pi_durable.entries import AssistantEntry
from pi_durable.harness.compaction import estimate_context, select_cut
from pi_durable.harness.usage import record_usage

__all__ = [
    "DEFAULT_POLL_AFTER_MS",
    "GenerationCheckpoint",
    "GenerationInput",
    "GenerationResult",
    "Request",
    "append_assistant",
    "compaction_failure",
    "convert_partial",
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
