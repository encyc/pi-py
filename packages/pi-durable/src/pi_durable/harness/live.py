"""``pi.live`` —— 一个对话的**运行中**状态 —— 移植 ``harness/live.ts``（268 行）。

``pi.live`` 是 UI 看的那一面：当前这次 generation 的尝试、当前这一轮工具的每个槽位、
嵌套调用、以及正在跑的压缩任务。它**不存结果**——工具结果在条目里，嵌套调用的结果在
调用者的任务文档里。

**SCC-1**：本文件是运行时强连通分量（`live → generation → compaction → submissions`）的
**第一块**，也是那套共享词汇（``LiveDoc`` / ``LiveState`` / ``ToolSlot`` / ``NestedToolSlot``）
的落地处。它到 ``generation`` 的那条边是**环上的回边**，按方案在**函数内延迟导入**
（见 PORTING.md〈SCC 四文件的拆分方案〉）——**那是结构妥协，不是性能优化**。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pi_chord.json import JsonRepresentation
from pi_durable.documents import define_doc
from pi_durable.types import (
    SubmissionSettlement,
    TaskId,
    TaskRecord,
)

#: ``end_run`` 的 ``tx`` 用的是上游的 ``Tx``（事务面，约 30 个方法）。
#: 那是「接口形状那一半」里尚未定义的一个——本模块只用到 ``settle_submission``，
#: 所以先收成 ``Any`` 并在 PORTING.md 的欠账清单里挂着，**不猜它的完整形状**（R28 情形一）。
__all__ = [
    "COMPACTION_TASK_KIND",
    "RUN_TASK_KINDS",
    "TOOL_TASK_KIND",
    "CompactionFailure",
    "CompactionStatus",
    "LiveDoc",
    "LiveState",
    "NestedToolSlot",
    "NestedToolSummary",
    "ToolSlot",
    "add_compaction_status",
    "clear_progress",
    "compaction_status",
    "end_run",
    "fail_compaction_status",
    "finish_slot",
    "remove_compaction_status",
    "remove_nested_slots",
    "settle_scheduler_outcome",
    "tool_slot",
]

#: 能拥有 ``pi.live.run`` 的内建任务种类（``live.ts:104``）。
RUN_TASK_KINDS: frozenset[str] = frozenset({"pi.generation"})
TOOL_TASK_KIND = "pi.tool"
COMPACTION_TASK_KIND = "pi.compaction"


class SlotProgress(TypedDict, total=False):
    """一次运行中的工具调用发布的东西，模型发起的与嵌套的共用（``live.ts:11-22``）。"""

    callId: str
    name: str
    status: Literal["pending", "running", "done"]
    #: 保留的运行中输出，以及边界丢掉了多少。
    output: str
    droppedBytes: int
    droppedLines: int
    #: 最后一次 ``details()`` 的值。
    details: Any
    #: 通过 ``api.diagnostic()`` 记下的说明。
    diagnostics: list[Any]


class ToolSlot(TypedDict, total=False):
    """当前这一轮里**一次模型发起的**工具调用的呈现（``live.ts:25-34``）。"""

    callId: str
    name: str
    status: Literal["pending", "running", "done"]
    output: str
    droppedBytes: int
    droppedLines: int
    details: Any
    diagnostics: list[Any]
    #: 还没开始（顺序轮）的调用、以及本次请求**没有提供**的调用**没有** ``taskId``
    #: ——后者一开始就是 ``done``，带 generation 写下的 ``entry``。
    taskId: TaskId
    #: 完成后的结果条目；工具任务出错或被孤立时缺失。
    entry: int


class NestedToolSummary(TypedDict, total=False):
    """一次嵌套调用是怎么结束的（``live.ts:53-58``）。"""

    isError: bool
    durationMs: float
    usage: JsonRepresentation
    error: str


class NestedToolSlot(TypedDict, total=False):
    """一次**嵌套**调用的呈现：运行中的工具通过 ``executeTool()`` 发起的调用（``live.ts:42-51``）。"""

    callId: str
    name: str
    status: Literal["pending", "running", "done"]
    output: str
    droppedBytes: int
    droppedLines: int
    details: Any
    diagnostics: list[Any]
    parentCallId: str
    parentTaskId: TaskId
    taskId: TaskId
    #: 发起时用的参数；一旦开始执行，会被**实际运行**的参数（修复、
    #: ``beforeTool``、强制转换之后）替换。模型发起的调用的参数在它的助手条目里。
    arguments: dict[str, Any]
    #: 完成后：这次调用怎么结束的，给状态行用。结果本身在调用者的 ``NestedCallDoc`` 里。
    summary: NestedToolSummary


class CompactionStatus(TypedDict, total=False):
    """一个正在跑的压缩任务的呈现（spec §8.7）（``live.ts:61-69``）。"""

    taskId: TaskId
    reason: str
    #: generation 是否在等它：generation **自己拥有的**压缩。
    blocking: bool
    attempt: int
    #: 下一次摘要尝试之前的持久退避。
    retry: dict[str, Any]


class CompactionFailure(TypedDict, total=False):
    """自上次压缩产出摘要以来**最新的一次失败**（spec §8.7）（``live.ts:72-77``）。"""

    taskId: TaskId
    reason: str
    #: 失败/出错结果的错误消息，或调度器把任务孤立的原因。
    error: str


class LiveState(TypedDict, total=False):
    """内建的运行中状态：运行控制 + 当前 generation 与工具轮的呈现（``live.ts:80-118``）。"""

    #: 运行控制：结算这次运行输入的那个任务，以及那些输入；**只在忙碌时存在**。
    run: dict[str, Any]
    #: 当前这次 generation 尝试的呈现。
    generation: dict[str, Any]
    #: 当前这一轮工具，按调用序，从「调用工具的那个回答」到 generation 的 ``tools`` 阶段结束它。
    tools: list[ToolSlot]
    #: 运行中工具调用的嵌套调用，按创建序——所以父在子之前、任务 ID 递增。
    #: 一次调用结算时它的嵌套槽位被移除。
    nestedTools: list[NestedToolSlot]
    #: 正在跑的压缩任务，按任务 ID 序；没有时缺失。
    compactions: list[CompactionStatus]
    #: 最新一次压缩失败，好让客户端在任务结束之后还能显示它。
    #: 产出摘要的压缩会移除它；被中止或没找到可压缩内容的**保留**它。
    compactionFailure: CompactionFailure


#: 内建的运行中状态文档（``live.ts:120-136``）。
#:
#: ``checkpointWhen`` 的规则是**spec §8.2**：**什么都不跑的时候写一个完整 base**
#: （没有 generation、也没有 running 的工具槽）。这在三种时刻成立：空闲时、
#: 把 generation 交给它的工具轮的那次提交、以及两次工具之间——所以增量链**最多跨一次 generation**
#: 或一轮工具的重叠执行。槽位只在运行中持有输出，所以每个 base 都很小；
#: 嵌套调用的结果在调用者的任务文档里，**从不在这里**。
#:
#: ⚠️ **上游的 REMINDER 明说「不要加增量计数上限」**——工具输出基准测试就是查这条规则的。
#: 这里照抄，不加。
LiveDoc = define_doc(
    {
        "kind": "pi.live",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "initial": lambda seed=None: {},
        "checkpointWhen": lambda value, ops, info: (
            value.get("generation") is None
            and not any(slot.get("status") == "running" for slot in value.get("tools") or [])
            and not any(slot.get("status") == "running" for slot in value.get("nestedTools") or [])
        ),
    }
)


def end_run(tx: Any, live: Any, task_id: TaskId, settlement: SubmissionSettlement) -> None:
    """结束 ``task_id`` 拥有的那次运行（``live.ts:143-154``）。

    结算它的每一个输入并移除 ``run``。**总是**移除 ``generation`` / ``tools`` / ``nestedTools``
    ——它们的呈现属于**正在结束的**那次运行。
    """
    run = live.get("run")
    if run is not None and run["taskId"] == task_id:
        for id in run["inputs"]:
            tx.settle_submission(id, settlement)
        del live["run"]
    for key in ("generation", "tools", "nestedTools"):
        if key in live:
            del live[key]


def add_compaction_status(live: Any, status: CompactionStatus) -> None:
    """把本次提交里创建的压缩任务的状态加进去；状态**保持任务 ID 序**（``live.ts:157-160``）。"""
    if live.get("compactions") is None:
        live["compactions"] = []
    live["compactions"].append(status)


def compaction_status(live: Any, task_id: TaskId) -> Any:
    """压缩任务 ``task_id`` 的状态，如果列着的话（``live.ts:163-165``）。"""
    statuses = live.get("compactions")
    if statuses is None:
        return None
    for status in statuses:
        if status["taskId"] == task_id:
            return status
    return None


def remove_compaction_status(live: Any, task_id: TaskId) -> None:
    """移除压缩任务 ``task_id`` 的状态，列表空了就一并移除（``live.ts:168-176``）。"""
    statuses = live.get("compactions")
    if statuses is None:
        return
    for index, status in enumerate(list(statuses)):
        if status["taskId"] == task_id:
            del statuses[index]
            break
    if len(statuses) == 0:
        del live["compactions"]


def fail_compaction_status(live: Any, task_id: TaskId, error: str) -> None:
    """移除压缩任务 ``task_id`` 的状态，并记下它**失败**了（``live.ts:179-183``）。"""
    status = compaction_status(live, task_id)
    if status is not None:
        live["compactionFailure"] = {
            "taskId": status["taskId"],
            "reason": status["reason"],
            "error": error,
        }
    remove_compaction_status(live, task_id)


def tool_slot(live: Any, task_id: TaskId) -> Any:
    """工具任务 ``task_id`` 的槽位，模型发起的或嵌套的（``live.ts:194-214``）。

    嵌套槽位用**二分查找**：一个工具可能发起上千次嵌套调用，每次都在好几次提交里查它的槽位，
    而搜索读到的**每一个元素**都要在草稿的变更跟踪器里花一个节点。
    它们的任务 ID 是递增的：每个槽位在**创建它那个任务**的提交里被追加，任务 ID 按提交序递增，
    移除也保持顺序。

    ⚠️ 上游在 ``live.tools`` 上用 ``find``（线性）——模型发起的槽位**很少**；
    只有 ``nestedTools`` 走二分。
    """
    tools = live.get("tools")
    if tools is not None:
        for slot in tools:
            if slot.get("taskId") == task_id:
                return slot
    nested = live.get("nestedTools")
    if nested is None:
        return None
    low, high = 0, len(nested) - 1
    while low <= high:
        middle = (low + high) >> 1
        candidate = nested[middle]
        if candidate["taskId"] == task_id:
            return candidate
        if candidate["taskId"] < task_id:
            low = middle + 1
        else:
            high = middle - 1
    return None


def finish_slot(slot: Any) -> None:
    """把一个槽位标记成 done 并清掉它的运行中输出（``live.ts:220-223``）。

    调用方再设模型发起调用的结果 ``entry``、或嵌套调用的 ``summary``。
    任务出错或被孤立时**两者都不设**。
    """
    slot["status"] = "done"
    clear_progress(slot)


def remove_nested_slots(live: Any, task_id: TaskId) -> None:
    """**传递地**移除工具任务 ``task_id`` 之下的嵌套槽位（``live.ts:226-236``）。

    列表里父在子之前，所以一遍扫描就够。
    """
    nested = live.get("nestedTools")
    if nested is None:
        return
    removed = {task_id}
    for slot in nested:
        if slot["parentTaskId"] in removed:
            removed.add(slot["taskId"])
    for index in range(len(nested) - 1, -1, -1):
        if nested[index]["parentTaskId"] in removed:
            del nested[index]
    if len(nested) == 0:
        del live["nestedTools"]


def clear_progress(slot: Any) -> None:
    """清掉一个工具在运行中发布的东西；它的结果条目或一次重跑会替换它（``live.ts:239-245``）。"""
    for key in ("output", "droppedBytes", "droppedLines", "details", "diagnostics"):
        if key in slot:
            del slot[key]


async def settle_scheduler_outcome(
    tx: Any,
    record: TaskRecord,
    outcome: dict[str, Any],
) -> None:
    """调度器**自己**写的终局结果（``faulted`` / ``orphaned``）的 Harness 清理（``live.ts:259-…``）。

    * 运行任务 → 结束它的运行；
    * 工具任务 → 把槽位标记 done 但**不设** ``entry``，由上下文派生合成那条缺失的结果；
    * 压缩任务 → 移除状态并记下失败。

    **其它种类一律忽略**，所以它**从不**在别处创建 ``pi.live``。调度器调用它时**并不知道任务种类**
    ——是 Harness 把它传进去的（spec §5.4）。

    ⚠️ 上游的 REMINDER：已提交的 generation 部分会在这里变成一条 **aborted 助手条目**，
    与 generation 的中止处理器**一模一样**——这样 transcript 保留模型产出过的东西、
    ``pi.usage`` 也计入它的花费。调度器那次提交**没有任务作用域**，所以那条条目没有 ``byTaskId``。
    出错来自任务 bug、畸形提供方数据（响应里的非 JSON 值）、或一次**没有效果**地被存储拒绝的提交；
    **不确定的**存储失败会让 Session 中毒，而**不**写结果。
    """
    if record.get("kind") == TOOL_TASK_KIND:
        live = await tx.doc(LiveDoc, record["conversationId"])
        slot = tool_slot(live, record["id"])
        if slot is not None:
            finish_slot(slot)
        remove_nested_slots(live, record["id"])
        return
    if record.get("kind") == COMPACTION_TASK_KIND:
        error = outcome["error"]["message"] if outcome["status"] == "faulted" else outcome["reason"]
        fail_compaction_status(await tx.doc(LiveDoc, record["conversationId"]), record["id"], error)
        return
    if record.get("kind") not in RUN_TASK_KINDS:
        return
    live = await tx.doc(LiveDoc, record["conversationId"])
    run = live.get("run")
    if run is None or run["taskId"] != record["id"]:
        return
    # ⚠️ **环上的回边**：函数内延迟导入（见 PORTING.md〈SCC 四文件的拆分方案〉）。
    # 这是**结构妥协，不是性能优化**——上游靠 ESM 提升与活绑定容忍这个环，
    # Python 不行。代价：环上若有问题，上游在 import 时报，这里在**首次调用**时报。
    from pi_durable.harness.generation import convert_partial

    await convert_partial(tx, live, record["conversationId"])
    settlement: SubmissionSettlement = (
        {"status": "unanswered", "reason": "faulted", "detail": outcome["error"]["message"]}
        if outcome["status"] == "faulted"
        else {"status": "unanswered", "reason": outcome["reason"]}
    )
    end_run(tx, live, record["id"], settlement)
