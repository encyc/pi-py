"""调度器的**模块级层** —— 移植 ``harness/scheduler.ts``(1953) 的 **Slice A**（约 265 行）。

## 为什么切在这里（按可测性，不按行数）

``scheduler.ts`` 是**一个类 + 一层模块级东西**：

* ``TaskScheduler`` —— **63 个方法、约 1640 行**，全部共享 ``#`` 私有状态；
* **模块级层**（本片）—— 类型 + 不碰类的私有状态的纯函数。

⚠️ **类本身按「函数粒度」切不开**：63 个方法读写同一份私有状态
（``#live`` / ``#below`` / ``#edges`` 等），抽走任何一个都要连带抽走那套状态——
那是**结构偏离**，不是切片。所以：

* **Slice A（本片）**：类型 + 模块级纯函数，**可独立验收** ✓
* **Slice B**：``TaskScheduler`` 类整体（63 个方法），**一个不可再切的单元**——
  届时用 `TaskRuntime` 的**契约测试**钉住它（就像 ``Tx`` 那次断言 ``Transaction`` 满足契约）

## ⚠️ 本片的一处偏离

上游在这里又写了一个 ``jsonEqual``；本端**复用** :func:`pi_chord.json.json_equal`
（语义相同：结构化、键序无关），**不重复实现第二份**。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
from dataclasses import dataclass
from typing import Any

from pi_chord.context import AbortController, AbortSignal, with_abort_signal
from pi_chord.json import copy_json, json_equal
from pi_durable.harness.util import Waiters, closed_error, scan_all
from pi_durable.session.session import SessionImpl
from pi_durable.session.transaction import Transaction
from pi_durable.types import TaskOutcome

__all__ = [
    "LIVE_STATUSES",
    "Invocation",
    "Phase",
    "Reservation",
    "TaskScheduler",
    "MAX_TIMER_DELAY",
    "SCAN_PAGE_SIZE",
    "InvocationBinding",
    "SchedulerOutcome",
    "TaskSchedulerOptions",
    "add_to",
    "can_reserve",
    "cancellation_intent",
    "conversation_node",
    "conversation_of",
    "delay",
    "delete_from",
    "ended_error",
    "erased",
    "failed_outcome",
    "json_equal",
    "memo_of",
    "missing_migration",
    "node_of",
    "overlay_of",
    "parent_node",
    "parent_of",
    "session_method",
    "with_abort_mark",
    "with_state",
]

#: 内部扫描的分页大小（``scheduler.ts:52``）。
SCAN_PAGE_SIZE = 256

#: 定时器的上限（``scheduler.ts:54``）——JS 的 ``setTimeout`` 超过它会立刻触发。
#:
#: 上游用它把长等待切成多段。本端用 ``asyncio`` 没有这个限制，
#: 但**保留这个常量与分段行为**，免得两端的退避时序在长等待上分叉。
MAX_TIMER_DELAY = 2_147_483_647

#: 调度器**看得见**的状态：终局任务不在其中（``scheduler.ts:55``）。
LIVE_STATUSES: tuple[str, ...] = ("pending", "running", "waiting", "completing")

#: 调度器**不跑任务代码**就写下的终局结果（``scheduler.ts:107``）。
#:
#: 上游是 ``Extract<TaskOutcome<JsonValue>, {status: "faulted" | "orphaned"}>``。
#: Python 没有 ``Extract``，所以这是**别名 + 说明**：只用 ``faulted`` 与 ``orphaned`` 两种。
SchedulerOutcome = TaskOutcome


@dataclass(frozen=True, slots=True)
class TaskSchedulerOptions:
    """建一个 :class:`TaskScheduler` 需要的一切（``scheduler.ts:151-197``）。

    全是**回调**：调度器不认识 Harness，Harness 把能力以函数的形式递给它——
    所以调度器能被单独构造与测试，这是 Slice B 可测的前提。
    """

    session: Any
    storage: Any
    registry: Any
    models: Any
    #: 按快照解析一个对话的 agent；运行期**每个阶段至多调一次**。
    agent: Any
    #: 解析设置；**每次访问都读**。
    settings: Any
    #: 用 ``HarnessOptions.env`` 建一个对话的环境。
    env: Any
    now: Any
    report: Any
    #: 把「调度器自己写下的终局结果」的 Harness 清理**搭在同一次提交里**。
    settle_outcome: Any
    #: 撤回一个对话排队中的输入（对话中止与中止级联用）。
    withdraw_inputs: Any
    #: 一个已存在对话的**调用期**句柄，给任务运行时与工具用。
    conversation: Any
    #: 调度器提交与调用用的上下文；**不带调用方的取消**。
    context: Any


#: 一个对话句柄绑定的**调用**：它的信号，以及一个「调用结束后就抛」的检查（``scheduler.ts:110``）。
InvocationBinding = dict[str, Any]

#: 一个节点的**不可变归属字段**（``scheduler.ts:113``）。
TaskNode = dict[str, Any]

#: 向下索引里的一个节点：一个任务，或一个对话（写成 ``c<id>``）（``scheduler.ts:133``）。
Node = Any


def conversation_node(id: int) -> str:
    """一个对话在向下索引里的节点名（``scheduler.ts:135-137``）。"""
    return f"c{id}"


def conversation_of(node: str) -> int:
    """从 ``c<id>`` 还原对话 ID（``scheduler.ts:139-141``）。"""
    return int(node[1:])


def parent_node(node: TaskNode) -> Any:
    """一个任务挂在哪个节点下面：它的拥有者任务，或它的对话（``scheduler.ts:144-146``）。"""
    owner = node.get("owner")
    return owner if owner is not None else conversation_node(node["conversationId"])


def parent_of(node: TaskNode) -> dict[str, Any]:
    """向上走一步的起点：拥有者任务，或对话（``scheduler.ts:1856-1858``）。"""
    owner = node.get("owner")
    if owner is not None:
        return {"task": owner}
    return {"conversation": node["conversationId"]}


def node_of(record: dict[str, Any]) -> TaskNode:
    """一条任务记录 → 它的归属字段（``scheduler.ts:1860-1866``）。

    ⚠️ **不含 ``id``**——它只描述「挂在谁下面」，不描述「我是谁」。
    """
    node: TaskNode = {
        "conversationId": record["conversationId"],
        "background": record["background"],
    }
    if record.get("owner") is not None:
        node["owner"] = record["owner"]
    return node


def with_abort_mark(record: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    """给一条记录打上**中止标记**（``scheduler.ts:1840-1844``）。

    ⚠️ 先把旧的 ``abortReason`` **整个删掉**再写新的——上游是
    ``const { abortReason: _reason, ...rest } = record``。
    不删的话，一次「不带原因的重启标记」会**留着上一次的原因**。
    """
    rest = {k: v for k, v in record.items() if k != "abortReason"}
    marked = {**rest, "abortRequested": True}
    if reason is not None:
        marked["abortReason"] = reason
    return marked


def failed_outcome(record: dict[str, Any]) -> bool:
    """这条记录**持有或结束于**一个非 ``completed`` 的结果（``scheduler.ts:1851-1854``）。"""
    state = record["state"]
    return (
        state["status"] in ("completing", "terminal") and state["outcome"]["status"] != "completed"
    )


def cancellation_intent(record: dict[str, Any]) -> bool:
    """一个**活着的**拥有者的持久取消意图：它的中止标记，或一个非 ``completed`` 的已持有结果
    （``scheduler.ts:1846-1849``）。

    ⚠️ ``state.status !== "terminal"`` 是**必要的**：终局任务本身不算「取消意图」，
    否则一个正常失败的历史任务会一直把它的子树当取消处理。
    """
    return record["state"]["status"] != "terminal" and (
        bool(record.get("abortRequested")) or failed_outcome(record)
    )


def with_state(record: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """替换一条**活**记录的状态；**结果一旦定了，备忘就消失**（``scheduler.ts:1914-1919``）。"""
    if state["status"] not in ("terminal", "completing"):
        return {**record, "state": state}
    rest = {k: v for k, v in record.items() if k != "memos"}
    return {**rest, "state": state}


def can_reserve(task: Any, record: dict[str, Any]) -> bool:
    """一个定义能不能接下这个任务：**版本相同**，或**更新且有迁移**（``scheduler.ts:1921-1926``）。"""
    definition = task.definition
    version = definition["version"] if isinstance(definition, dict) else definition.version
    migrate = (
        definition.get("migrate")
        if isinstance(definition, dict)
        else getattr(definition, "migrate", None)
    )
    current = record["version"]
    return version == current or (version > current and migrate is not None)


def memo_of(record: dict[str, Any] | None, name: str) -> Any:
    """**只取自己的**备忘项（``scheduler.ts:1888-1891``）。

    ⚠️ 上游注释明说：像 ``toString`` 这样的名字**不能**解析到继承来的属性。
    Python 的对应风险是 ``__class__`` / ``__len__`` 之类的**魔术名**，
    所以这里用 ``in`` 而**不是** ``getattr``——``getattr`` 会一路走到类上去。
    """
    if record is None:
        return None
    memos = record.get("memos")
    if memos is None or name not in memos:
        return None
    return memos[name]


def erased(task: Any) -> Any:
    """把一个任务的定义**擦除类型**（``scheduler.ts:1905-1907``）。

    调度器只按运行期字段用它，不需要 ``I``/``S``/``R``/``H``。
    """
    return task.definition


def missing_migration(record: dict[str, Any], definition: Any) -> Exception:
    """没有可从 ``record.version`` 迁到 ``definition.version`` 的迁移（``scheduler.ts:1901-1903``）。"""
    version = definition["version"] if isinstance(definition, dict) else definition.version
    return RuntimeError(
        f"Task {record['kind']} version {version} has no migration from {record['version']}"
    )


def ended_error(invocation: Any) -> Exception:
    """一次调用结束后，任何操作都抛这个（``scheduler.ts:1909-1911``）。"""
    task_id = invocation["taskId"] if isinstance(invocation, dict) else invocation.task_id
    return RuntimeError(f"Task {task_id} invocation has ended")


def session_method(session: SessionImpl, name: str) -> Any:
    """把一个 Session 方法**绑定**好，用于原样转发参数表（``scheduler.ts:1894-1899``）。

    上游用 ``session[name].bind(session)``。Python 里 ``getattr`` 取到的**已经是绑定方法**，
    所以这里就是取属性——**但保留这层封装**：它把「允许转发的三个名字」
    写在一处，调用点就不必各自记得。
    """
    return getattr(session, name)


def add_to(map_: dict[Any, set[Any]], key: Any, value: Any) -> None:
    """往一个「键 → 集合」的索引里加一项（``scheduler.ts:1874-1878``）。"""
    values = map_.get(key)
    if values is None:
        map_[key] = {value}
    else:
        values.add(value)


def delete_from(map_: dict[Any, set[Any]], key: Any, value: Any) -> None:
    """从索引里删一项；**集合空了就把键也删掉**（``scheduler.ts:1880-1886``）。

    留着空集合会让「这个节点还有没有下层活」的判断一直为真，
    于是它永远不会被回收。有测试钉住这条。
    """
    values = map_.get(key)
    if values is None:
        return
    values.discard(value)
    if not values:
        del map_[key]


def overlay_of(tx: Transaction) -> dict[str, Any]:
    """一次提交**暂存**的记录，它们在归属遍历里**覆盖**已提交的（``scheduler.ts:1868-1872``）。"""
    tasks = {record["id"]: record for record in tx.staged_tasks()}
    edges = {
        record["id"]: (record["owner"]["taskId"] if record.get("owner") is not None else None)
        for record in tx.staged_conversations()
    }
    return {"tasks": tasks, "edges": edges}


async def delay(ms: float, signal: AbortSignal) -> None:
    """等 ``ms`` 毫秒；``signal`` 中止时**用它自己的原因**拒绝（``scheduler.ts:1928-1941``）。

    ⚠️ 上游在**超时**时移除监听器、在**中止**时清掉定时器——两边都要做，
    否则长等待会攒下一堆不会再触发的监听器。

    ⚠️ 本端把 ``ms`` 切成 ``MAX_TIMER_DELAY`` 的段：``asyncio`` 本身没有 JS 那个 32 位上限，
    但**分段保持与上游一样的唤醒时序**（每一步都重新检查信号）。
    """
    remaining = ms
    while remaining > 0:
        step = min(remaining, MAX_TIMER_DELAY)
        try:
            await asyncio.wait_for(_sleep_until_aborted(signal), timeout=step / 1000)
        except TimeoutError:
            remaining -= step
            continue
        return
    # ⚠️ 剩余为 0 时**仍然要检查一次中止**。
    # （第一版这里只写了注释、没写代码——测试立刻抓到了「注释承诺了但没做」。）
    if signal.aborted:
        raise _abort_reason(signal)


async def _sleep_until_aborted(signal: AbortSignal) -> None:
    """等到信号中止（正常路径是外层 ``wait_for`` 超时）。"""
    if signal.aborted:
        raise _abort_reason(signal)
    event = asyncio.Event()

    def on_abort() -> None:
        event.set()

    signal.add_listener(on_abort)
    try:
        await event.wait()
    finally:
        signal.remove_listener(on_abort)
    raise _abort_reason(signal)


def any_signal(signals: list[Any]) -> Any:
    """把多个信号合成一个——任一取消就取消（``AbortSignal.any`` 的对应物）。

    ⚠️ **不是 ``asyncio`` 的东西**：这是 chord 的 ``AbortSignal``。
    只有一个信号时直接返回它（省一层包装）。
    """
    if len(signals) == 1:
        return signals[0]
    from pi_chord.context import AbortSignal as _Signal

    return _Signal.any(signals)


def schedule_coroutine(coro: Any) -> None:
    """把一个协程排进事件循环；**没有循环时丢掉**（关闭路径上不该因此抛）。"""
    if _has_loop():
        asyncio.ensure_future(coro)


def schedule_watch_stop(watch: Any) -> None:
    """请求停掉一个观察，**不等它、也不因为它的失败而抛**（``void watch.stop()``）。

    ⚠️ ``watch.stop()`` 是异步的，而这里在**同步的关闭路径**上——
    所以只能排进循环。**关闭路径不能因为一个观察停不掉而中断。**
    """
    schedule_coroutine(_swallow(watch.stop()))


async def _swallow(awaitable: Any) -> None:
    """等一个 awaitable，**吞掉它的异常**（``.catch(() => {})`` 的对应物）。

    ⚠️ **必须真的 await 它**：`read_on_line` 是协程，不 await 就永远不会跑。
    """
    # 上游用 `.catch(() => {})` 吞掉一切。
    with contextlib.suppress(BaseException):
        await awaitable


def _abort_reason(signal: AbortSignal) -> BaseException:
    reason = getattr(signal, "reason", None)
    if isinstance(reason, BaseException):
        return reason
    from pi_chord.context import AbortError

    return AbortError("The operation was aborted")


# --------------------------------------------------------------------------------------
# TaskScheduler：核心类型（``scheduler.ts:56-103``）
# --------------------------------------------------------------------------------------


class _Missing:
    """「**还没加载**」的哨兵——与 ``None``（「无主」）是**两个不同的答案**。"""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"


#: ``#edge()`` 的「还没加载」。**不能压成 ``None``**：``None`` 表示**确定无主**。
_MISSING = _Missing()


#: 一个 pending 任务为什么不能在某个注册表快照下被预留（``scheduler.ts:57``）。
#:
#: **派生的，从不持久化。**
BlockedReason = str

#: 一次「能不能接下」的判定（``scheduler.ts:59-61``）。
Resolution = dict[str, Any]

#: 一个**能**接下某记录的定义，或者没有能接下的原因（``scheduler.ts:63-66``）。
Fit = dict[str, Any]


@dataclass(slots=True)
class Invocation:
    """一次**在内存里**的任务执行，run 或 abort 模式（``scheduler.ts:70-83``）。

    ⚠️ ``ended`` 是**可变的**：调用结束的那一刻置位，之后任何操作都拒绝它。
    """

    task_id: int
    conversation_id: int
    mode: str  # "run" | "abort"
    controller: Any
    context: Any
    #: 通过运行时取得的观察；**调用结束时停掉**。
    watches: set[Any] = dataclasses.field(default_factory=set)
    ended: bool = False
    done: Any = None
    finish: Any = None

    def __post_init__(self) -> None:
        if self.done is None:
            self.done = asyncio.get_event_loop().create_future() if _has_loop() else None


def _has_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@dataclass(slots=True)
class Phase:
    """一个 phase 处理器读的东西：该阶段的快照与任务，以及**懒解析**的 agent
    （``scheduler.ts:85-89``）。"""

    snapshot: Any
    task: Any
    agent: Any = None


@dataclass(frozen=True, slots=True)
class Reservation:
    """一次预留：调用 + 任务 + 快照（``scheduler.ts:91-96``）。"""

    invocation: Any
    task: Any
    snapshot: Any


#: 刚刚返回的那个 phase 的结果，由下一步判定（``scheduler.ts:101``）。
PhaseResult = dict[str, Any]

#: 一步的决策：继续下一个 phase、结束调用，或者**写 ``faulted``** 结束
#: （``scheduler.ts:104``）。
Decision = Any


class TaskScheduler:
    """一个 Harness 的**持久任务调度器**（``scheduler.ts:199-1839``）。

    ## 它的状态模型（本片落地的部分）

    **40 个字段**分三类：

    * **13 个来自 options**（``#session``/``#storage``/``#registry``/…）——调度器不认识 Harness，
      Harness 把能力以**回调**递进来。**这正是它能被单独构造与测试的前提。**
    * **19 个状态容器**：
      * **归属图**：``#edges``（每个已知对话的拥有者任务，``None`` 表示无主）、
        ``#settled``（已知终局任务的归属字段）、``#below``（**活的活**周围那棵树）、
        ``#roots``（有活的无主对话）、``#unloaded``（链可能没加载的已知任务）；
      * **工作队列**：``#live``（每条已提交的非终局任务记录）、``#intent``（有取消意图的活任务）、
        ``#runnable``（不是 ``completing`` 的活任务，预留遍历的候选）、
        ``#finalizeChecks``、``#waiters``、``#failFastChecks``、``#abandoned``；
      * **其它**：``#contexts``/``#expiry``/``#invocations``/``#taskWaiters``/``#idleWaiters``/
        ``#failedMigrations``/``#dropQueue``。
    * **8 个标志**：``#reconcileScheduled``/``#sweepScheduled``/``#cascadePending``/
      ``#unsubscribeRegistry``/``#enabled``/``#closing``/``#dirty``/``#draining``。

    ## ⚠️ 为什么这一整个类是不可切的单元

    **``#track`` 同时维护「归属图」与「工作队列」**——一次写 ``#live``/``#settled``/``#below``
    **和** ``#runnable``/``#intent``/``#finalizeChecks``/``#waiters``。
    **一个方法横跨两个概念时，任何按概念切的分法都会把它劈开**，而劈开它就要把它的状态也劈开。
    详见 PORTING.md〈不可切的具体成因〉。

    ## 不变量（上游类文档，逐字保留其含义）

    ``#live`` 镜像**每一条已提交的非终局任务记录**。**同步的提交监听器在 Session 线上更新它**，
    所以在线上跑的代码从它读到的**恰好是已提交状态**。

    任务与对话构成**一棵归属树**（spec §5.5）：一个任务的父是它的拥有者任务、或它的对话；
    一个对话的父是它的拥有者任务（如果有）。向上的遍历决定什么能到达某个任务：
    一个正在取消的拥有者、一个作用域。``#below`` 把树**向下**列在活的活周围。

    **不变量**：每一次任务状态转换都由**一个在 Session 线上串行化的回调**决定并写下。
    它覆盖预留、标记、运行时提交、终结，以及每个 phase 之前那一步同步的**优先级裁决**。
    处理器与 join **不在线上**跑。一次调用**在决定它结束的那一步之内**结束——
    所以它排队的运行时提交要么落在那个决定**之前**，要么被拒绝。
    """

    def __init__(self, options: TaskSchedulerOptions) -> None:
        # ── 来自 options 的 13 个能力 ────────────────────────────────────────────────
        self._session = options.session
        self._storage = options.storage
        self._registry = options.registry
        self._models = options.models
        self._agent = options.agent
        self._settings = options.settings
        self._env = options.env
        self._now = options.now
        self._report = options.report
        self._settle_outcome = options.settle_outcome
        self._withdraw_inputs = options.withdraw_inputs
        self._conversation = options.conversation
        self._context = options.context

        # ── 状态容器 ────────────────────────────────────────────────────────────────
        #: 每条**已提交的非终局**任务记录。
        self._live: dict[int, dict[str, Any]] = {}
        #: 每个对话**最后一次**经任务运行时读到的上下文范围；``idleSince`` 是首次见到它空闲的
        #: Harness 时间。**派生、从不持久化**。
        self._contexts: dict[int, dict[str, Any]] = {}
        #: 最早一个空闲上下文过期用的定时器（见 ``_schedule_expiry``）。
        self._expiry: dict[str, Any] | None = None
        self._invocations: dict[int, Invocation] = {}
        self._task_waiters: Any = Waiters()
        #: 按对话的空闲等待者；``None`` 表示等**整个** Harness。
        self._idle_waiters: Any = Waiters()
        #: 每个任务上迁移失败的那个定义；**只有注册表解析出另一个定义**时才重试。
        self._failed_migrations: dict[int, dict[str, Any]] = {}
        #: 每个已知对话的拥有者任务，``None`` 表示无主。
        self._edges: dict[int, int | None] = {}
        #: 已知**终局**任务的归属字段，遍历会穿过它们。
        self._settled: dict[int, Any] = {}
        #: 发现「不再需要」的已结束任务与对话；由提交发布末尾的 ``_sweep()`` 丢弃——
        #: 链加载会**先装节点、后列下层**，所以中间态看起来是「不需要」的。
        self._drop_queue: set[Any] = set()
        #: 活的活周围那棵**向下**的树。
        self._below: dict[Any, set[Any]] = {}
        self._roots: set[int] = set()
        #: **链可能没加载**的已知任务；其它已知任务的链**已加载、且保持已加载**。
        self._unloaded: set[int] = set()
        #: 有取消意图的活任务——中止级联的源头（spec §5.4）。
        self._intent: set[int] = set()
        #: 不是 ``completing`` 的活任务，预留遍历的候选；发现自己在等待的会离开它。
        self._runnable: set[int] = set()
        #: 下一次终结遍历要检查的 ``completing`` 任务。
        self._finalize_checks: set[int] = set()
        #: 按 ``on`` 里每个任务索引的活等待任务。
        self._waiters: dict[int, set[int]] = {}
        #: 下一次对账要查「``on`` 里有没有失败任务」的 ``failFast`` 等待者。
        self._fail_fast_checks: set[int] = set()
        #: 开档时发现的、带 ``abandonOnRestart`` 的活任务（来自更早的 Harness）。
        #: 第一次预留遍历会把它们打上 reason ``restart`` 的中止标记。
        self._abandoned: set[int] = set()

        # ── 标志 ────────────────────────────────────────────────────────────────────
        self._reconcile_scheduled = False
        self._sweep_scheduled = False
        self._cascade_pending = False
        self._unsubscribe_registry: Any = lambda: None
        self._enabled = False
        self._closing = False
        self._dirty = False
        self._draining = False

    async def join(self) -> None:
        """给**每一个**调用发信号并等它们；**什么都不写**（``scheduler.ts:129-135``）。

        ⚠️ Close 在**每一个关闭监听器都跑完之后**才调它——这样任务持有的等待、观察与流
        是以 **Session 的原因**结束的，而**不是**被它自己的信号当成取消。
        这个顺序很重要：反过来会让「Harness 关闭」在 transcript 里显示成「用户取消了任务」。
        """
        for invocation in list(self._invocations.values()):
            invocation.controller.abort()
        # ⚠️ 没有 `done` 的调用要**跳过**，不能把它塞进 `gather`（会 TypeError）。
        # 正常路径下 `Invocation` 是在事件循环里建的，`done` 一定有；
        # 但 `join` 是**关闭路径**上的代码，它不该因为一个缺失的 Future 而抛。
        pending = [
            invocation.done
            for invocation in self._invocations.values()
            if invocation.done is not None
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ─── 归属（``scheduler.ts:680-1073``，C3）────────────────────────────────────────

    async def _load_scopes(self, queued: bool) -> list[int]:
        """加载 ``#unloaded`` 里那些任务的拥有者链；``queued`` 时**还要**加载每个有排队输入的对话
        （``scheduler.ts:680-697``）。

        ⚠️ **直接读已提交的 Storage**，所以它**可以在提交回调里跑**。
        """
        for id in list(self._unloaded):
            # 这一轮里更早的一次加载可能已经把它处理掉了。
            if id in self._unloaded:
                await self._load_chain({"task": id})
        if not queued:
            return []
        submissions = await scan_all(
            lambda cursor: self._storage.scan_submissions(
                {"status": "queued"}, SCAN_PAGE_SIZE, cursor, self._context
            )
        )
        inputs = [s for s in submissions if s["type"] == "input"]
        conversations = list(dict.fromkeys(s["conversationId"] for s in inputs))
        for id in conversations:
            await self._load_chain({"conversation": id})
        return conversations

    async def _load_chain(
        self, start: dict[str, Any], overlay: dict[str, Any] | None = None
    ) -> None:
        """从 ``start`` 向上加载拥有者边与任务节点，直到无主的根（``scheduler.ts:699-733``）。

        ``#unloaded`` **之外**的已知任务会**结束这次遍历**——它的链已经加载了；
        途中经过的 ``#unloaded`` 任务在之后就有了加载好的链。

        ⚠️ **加载一个节点可能揭示一个正在取消的拥有者**，所以它会让**下一次对账**重新推导级联标记。
        """
        passed: list[int] = []
        at: dict[str, Any] | None = start
        while at is not None:
            if "task" in at:
                id = at["task"]
                node = self._live.get(id) or self._settled.get(id)
                # 本次提交里创建的：从它自己的记录加载覆盖了它上面的东西。
                if node is None and overlay is not None and id in overlay["tasks"]:
                    break
                if node is not None and id not in self._unloaded:
                    break
                if node is None:
                    record = await self._storage.task(id, self._context)
                    if record is None:
                        return
                    node = node_of(record)
                    if record["state"]["status"] == "terminal":
                        self._settle_node(id, node)
                    # 加载出来的节点可能揭示一个正在取消的拥有者：下一次对账会重新推导标记。
                    self._schedule_reconcile()
                passed.append(id)
                at = parent_of(node)
            else:
                edge = self._edge(at["conversation"], overlay)
                if edge is _MISSING:
                    record = await self._storage.conversation(at["conversation"], self._context)
                    edge = (
                        record["owner"]["taskId"]
                        if record is not None and record.get("owner")
                        else None
                    )
                    self._set_edge(at["conversation"], edge)
                    self._schedule_reconcile()
                at = None if edge is None else {"task": edge}
        for id in passed:
            self._unloaded.discard(id)

    def _set_edge(self, conversation_id: int, owner: int | None) -> None:
        """记下一个对话的拥有者边（``scheduler.ts:537-543``）。"""
        self._edges[conversation_id] = owner
        # 这个对话现在挂在它的拥有者下面、或者是个根；它下面没有东西，所以等扫除。
        self._relist(conversation_node(conversation_id))
        self._drop_if_unneeded(conversation_node(conversation_id))

    def _settle_node(self, id: int, node: Any) -> None:
        """记下一个链加载读到的**终局**任务，好让遍历穿过它；它自己的链**还没加载**
        （``scheduler.ts:545-551``）。"""
        self._settled[id] = node
        self._unloaded.add(id)
        self._relist(id)
        self._drop_if_unneeded(id)

    def _drop_if_unneeded(self, node: Any) -> None:
        """它下面没有列出任何东西时，把一个已结束的任务或一个对话**排进扫除队列**
        （``scheduler.ts:553-559``）。"""
        if not isinstance(node, str) and node in self._live:
            return
        if node in self._below:
            return
        self._drop_queue.add(node)
        self._schedule_sweep()

    def _schedule_sweep(self) -> None:
        """在**会话线上的一个独立作业**里扫除（``scheduler.ts:564-578``）。

        ⚠️ 之所以要独立作业：**一次被拒绝的提交、一次没什么可写的提交、以及一次读，
        都不会发布任何东西**——所以在 ``#observe`` 末尾扫除的话，它们加载的东西就扫不到。
        """
        if self._sweep_scheduled or self._closing:
            return
        # ⚠️ **必须在置位之前查有没有事件循环**（与 `_schedule_reconcile` 同一取舍）：
        # `ensure_future` 需要一个**正在跑的**循环（R15）。没有循环时**不置位**——
        # 置了位就再也不会有下一次扫除，那棵树会永远留着。
        if not _has_loop():
            return
        self._sweep_scheduled = True

        async def job() -> None:
            self._sweep_scheduled = False
            self._sweep()

        # ⚠️ 只在 Session 正在关闭或已失败时才拒绝；那时反正什么都不留。
        # 这里**不能**照抄 JS 的 `.catch(() => {})`——Python 的 awaitable 没有 `.catch`，
        # 而且 `read_on_line(job)` 会被调用**两次**（第一版就是这么错的）。
        asyncio.ensure_future(_swallow(self._session.read_on_line(job)))

    def _sweep(self) -> None:
        """丢弃队列里**仍然**不再需要的节点（``scheduler.ts:580-597``）。

        ⚠️ **一个平铺的循环**：取消列出时，向上路径上每个「刚失去最后一个已列出节点」的节点
        **已经被排队了**。
        """
        for node in self._drop_queue:
            if node in self._below:
                continue
            if isinstance(node, str):
                id = conversation_of(node)
                self._edges.pop(id, None)
                self._roots.discard(id)
            elif node not in self._live:
                self._settled.pop(node, None)
                self._unloaded.discard(node)
        self._drop_queue.clear()

    def _track(self, previous: dict[str, Any] | None, record: dict[str, Any]) -> None:
        """把调度器的各个集合与一条**已提交**的任务记录对齐（``scheduler.ts:599-633``）。

        ⚠️ **这就是那个「横跨两个概念」的中枢方法**：它一次写**归属图**
        （``#live``/``#settled``/``#below``）**和**工作队列
        （``#runnable``/``#intent``/``#finalizeChecks``/``#waiters``）。
        任何按概念切的分法都会把它劈开——见 PORTING.md〈不可切的具体成因〉。
        """
        id = record["id"]
        if previous is not None and previous["state"]["status"] == "waiting":
            for on in previous["state"]["on"]:
                delete_from(self._waiters, on, id)
        if record["state"]["status"] == "terminal":
            node = node_of(record)
            self._live.pop(id, None)
            self._intent.discard(id)
            self._runnable.discard(id)
            self._finalize_checks.discard(id)
            self._settled[id] = node
            # 在任何东西被忘掉**之前**：向上走到它所持有的那个任务的路径会穿过它们之间的已结束任务。
            self._release(node)
            for waiter in sorted(self._waiters.get(id) or ()):
                self._runnable.add(waiter)
            self._relist(id)
            self._drop_if_unneeded(id)
            return
        self._live[id] = record
        if previous is None:
            if not self._known(parent_node(record)):
                self._unloaded.add(id)
            self._relist(id)
        if cancellation_intent(record):
            self._intent.add(id)
        else:
            self._intent.discard(id)
        if record["state"]["status"] == "completing":
            self._runnable.discard(id)
            self._finalize_checks.add(id)
        else:
            self._runnable.add(id)
        if record["state"]["status"] == "waiting":
            for on in record["state"]["on"]:
                add_to(self._waiters, on, id)

    def _release(self, node: Any, overlay: dict[str, Any] | None = None) -> Any:
        """已结束的普通活之上最近的那个**活**任务可能空出来了（``scheduler.ts:635-641``）：

        一个**持有自己结果**的、或一个**已打中止标记**的。
        """
        above = self._nearest_live_above(node, overlay)
        if above is not None and above["state"]["status"] == "completing":
            self._finalize_checks.add(above["id"])
        elif above is not None and above.get("abortRequested") is True:
            self._runnable.add(above["id"])
        return above

    def _relist(self, start: Any) -> None:
        """把 ``start`` 列进它父节点下的 ``#below``（``scheduler.ts:643-683``）。

        条件是「它是活任务**或**它下面有列出的节点」；否则取消列出，然后对它的父做同样的事，
        一路向上直到不再变化。**所以 ``#below`` 恰好持有活工作周围那棵树**；
        一个失去最后一个已列出节点的父会被**排进扫除队列**。

        ⚠️ **循环而不是递归**：拥有者链可能比调用栈还深。
        """
        node = start
        while True:
            needed = (not isinstance(node, str) and node in self._live) or node in self._below
            parent: Any
            if isinstance(node, str):
                parent = self._edges.get(conversation_of(node), _MISSING)
            else:
                known = self._live.get(node) or self._settled.get(node)
                if known is None:
                    return
                parent = parent_node(known)
            if parent is _MISSING:
                return
            if parent is None:
                root = conversation_of(node)
                if needed:
                    self._roots.add(root)
                else:
                    self._roots.discard(root)
                return
            listed = node in (self._below.get(parent) or set())
            if listed == needed:
                return
            if needed:
                first = parent not in self._below
                add_to(self._below, parent, node)
                if not first:
                    return
            else:
                delete_from(self._below, parent, node)
                if parent in self._below:
                    return
                self._drop_if_unneeded(parent)
            node = parent

    def _nearest_live_above(self, node: Any, overlay: dict[str, Any] | None = None) -> Any:
        """已结束的普通活 ``node`` 之上最近的那个**活**任务（``scheduler.ts:685-697``）。

        ⚠️ **后台任务及其下面的工作不持有任何东西**——所以循环条件是 ``!current.background``。
        """
        current = node
        while not current["background"]:
            owner = current.get("owner")
            if owner is None:
                owner = self._edge(current["conversationId"], overlay)
            if owner is None:
                return None
            record = (
                overlay["tasks"].get(owner) if overlay is not None else None
            ) or self._live.get(owner)
            if record is not None and record["state"]["status"] != "terminal":
                return record
            above = record if record is not None else self._settled.get(owner)
            if above is None:
                return None
            current = above
        return None

    def _known(self, node: Any) -> bool:
        """``node`` 之上的链**是否已加载**——不靠遍历（``scheduler.ts:699-706``）：

        ``#unloaded`` **之外**的已知任务，链已经加载了。
        """
        if not isinstance(node, str):
            return (node in self._live or node in self._settled) and node not in self._unloaded
        owner = self._edges.get(conversation_of(node), _MISSING)
        if owner is _MISSING:
            return False
        return owner is None or self._known(owner)

    def _edge(self, id: int, overlay: dict[str, Any] | None = None) -> Any:
        """一个对话的拥有者任务；``None`` 表示无主，``_MISSING`` 表示**还没加载**
        （``scheduler.ts:708-710``）。"""
        if overlay is not None and id in overlay["edges"]:
            return overlay["edges"][id]
        return self._edges.get(id, _MISSING)

    def _node(self, id: int, overlay: dict[str, Any] | None = None) -> Any:
        """一个任务的归属字段（``scheduler.ts:712-714``）。"""
        if overlay is not None and id in overlay["tasks"]:
            return overlay["tasks"][id]
        return self._live.get(id) or self._settled.get(id)

    def _above(self, start: dict[str, Any], overlay: dict[str, Any] | None = None) -> Any:
        """从 ``start`` 向上走：拥有者任务与对话，直到**无主的根**或**一条还没加载的边**
        （``scheduler.ts:716-735``）。"""
        at: dict[str, Any] | None = start
        # ⚠️ **偏离**：上游**没有**环检测——它依赖「归属图是棵树」这条不变量。
        # 但违反不变量的代价在 Python 里是**无限循环**（一个静默挂起），
        # 而在上游那边同样会挂住。**把挂起换成一个明确的错误**——
        # 这是本端唯一能在「不变量被破坏」时救回诊断信息的地方。
        # 见 PORTING.md 的偏离登记。
        seen: set[Any] = set()
        while at is not None:
            key = ("task", at["task"]) if "task" in at else ("conversation", at["conversation"])
            if key in seen:
                raise RuntimeError(
                    f"Ownership cycle at {key[0]} {key[1]}: the task graph must be a tree"
                )
            seen.add(key)
            if "task" in at:
                node = self._node(at["task"], overlay)
                if node is None:
                    yield {"unknown": True}
                    return
                yield {"task": at["task"], "node": node}
                at = parent_of(node)
            else:
                yield {"conversation": at["conversation"]}
                edge = self._edge(at["conversation"], overlay)
                if edge is _MISSING:
                    yield {"unknown": True}
                    return
                at = None if edge is None else {"task": edge}

    def _walk_down(self, start: Any, options: dict[str, Any] | None = None) -> Any:
        """向下走到 ``start`` 下面的任务（``scheduler.ts:737-776``）。

        **后台任务会被到达但不进入**（除非 ``cross_background``），``enter`` 拒绝的也不进入。
        ⚠️ **循环而不是递归**：链可能比调用栈还深。
        """
        options = options or {}
        overlay = options.get("overlay")
        cross_background = bool(options.get("crossBackground"))
        enter = options.get("enter")
        links = None if overlay is None else self._links(overlay)
        pending: list[Any] = [start]
        while pending:
            at = pending.pop()
            for children in (self._below.get(at), None if links is None else links.get(at)):
                for child in children or ():
                    if isinstance(child, str):
                        pending.append(child)
                        continue
                    record = (
                        overlay["tasks"].get(child) if overlay is not None else None
                    ) or self._live.get(child)
                    node = record if record is not None else self._settled.get(child)
                    if node is None:
                        continue
                    live = (
                        record
                        if record is not None and record["state"]["status"] != "terminal"
                        else None
                    )
                    yield {"id": child, "node": node, "live": live}
                    if (cross_background or not node["background"]) and (
                        enter is None or enter(child)
                    ):
                        pending.append(child)

    def _links(self, overlay: dict[str, Any]) -> dict[Any, set[Any]]:
        """向下遍历需要的、本次提交新任务的链接（``scheduler.ts:778-807``）。"""
        if overlay.get("links") is not None:
            existing: dict[Any, set[Any]] = overlay["links"]
            return existing
        links: dict[Any, set[Any]] = {}
        for record in overlay["tasks"].values():
            if record["id"] in self._live or record["id"] in self._settled:
                continue
            child: Any = record["id"]
            parent: Any = parent_node(record)
            while parent is not None and child not in (links.get(parent) or set()):
                add_to(links, parent, child)
                if not isinstance(parent, str) and (
                    parent in self._live or parent in overlay["tasks"]
                ):
                    break
                if isinstance(parent, str):
                    above = self._edge(conversation_of(parent), overlay)
                else:
                    fields = self._settled.get(parent)
                    above = None if fields is None else parent_node(fields)
                if (
                    above is not None
                    and above is not _MISSING
                    and child is not above
                    and (parent in (self._below.get(above) or set()))
                ):
                    break
                child = parent
                parent = None if above is _MISSING else above
        overlay["links"] = links
        return links

    def _owned_live(self, owner: int, options: dict[str, Any] | None = None) -> list[int]:
        """``owner`` 的**活的普通拥有工作**（spec §5.5）（``scheduler.ts:809-825``）：

        它下面、向下走能到达、且**不进入后台任务**的活非后台任务；``first`` 时只取第一个。
        """
        options = options or {}
        overlay = options.get("overlay")
        first = bool(options.get("first"))
        done = options.get("done")
        walk_options: dict[str, Any] = {}
        if overlay is not None:
            walk_options["overlay"] = overlay
        if done is not None:
            walk_options["enter"] = lambda id: id not in done
        found: list[int] = []
        for step in self._walk_down(owner, walk_options):
            if step["live"] is None or step["node"]["background"]:
                continue
            found.append(step["id"])
            if first:
                break
        return found

    def _has_owned_live(
        self, owner: int, overlay: dict[str, Any] | None = None, done: set[int] | None = None
    ) -> bool:
        """``owner`` 下面还有没有活的普通拥有工作（``scheduler.ts:827-834``）。"""
        options: dict[str, Any] = {"first": True}
        if overlay is not None:
            options["overlay"] = overlay
        if done is not None:
            options["done"] = done
        return len(self._owned_live(owner, options)) > 0

    def _in_scope(
        self, start: dict[str, Any], scope: dict[str, Any], cross_background: bool = False
    ) -> Any:
        """从 ``scope`` 出发的普通遍历**能不能到达** ``start``（``scheduler.ts:836-855``）。

        向上走要到达 scope 的那个对话（``roots`` 时是任意无主对话），且**不穿过后台拥有者**。
        **边还没加载**时返回 ``None``——**「不知道」不是 ``False``**。
        """
        for step in self._above(start):
            if step.get("unknown"):
                return None
            if "conversation" in step:
                if "conversation" in scope and step["conversation"] == scope["conversation"]:
                    return True
            elif step["node"]["background"] and not cross_background:
                return False
        return "roots" in scope

    def _below_cancelled(self, start: dict[str, Any]) -> bool:
        """一个活拥有者的取消意图**能不能到达** ``start``（``scheduler.ts:857-860``）。"""
        return self._cancelling_owner(start) is not None

    def _cancelling_owner(self, start: dict[str, Any]) -> Any:
        """``start`` 之上最近的、取消意图能到达它的**活**拥有者（``scheduler.ts:862-874``）。

        ⚠️ **``#intent`` 为空时直接返回**——那是常见情形，**一次遍历都不做**。
        """
        if not self._intent:
            return None
        for step in self._above(start):
            if step.get("unknown"):
                return None
            if "task" not in step:
                continue
            live = self._live.get(step["task"])
            if live is not None and cancellation_intent(live):
                return live
            if step["node"]["background"]:
                return None
        return None

    # ─── 对账（``scheduler.ts:304-481``，C2 的调度部分）────────────────────────────

    def _fail_session(self, error: BaseException) -> None:
        """因为调度器自己的一次提交里抛了，就把 Session 弄失败（``scheduler.ts:876-879``）。

        ⚠️ **一次提交回调里抛出的错误不可能靠"重跑一遍"修好**——那是存储故障、宿主回调、或者 bug。
        所以它**弄失败 Session**（Session 会报告它），而不是重试。
        """
        if not self._closing:
            self._session.fail(error)

    def _context_retention_ms(self) -> float:
        """``settings.contextRetentionMs``；**读它自己也可能抛**，那时报告并用 ``0``
        （``scheduler.ts:304-311``）。

        ⚠️ 用 ``0`` 兜底是**保守**的选择：``0`` 表示「一空闲就丢」，于是**最多只是少一点缓存**，
        而不是无限期留着上下文。
        """
        try:
            return float(self._settings()["contextRetentionMs"])
        except BaseException as error:  # noqa: BLE001 - 上游同样捕获一切
            self._report(error)
            return 0.0

    def _settle_idle(self) -> None:
        """结算空闲等待者，并丢弃每个**已空闲超过保留期**的上下文（``scheduler.ts:313-337``）。"""
        for conversation_id in self._idle_waiters.keys():  # noqa: SIM118 - Waiters 不是 dict
            if self._idle(conversation_id):
                self._idle_waiters.resolve(conversation_id, None)
        now = self._now()
        retention = self._context_retention_ms()
        for conversation_id, kept in list(self._contexts.items()):
            idle_since = kept.get("idleSince")
            if idle_since is not None and now - idle_since >= retention:
                del self._contexts[conversation_id]
            elif not self._idle(conversation_id):
                kept["idleSince"] = None
            elif retention > 0:
                if kept.get("idleSince") is None:
                    kept["idleSince"] = now
            else:
                del self._contexts[conversation_id]
        self._schedule_expiry()

    def _schedule_expiry(self) -> None:
        """在最早一个空闲上下文过期时跑 ``_settle_idle()``（``scheduler.ts:339-367``）。

        ⚠️ 上游在**定时器不能 unref 的环境**（如 Cloudflare Workers）里**一个都不留**——
        挂着的定时器会阻止 Durable Object 被驱逐，而驱逐本来会释放上下文。

        ⚠️ **本端偏离**：Python 的 ``asyncio`` 定时器不会像 JS 那样把进程钉住，
        所以这里**总是**留一个真正的定时器，并用 ``call_later`` 的 handle 做同样的
        「同一个 ``at`` 就不重排」判断。见 PORTING.md 的偏离登记。
        """
        retention = self._context_retention_ms()
        at: float | None = None
        for kept in self._contexts.values():
            idle_since = kept.get("idleSince")
            if idle_since is not None and (at is None or idle_since + retention < at):
                at = idle_since + retention
        if self._expiry is not None and self._expiry["at"] == at:
            return
        if self._expiry is not None:
            self._expiry["timer"].cancel()
        self._expiry = None
        if at is None or self._closing or not _has_loop():
            return
        delay_ms = min(max(0.0, at - self._now()), MAX_TIMER_DELAY)

        def fire() -> None:
            self._expiry = None
            self._settle_idle()

        loop = asyncio.get_running_loop()
        timer = loop.call_later(delay_ms / 1000, fire)
        self._expiry = {"at": at, "timer": timer}

    def _schedule_reconcile(self) -> None:
        """排一次对账（``scheduler.ts:567-571``）。

        ⚠️ 用 ``call_soon`` 而**不是** ``ensure_future``（**R2**）：``queueMicrotask`` 的对应物是
        ``call_soon``，而 ``#reconcile`` 是协程——所以在 ``call_soon`` 的回调里才创建 task。
        **在 ``call_soon`` 里返回一个协程是错的**（没人 await 它）。
        """
        if self._reconcile_scheduled or self._closing:
            return
        self._reconcile_scheduled = True
        if not _has_loop():
            return
        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self._reconcile()))

    async def _reconcile(self) -> None:
        """**一次提交**，把已提交记录蕴含的东西施加下去（``scheduler.ts:573-629``）：

        活的、有取消意图的拥有者下面的中止标记（spec §5.4）、``failFast`` 标记（spec §5.5）、
        被取消的拥有者下面撤回的排队输入、以及每个「普通拥有工作已经没了」的 ``completing``
        任务的终局记录。

        ⚠️ **持久记录就是意图**，所以它**同时修复崩溃留下的未施加状态**。
        """
        self._reconcile_scheduled = False
        cascade = self._cascade_pending
        self._cascade_pending = False
        checks = list(self._fail_fast_checks)
        self._fail_fast_checks.clear()
        try:

            async def change(tx: Any) -> None:
                if self._closing:
                    return
                queued = await self._load_scopes(cascade)
                marks: dict[int, dict[str, Any]] = {}

                def mark(record: dict[str, Any], reason: str | None) -> None:
                    staged = marks.get(record["id"])
                    # 一个「请求」赢过同一轮里的 `restart` 标记。
                    if staged is not None and staged["reason"] is None:
                        return
                    if record.get("abortRequested") and (
                        record.get("abortReason") is None or reason is not None
                    ):
                        return
                    marks[record["id"]] = {"record": record, "reason": reason}

                # 每一个「活的、有取消意图的拥有者」下面的活的**前台**任务——它就是它最近的那个：
                # 从每个有意图的拥有者向下走**不进入另一个**，另一个自己的遍历覆盖它下面的。
                for id in sorted(self._intent):
                    owner = self._live[id]
                    # 只有「重启后被抛弃」的拥有者才把自己的原因传下去。
                    restart = owner.get("abortReason") == "restart" and not failed_outcome(owner)
                    for step in self._walk_down(
                        id, {"enter": lambda below: below not in self._intent}
                    ):
                        if step["live"] is not None and not step["node"]["background"]:
                            mark(step["live"], "restart" if restart else None)
                for id in checks:
                    waiter = self._live.get(id)
                    if waiter is None or waiter["state"]["status"] != "waiting":
                        continue
                    if not await self._any_failed(waiter["state"]["on"]):
                        continue
                    # 其它每一个活任务：失败的那个保留自己的结果。
                    for member in waiter["state"]["on"]:
                        record = self._live.get(member)
                        if record is not None and not failed_outcome(record):
                            mark(record, None)
                for entry in marks.values():
                    tx.set_task(with_abort_mark(entry["record"], entry["reason"]))
                for id in queued:
                    if self._below_cancelled({"conversation": id}):
                        await self._withdraw_inputs(tx, id)
                await self._finalize(tx)

            await self._session.commit_with(change, self._context)
        except BaseException as error:  # noqa: BLE001 - 上游同样捕获一切
            # 这次提交里**不跑任何扩展代码**：抛出的只能是存储故障、宿主回调、或者 bug。
            # 三者都**不会因为再跑一遍而修好**，所以弄失败 Session。
            self._fail_session(error)
        self._settle_idle()

    async def _any_failed(self, ids: list[int]) -> bool:
        """``ids`` 里有没有任何一个**持有或结束于**非 ``completed`` 的结果
        （``scheduler.ts:433-444``）。"""
        for id in ids:
            record = self._live.get(id)
            if record is None:
                record = await self._storage.task(id, self._context)
            if record is not None and failed_outcome(record):
                return True
        return False

    async def _finalize(self, tx: Any) -> None:
        """写下每个「下面没有活的普通拥有工作」的 ``completing`` 任务的终局记录
        （``scheduler.ts:446-480``）。

        终结一个可能**释放它的拥有者**，所以这在本次提交的候选上**反复**进行直到不再变化。
        调度器自己写下的结果**在这里做 Harness 清理**。

        ⚠️ **按轮进行**：一轮的任务释放出来的持有者在**下一轮**才检查一次——
        这样一个「许多任务同时结束」的持有者只被**走一次**，而不是每个任务走一次。
        """
        round_ = list(self._finalize_checks)
        self._finalize_checks.clear()
        done: set[int] = set()
        overlay = overlay_of(tx)
        while round_:
            next_round: set[int] = set()
            for id in round_:
                record = overlay["tasks"].get(id) or self._live.get(id)
                if record is None or record["state"]["status"] != "completing" or id in done:
                    continue
                if self._has_owned_live(id, overlay, done):
                    continue
                outcome = record["state"]["outcome"]
                terminal = with_state(record, {"status": "terminal", "outcome": outcome})
                tx.set_task(terminal)
                overlay["tasks"][id] = terminal
                done.add(id)
                # REMINDER：**只有调度器**写 `faulted` 与 `orphaned`（spec §5.4）；
                # 它们的清理**等这次提交**。
                if outcome["status"] in ("faulted", "orphaned"):
                    await self._settle_outcome(tx, record, outcome)
                above = self._nearest_live_above(record, overlay)
                if above is not None and above["state"]["status"] == "completing":
                    next_round.add(above["id"])
            round_ = list(next_round)

    def _idle(self, conversation_id: int | None) -> bool:
        """一个对话（或整个 Harness）**是否空闲**（``scheduler.ts:1570-1587``）。

        ⚠️ **链还没加载的任务不算在它的作用域下面**——所以**除非向上走明确说不在**，
        否则它算**在里面**（保守：宁可说不空闲）。
        """
        scope: dict[str, Any] = (
            {"roots": True} if conversation_id is None else {"conversation": conversation_id}
        )
        for id in self._unloaded:
            record = self._live.get(id)
            if (
                record is not None
                and not record["background"]
                and self._in_scope(parent_of(record), scope) is not False
            ):
                return False
        starts = list(self._roots) if conversation_id is None else [conversation_id]
        for start in starts:
            for step in self._walk_down(conversation_node(start)):
                if step["live"] is not None and not step["node"]["background"]:
                    return False
        return True

    # ─── 决议（``scheduler.ts:1079-1093, 1201-1258``，C4 中不依赖 C6 的部分）─────────

    def _seal(self) -> None:
        """关闭调度器（``scheduler.ts:1079-1093``）。

        ⚠️ 顺序有意义：**先**退订注册表（它在关闭之后还会回调），**再**拒绝两类等待者，
        **最后**清掉上下文并取消过期定时器。

        ⚠️ 退订**可能抛**（宿主的回调），而**关闭路径不能因此中断**——
        所以它被包起来报告。**关闭路径上的代码必须比正常路径更耐操。**
        """
        self._closing = True
        try:
            self._unsubscribe_registry()
        except BaseException as error:  # noqa: BLE001 - 关闭路径不中断
            self._report(error)
        closed = closed_error(self._session)
        self._task_waiters.reject_all(closed)
        self._idle_waiters.reject_all(closed)
        self._contexts.clear()
        if self._expiry is not None:
            self._expiry["timer"].cancel()
        self._expiry = None

    def _waiting_on(self, record: dict[str, Any], first: bool = False) -> list[int]:
        """一个任务在下次调用之前**要等的活任务**（``scheduler.ts:1201-1209``）。

        ⚠️ 两种情形，**顺序不能反**：

        * **已打中止标记** → 等它下面**活的普通拥有工作**（因为**中止是自下而上**跑的：
          必须先让子任务收尾，父的中止处理器才有意义）；
        * 否则 → 等 ``waiting`` 状态的 ``on`` 里**活着的**那些。

        ``first`` 时只取第一个——那是**快路径**，只想知道「有没有」。
        """
        if record.get("abortRequested"):
            return self._owned_live(record["id"], {"first": first})
        if record["state"]["status"] != "waiting":
            return []
        on = record["state"]["on"]
        if not first:
            return [id for id in on if id in self._live]
        for id in on:
            if id in self._live:
                return [id]
        return []

    def _fit(self, record: dict[str, Any], task: Any) -> dict[str, Any]:
        """一个定义能不能接下这条记录（``scheduler.ts:1228-1238``）。

        四种结果，**顺序就是优先级**：

        * 没有定义 → ``missing_task``；
        * 版本相同 → 能接，**不迁移**；
        * 定义**更旧** → ``task_too_old``（**不能拿旧定义跑新数据**）；
        * 定义更新 → 能接但**要迁移**；除非这个定义**已经**迁移失败过（``migration_failed``）。
        """
        if task is None:
            return {"reason": "missing_task"}
        definition = erased(task)
        version = definition["version"] if isinstance(definition, dict) else definition.version
        if version == record["version"]:
            return {"task": task, "migrates": False}
        if version < record["version"]:
            return {"reason": "task_too_old"}
        failed = self._failed_migrations.get(record["id"])
        if failed is not None and failed["task"] is task:
            return {"reason": "migration_failed", "error": failed["error"]}
        return {"task": task, "migrates": True}

    def _resolve(self, record: dict[str, Any], snapshot: Any) -> dict[str, Any]:
        """按种类解析记录的定义，**必要时迁移旧版本**（``scheduler.ts:1210-1227``）。

        ⚠️ **迁移失败不会抛**——它把失败记进 ``_failed_migrations``、报告、并返回
        ``migration_failed``。**同一个定义不会重试**（``_fit`` 会认出来），
        直到注册表解析出**另一个**定义。
        **这是「失败要记住，但不要一直重试」的实例**：一直重试会让一个坏迁移
        在每个调度轮次里烧一次 CPU 并重复报告。
        """
        fit = self._fit(record, snapshot.task(record["kind"]))
        if "reason" in fit:
            return {"kind": "blocked", "reason": fit["reason"]}
        if not fit["migrates"]:
            return {"kind": "ready", "task": fit["task"], "record": record}
        definition = erased(fit["task"])
        try:
            migrate = (
                definition.get("migrate")
                if isinstance(definition, dict)
                else getattr(definition, "migrate", None)
            )
            if migrate is None:
                raise missing_migration(record, definition)
            migrated = migrate(record["input"], record["state"]["checkpoint"], record["version"])
            state = {**record["state"], "checkpoint": copy_json(migrated["checkpoint"])}
            migrated_record = {
                **record,
                "version": definition["version"]
                if isinstance(definition, dict)
                else definition.version,
                "input": copy_json(migrated["input"]),
                "state": state,
            }
            return {"kind": "ready", "task": fit["task"], "record": migrated_record}
        except BaseException as error:  # noqa: BLE001 - 上游同样捕获一切
            self._failed_migrations[record["id"]] = {"task": fit["task"], "error": error}
            self._report(error)
            return {"kind": "blocked", "reason": "migration_failed"}

    @property
    def index_sizes(self) -> dict[str, int]:
        """归属索引的**大小**，给「调度器内存有上界」的测试用（``scheduler.ts:1239-1257``）。

        ⚠️ 这不是调试便利——它是一个**可断言的不变量**：归属索引的大小**由活的活界定**
        （一个拥有者边、一个已结束任务的归属字段，只在它下面还列着东西时才留着）。
        所以这些数字**不该随历史增长**。
        """
        below = 0
        for children in self._below.values():
            below += len(children)
        return {
            "live": len(self._live),
            "settled": len(self._settled),
            "edges": len(self._edges),
            "below": below,
            "roots": len(self._roots),
            "unloaded": len(self._unloaded),
            "dropQueue": len(self._drop_queue),
        }

    # ─── 调用生命周期（``scheduler.ts:1283-1298, 1557-1568, 1812-1839``）─────────────

    def _create_invocation(self, record: dict[str, Any], mode: str) -> Invocation:
        """建一次**在内存里的**调用并登记它（``scheduler.ts:1283-1298``）。

        ⚠️ ``context`` 是 ``#context`` **加上这次调用的信号**——
        于是「调用结束」对所有拿着它的东西（等待、观察、流）**同时可见**。
        """
        controller = AbortController()
        loop = asyncio.get_running_loop() if _has_loop() else None
        done = loop.create_future() if loop is not None else None
        invocation = Invocation(
            task_id=record["id"],
            conversation_id=record["conversationId"],
            mode=mode,
            controller=controller,
            context=with_abort_signal(controller.signal, self._context),
            done=done,
        )
        if done is not None:

            def finish() -> None:
                if not done.done():
                    done.set_result(None)

            invocation.finish = finish
        self._invocations[record["id"]] = invocation
        return invocation

    def _end(self, invocation: Invocation) -> None:
        """结束一次调用（``scheduler.ts:1557-1568``）。

        ⚠️ **幂等**：已经结束就什么都不做。这很重要——``#start`` 的 ``finally`` 会调它，
        而中止路径上它可能已经被别的路径调过。

        四件事，缺一不可：

        1. 登记表里**还是它**才删（防止一次重跑把新调用删掉）；
        2. 活的任务**重新成为可运行候选**（否则那个任务永远不会再被调度）；
        3. **停掉它持有的每一个观察**（不停就是资源泄漏）；
        4. 用 ``endedError`` **中止它的控制器**——绑在这次调用上的等待（比如工具的
           ``waitForTask()``）**跟着一起拒绝**，而不是永远挂着。
        """
        if invocation.ended:
            return
        invocation.ended = True
        if self._invocations.get(invocation.task_id) is invocation:
            del self._invocations[invocation.task_id]
            if invocation.task_id in self._live:
                self._runnable.add(invocation.task_id)
        for watch in list(invocation.watches):
            schedule_watch_stop(watch)
        invocation.controller.abort(ended_error(invocation))

    async def _sleep(self, invocation: Invocation, until: float, context: Any) -> None:
        """等到 Harness 时钟走到 ``until``（``scheduler.ts:1812-1823``）。

        ⚠️ 它同时被**调用自己的信号**与**调用方传进来的 context** 取消——
        两者任一取消都要立刻停。而且**每一轮都重新检查**：
        因为 ``delay`` 会被切成 ``MAX_TIMER_DELAY`` 的段，醒来时可能已经该走了。
        """
        if invocation.ended:
            raise ended_error(invocation)
        signals = [invocation.controller.signal]
        abort_signal = getattr(context, "abort_signal", None)
        if abort_signal is not None:
            signals.append(abort_signal)
        signal = any_signal(signals)
        while True:
            signal.throw_if_aborted()
            remaining = until - self._now()
            if remaining <= 0:
                return
            await delay(min(remaining, MAX_TIMER_DELAY), signal)

    async def _watch_doc(self, invocation: Invocation, args: list[Any]) -> Any:
        """通过调度器观察一个文档，并**把观察绑到这次调用上**（``scheduler.ts:1825-1839``）。

        ⚠️ **取到之后要再查一次 `ended`**：观察的获取是异步的，而调用可能在**那之间**结束。
        不查就会把一个**属于已结束调用**的观察挂上去——于是它**永远不会被停**。
        """
        if invocation.ended:
            raise ended_error(invocation)
        watch = await session_method(self._session, "watch_doc")(*args)
        if watch is None:
            return None
        if invocation.ended:
            schedule_watch_stop(watch)
            raise ended_error(invocation)
        invocation.watches.add(watch)

        async def forget() -> None:
            invocation.watches.discard(watch)

        schedule_coroutine(forget())
        return watch

    # ─── 调用的门（``scheduler.ts:1781-1806``）─────────────────────────────────────

    async def _read(self, invocation: Invocation, read: Any) -> Any:
        """跑一次**已提交状态的读**，除非这次调用已经结束（``scheduler.ts:1781-1784``）。

        ⚠️ 这是 ``TaskRuntime`` **所有读**的统一入口（``snapshot``/``snapshot_as_of``/
        ``get_task``/``owned_tasks``/``outcomes``/``conversation``/``entry``/``context``）。
        不在这里拦，已结束的调用就能读到**它不该再看到的状态**——
        而它拿到的每个值都可能在写回时撞上「任务已终局」。
        """
        if invocation.ended:
            raise ended_error(invocation)
        return await read()

    def _gated(self, invocation: Invocation, change: Any, context: Any) -> Any:
        """**在会话线上重读任务、并给这次调用放行之后**才提交（``scheduler.ts:1786-1806``）。

        ⚠️ 这是整个调度器的**不变量执行点**：
        「**每一次任务状态转换都由一个在 Session 线上串行化的回调决定并写下**」。
        五条前置条件，**顺序即优先级**：

        1. 调用已结束 → 拒绝；
        2. **Harness 正在关闭** → 拒绝（`closedError`）；
        3. 任务**不在 `#live` 里** → 它已终局；
        4. 任务状态**不是 `running`** → 报出它实际是什么；
        5. **run 模式**下带了**持久中止标记** → 拒绝。

        ⚠️ **第 1 条查两次**：进入线上之前查一次（便宜、避免白排一次队），
        **进了线之后再查一次**——因为**排队期间调用可能已经结束**。
        只查一次会让一个已结束的调用**通过门去改状态**。
        """
        if invocation.ended:
            return _rejected(ended_error(invocation))

        async def on_line(tx: Any) -> Any:
            if invocation.ended:
                raise ended_error(invocation)
            if self._closing:
                raise closed_error(self._session)
            found = self._live.get(invocation.task_id)
            if found is None:
                raise RuntimeError(f"Task {invocation.task_id} is terminal")
            if found["state"]["status"] != "running":
                raise RuntimeError(f"Task {invocation.task_id} is {found['state']['status']}")
            if invocation.mode == "run" and found.get("abortRequested"):
                raise RuntimeError(f"Task {invocation.task_id} has a durable abort mark")
            # ⚠️ 上游的类型是 `T | Promise<T>`——**同步与异步都要接受**。
            # 直接 `await` 一个同步返回值会报 `object str can't be used in 'await' expression`。
            result = change(tx, found)
            if inspect.isawaitable(result):
                result = await result
            return result

        return self._session.commit_with(
            on_line,
            context,
            {"conversationId": invocation.conversation_id, "taskId": invocation.task_id},
        )

    async def _commit_state(
        self, tx: Any, invocation: Invocation, current: dict[str, Any], next: dict[str, Any]
    ) -> None:
        """把一个任务为**自己**提交的下一个状态写下去（``scheduler.ts:1453-1466``）。

        ⚠️ **返回 ``terminal`` 不一定写下 ``terminal``**（spec §5.5）：

        * 它下面还有**活的普通拥有工作** → 写 **``completing``**，把结果**先held住**——
          那些子任务还要跑完，而父的结果此刻还不能定；
        * 否则才写 ``terminal``。

        ⚠️ 而且判这个之前要**先把链都加载好**：``_has_owned_live`` 靠向下遍历，
        而向下遍历要求每个活任务的链已加载。**不先加载就会把一个其实有子任务的父误判成终局。**
        """
        if next["status"] == "waiting":
            await self._validate_wait(tx, invocation, current, next["on"], next["policy"])
        if next["status"] == "terminal":
            overlay = overlay_of(tx)
            await self._load_scopes(False)
            for record in overlay["tasks"].values():
                await self._load_chain(parent_of(record), overlay)
            if self._has_owned_live(current["id"], overlay):
                tx.set_task(
                    with_state(current, {"status": "completing", "outcome": next["outcome"]})
                )
                return
        tx.set_task(with_state(current, next))

    async def _validate_wait(
        self,
        tx: Any,
        invocation: Invocation,
        current: dict[str, Any],
        on: list[int],
        policy: str,
    ) -> None:
        """校验一次等待（spec §5.1）（``scheduler.ts:1468-1505``）。

        等待**指向已存在的任务**，而 ``failFast`` **只能等它自己拥有的**。
        它**不能闭合成环**——等待图的边从每个任务指向「它等的东西」与「它活的普通拥有工作」。

        ⚠️ **不变量：等待图无环。** 只有「等待」会在**已存在的任务之间**加边
        （新任务只有入边），所以**逐条检查每一次等待**就足以维持它。
        """
        if invocation.mode == "abort":
            raise RuntimeError(f"Abort handler of task {current['id']} cannot wait")
        overlay = overlay_of(tx)
        targets: list[int] = []
        for id in on:
            member = overlay["tasks"].get(id) or self._live.get(id)
            if member is None:
                member = await self._storage.task(id, self._context)
            if member is None:
                raise RuntimeError(f"Task {id} does not exist")
            if policy == "failFast" and member.get("owner") != current["id"]:
                raise RuntimeError(
                    f"Task {current['id']} can wait failFast only on tasks it owns; {id} is not one"
                )
            # ⚠️ **它自己拥有的活子任务已经持有它**（任务拥有的任务从不是后台）——
            # 等它**不加边**（那条边本来就在）。终局任务没有边。
            if member["state"]["status"] != "terminal" and member.get("owner") != current["id"]:
                targets.append(id)
        if not targets:
            return
        cycle = await self._wait_cycle(current["id"], targets, overlay)
        if cycle is not None:
            path = " -> ".join(str(x) for x in [current["id"], *cycle])
            raise RuntimeError(
                f"Task {current['id']} cannot wait on {cycle[0]}: {path} is a wait cycle"
            )

    async def _wait_cycle(
        self, task: int, on: list[int], overlay: dict[str, Any]
    ) -> list[int] | None:
        """等待图里从 ``on`` 的某个成员**回到 ``task``** 的路径（如果这次等待会闭合成环）
        （``scheduler.ts:1507-1554``）。

        **边 ``X -> Y`` 的含义**：``X`` **不能在 ``Y`` 之前结算**——``Y`` 要么在一个
        **没打中止标记的**等待 ``X`` 的 ``on`` 里，要么是 ``X`` 的**活的普通拥有工作**
        （那会让 ``X`` 保持 ``completing``）。

        ⚠️ **每个任务只访问一次、每棵子树只走一次**，所以一次检查的代价**由它到达的活工作**界定
        ——不是由整个图界定。
        """
        # 拥有工作靠**向下遍历**找，而那要求每个活任务的链都已加载。
        await self._load_scopes(False)
        came_from: dict[int, int | None] = {}
        walked: set[int] = set()
        pending: list[int] = []

        def reach(id: int, via: int | None) -> None:
            if id in came_from:
                return
            came_from[id] = via
            pending.append(id)

        def path(end: int) -> list[int]:
            steps: list[int] = []
            at: int | None = end
            while at is not None:
                steps.insert(0, at)
                at = came_from.get(at)
            return steps

        for id in on:
            reach(id, None)
        while pending:
            id = pending.pop()
            if id == task:
                return path(id)
            record = overlay["tasks"].get(id) or self._live.get(id)
            if record is None or record["state"]["status"] == "terminal":
                continue
            # ⚠️ **已打中止标记的任务只等它拥有的工作**——因为中止是自下而上跑的（见 ``_waiting_on``）。
            if record["state"]["status"] == "waiting" and not record.get("abortRequested"):
                for member in record["state"]["on"]:
                    reach(member, id)
            if id in walked:
                continue
            walked.add(id)
            # ⚠️ **已走过的任务下面不再走**：它自己的那次遍历已经覆盖了它下面的东西。
            found: list[int] = []
            for step in self._walk_down(
                id, {"overlay": overlay, "enter": lambda below: below not in walked}
            ):
                if step["live"] is None or step["node"]["background"]:
                    continue
                found.append(step["id"])
                reach(step["id"], id)
            walked.update(found)
        return None


def _resolved(value: Any) -> Any:
    """一个**已经成功**的 awaitable。

    ⚠️ 与 :func:`_rejected` 同理（**R14**）：同步函数返回 awaitable，
    所以不能直接 `return value`——那会在 `await` 时炸。
    """
    if not _has_loop():
        return value
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


def _rejected(error: BaseException) -> Any:
    """一个**已经失败的** awaitable（**R14**）。

    ⚠️ **不能直接 `raise`**：``_gated`` 是同步函数、返回 awaitable（上游返回
    ``Promise.reject(...)``）。直接抛会让「同步抛出」与「返回拒绝的 awaitable」
    在调用点表现不同——前者在 ``await`` 之前就炸，后者在 ``await`` 时炸。
    """
    if not _has_loop():
        raise error
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_exception(error)
    return future

    # ─── 提交状态（``scheduler.ts:1453-1554``）──────────────────────────────────────
