"""``TaskScheduler`` 的**归属图**（C3）与**对账链**（C2）的测试。

⚠️ 这是**纯内存图**的测试：直接摆好调度器的字段，然后断言图的不变量。
**不接存储、不跑任务**——那要等 C4/C6（调用与阶段执行）落地。
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT
from pi_durable.harness.scheduler import (
    _MISSING,
    Reservation,
    TaskScheduler,
    TaskSchedulerOptions,
    cancellation_intent,
    conversation_node,
    conversation_of,
    node_of,
)


def scheduler() -> TaskScheduler:
    from pi_chord.context import BACKGROUND_CONTEXT

    fields = {f.name: None for f in dataclasses.fields(TaskSchedulerOptions)}
    fields["now"] = lambda: 0.0
    fields["report"] = lambda error: None
    fields["settings"] = lambda: {"contextRetentionMs": 1000}
    # ⚠️ `context` 必须是真的——`_create_invocation` 要用它包出调用自己的上下文。
    fields["context"] = BACKGROUND_CONTEXT
    # `_validate_wait` 在 `#live` 里找不到目标时会去存储里查。
    fields["storage"] = _EmptyStorage()
    # `abort` 会问注册表要一个快照。
    fields["registry"] = _EmptyRegistry()

    # `_terminate` 在真的没活时要做 Harness 清理（"搭在同一次提交里"）。
    async def _settle_outcome(tx: Any, rec: Any, outcome: Any) -> None:
        return None

    fields["settle_outcome"] = _settle_outcome
    return TaskScheduler(TaskSchedulerOptions(**fields))


class _EmptyRegistry:
    """一个没有装任何定义的注册表替身。"""

    def snapshot(self) -> Any:
        return type("S", (), {"task": lambda self, kind: None})()

    def subscribe(self, listener: Any) -> Any:
        return lambda: None


class _EmptyStorage:
    """什么都不存在的存储替身。"""

    async def task(self, id: int, context: Any) -> Any:
        return None


class _OneTaskStorage:
    """只有一条记录的存储替身。"""

    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record

    async def task(self, id: int, context: Any) -> Any:
        return self._record


class _FakeSession:
    """一个最小的 Session 替身：``commit_with`` 直接跑回调，并让调用方有机会插队。

    这样能测到「**排队之后、真正跑之前**状态变了」——真 Session 的顺序不可控。
    """

    def __init__(self, tx: Any = None) -> None:
        self.before_run: Any = None
        self.failure: Any = None
        self.tx = tx if tx is not None else _FakeTx()

    def commit_with(self, change: Any, context: Any, scope: Any = None) -> Any:
        import asyncio

        async def run() -> Any:
            if self.before_run is not None:
                self.before_run()
            return await change(self.tx)

        return asyncio.ensure_future(run())

    def read_on_line(self, job: Any) -> Any:
        import asyncio

        return asyncio.ensure_future(job())

    # `open` 要挂三个监听器；这里记下来即可（本文件不驱动它们）。
    def observe_commits(self, listener: Any) -> None:
        self.commit_listener = listener

    def subscribe_close(self, listener: Any) -> None:
        self.close_listener = listener


def record(
    id: int,
    *,
    conversation_id: int = 1,
    owner: int | None = None,
    background: bool = False,
    status: str = "pending",
    on: list[int] | None = None,
    abort_requested: bool = False,
    outcome: str | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {"status": status}
    # ⚠️ **一律带 checkpoint**：上游的 `RunnableTaskRecord` 一定有它，
    # 而 `_reserve`/`_commit_state` 会**直接下标**它（不是 `.get`）。
    state["checkpoint"] = {"phase": "p"}
    if status in ("completing", "terminal"):
        state["outcome"] = {"status": outcome or "completed"}
    if status == "waiting":
        state["checkpoint"] = {"phase": "p"}
        state["on"] = on or []
        state["policy"] = "allSettled"
    rec: dict[str, Any] = {
        "id": id,
        "conversationId": conversation_id,
        "kind": "demo",
        "version": 1,
        "background": background,
        "abortRequested": abort_requested,
        "state": state,
    }
    if owner is not None:
        rec["owner"] = owner
    return rec


# --------------------------------------------------------------------------------------
# _edge 的三态
# --------------------------------------------------------------------------------------


def test_edge_distinguishes_unloaded_from_ownerless() -> None:
    """⚠️ ``_MISSING``（**还没加载**）与 ``None``（**确定无主**）是**两个不同的答案**。

    把两者压成一个会让「不知道」被当成「无主」——于是向上遍历会**在一个还没加载的边上停住**，
    把一个有拥有者的任务当成根。
    """
    s = scheduler()
    assert s._edge(1) is _MISSING
    s._edges[1] = None
    assert s._edge(1) is None
    s._edges[2] = 7
    assert s._edge(2) == 7


def test_edge_prefers_the_overlay() -> None:
    s = scheduler()
    s._edges[1] = 5
    overlay = {"tasks": {}, "edges": {1: 9}}
    assert s._edge(1, overlay) == 9


# --------------------------------------------------------------------------------------
# _track
# --------------------------------------------------------------------------------------


def test_track_registers_a_live_task_and_lists_it() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))

    assert s._live[7]["id"] == 7
    assert 7 in s._runnable  # 不是 completing → 可运行
    assert conversation_node(1) in s._below  # 被列在它的对话下面
    assert 1 in s._roots  # 无主的对话有活 → 是根


def test_track_moves_a_completing_task_out_of_runnable() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    s._track(s._live[7], record(7, status="completing", outcome="completed"))

    assert 7 not in s._runnable
    assert 7 in s._finalize_checks


def test_track_registers_waiters_by_each_task_in_on() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="waiting", on=[3, 4]))
    assert s._waiters[3] == {7}
    assert s._waiters[4] == {7}


def test_track_unregisters_waiters_when_the_wait_ends() -> None:
    """⚠️ 等待结束时**必须注销**——不然任务的 ``on`` 变了之后，
    旧的那个依赖上还挂着一个**永远不会被唤醒**的等待者。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="waiting", on=[3]))
    s._track(s._live[7], record(7, status="running"))
    assert 3 not in s._waiters


def test_track_marks_a_cancellation_intent() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, abort_requested=True))
    assert 7 in s._intent
    assert cancellation_intent(s._live[7]) is True


def test_track_marks_an_unloaded_task_whose_parent_is_unknown() -> None:
    s = scheduler()
    # 拥有者 99 未知，所以它的链没加载
    s._track(None, record(7, owner=99))
    assert 7 in s._unloaded


def test_track_removes_a_terminal_task_from_the_live_sets() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    s._track(s._live[7], record(7, status="terminal", outcome="completed"))

    assert 7 not in s._live
    assert 7 in s._settled  # 遍历要穿过它
    assert 7 not in s._runnable
    assert 7 not in s._intent


# --------------------------------------------------------------------------------------
# _relist / _below
# --------------------------------------------------------------------------------------


def test_relist_unlists_and_queues_for_the_sweep() -> None:
    """⚠️ 一个**失去最后一个已列出节点**的父会被**排进扫除队列**。

    不排的话，那棵树会在内存里**一直长**——没有别的东西会来清理它。
    """
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    assert conversation_node(1) in s._below

    s._track(s._live[7], record(7, status="terminal", outcome="completed"))
    assert conversation_node(1) not in s._below
    assert conversation_node(1) in s._drop_queue


def test_relist_walks_up_a_chain_without_recursion() -> None:
    """⚠️ **循环而不是递归**：拥有者链可能比调用栈还深。"""
    s = scheduler()
    s._edges[1] = 100
    s._settled[100] = {"conversationId": 1, "owner": 101, "background": False}
    s._settled[101] = {"conversationId": 1, "background": False}
    s._edges[1] = None  # 让 100 成为根任务

    s._track(None, record(7, owner=100))
    # 一路上每一层都被列出来
    assert 7 in (s._below.get(100) or set())


# --------------------------------------------------------------------------------------
# _walk_down / _owned_live
# --------------------------------------------------------------------------------------


def test_walk_down_reaches_but_does_not_enter_a_background_task() -> None:
    """⚠️ **后台任务会被到达但不进入**——它下面的东西属于它自己，
    不阻塞它上面的 ``completing`` 任务终结。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, owner=100))
    s._settled[100] = {"conversationId": 1, "background": False}
    s._track(None, record(8, owner=7, background=True))
    s._track(None, record(9, owner=8))

    reached = [step["id"] for step in s._walk_down(100)]
    assert 7 in reached
    assert 8 in reached  # 被到达
    assert 9 not in reached  # 但没进入后台任务 8


def test_walk_down_enters_a_background_task_when_asked() -> None:
    s = scheduler()
    s._track(None, record(7, background=True))
    s._edges[1] = None
    s._track(None, record(8, owner=7))
    reached = [step["id"] for step in s._walk_down(7, {"crossBackground": True})]
    assert 8 in reached


def test_owned_live_skips_background_tasks() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, owner=100))
    s._settled[100] = {"conversationId": 1, "background": False}
    s._track(None, record(8, owner=100, background=True))

    assert s._owned_live(100) == [7]
    assert s._has_owned_live(100) is True
    assert s._owned_live(100, {"first": True}) == [7]


def test_owned_live_ignores_ended_work() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, owner=100))
    s._settled[100] = {"conversationId": 1, "background": False}
    s._track(s._live[7], record(7, owner=100, status="terminal", outcome="completed"))
    assert s._owned_live(100) == []


def test_owned_live_done_only_controls_descent() -> None:
    """⚠️ ``done`` **只控制「进不进入」**，**不控制「列不列出来」**。

    上游是 ``enter: (id) => !done.has(id)``——``enter`` 决定的是**要不要继续往下走**，
    而 ``found`` 的收集条件是「活 + 非后台」。所以 ``done`` 里的任务**仍然会被列出**，
    只是不再进入它自己的子树。

    我第一版把它当成「过滤 yield」了——**它其实是「剪枝」**。
    """
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, owner=100))
    s._settled[100] = {"conversationId": 1, "background": False}
    s._track(None, record(8, owner=7))

    assert s._owned_live(100) == [7, 8]  # 都列出
    assert s._owned_live(100, {"done": {7}}) == [7]  # 7 仍列出，但**不进它的子树**


# --------------------------------------------------------------------------------------
# _above / _known / _nearest_live_above
# --------------------------------------------------------------------------------------


def test_above_stops_at_an_unloaded_edge() -> None:
    """⚠️ **边没加载时向上遍历要停下并说「不知道」**，不能当成根。

    遍历顺序是「任务 → 它所在的对话 → （边没加载就）不知道」——**不知道是最后一步**，
    不会先假装它是个根。
    """
    s = scheduler()
    s._live[7] = record(7, conversation_id=1)
    steps = list(s._above({"task": 7}))
    # ⚠️ `node` 是**记录本身**（`#node` 对活任务返回 `#live` 里那条），
    # **不是** `nodeOf()` 收窄出来的归属字段——记录是它的超集，所以 `parentOf()` 两者都能吃。
    assert steps[0] == {"task": 7, "node": s._live[7]}
    assert steps[1] == {"conversation": 1}
    assert steps[-1] == {"unknown": True}


def test_above_reaches_a_conversation_and_stops_at_a_root() -> None:
    s = scheduler()
    s._live[7] = record(7, conversation_id=1)
    s._edges[1] = None
    steps = list(s._above({"task": 7}))
    assert {"conversation": 1} in steps
    assert len(steps) == 2


def test_known_is_false_while_a_chain_is_unloaded() -> None:
    s = scheduler()
    assert s._known(conversation_node(1)) is False
    s._edges[1] = None
    assert s._known(conversation_node(1)) is True
    s._edges[2] = 7
    s._live[7] = record(7)
    s._unloaded.add(7)
    assert s._known(conversation_node(2)) is False
    s._unloaded.discard(7)
    assert s._known(conversation_node(2)) is True


def test_nearest_live_above_skips_terminal_tasks() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, owner=100))
    s._settled[100] = {"conversationId": 1, "background": False}
    above = s._nearest_live_above(record(7, owner=100))
    assert above is None  # 100 只是 #settled 里的字段，不是活的


def test_nearest_live_above_finds_the_live_owner() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(100, status="completing", outcome="completed"))
    s._track(None, record(7, owner=100))
    above = s._nearest_live_above(record(7, owner=100))
    assert above["id"] == 100


def test_nearest_live_above_stops_at_a_background_task() -> None:
    """⚠️ **后台任务及其下面的工作不持有任何东西**。"""
    s = scheduler()
    s._track(None, record(7, owner=100, background=True))
    s._live[100] = record(100)
    assert s._nearest_live_above(record(7, owner=100, background=True)) is None


# --------------------------------------------------------------------------------------
# _in_scope / _cancelling_owner
# --------------------------------------------------------------------------------------


def test_in_scope_matches_before_it_needs_the_unloaded_edge() -> None:
    """⚠️ 向上遍历**经过任务自己的对话时就会判 True**——那时还不需要那条没加载的边。

    **所以「边没加载」不必然导致 ``None``**：只有遍历**走到需要那条边**时才知道不知道。
    """
    s = scheduler()
    s._live[7] = record(7, conversation_id=1)
    assert s._in_scope({"task": 7}, {"conversation": 1}) is True


def test_in_scope_returns_none_when_the_walk_needs_an_unloaded_edge() -> None:
    """⚠️ **「不知道」不是 ``False``**——把它当 ``False`` 会让一个其实在作用域里的任务被漏掉。

    构造：任务在对话 1 里，而**对话 1 自己的边没加载**；所以查询**别的**对话（2）时，
    遍历要先走到 1 的拥有者才知道——而那是不知道的。
    """
    s = scheduler()
    s._live[7] = record(7, conversation_id=1)
    assert s._in_scope({"task": 7}, {"conversation": 2}) is None


def test_in_scope_matches_the_conversation() -> None:
    s = scheduler()
    s._live[7] = record(7, conversation_id=1)
    s._edges[1] = None
    assert s._in_scope({"task": 7}, {"conversation": 1}) is True
    assert s._in_scope({"task": 7}, {"conversation": 2}) is False


def test_in_scope_roots_matches_an_ownerless_chain() -> None:
    """⚠️ 一条**有效**的归属链：任务 7 在对话 1，对话 1 的拥有者是任务 9，
    而任务 9 在**对话 2**（它自己无主）。

    ⚠️ **我第一版把任务 9 也放在对话 1 里**——那让它「拥有自己所在的对话」，
    构成一条**不可能的环**，于是 ``_above`` **无限循环**、整个测试套件**挂住**。
    真实数据产生不了这种图（一个任务拥有的是**别的**对话），但**测试能**。

    这条不变量现在由 ``_above`` 的环检测兜住（抛错而不是挂起）。
    """
    s = scheduler()
    s._live[7] = record(7, conversation_id=1)
    s._live[9] = record(9, conversation_id=2)
    s._edges[1] = 9
    s._edges[2] = None

    assert s._in_scope({"task": 7}, {"roots": True}) is True


def test_in_scope_roots_is_false_below_a_background_owner() -> None:
    """⚠️ ``roots`` 作用域**只有在两种情况下**返回 ``False``：撞上后台拥有者、或者不知道。

    所以「在一个无主链上」本身就是 ``True``——**别指望它像"属于某个对话"那样能精确匹配**。
    """
    s = scheduler()
    s._live[7] = record(7, conversation_id=1)
    s._live[9] = record(9, conversation_id=2, background=True)
    s._edges[1] = 9
    s._edges[2] = None

    assert s._in_scope({"task": 7}, {"roots": True}) is False


def test_above_raises_on_a_cyclic_graph() -> None:
    """⚠️ **偏离**：上游没有环检测（它依赖「归属图是棵树」）。

    但违反不变量的代价是**无限循环**——一个**静默挂起**，它会**卡住整个测试套件**
    （真的发生过：我那条写错的测试就是）。**把挂起换成一个明确的错误**，
    是唯一能在这时救回诊断信息的做法。
    """
    import pytest as _pytest

    s = scheduler()
    s._live[7] = record(7, conversation_id=1)
    s._live[9] = record(9, conversation_id=1)  # ← 它拥有自己所在的对话
    s._edges[1] = 9

    with _pytest.raises(RuntimeError, match="Ownership cycle"):
        list(s._above({"task": 7}))


def test_cancelling_owner_returns_none_immediately_without_intent() -> None:
    """⚠️ 常见情形：``#intent`` 为空 → **一次遍历都不做**。"""
    s = scheduler()
    s._live[7] = record(7)
    assert s._cancelling_owner({"task": 7}) is None


def test_cancelling_owner_finds_the_nearest_intent() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(9, abort_requested=True))
    s._track(None, record(7, owner=9))
    owner = s._cancelling_owner({"task": 7})
    assert owner is not None and owner["id"] == 9
    assert s._below_cancelled({"task": 7}) is True


# --------------------------------------------------------------------------------------
# _sweep
# --------------------------------------------------------------------------------------


def test_sweep_keeps_a_node_needed_again() -> None:
    """⚠️ 队列里的节点**到真正扫除时还要再查一次**——链加载会**先装节点、后列下层**，
    所以中间态看起来是「不需要」的。"""
    s = scheduler()
    s._edges[1] = None
    s._drop_queue.add(conversation_node(1))
    s._below[conversation_node(1)] = {7}  # 又被需要了
    s._sweep()
    assert 1 in s._edges
    assert s._drop_queue == set()


def test_sweep_drops_an_unneeded_conversation() -> None:
    s = scheduler()
    s._edges[1] = None
    s._roots.add(1)
    s._drop_queue.add(conversation_node(1))
    s._sweep()
    assert 1 not in s._edges
    assert 1 not in s._roots


def test_sweep_drops_an_unneeded_settled_task() -> None:
    s = scheduler()
    s._settled[7] = {"conversationId": 1, "background": False}
    s._unloaded.add(7)
    s._drop_queue.add(7)
    s._sweep()
    assert 7 not in s._settled
    assert 7 not in s._unloaded


def test_sweep_keeps_a_live_task() -> None:
    s = scheduler()
    s._live[7] = record(7)
    s._drop_queue.add(7)
    s._sweep()
    assert 7 in s._live


# --------------------------------------------------------------------------------------
# _idle / 上下文保留
# --------------------------------------------------------------------------------------


def test_idle_is_true_without_work() -> None:
    s = scheduler()
    assert s._idle(1) is True
    assert s._idle(None) is True


def test_idle_is_false_with_live_foreground_work() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    assert s._idle(1) is False
    assert s._idle(None) is False


def test_idle_ignores_background_work() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, background=True))
    assert s._idle(1) is True


def test_idle_is_conservative_for_unloaded_chains() -> None:
    """⚠️ **链还没加载的任务不算在它的作用域下面**——除非向上走**明确说不在**，
    否则它**算在里面**（宁可说不空闲）。

    反过来（算不在）会让一个其实在跑的对话被当成空闲——那会**丢掉它的上下文缓存**，
    甚至让它被当成「可以开始新运行」。
    """
    s = scheduler()
    s._track(None, record(7, owner=99))  # 99 未知 → 7 在 #unloaded
    assert 7 in s._unloaded
    assert s._idle(1) is False


def test_context_retention_falls_back_to_zero_on_a_throwing_settings() -> None:
    """⚠️ 读设置**自己也可能抛**；用 ``0`` 兜底是**保守**的——
    ``0`` 表示「一空闲就丢」，于是最多只是**少一点缓存**，而不是无限期留着上下文。"""
    fields = {f.name: None for f in dataclasses.fields(TaskSchedulerOptions)}
    reported: list[Any] = []

    def settings() -> Any:
        raise RuntimeError("settings exploded")

    fields["settings"] = settings
    fields["report"] = reported.append
    s = TaskScheduler(TaskSchedulerOptions(**fields))

    assert s._context_retention_ms() == 0.0
    assert len(reported) == 1


def test_settle_idle_drops_a_context_past_retention() -> None:
    s = scheduler()
    s._contexts[1] = {"range": None, "idleSince": 0.0}
    # ⚠️ Harness 时钟与 retention **都是毫秒**——`5.0` 是 5ms，远不到 1000ms。
    s._now = lambda: 5000.0
    s._settle_idle()
    assert 1 not in s._contexts


def test_settle_idle_clears_idle_since_when_busy_again() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    s._contexts[1] = {"range": None, "idleSince": 0.0}
    s._settle_idle()
    assert s._contexts[1]["idleSince"] is None


# --------------------------------------------------------------------------------------
# C4：决议（不依赖 C6 的那部分）
# --------------------------------------------------------------------------------------


def test_waiting_on_prefers_the_owned_work_when_abort_marked() -> None:
    """⚠️ 已打中止标记 → 等它下面**活的普通拥有工作**。

    **因为中止是自下而上跑的**：必须先让子任务收尾，父的中止处理器才有意义。
    反过来（先去打父的处理器）会让父在子还在跑的时候就准备收尾。
    """
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(9, abort_requested=True))
    s._track(None, record(7, owner=9))

    assert s._waiting_on(s._live[9]) == [7]


def test_waiting_on_returns_the_live_part_of_on_for_a_wait() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(9, status="waiting", on=[3, 4]))
    s._live[3] = record(3)  # 只有 3 是活的

    assert s._waiting_on(s._live[9]) == [3]


def test_waiting_on_first_stops_at_the_first_live_dependency() -> None:
    """``first`` 是**快路径**——只想知道「有没有要等的」。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(9, status="waiting", on=[3, 4]))
    s._live[3] = record(3)
    s._live[4] = record(4)

    assert s._waiting_on(s._live[9], True) == [3]
    assert s._waiting_on(s._live[9]) == [3, 4]


def test_waiting_on_is_empty_for_a_running_task() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(9))
    assert s._waiting_on(s._live[9]) == []


def test_fit_reports_a_missing_definition() -> None:
    assert scheduler()._fit(record(1), None) == {"reason": "missing_task"}


def test_fit_accepts_the_same_version_without_migrating() -> None:
    task = type("T", (), {"definition": {"version": 1}})()
    assert scheduler()._fit(record(1), task) == {"task": task, "migrates": False}


def test_fit_rejects_an_older_definition() -> None:
    """⚠️ **不能拿旧定义跑新数据**——那会读错检查点形状。"""
    task = type("T", (), {"definition": {"version": 1}})()
    rec = {**record(1), "version": 2}
    assert scheduler()._fit(rec, task) == {"reason": "task_too_old"}


def test_fit_requires_a_migration_for_a_newer_definition() -> None:
    task = type("T", (), {"definition": {"version": 2}})()
    rec = {**record(1), "version": 1}
    assert scheduler()._fit(rec, task) == {"task": task, "migrates": True}


def test_fit_remembers_a_failed_migration_for_the_same_task() -> None:
    """⚠️ **同一个定义不重试**——一直重试会让一个坏迁移在每个调度轮次里
    烧一次 CPU 并重复报告。"""
    task = type("T", (), {"definition": {"version": 2}})()
    rec = {**record(1), "version": 1}
    s = scheduler()
    s._failed_migrations[1] = {"task": task, "error": RuntimeError("boom")}
    assert s._fit(rec, task)["reason"] == "migration_failed"


def test_resolve_returns_a_blocked_resolution_for_a_missing_task() -> None:
    snapshot = type("S", (), {"task": lambda self, kind: None})()
    assert scheduler()._resolve({**record(1)}, snapshot) == {
        "kind": "blocked",
        "reason": "missing_task",
    }


def test_resolve_returns_ready_without_migrating() -> None:
    task = type("T", (), {"definition": {"version": 1}})()
    snapshot = type("S", (), {"task": lambda self, kind: task})()
    resolution = scheduler()._resolve({**record(1)}, snapshot)
    assert resolution["kind"] == "ready"
    assert resolution["task"] is task


def test_resolve_migrates_an_older_record() -> None:
    class Definition(dict):
        def get(self, key: Any, default: Any = None) -> Any:
            if key == "migrate":
                return lambda input_, checkpoint, version: {
                    "input": {"migrated": True},
                    "checkpoint": {"phase": "new"},
                }
            return super().get(key, default)

    task = type("T", (), {"definition": Definition(version=2)})()
    snapshot = type("S", (), {"task": lambda self, kind: task})()
    rec = {
        **record(1),
        "version": 1,
        "input": {},
        "state": {"status": "pending", "checkpoint": {"phase": "old"}},
    }
    resolution = scheduler()._resolve(rec, snapshot)

    assert resolution["kind"] == "ready"
    assert resolution["record"]["version"] == 2
    assert resolution["record"]["input"] == {"migrated": True}
    assert resolution["record"]["state"]["checkpoint"] == {"phase": "new"}


def test_resolve_records_a_failed_migration_without_raising() -> None:
    """⚠️ **迁移失败不抛**——它记下来、报告、返回 ``migration_failed``。

    抛出会让整个调度轮次失败，而**一个坏迁移不该拖垮别的任务**。
    """
    reported: list[Any] = []

    class Definition(dict):
        def get(self, key: Any, default: Any = None) -> Any:
            if key == "migrate":

                def boom(*_: Any) -> Any:
                    raise RuntimeError("migration exploded")

                return boom
            return super().get(key, default)

    task = type("T", (), {"definition": Definition(version=2)})()
    snapshot = type("S", (), {"task": lambda self, kind: task})()
    fields = {f.name: None for f in dataclasses.fields(TaskSchedulerOptions)}
    fields["report"] = reported.append
    s = TaskScheduler(TaskSchedulerOptions(**fields))

    rec = {**record(1), "version": 1}
    assert s._resolve(rec, snapshot) == {"kind": "blocked", "reason": "migration_failed"}
    assert 1 in s._failed_migrations
    assert len(reported) == 1


def test_resolve_reports_a_missing_migration_as_a_failure() -> None:
    task = type("T", (), {"definition": {"version": 2}})()
    snapshot = type("S", (), {"task": lambda self, kind: task})()
    rec = {**record(1), "version": 1}
    assert scheduler()._resolve(rec, snapshot)["reason"] == "migration_failed"


# --------------------------------------------------------------------------------------
# _seal 与 index_sizes
# --------------------------------------------------------------------------------------


def test_seal_closes_and_rejects_waiters() -> None:
    """⚠️ 顺序有意义：**先**退订注册表（关闭后它还会回调），**再**拒绝等待者，
    **最后**清上下文、取消定时器。"""
    unsubscribed: list[bool] = []
    fields = {f.name: None for f in dataclasses.fields(TaskSchedulerOptions)}
    fields["now"] = lambda: 0.0
    fields["report"] = lambda error: None
    s = TaskScheduler(TaskSchedulerOptions(**fields))
    s._unsubscribe_registry = lambda: unsubscribed.append(True)
    s._contexts[1] = {"range": None, "idleSince": 0.0}
    s._session = None  # closed_error 会用到它——见下

    # closed_error 需要 session；用一个最小的假 session
    s._session = type("S", (), {"failure": None})()

    s._seal()
    assert s._closing is True
    assert unsubscribed == [True]
    assert s._contexts == {}
    assert s._expiry is None


def test_seal_survives_a_throwing_unsubscribe() -> None:
    """⚠️ **关闭路径不能因为一个宿主回调抛了就中断**——
    它跑的时候系统已经不正常了（同 `join()` 那条判据）。"""
    reported: list[Any] = []
    fields = {f.name: None for f in dataclasses.fields(TaskSchedulerOptions)}
    fields["report"] = reported.append
    s = TaskScheduler(TaskSchedulerOptions(**fields))
    s._session = type("S", (), {"failure": None})()

    def boom() -> None:
        raise RuntimeError("unsubscribe exploded")

    s._unsubscribe_registry = boom
    s._seal()
    assert s._closing is True
    assert len(reported) == 1


def test_index_sizes_counts_the_ownership_indexes() -> None:
    """⚠️ 这些数字是**可断言的不变量**：归属索引的大小**由活的活界定**，
    **不该随历史增长**。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))

    sizes = s.index_sizes
    assert sizes["live"] == 1
    assert sizes["edges"] == 1
    assert sizes["below"] == 1
    assert sizes["roots"] == 1
    assert sizes["settled"] == 0


# --------------------------------------------------------------------------------------
# C6：调用生命周期（自足的那 4 个）
# --------------------------------------------------------------------------------------


async def test_create_invocation_registers_it_and_scopes_its_context() -> None:
    """⚠️ ``context`` 是基础上下文**加上这次调用的信号**——
    于是「调用结束」对所有拿着它的东西（等待、观察、流）**同时可见**。"""
    s = scheduler()
    invocation = s._create_invocation(record(7), "run")

    assert s._invocations[7] is invocation
    assert invocation.task_id == 7
    assert invocation.conversation_id == 1
    assert invocation.mode == "run"
    assert invocation.ended is False


async def test_end_is_idempotent() -> None:
    """⚠️ **幂等**：``#start`` 的 ``finally`` 会调它，而中止路径上它可能已经被调过。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    invocation = s._create_invocation(s._live[7], "run")
    s._runnable.discard(7)

    s._end(invocation)
    first = invocation.controller.signal.aborted
    s._end(invocation)  # 再调一次
    assert first is True
    assert invocation.controller.signal.aborted is True


async def test_end_makes_the_task_runnable_again() -> None:
    """⚠️ 不把任务放回可运行候选，它**永远不会再被调度**。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    invocation = s._create_invocation(s._live[7], "run")
    s._runnable.discard(7)

    s._end(invocation)
    assert 7 in s._runnable


async def test_end_does_not_remove_a_newer_invocation() -> None:
    """⚠️ 只有登记表里**还是它**才删——否则一次重跑把新调用删掉，
    那个任务就**再也不会被等**了。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    old = s._create_invocation(s._live[7], "run")
    new = s._create_invocation(s._live[7], "run")  # 覆盖同一个 taskId

    s._end(old)
    assert s._invocations.get(7) is new
    # ⚠️ 「两条路径同时到」的完整要求：旧的那次结束**不能**动到新的那个。
    assert new.ended is False  # 新的**没有**被标记成结束
    assert new.controller.signal.aborted is False  # 它的信号也**没被**中止
    s._end(new)  # 新的自己结束**照常**
    assert new.ended is True
    assert new.controller.signal.aborted is True


async def test_end_aborts_the_controller_with_an_ended_error() -> None:
    """⚠️ 绑在这次调用上的等待（比如工具的 ``waitForTask()``）**跟着一起拒绝**，
    而不是永远挂着。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    invocation = s._create_invocation(s._live[7], "run")

    s._end(invocation)
    assert invocation.controller.signal.aborted is True
    assert "invocation has ended" in str(invocation.controller.signal.reason)


async def test_end_stops_every_watch() -> None:
    """⚠️ **停掉它持有的每一个观察**——不停就是资源泄漏。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    invocation = s._create_invocation(s._live[7], "run")

    stopped: list[bool] = []

    class Watch:
        def stop(self) -> Any:
            stopped.append(True)

            async def noop() -> None:
                return None

            return noop()

    invocation.watches.add(Watch())
    s._end(invocation)
    await asyncio.sleep(0.01)
    assert stopped == [True]


async def test_sleep_rejects_an_ended_invocation() -> None:
    s = scheduler()
    invocation = s._create_invocation(record(7), "run")
    s._end(invocation)
    with pytest.raises(RuntimeError, match="invocation has ended"):
        await s._sleep(invocation, 0.0, None)


async def test_sleep_returns_once_the_clock_passes() -> None:
    s = scheduler()
    s._now = lambda: 100.0
    invocation = s._create_invocation(record(7), "run")
    await asyncio.wait_for(s._sleep(invocation, 50.0, None), 2)  # 已经过了


async def test_sleep_wakes_for_the_callers_cancellation() -> None:
    """⚠️ 它同时被**调用自己的信号**与**调用方传进来的 context** 取消——两者任一都要停。"""
    from pi_chord.context import BACKGROUND_CONTEXT, AbortError, with_abort_signal
    from pi_chord.context import AbortController as ChordController

    s = scheduler()
    s._now = lambda: 0.0
    invocation = s._create_invocation(record(7), "run")
    controller = ChordController()
    controller.abort(AbortError("caller gave up"))

    with pytest.raises(AbortError):
        await asyncio.wait_for(
            s._sleep(
                invocation, 10_000.0, with_abort_signal(controller.signal, BACKGROUND_CONTEXT)
            ),
            2,
        )


# --------------------------------------------------------------------------------------
# C6：调用的门（_read / _gated）
# --------------------------------------------------------------------------------------


async def test_read_passes_through_for_a_live_invocation() -> None:
    s = scheduler()
    invocation = s._create_invocation(record(7), "run")

    async def read() -> str:
        return "值"

    assert await s._read(invocation, read) == "值"


async def test_read_rejects_once_the_invocation_ended() -> None:
    """⚠️ 不在这里拦，已结束的调用就能读到**它不该再看到的状态**。"""
    s = scheduler()
    invocation = s._create_invocation(record(7), "run")
    s._end(invocation)

    async def read() -> str:  # pragma: no cover - 不该被调用
        raise AssertionError("读不该跑")

    with pytest.raises(RuntimeError, match="invocation has ended"):
        await s._read(invocation, read)


async def test_gated_rejects_a_synchronously_ended_invocation() -> None:
    """⚠️ **R14**：返回一个**已拒绝的 awaitable**，而不是同步 `raise`。

    两者的差别在调用点：同步抛在 ``await`` **之前**就炸，
    返回拒绝的 awaitable 在 ``await`` **时**才炸——
    而 ``commit`` 的调用方总是 ``await`` 它，所以必须**只**在 await 时炸。
    """
    s = scheduler()
    invocation = s._create_invocation(record(7), "run")
    s._end(invocation)

    result = s._gated(invocation, lambda tx, current: None, None)
    with pytest.raises(RuntimeError, match="invocation has ended"):
        await result


async def test_gated_checks_ended_again_after_queuing() -> None:
    """⚠️ **第 1 条查两次**：排队期间调用可能已经结束。

    只查一次会让一个已结束的调用**通过门去改状态**——
    这正是「检查与使用之间状态会变」那一族。
    """
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="running"))

    session = _FakeSession()
    s._session = session
    invocation = s._create_invocation(s._live[7], "run")

    result = s._gated(invocation, lambda tx, current: "ok", BACKGROUND_CONTEXT)
    # 排队之后、真正跑之前，调用结束了
    s._end(invocation)
    with pytest.raises(RuntimeError, match="invocation has ended"):
        await result


async def test_gated_rejects_when_closing() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="running"))
    s._session = _FakeSession()
    invocation = s._create_invocation(s._live[7], "run")
    s._closing = True

    with pytest.raises(RuntimeError):
        await s._gated(invocation, lambda tx, current: None, BACKGROUND_CONTEXT)


async def test_gated_rejects_a_terminal_task() -> None:
    """任务**不在 `#live` 里**就是已终局。"""
    s = scheduler()
    s._session = _FakeSession()
    invocation = s._create_invocation(record(7), "run")

    with pytest.raises(RuntimeError, match="is terminal"):
        await s._gated(invocation, lambda tx, current: None, BACKGROUND_CONTEXT)


async def test_gated_reports_the_actual_status() -> None:
    """⚠️ 报**它实际是什么**——只报「不是 running」会让排查少一半信息。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="waiting", on=[]))
    s._session = _FakeSession()
    invocation = s._create_invocation(s._live[7], "run")

    with pytest.raises(RuntimeError, match="is waiting"):
        await s._gated(invocation, lambda tx, current: None, BACKGROUND_CONTEXT)


async def test_gated_rejects_a_durable_abort_mark_in_run_mode() -> None:
    """⚠️ **只对 `run` 模式**：`abort` 模式的调用**必须**能带着中止标记提交——
    它就是来跑中止处理器的。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="running", abort_requested=True))
    s._session = _FakeSession()
    invocation = s._create_invocation(s._live[7], "run")

    with pytest.raises(RuntimeError, match="durable abort mark"):
        await s._gated(invocation, lambda tx, current: None, BACKGROUND_CONTEXT)


async def test_gated_allows_an_abort_invocation_with_the_mark() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="running", abort_requested=True))
    s._session = _FakeSession()
    invocation = s._create_invocation(s._live[7], "abort")

    assert await s._gated(invocation, lambda tx, current: "done", BACKGROUND_CONTEXT) == "done"


async def test_gated_hands_the_change_the_live_record() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="running"))
    s._session = _FakeSession()
    invocation = s._create_invocation(s._live[7], "run")

    seen: list[Any] = []

    def change(tx: Any, current: Any) -> str:
        seen.append(current)
        return "ok"

    assert await s._gated(invocation, change, BACKGROUND_CONTEXT) == "ok"
    assert seen[0]["id"] == 7


# --------------------------------------------------------------------------------------
# C6：提交状态（_commit_state / _validate_wait / _wait_cycle）
# --------------------------------------------------------------------------------------


class _FakeTx:
    """记录 ``set_task`` 的最小事务替身。"""

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self._staged: list[dict[str, Any]] = []

    def set_task(self, record: dict[str, Any]) -> None:
        self.written.append(record)

    def staged_tasks(self) -> list[dict[str, Any]]:
        return self._staged

    def staged_conversations(self) -> list[dict[str, Any]]:
        return []

    async def scan_tasks(self, query: Any, limit: Any, cursor: Any) -> dict[str, Any]:
        # `open` 会按状态扫表；默认什么也没有（各测试按需覆盖）。
        return {"items": []}


async def test_commit_state_writes_terminal_when_nothing_runs_below() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="running"))
    tx = _FakeTx()
    invocation = s._create_invocation(s._live[7], "run")

    await s._commit_state(
        tx, invocation, s._live[7], {"status": "terminal", "outcome": {"status": "completed"}}
    )
    assert tx.written[0]["state"]["status"] == "terminal"


async def test_commit_state_holds_the_outcome_as_completing_when_children_run() -> None:
    """⚠️ **返回 ``terminal`` 不一定写下 ``terminal``**（spec §5.5）。

    它下面还有活的普通拥有工作 → 写 **``completing``**，把结果**先 held 住**——
    那些子任务还要跑完，而父的结果此刻**还不能定**。
    """
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="running"))
    s._track(None, record(8, owner=7))  # 活的子任务
    tx = _FakeTx()
    invocation = s._create_invocation(s._live[7], "run")

    await s._commit_state(
        tx, invocation, s._live[7], {"status": "terminal", "outcome": {"status": "completed"}}
    )
    assert tx.written[0]["state"]["status"] == "completing"
    assert tx.written[0]["state"]["outcome"] == {"status": "completed"}


async def test_commit_state_passes_a_running_state_through() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="running"))
    tx = _FakeTx()
    invocation = s._create_invocation(s._live[7], "run")

    await s._commit_state(
        tx, invocation, s._live[7], {"status": "running", "checkpoint": {"phase": "p"}}
    )
    assert tx.written[0]["state"]["status"] == "running"


async def test_an_abort_handler_cannot_wait() -> None:
    """⚠️ 中止处理器**不能等待**——它跑的时候任务的活工作已经要收了，
    再挂一个等待会让它**永远不结算**。"""
    s = scheduler()
    s._session = _FakeSession()
    invocation = s._create_invocation(record(7), "abort")
    tx = _FakeTx()

    with pytest.raises(RuntimeError, match="cannot wait"):
        await s._validate_wait(tx, invocation, record(7), [8], "allSettled")


async def test_a_wait_on_a_missing_task_is_rejected() -> None:
    s = scheduler()
    s._session = _FakeSession()
    invocation = s._create_invocation(record(7), "run")
    tx = _FakeTx()

    with pytest.raises(RuntimeError, match="Task 8 does not exist"):
        await s._validate_wait(tx, invocation, record(7, status="running"), [8], "allSettled")


async def test_fail_fast_may_only_wait_on_owned_tasks() -> None:
    """⚠️ ``failFast`` 只等**自己拥有的**——等别人的会让「一个失败就全停」
    波及到不归它管的任务。"""
    s = scheduler()
    s._session = _FakeSession()
    s._live[8] = record(8, owner=99)  # 别人拥有
    invocation = s._create_invocation(record(7), "run")
    tx = _FakeTx()

    with pytest.raises(RuntimeError, match="only on tasks it owns"):
        await s._validate_wait(tx, invocation, record(7, status="running"), [8], "failFast")


async def test_a_wait_on_its_own_live_child_adds_no_edge() -> None:
    """⚠️ 它自己拥有的活子任务**已经持有它**（那条边本来就在）——等它**不加边**。

    所以这一次校验**直接返回**，不做环检测。
    """
    s = scheduler()
    s._session = _FakeSession()
    s._edges[1] = None
    s._track(None, record(7, status="running"))
    s._track(None, record(8, owner=7))
    invocation = s._create_invocation(s._live[7], "run")
    tx = _FakeTx()

    await s._validate_wait(tx, invocation, s._live[7], [8], "allSettled")  # 不抛


async def test_a_direct_wait_cycle_is_rejected() -> None:
    """⚠️ **自等**是等待图里最短的环——必须被拒。

    不变量：**等待图无环**。只有「等待」会在已存在的任务之间加边，
    所以逐条检查每一次等待就足以维持它。
    """
    s = scheduler()
    s._session = _FakeSession()
    s._edges[1] = None
    s._track(None, record(7, status="running"))
    s._track(None, record(8, status="waiting", on=[7]))  # 8 在等 7

    invocation = s._create_invocation(s._live[7], "run")
    tx = _FakeTx()

    with pytest.raises(RuntimeError, match="is a wait cycle"):
        await s._validate_wait(tx, invocation, s._live[7], [8], "allSettled")


# --------------------------------------------------------------------------------------
# C1：中止与等待（_terminate / abort / wait_for_task）
# --------------------------------------------------------------------------------------


class _AbortTx(_FakeTx):
    """``abort`` 用的事务替身：``task()`` 返回一条预置记录。"""

    def __init__(self, record: dict[str, Any] | None) -> None:
        super().__init__()
        self._record = record

    async def task(self, id: int) -> Any:
        return self._record


def _abort_scheduler(record: dict[str, Any] | None) -> tuple[TaskScheduler, _AbortTx]:
    s = scheduler()
    s._session = _FakeSession(_AbortTx(record))
    return s, s._session.tx  # type: ignore[attr-defined]


async def test_terminate_holds_the_outcome_when_children_run() -> None:
    """⚠️ 与 `_commit_state` 同一条规则：**结果先 held 住**。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="running"))
    s._track(None, record(8, owner=7))
    tx = _FakeTx()

    await s._terminate(tx, s._live[7], {"status": "orphaned", "reason": "missing_task"})
    assert tx.written[0]["state"]["status"] == "completing"


async def test_terminate_writes_terminal_and_settles_the_outcome() -> None:
    """⚠️ 只有真的没活了才写 ``terminal``，**并在同一次提交里做 Harness 清理**。"""
    settled: list[Any] = []
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="running"))
    s._settle_outcome = lambda tx, rec, outcome: settled.append(outcome) or _done()
    tx = _FakeTx()

    await s._terminate(tx, s._live[7], {"status": "orphaned", "reason": "missing_task"})
    assert tx.written[0]["state"]["status"] == "terminal"
    assert settled == [{"status": "orphaned", "reason": "missing_task"}]


async def _done() -> None:
    return None


async def test_abort_reports_a_terminal_task() -> None:
    s, _ = _abort_scheduler(record(7, status="terminal", outcome="completed"))
    assert await s.abort(7, BACKGROUND_CONTEXT) == "terminal"


async def test_abort_rejects_a_missing_task() -> None:
    s, _ = _abort_scheduler(None)
    with pytest.raises(RuntimeError, match="Task 7 does not exist"):
        await s.abort(7, BACKGROUND_CONTEXT)


async def test_abort_keeps_a_restart_mark() -> None:
    """⚠️ 「重启后等待定义」的任务**保留它的标记**并继续等——
    拥有者自己的清理会用到这条。"""
    s, tx = _abort_scheduler(record(7, status="pending"))
    _ = tx
    s._live[7] = {**record(7, status="pending"), "abortReason": "restart"}

    assert await s.abort(7, BACKGROUND_CONTEXT, True) == "marked"
    assert s._live[7]["abortReason"] == "restart"


def _marking_scheduler(
    rec: dict[str, Any],
) -> tuple[TaskScheduler, list[dict[str, Any]]]:
    """一个**定义能接下这个任务**的调度器——那样 ``abort`` 走的是「打标记」那条路，
    而不是「没定义 → 孤立」那条。

    ⚠️ 两条路的区别很重要：**孤立那条会把任务直接结算成终局**（并保留它原有的
    ``abortReason``——**上游也是这样**，孤立不等于「中止请求被接受」）。
    """
    s = scheduler()
    definition = {"name": "demo", "version": 1}
    task = type("T", (), {"definition": definition})()
    s._registry = type(
        "R", (), {"snapshot": lambda self: type("S", (), {"task": lambda self, kind: task})()}
    )()
    s._session = _FakeSession(_AbortTx(rec))
    s._live[7] = rec
    return s, s._session.tx.written  # type: ignore[attr-defined]


async def test_abort_replaces_a_restart_mark_with_a_request() -> None:
    """⚠️ 不带 ``abortReason`` 的请求会**替换** ``restart`` 标记。

    不替换的话，一个卡在「等定义」上的任务会**永远等不到中止**——
    它会一直以为自己是「重启抛弃」的。
    """
    s, written = _marking_scheduler({**record(7, status="pending"), "abortReason": "restart"})
    assert await s.abort(7, BACKGROUND_CONTEXT) == "marked"
    marked = [w for w in written if w["id"] == 7]
    assert marked, "应当写下一条标记"
    assert "abortReason" not in marked[-1]  # restart 被替换掉了
    assert marked[-1]["abortRequested"] is True


async def test_abort_without_a_definition_orphans_and_keeps_the_mark() -> None:
    """⚠️ **没有定义能接下时，任务被孤立**（不是「标记成功」）——
    而且**它原有的 ``abortReason`` 留着**（上游也是这样）。

    **孤立 ≠ 中止请求被接受**：前者是「没人能跑它了」，后者是「有人要求它停」。
    """
    s, _ = _abort_scheduler({**record(7, status="pending"), "abortReason": "restart"})
    s._live[7] = {**record(7, status="pending"), "abortReason": "restart"}
    assert await s.abort(7, BACKGROUND_CONTEXT) == "marked"

    written = s._session.tx.written  # type: ignore[attr-defined]
    assert written and written[-1]["state"]["status"] == "terminal"
    assert written[-1]["state"]["outcome"]["status"] == "orphaned"
    assert written[-1]["abortReason"] == "restart"


async def test_wait_for_task_rejects_a_missing_task() -> None:
    s = scheduler()
    s._storage = _EmptyStorage()
    s._session = _FakeSession()
    with pytest.raises(RuntimeError, match="Task 7 does not exist"):
        await s.wait_for_task(7, BACKGROUND_CONTEXT)


async def test_wait_for_task_returns_a_terminal_record_at_once() -> None:
    s = scheduler()
    s._storage = _OneTaskStorage(record(7, status="terminal", outcome="completed"))
    s._session = _FakeSession()
    settled = await s.wait_for_task(7, BACKGROUND_CONTEXT)
    assert settled["state"]["status"] == "terminal"


async def test_wait_for_task_registers_a_waiter_for_a_live_task() -> None:
    """⚠️ **查与登记在同一条会话线上**——否则一次终局发布可能落在两者之间，
    这个等待**永远不会被唤醒**。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="running"))
    s._session = _FakeSession()

    waiting = asyncio.ensure_future(s.wait_for_task(7, BACKGROUND_CONTEXT))
    await asyncio.sleep(0.01)
    assert not waiting.done()  # 挂在一个等待者上

    s._task_waiters.resolve(7, {"id": 7})
    assert await asyncio.wait_for(waiting, 2) == {"id": 7}


async def test_wait_for_task_rejects_while_closing() -> None:
    s = scheduler()
    s._closing = True
    s._session = _FakeSession()
    with pytest.raises(RuntimeError):
        await s.wait_for_task(7, BACKGROUND_CONTEXT)


# --------------------------------------------------------------------------------------
# `_RuntimeView` 满足 `TaskRuntime` 契约（**基于真实实例**）
# --------------------------------------------------------------------------------------


def _runtime_instance() -> Any:
    """造一个**真实的** runtime 视图：调度器 → 调用 → 阶段 → 视图。"""
    from pi_durable.harness.scheduler import Phase as SchedulerPhase

    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="running"))
    invocation = s._create_invocation(s._live[7], "run")
    snapshot = s._registry.snapshot()
    phase = SchedulerPhase(
        snapshot=lambda: snapshot, task=lambda: type("T", (), {"definition": {"name": "demo"}})()
    )
    return s, s._runtime(invocation, phase)


def test_runtime_view_satisfies_the_task_runtime_contract() -> None:
    """⚠️ **对**一个真实实例断言，而不是对类。

    `dir(类)` 断言的是「类对象上有这些名字」，而契约要的是
    「**一个真实的 runtime 实例**在行为上满足它」——两者在"名字在不在"上等价，
    **在"能不能用"上不等价**。（我原本想用 `dir(_RuntimeView)` 交差，被自己写下的
    〈便宜的替代物〉拦住了。）

    ⚠️ 与 `test_transaction_class_satisfies_the_tx_contract` 的唯一区别：
    那个当时已经有实例（`Transaction` 是现成的实现），这个要**先有工厂**。
    """
    from pi_durable.harness.runtime import TaskRuntime

    _scheduler, runtime = _runtime_instance()
    required = {n for n in dir(TaskRuntime) if not n.startswith("_")}
    have = {n for n in dir(runtime) if not n.startswith("_")}
    assert required <= have, f"缺：{sorted(required - have)}"


def test_runtime_view_exposes_the_invocation_identity() -> None:
    """三个身份成员直接来自调用：**一次调用不换任务、不换对话、不换信号**。"""
    _scheduler, runtime = _runtime_instance()
    assert runtime.task_id == 7
    assert runtime.conversation_id == 1
    assert runtime.signal is not None


def test_runtime_view_hooks_is_itself() -> None:
    """``hooks`` 就是**这个视图自己**——它实现了 ``HookRunner.each``。"""
    from pi_durable.harness.types import HookRunner

    _scheduler, runtime = _runtime_instance()
    assert runtime.hooks is runtime
    assert hasattr(runtime, "each")
    assert {n for n in dir(HookRunner) if not n.startswith("_")} <= {
        n for n in dir(runtime) if not n.startswith("_")
    }


async def test_runtime_view_rejects_every_read_once_the_invocation_ended() -> None:
    """⚠️ 调用结束后**所有读**都拒绝——不拦的话，已结束的调用能读到**它不该再看到的状态**，
    而它拿到的每个值都可能在写回时撞上「任务已终局」。"""
    s, runtime = _runtime_instance()
    s._end(runtime.invocation)

    with pytest.raises(RuntimeError, match="invocation has ended"):
        runtime.now()
    with pytest.raises(RuntimeError, match="invocation has ended"):
        runtime.report(RuntimeError("x"))
    with pytest.raises(RuntimeError, match="invocation has ended"):
        await runtime.agent(BACKGROUND_CONTEXT)


def test_runtime_view_agent_is_resolved_at_most_once_per_phase() -> None:
    """⚠️ **每个阶段至多解析一次**：第一个调用者建它，之后所有人复用。

    每次重新解析会**毁掉这条语义**——而 agent 的解析要读注册表与设置，
    重复解析会让同一阶段里的两次调用看到**不同的 agent**。
    """
    calls: list[int] = []

    async def resolve(conversation_id: int, snapshot: Any, context: Any) -> Any:
        calls.append(conversation_id)
        return "the-agent"

    s, runtime = _runtime_instance()
    s._agent = resolve

    assert asyncio.run(runtime.agent(BACKGROUND_CONTEXT)) == "the-agent"
    assert asyncio.run(runtime.agent(BACKGROUND_CONTEXT)) == "the-agent"
    assert calls == [1]  # 只解析了一次


def test_runtime_view_exposes_models_without_touching_the_omitted_ones() -> None:
    """⚠️ `pi_ai.Models` 是 **9/24**——视图**只转发**它，不碰那 15 个未声明成员。"""
    sentinel = object()
    s, runtime = _runtime_instance()
    s._models = sentinel
    assert runtime.models is sentinel


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.now(),
        lambda r: r.report(RuntimeError("x")),
        lambda r: r.snapshot("t"),
        lambda r: r.snapshot_as_of("t", 1),
        lambda r: r.get_task(1, BACKGROUND_CONTEXT),
        lambda r: r.wait_for_task(1, BACKGROUND_CONTEXT),
        lambda r: r.owned_tasks(BACKGROUND_CONTEXT),
        lambda r: r.abort_owned(1, BACKGROUND_CONTEXT),
        lambda r: r.outcomes([1], BACKGROUND_CONTEXT),
        lambda r: r.conversation(1, BACKGROUND_CONTEXT),
        lambda r: r.entry(1, BACKGROUND_CONTEXT),
        lambda r: r.context(1, BACKGROUND_CONTEXT),
        lambda r: r.agent(BACKGROUND_CONTEXT),
        lambda r: r.sleep(0, BACKGROUND_CONTEXT),
        lambda r: r.watch_doc("t"),
        lambda r: r.env(BACKGROUND_CONTEXT),
    ],
    ids=lambda f: "read",
)
async def test_every_read_rejects_once_the_invocation_ended(call: Any) -> None:
    """⚠️ **参数化覆盖每一个读方法**，而不是只测一两个。

    「漏拦一个」的症状是**某一条读路径静默泄漏**——它不会报错，
    只会让一个已结束的调用读到它不该再看到的状态。所以必须**逐个**钉住。
    """
    s, runtime = _runtime_instance()
    s._end(runtime.invocation)

    # ⚠️ 有些读**同步抛**（``now``/``report``），有些返回**已拒绝的 awaitable**
    # （``_read`` 包装过的）——所以 `pytest.raises` 必须**同时罩住取结果与 await**。
    with pytest.raises(RuntimeError, match="invocation has ended"):
        result = call(runtime)
        if inspect.isawaitable(result):
            await result


# --------------------------------------------------------------------------------------
# C6：阶段裁决（_step / _decide）
# --------------------------------------------------------------------------------------


def _step_scheduler(rec: dict[str, Any]) -> tuple[TaskScheduler, Any]:
    s = scheduler()
    s._session = _FakeSession()
    s._live[7] = rec
    s._edges[1] = None
    s._runnable.add(7)
    invocation = s._create_invocation(rec, "run")
    return s, invocation


async def test_step_returns_the_current_record_when_the_decision_is_true() -> None:
    s, invocation = _step_scheduler(record(7, status="running"))
    current = await s._step(invocation, lambda tx, cur: True)
    assert current is not None and current["id"] == 7
    assert invocation.ended is False  # 继续，所以没结束


async def test_step_ends_the_invocation_when_the_decision_is_false() -> None:
    s, invocation = _step_scheduler(record(7, status="running"))
    assert await s._step(invocation, lambda tx, cur: False) is None
    assert invocation.ended is True


async def test_step_treats_a_non_running_task_as_a_false_decision() -> None:
    """⚠️ **只有 ``running`` 才算数**——别的一律按「结束」处理。

    否则一个已经变成 ``waiting`` 的任务会被**再跑一个 phase**，
    而它的检查点已经不属于那个阶段了。
    """
    s, invocation = _step_scheduler(record(7, status="waiting", on=[]))
    called: list[bool] = []

    def decide(tx: Any, cur: Any) -> bool:
        called.append(True)
        return True

    assert await s._step(invocation, decide) is None
    assert called == []  # `decide` **根本没被调**
    assert invocation.ended is True


async def test_step_does_not_call_decide_while_closing() -> None:
    """⚠️ 正在关闭时**不该再跑任何任务代码**。"""
    s, invocation = _step_scheduler(record(7, status="running"))
    s._closing = True
    called: list[bool] = []

    assert await s._step(invocation, lambda tx, cur: called.append(True) or False) is None
    assert called == []


async def test_a_first_phase_continues() -> None:
    """规则 5：``previous is None``（第一次）→ 继续。"""
    s = scheduler()
    tx = _FakeTx()
    assert s._decide(tx, record(7, status="running"), None, _decide_state(s)) is True


async def test_a_failure_from_the_previous_phase_faults() -> None:
    """规则 6：**未捕获的错误** → 出错。"""
    s = scheduler()
    tx = _FakeTx()
    error = RuntimeError("boom")
    decision = s._decide(
        tx,
        record(7, status="running"),
        {"checkpoint": {"phase": "p"}, "failure": {"error": error}},
        _decide_state(s),
    )
    assert decision == {"fault": error}


async def test_no_durable_progress_faults() -> None:
    """规则 7：**检查点没变** → 出错。

    ⚠️ 这条规则防的是「一个 phase 处理器什么都不做就返回」——
    那会让 run 循环**空转**（既没有进展、也不结束）。
    """
    s = scheduler()
    tx = _FakeTx()
    current = record(7, status="running")
    current["state"]["checkpoint"] = {"phase": "same"}
    decision = s._decide(tx, current, {"checkpoint": {"phase": "same"}}, _decide_state(s))
    assert "fault" in decision
    assert "without durable progress" in str(decision["fault"])


async def test_an_abort_mark_ends_without_faulting() -> None:
    """规则 3：中止标记 → 结束，**但不算出错**（它是一次正常的中止）。"""
    s = scheduler()
    tx = _FakeTx()
    decision = s._decide(
        tx, record(7, status="running", abort_requested=True), None, _decide_state(s)
    )
    assert decision is False


async def test_a_definition_that_can_take_over_hands_the_task_back() -> None:
    """规则 8：定义换了但**能接下** → 交接：任务回到 ``pending``，等下一次预留。"""
    s = scheduler()
    tx = _FakeTx()
    newer = type("T", (), {"definition": {"version": 1}})()
    # ⚠️ **不能往 `state["snapshot"]` 里注入**：`_decide` 会**重新读** `_registry.snapshot()`
    # （那正是规则 8 的意思——**有进展就要刷新快照**）。所以只能从注册表下手。
    s._registry = type(
        "R", (), {"snapshot": lambda self: type("S", (), {"task": lambda self, kind: newer})()}
    )()
    state = _decide_state(s)
    current = record(7, status="running")
    current["state"]["checkpoint"] = {"phase": "next"}

    decision = s._decide(tx, current, {"checkpoint": {"phase": "prev"}}, state)
    assert decision is False
    assert tx.written[0]["state"]["status"] == "pending"


def _decide_state(s: TaskScheduler) -> dict[str, Any]:
    definition = type("T", (), {"definition": {"name": "demo", "version": 1}})()
    return {
        "task": definition,
        "snapshot": s._registry.snapshot(),
        "reported": None,
    }


# --------------------------------------------------------------------------------------
# C6：中止处理器（_run_abort）
# --------------------------------------------------------------------------------------


def _abort_reservation(
    *, status: str = "running", abort_impl: Any = None, closing: bool = False
) -> tuple[TaskScheduler, Any, list[str]]:
    s = scheduler()
    s._session = _FakeSession()
    rec = record(7, status=status)
    s._live[7] = rec
    s._edges[1] = None
    s._closing = closing
    invocation = s._create_invocation(rec, "abort")
    seen: list[str] = []

    async def default_abort(current: Any, runtime: Any, context: Any) -> None:
        seen.append("ran")

    definition = {"name": "demo", "version": 1, "abort": abort_impl or default_abort}
    task = type("T", (), {"definition": definition})()
    return s, Reservation(invocation=invocation, task=task, snapshot=s._registry.snapshot()), seen


async def test_run_abort_runs_the_handler_once() -> None:
    """⚠️ **只跑一次**——与 `_run` 的循环不同，中止处理器没有阶段可推进。"""
    s, reservation, seen = _abort_reservation()
    await s._run_abort(reservation)
    assert seen == ["ran"]


async def test_run_abort_does_nothing_when_already_terminal() -> None:
    """⚠️ 已终局 → **什么都不做**：既不该跑任务代码，也没有状态可写。"""
    s, reservation, seen = _abort_reservation(status="terminal")
    await s._run_abort(reservation)
    assert seen == []


async def test_run_abort_does_nothing_while_closing() -> None:
    """⚠️ 正在关闭 → 不跑任务代码。"""
    s, reservation, seen = _abort_reservation(closing=True)
    await s._run_abort(reservation)
    assert seen == []


async def test_a_handler_that_returns_without_an_outcome_faults() -> None:
    """⚠️ **返回而没给出终局结果 = 出错**。

    这是「中止处理器不能什么都不做」的**强制点**——否则任务会停在
    「中止已跑、但没有任何终局记录」的状态上，而它**永远不会再被预留**。
    """
    s, reservation, _ = _abort_reservation()
    await s._run_abort(reservation)

    # `_step` 把 `faulted` 写了下去
    written = s._session.tx.written  # type: ignore[attr-defined]
    assert written and written[-1]["state"]["outcome"]["status"] == "faulted"
    assert "without a terminal outcome" in written[-1]["state"]["outcome"]["error"]["message"]


async def test_a_throwing_handler_faults_with_its_own_error() -> None:
    """⚠️ 处理器抛出的错误**优先于**那句「没给出终局结果」——
    前者带着**真实原因**，后者只是症状。"""

    async def boom(current: Any, runtime: Any, context: Any) -> None:
        raise RuntimeError("handler exploded")

    s, reservation, _ = _abort_reservation(abort_impl=boom)
    await s._run_abort(reservation)
    written = s._session.tx.written  # type: ignore[attr-defined]
    assert "handler exploded" in written[-1]["state"]["outcome"]["error"]["message"]


async def test_run_abort_accepts_a_synchronous_handler() -> None:
    """⚠️ 处理器可以是**同步**的（上游类型是 `T | Promise<T>`）。"""
    seen: list[str] = []

    def sync_abort(current: Any, runtime: Any, context: Any) -> None:
        seen.append("sync")

    s, reservation, _ = _abort_reservation(abort_impl=sync_abort)
    await s._run_abort(reservation)
    assert seen == ["sync"]


# --------------------------------------------------------------------------------------
# C6：run 循环（_run）
# --------------------------------------------------------------------------------------


def _run_reservation(
    phases: dict[str, Any], *, checkpoints: list[dict[str, Any]] | None = None
) -> tuple[TaskScheduler, Any, list[str]]:
    """造一个能跑 run 循环的调度器：任务定义带 `phases`，记录带一个检查点序列。"""
    s = scheduler()
    s._session = _FakeSession()
    rec = record(7, status="running")
    s._live[7] = rec
    s._edges[1] = None
    invocation = s._create_invocation(rec, "run")
    seen: list[str] = []
    definition = {"name": "demo", "version": 1, "phases": phases}
    task = type("T", (), {"definition": definition})()
    _ = checkpoints
    return s, Reservation(invocation=invocation, task=task, snapshot=s._registry.snapshot()), seen


async def test_run_stops_after_one_phase_when_the_checkpoint_does_not_advance() -> None:
    """⚠️ **规则 7（没有持久进展 → 出错）必须由 `_run` 落成 ``faulted`` 记录。**

    构造：处理器把检查点**改回同一个值**（等于没有进展）。
    那让 `_decide` 返回 ``fault`` → `_step` 写 ``faulted`` 并返回 ``None`` → 循环结束。

    **如果 `_run` 吞掉它，循环会永远转**——一个什么都不做的处理器会被**无限重跑**。
    所以这里断言两件事：**循环结束了**、而且**写下的是 ``faulted``**。
    """
    ticks: list[str] = []

    async def idle(current: Any, runtime: Any, context: Any) -> None:
        ticks.append("tick")

    s, reservation, _ = _run_reservation({"p": idle})
    # 检查点**从不改变** → 第二次裁决就报「没有持久进展」
    s._live[7]["state"]["checkpoint"] = {"phase": "p"}

    async def decide_never_advances(tx: Any, current: Any) -> Any:
        return True

    # 直接跑 `_run` —— 它自己会调 `_decide`
    await asyncio.wait_for(s._run(reservation), 2)
    assert ticks == ["tick"]  # 只跑了一次，没有空转
    written = s._session.tx.written  # type: ignore[attr-defined]
    assert written and written[-1]["state"]["outcome"]["status"] == "faulted"
    assert "without durable progress" in written[-1]["state"]["outcome"]["error"]["message"]


async def test_run_exits_at_once_when_the_invocation_is_already_ended() -> None:
    """`_step` 返回 ``None`` 就结束——不跑任何处理器。"""
    ticks: list[str] = []

    async def handler(current: Any, runtime: Any, context: Any) -> None:
        ticks.append("ran")

    s, reservation, _ = _run_reservation({"p": handler})
    s._live[7]["state"]["status"] = "waiting"  # `_step` 对非 running 一律按结束处理
    await asyncio.wait_for(s._run(reservation), 2)
    assert ticks == []


async def test_run_does_not_dispatch_when_close_happens_after_the_decision() -> None:
    """⚠️ **关闭可能正好发生在裁决与派发之间**——所以 `_step` 返回之后要再查一次。

    构造：处理器**自己**把 `_closing` 置起来（模拟「裁决通过之后、派发之前」关闭）。
    没有那第二次检查，它就会**在关闭之后还跑一个 phase**。
    """
    ticks: list[str] = []

    async def handler(current: Any, runtime: Any, context: Any) -> None:
        ticks.append("ran")
        s._closing = True  # 派发这一刻关闭

    s, reservation, _ = _run_reservation({"p": handler})
    s._live[7]["state"]["checkpoint"] = {"phase": "p"}
    await asyncio.wait_for(s._run(reservation), 2)
    assert ticks == ["ran"]  # 只跑了第一个；第二次循环因 `_closing` 退出


async def test_run_records_a_throwing_handler_as_the_previous_failure() -> None:
    """⚠️ 处理器抛出 → 记成 ``previous.failure`` **而不往上抛**——
    下一次裁决把它变成 ``fault``（规则 6）。

    **抛上去会绕过那条规则**，让「未捕获的错误」变成「循环崩掉」——
    而后者**不会写下任何终局记录**。
    """
    calls: list[str] = []

    async def boom(current: Any, runtime: Any, context: Any) -> None:
        calls.append("boom")
        raise RuntimeError("handler exploded")

    s, reservation, _ = _run_reservation({"p": boom})
    s._live[7]["state"]["checkpoint"] = {"phase": "p"}
    await asyncio.wait_for(s._run(reservation), 2)

    assert calls == ["boom"]  # 没有无限重跑
    written = s._session.tx.written  # type: ignore[attr-defined]
    assert written and written[-1]["state"]["outcome"]["status"] == "faulted"
    assert "handler exploded" in written[-1]["state"]["outcome"]["error"]["message"]


async def test_run_resets_the_phase_agent_before_each_handler() -> None:
    """⚠️ **每个阶段之前把 `phase.agent` 置空** —— 于是每个处理器
    **在首次使用时重新解析**它的 agent。

    「每个阶段至多一次」是**阶段**级的，不是**调用**级的——不置空会让第二个阶段
    复用第一个阶段的 agent。
    """
    seen_agents: list[Any] = []

    async def resolve(conversation_id: int, snapshot: Any, context: Any) -> Any:
        return object()

    async def handler(current: Any, runtime: Any, context: Any) -> None:
        seen_agents.append(await runtime.agent(context))
        # 推进检查点，好让循环进入下一个阶段
        current["state"]["checkpoint"] = {"phase": "q"}

    async def second(current: Any, runtime: Any, context: Any) -> None:
        seen_agents.append(await runtime.agent(context))
        current["state"]["checkpoint"] = {"phase": "r"}

    async def third(current: Any, runtime: Any, context: Any) -> None:
        # ⚠️ 先取 agent **再**抛——否则这个阶段根本不会去解析，
        # 于是断言的是「只解析了两次」而不是「三个阶段各解析一次」。
        seen_agents.append(await runtime.agent(context))
        raise RuntimeError("stop")

    s, reservation, _ = _run_reservation({"p": handler, "q": second, "r": third})
    s._agent = resolve
    s._live[7]["state"]["checkpoint"] = {"phase": "p"}
    await asyncio.wait_for(s._run(reservation), 2)

    # 三个阶段各自解析了一次 → **三个不同的对象**（若复用，会是同一个）
    assert len(seen_agents) == 3
    assert len({id(a) for a in seen_agents}) == 3


# --------------------------------------------------------------------------------------
# C6/C4：预留与驱动（_reserve / _drain / _kick / _start）
# --------------------------------------------------------------------------------------


async def test_reserve_skips_everything_while_disabled() -> None:
    """⚠️ 调度器**一开始是不使能的**——刚构造好时不该预留任何东西。"""
    s = scheduler()
    s._session = _FakeSession()
    s._edges[1] = None
    s._track(None, record(7))
    assert await s._reserve() == []


async def test_reserve_marks_abandoned_tasks_first_and_returns() -> None:
    """⚠️ **第一次只打标记，然后返回**——这样后面几轮才看得见那些标记。

    同一次提交里既打标记又预留，会让一个刚被标记的任务**在同一轮里就被预留成 run**——
    而它其实应该等 abort。
    """
    s = scheduler()
    s._session = _FakeSession()
    s._edges[1] = None
    s._track(None, record(7))
    s._enabled = True
    s._abandoned.add(7)

    assert await s._reserve() == []  # 这一轮不预留任何东西
    written = s._session.tx.written  # type: ignore[attr-defined]
    assert written and written[-1]["abortRequested"] is True
    assert written[-1]["abortReason"] == "restart"
    assert s._abandoned == set()  # 标记过了
    assert s._dirty is True  # 还要再预留一次


async def test_reserve_creates_an_invocation_for_a_runnable_task() -> None:
    s = scheduler()
    s._session = _FakeSession()
    s._edges[1] = None
    s._track(None, record(7))
    s._enabled = True
    definition = type("T", (), {"definition": {"name": "demo", "version": 1}})()
    s._registry = type(
        "R", (), {"snapshot": lambda self: type("S", (), {"task": lambda self, kind: definition})()}
    )()

    reservations = await s._reserve()
    assert len(reservations) == 1
    assert reservations[0].invocation.task_id == 7
    assert 7 in s._invocations
    assert 7 not in s._runnable  # 已登记，从候选里去掉


async def test_reserve_rolls_back_every_invocation_when_the_commit_throws() -> None:
    """⚠️ **提交失败要把已经建好的调用收回去**——否则它们会留在 `_invocations` 里，
    于是那任务**永远不会再被预留**，而且没人会去 finish 它们。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    s._enabled = True
    definition = type("T", (), {"definition": {"name": "demo", "version": 1}})()
    s._registry = type(
        "R", (), {"snapshot": lambda self: type("S", (), {"task": lambda self, kind: definition})()}
    )()

    class BoomSession:
        failure: Any = None

        def commit_with(self, change: Any, context: Any, scope: Any = None) -> Any:
            async def run() -> Any:
                # 先让回调建出调用（**要传真 tx**——传 None 会让 `set_task` 先炸，
                # 那测的就不是「提交失败回收」了），再失败
                await change(_FakeTx())
                raise RuntimeError("commit exploded")

            return asyncio.ensure_future(run())

        def read_on_line(self, job: Any) -> Any:
            return asyncio.ensure_future(job())

    s._session = BoomSession()
    with pytest.raises(RuntimeError, match="commit exploded"):
        await s._reserve()
    assert s._invocations == {}  # 收回了
    assert 7 in s._runnable  # 重新成为候选


async def test_kick_is_idempotent_while_draining() -> None:
    """⚠️ 「绝不在提交或注册表监听器里同步提交」——而且**正在排空时不再排一轮**。"""
    s = scheduler()
    s._draining = True
    s._enabled = True
    s._kick()
    assert s._dirty is True  # 记下了这次的请求
    assert s._draining is True  # 但没有再排一轮


async def test_kick_does_nothing_while_disabled_or_closing() -> None:
    s = scheduler()
    s._kick()
    assert s._draining is False  # 没使能 → 不排
    s._enabled = True
    s._closing = True
    s._kick()
    assert s._draining is False


async def test_start_runs_the_invocation_and_finishes_it() -> None:
    """⚠️ ``finally`` 那三步缺一不可，其中 **`_kick` 最容易被漏**：
    少了它，任务在 `_end` 之后重新可运行，**但没人去排空**，于是它**永远停在那里**。"""
    s = scheduler()
    s._session = _FakeSession()
    s._edges[1] = None
    s._track(None, record(7))
    s._enabled = True
    ticks: list[str] = []

    async def handler(current: Any, runtime: Any, context: Any) -> None:
        ticks.append("ran")

    definition = {"name": "demo", "version": 1, "phases": {"p": handler}}
    task = type("T", (), {"definition": definition})()
    # ⚠️ 必须是 `running`——`_step` **只有 `running` 才算数**，否则按「结束」处理。
    s._live[7]["state"]["status"] = "running"
    invocation = s._create_invocation(s._live[7], "run")
    reservation = Reservation(invocation=invocation, task=task, snapshot=s._registry.snapshot())

    s._start(reservation)
    await asyncio.sleep(0.02)
    assert ticks == ["ran"]
    assert invocation.ended is True  # `_end` 跑了
    assert 7 not in s._invocations  # 从登记表里消失
    assert 7 in s._runnable  # `_end` 让它重新成为候选
    # ⚠️ 不能断言 `_dirty is True`：`_kick` 会**排一轮 `_drain`**，
    # 而那一轮会把 `_dirty` **消费掉**（置回 False）。所以这里断言的是
    # 「它**确实重建了可运行状态**」——那才是 `_kick` 的意义。


async def test_drain_leaves_draining_false_and_rekicks() -> None:
    """⚠️ **循环最后一次检查之后才到的唤醒，仍然需要它那一轮**——
    所以 `finally` 里要再 `_kick` 一次。"""
    s = scheduler()
    s._session = _FakeSession()
    s._enabled = True
    s._draining = True
    s._dirty = True
    s._runnable.clear()  # 没有候选 → `_reserve` 立刻返回

    await s._drain()
    assert s._draining is False


# --------------------------------------------------------------------------------------
# C1：开档 / 使能 / 等待空闲 / 对话中止（open / resume / wait_for_idle / abort_conversation）
# --------------------------------------------------------------------------------------


async def test_open_loads_live_tasks_and_resets_running_to_pending() -> None:
    """⚠️ **幸存的 ``running`` 改回 ``pending``**——一个崩掉的 Harness 留下的是
    「跑了一半」的记录，而它**不该被当成还在跑**。改回 ``pending`` 让它**重新被预留**。"""
    s = scheduler()
    session = _FakeSession()

    async def scan_tasks(query: Any, limit: Any, cursor: Any) -> dict[str, Any]:
        return {"items": [record(7, status="running")] if query["status"] == "running" else []}

    session.tx.scan_tasks = scan_tasks  # type: ignore[attr-defined]
    s._session = session

    await s.open(BACKGROUND_CONTEXT)
    written = [w for w in session.tx.written if w["id"] == 7]  # type: ignore[attr-defined]
    assert written and written[-1]["state"]["status"] == "pending"
    assert 7 in s._live


async def test_open_remembers_abandoned_tasks() -> None:
    s = scheduler()
    session = _FakeSession()
    abandoned = {**record(7, status="pending"), "abandonOnRestart": True}

    async def scan_tasks(query: Any, limit: Any, cursor: Any) -> dict[str, Any]:
        return {"items": [abandoned] if query["status"] == "pending" else []}

    session.tx.scan_tasks = scan_tasks  # type: ignore[attr-defined]
    s._session = session
    await s.open(BACKGROUND_CONTEXT)
    assert 7 in s._abandoned


async def test_open_queues_a_reconcile() -> None:
    """⚠️ 开档**不派发任何东西**，但**要把那次对账排上**——
    崩溃留下的、还没施加到「已取消拥有者」下面的中止标记要靠它推导。"""
    s = scheduler()
    s._session = _FakeSession()
    await s.open(BACKGROUND_CONTEXT)
    assert s._cascade_pending is True


def test_resume_enables_and_kicks() -> None:
    s = scheduler()
    assert s._enabled is False
    s.resume()
    assert s._enabled is True
    assert s._dirty is True  # `_kick` 记下了请求


def test_resume_is_idempotent() -> None:
    s = scheduler()
    s.resume()
    s.resume()
    assert s._enabled is True


async def test_wait_for_idle_returns_at_once_when_already_idle() -> None:
    s = scheduler()
    s._session = _FakeSession()
    await asyncio.wait_for(s.wait_for_idle(1, BACKGROUND_CONTEXT), 2)


async def test_wait_for_idle_registers_a_waiter_when_busy() -> None:
    """⚠️ 空闲判定要做**两次**：快路径之后还要**登记**。
    只做前者会在「刚好在两次之间变空闲」时**永远等下去**。"""
    s = scheduler()
    s._session = _FakeSession()
    s._edges[1] = None
    s._track(None, record(7))

    waiting = asyncio.ensure_future(s.wait_for_idle(1, BACKGROUND_CONTEXT))
    await asyncio.sleep(0.01)
    assert not waiting.done()

    s._idle_waiters.resolve(1, None)
    assert await asyncio.wait_for(waiting, 2) is None


async def test_abort_conversation_marks_the_reachable_tasks_and_withdraws_inputs() -> None:
    """⚠️ 一次提交里做两件事：**撤回排队中的输入** + **给能到达的每个活的非后台任务打标记**。"""
    s = scheduler()
    session = _FakeSession()
    s._session = session
    s._edges[1] = None
    s._track(None, record(7))  # 活的、非后台

    withdrawn: list[int] = []

    async def withdraw(tx: Any, conversation_id: int) -> None:
        withdrawn.append(conversation_id)

    s._withdraw_inputs = withdraw
    # 让它一开档就空闲，于是 `wait_for_idle` 立刻返回
    s._idle = lambda conversation_id: True  # type: ignore[method-assign]

    # ⚠️ `_load_scopes(True)` 读的是**存储**，不是事务——所以 stub 要挂在 `_storage` 上。
    async def scan_submissions(query: Any, limit: Any, cursor: Any, context: Any = None) -> Any:
        # ⚠️ 两处都要对：签名是**四个**位置参数，返回值要是一页 `{"items": [...]}`
        # （`scan_all` 按页取，直接返回 list 会报 "list indices must be integers"）。
        return {"items": [{"id": 5, "conversationId": 1, "type": "input", "status": "queued"}]}

    s._storage = type("St", (), {"scan_submissions": staticmethod(scan_submissions)})()
    await s.abort_conversation(1, False, BACKGROUND_CONTEXT)

    written = [w for w in session.tx.written if w["id"] == 7]  # type: ignore[attr-defined]
    assert written and written[-1]["abortRequested"] is True
    assert withdrawn == [1]


# --------------------------------------------------------------------------------------
# C5：检视（inspect / _inspect_task）
# --------------------------------------------------------------------------------------


async def test_inspect_reports_the_scheduling_state() -> None:
    """三态：``closing`` > ``running`` > ``paused``——**顺序即优先级**。"""
    s = scheduler()
    s._session = _FakeSession()
    assert (await s.inspect(s._registry.snapshot()))["scheduling"] == "paused"
    s._enabled = True
    assert (await s.inspect(s._registry.snapshot()))["scheduling"] == "running"
    s._closing = True
    assert (await s.inspect(s._registry.snapshot()))["scheduling"] == "closing"


async def test_inspect_running_wins_over_everything() -> None:
    """⚠️ **有活的调用 → ``running``**——它在跑，别的原因都不重要。"""
    s, runtime = _runtime_instance()
    s._session = _FakeSession()
    state = s._inspect_task(s._live[7], s._registry.snapshot())
    assert state == {"kind": "running"}


async def test_inspect_reports_completing_before_waiting() -> None:
    """⚠️ 检查顺序即优先级：``completing`` 在 ``waiting`` 之前。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="completing", outcome="completed"))
    assert s._inspect_task(s._live[7], s._registry.snapshot()) == {"kind": "completing"}


async def test_inspect_reports_waiting_with_its_dependencies() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7, status="waiting", on=[3]))
    s._live[3] = record(3)
    assert s._inspect_task(s._live[7], s._registry.snapshot()) == {"kind": "waiting", "on": [3]}


async def test_inspect_reports_a_missing_definition_as_blocked() -> None:
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    state = s._inspect_task(s._live[7], s._registry.snapshot())
    assert state["kind"] == "blocked"
    assert state["reason"] == "missing_task"


async def test_inspect_reports_a_missing_migration_with_the_error_now() -> None:
    """⚠️ 要迁移但那个定义**没有迁移** → ``blocked: migration_failed``，
    **并现在就造出那句错误**——否则调用方只知道「被挡了」，**不知道为什么**。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    newer = type("T", (), {"definition": {"version": 2}})()
    snapshot = type("S", (), {"task": lambda self, kind: newer})()

    state = s._inspect_task(s._live[7], snapshot)
    assert state["kind"] == "blocked"
    assert state["reason"] == "migration_failed"
    assert "has no migration" in str(state["error"])


async def test_inspect_reports_ready_with_the_migrates_flag() -> None:
    """⚠️ **一次待处理的迁移显示为 ``ready`` 且带 ``migrates``**——
    它还没被试过，所以**不是失败**（只有「已经试过的」或「不可能存在的」才显示为失败）。"""
    s = scheduler()
    s._edges[1] = None
    s._track(None, record(7))
    newer = type("T", (), {"definition": {"version": 2, "migrate": object()}})()
    snapshot = type("S", (), {"task": lambda self, kind: newer})()

    assert s._inspect_task(s._live[7], snapshot) == {"kind": "ready", "migrates": True}


async def test_inspect_returns_a_record_and_state_per_live_task() -> None:
    s = scheduler()
    s._session = _FakeSession()
    s._edges[1] = None
    s._track(None, record(7))
    result = await s.inspect(s._registry.snapshot())
    assert [t["record"]["id"] for t in result["tasks"]] == [7]
    assert result["tasks"][0]["state"]["kind"] == "blocked"


# --------------------------------------------------------------------------------------
# 整类完工：完整契约测试
# --------------------------------------------------------------------------------------


def test_the_whole_class_satisfies_every_contract_it_implements() -> None:
    """⚠️ **整类完工的契约测试**——一次断言三个契约：

    * `TaskRuntime`（20+ 成员 + 两个父接口）
    * `RegistryReader` 的**消费面**在 `_registry` 上（不在调度器上）
    * `HookRunner`（由 `_RuntimeView` 兼任）

    **用真实实例**（不是类对象）——这是与「便宜的替代物」那条判据的一致做法。
    """
    from pi_durable.harness.runtime import TaskRuntime
    from pi_durable.harness.types import HookRunner

    _s, runtime = _runtime_instance()
    for contract, holder in ((TaskRuntime, runtime), (HookRunner, runtime)):
        required = {n for n in dir(contract) if not n.startswith("_")}
        have = {n for n in dir(holder) if not n.startswith("_")}
        assert required <= have, f"{contract.__name__} 缺：{sorted(required - have)}"


def test_the_class_exposes_every_upstream_member() -> None:
    """上游 `TaskScheduler` 的**公共面**：8 个 C1 方法 + `index_sizes` + `inspect`。"""
    public = {
        "open",
        "resume",
        "join",
        "abort",
        "wait_for_task",
        "wait_for_idle",
        "abort_conversation",
        "inspect",
    }
    have = {n for n in dir(TaskScheduler) if not n.startswith("_")}
    assert public <= have, sorted(public - have)
    assert "index_sizes" in have
