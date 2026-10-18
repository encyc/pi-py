"""``TaskScheduler`` 的**归属图**（C3）与**对账链**（C2）的测试。

⚠️ 这是**纯内存图**的测试：直接摆好调度器的字段，然后断言图的不变量。
**不接存储、不跑任务**——那要等 C4/C6（调用与阶段执行）落地。
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT
from pi_durable.harness.scheduler import (
    _MISSING,
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
    return TaskScheduler(TaskSchedulerOptions(**fields))


class _EmptyStorage:
    """什么都不存在的存储替身。"""

    async def task(self, id: int, context: Any) -> Any:
        return None


class _FakeSession:
    """一个最小的 Session 替身：``commit_with`` 直接跑回调，并让调用方有机会插队。

    这样能测到「**排队之后、真正跑之前**状态变了」——真 Session 的顺序不可控。
    """

    def __init__(self) -> None:
        self.before_run: Any = None
        self.failure: Any = None

    def commit_with(self, change: Any, context: Any, scope: Any = None) -> Any:
        import asyncio

        async def run() -> Any:
            if self.before_run is not None:
                self.before_run()
            return await change(None)

        return asyncio.ensure_future(run())

    def read_on_line(self, job: Any) -> Any:
        import asyncio

        return asyncio.ensure_future(job())


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
