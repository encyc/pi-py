"""任务图 —— 移植 ``harness/task-graph.ts``（236 行）。

Session 里**每个活任务**的一棵只读视图（spec §9.5）：状态、阶段、等待的依赖、它拥有的对话。
给 UI 和调度器看「现在都有什么在跑」。

**它建在 Session 线上**：第一个观察者到来时构建，最后一个离开时丢弃。
之后靠 **Session 的提交发布**推进——那些发布是**持久**的，所以图不会与存储脱节。

⚠️ 本模块**不依赖 `tool.ts` / `generation.ts`**（那是 SCC 与 D-e），
所以它在被阻塞的那条关键路径之外，可以独立交付。它是 ``harness.ts`` 与
``scheduler.ts`` 的前置。
"""

from __future__ import annotations

from typing import Any

from pi_chord.context import Context, without_abort_signal
from pi_chord.delta import Op, apply_immutable
from pi_chord.json import json_equal
from pi_chord.state import replicated_state
from pi_durable.harness.util import closed_error, scan_all
from pi_durable.session.observation import CommittedStateSource, CommittedWatch
from pi_durable.session.session import SessionImpl, contained
from pi_durable.types import Storage

__all__ = [
    "SCAN_PAGE_SIZE",
    "TaskGraph",
    "TaskGraphNode",
    "TaskGraphState",
    "TaskGraphView",
    "TaskGraphWatch",
]

#: 图里保留的状态：终局任务**离开**图（``task-graph.ts:54``）。
LIVE_STATUSES: tuple[str, ...] = ("pending", "running", "waiting", "completing")

#: 内部扫描的分页大小（``task-graph.ts:55``）。
SCAN_PAGE_SIZE = 256

#: 一个活任务的持久状态，**不含**检查点与结果负载（spec §9.5）（``task-graph.ts:19-28``）。
TaskGraphState = dict[str, Any]

#: 图里的一个任务节点（``task-graph.ts:30-42``）。
TaskGraphNode = dict[str, Any]

#: Session 的**全部**活任务（spec §9.5）（``task-graph.ts:44-47``）。
TaskGraph = dict[str, Any]

#: 任务图的观察句柄。
TaskGraphWatch = Any


class _Mount:
    """图的一份挂载：当前值 + 附着在它上面的观察者（``task-graph.ts:57-60``）。"""

    __slots__ = ("observers", "value")

    def __init__(self, value: TaskGraph) -> None:
        self.value = value
        self.observers: set[Any] = set()


class TaskGraphView:
    """Harness 的任务图**挂载**（``task-graph.ts:62-149``）。

    由**第一个**观察者在 Session 线上构建，随**最后一个**观察者离开而丢弃。
    它从 Session 的提交发布推进——那些发布是持久的。
    """

    def __init__(self, session: SessionImpl, storage: Storage) -> None:
        self._session = session
        self._storage = storage
        self._mount: _Mount | None = None
        self._closed = False

        def on_commit(publication: dict[str, Any], context: Context) -> None:
            if self._mount is not None:
                advance(self._mount, publication, context, session.report)

        session.observe_commits(on_commit)

        def on_close() -> None:
            self._closed = True
            for observer in list(self._mount.observers if self._mount else ()):

                def close_it(obs: Any = observer) -> None:
                    obs.close_session(self._session.failure)

                contained(close_it, session.report)
            self._mount = None

        session.subscribe_close(on_close)

    async def state(self, context: Context) -> Any:
        """一份**可释放**的只读 chord 状态（``task-graph.ts:101-112``）。"""

        def make_state(value: Any, release: Any) -> Any:
            return CommittedStateSource(value, release)

        observer, detach = await self._attach(make_state, context)
        try:
            return replicated_state(observer, _options(self._session))
        except BaseException:
            detach()
            raise

    async def watch(self, context: Context) -> Any:
        """一份**串行化**的精确帧观察；取消 ``context`` 就停掉它（``task-graph.ts:115-127``）。"""

        def make_watch(value: Any, release: Any) -> Any:
            return CommittedWatch(value, release, self._session.report)

        observer, _detach = await self._attach(make_watch, context)
        signal = context.abort_signal
        if signal is not None and signal.aborted:
            observer.cancel()
            raise _abort_reason(signal)
        if signal is not None:
            observer.observe_cancellation(signal)
        return observer

    async def _attach(self, create: Any, context: Context) -> tuple[Any, Any]:
        """在当前修订上、**原子地**在 Session 线上注册一个观察者（``task-graph.ts:130-146``）。"""

        async def job() -> Any:
            mount = self._mount
            if mount is None:
                mount = _Mount(await self._build(context))
            holder: dict[str, Any] = {}

            def detach() -> None:
                mount.observers.discard(holder["observer"])
                if not mount.observers and self._mount is mount:
                    self._mount = None

            observer = create(mount.value, detach)
            holder["observer"] = observer
            # 构建挂载期间**可能**已经开始关闭或取消；那时什么都不注册。
            if self._closed:
                raise closed_error(self._session)
            if context.abort_signal is not None:
                context.abort_signal.throw_if_aborted()
            self._mount = mount
            mount.observers.add(observer)
            return observer, detach

        return await self._session.read_on_line(job)

    async def _build(self, context: Context) -> TaskGraph:
        """从存储构建一份完整图（``task-graph.ts:148-162``）。"""
        tasks: dict[str, Any] = {}
        records: list[dict[str, Any]] = []
        for status in LIVE_STATUSES:

            async def scan_status(cursor: Any, s: str = status) -> Any:
                return await self._storage.scan_tasks(
                    {"status": s}, SCAN_PAGE_SIZE, cursor, context
                )

            records.extend(await scan_all(scan_status))
        records.sort(key=lambda record: record["id"])
        for record in records:

            async def scan_owned(cursor: Any, rid: int = record["id"]) -> Any:
                return await self._storage.scan_conversations(
                    {"ownerTaskId": rid}, SCAN_PAGE_SIZE, cursor, context
                )

            owned = await scan_all(scan_owned)
            conversations = sorted(conversation["id"] for conversation in owned)
            tasks[str(record["id"])] = _node_of(record, conversations)
        return {"tasks": tasks}


def advance(
    mount: _Mount,
    publication: dict[str, Any],
    context: Context,
    report: Any,
) -> None:
    """从一次发布推导出操作、套用、再把修订交给每个观察者（``task-graph.ts:160-204``）。

    一个观察者抛出**只被报告**，其余照常拿到修订。
    """
    ops: list[Op] = []
    # 这次发布设置或删除的节点，覆盖在挂载的当前值之上。
    changed: dict[str, Any] = {}

    def node(key: str) -> Any:
        return changed[key] if key in changed else mount.value["tasks"].get(key)

    for change in publication["changes"]:
        if change["type"] != "task":
            continue
        record = change["value"]
        key = str(record["id"])
        previous = node(key)
        if record["state"]["status"] == "terminal":
            if previous is None:
                continue
            ops.append(("d", ("tasks", key)))
            changed[key] = None
            continue
        next_node = _node_of(record, (previous or {}).get("conversations") or [])
        if previous is not None and json_equal(next_node, previous):
            continue
        ops.append(("s", ("tasks", key), next_node))
        changed[key] = next_node

    # 放在任务**之后**：与拥有者任务在同一次提交里创建的对话，才能找到拥有者的节点。
    # 一次发布内部的变更顺序是**未定义的**，所以每个拥有者的列表要重新排序。
    created: dict[str, list[int]] = {}
    for change in publication["changes"]:
        if change["type"] != "conversation" or change["value"].get("owner") is None:
            continue
        key = str(change["value"]["owner"]["taskId"])
        if node(key) is not None:
            created.setdefault(key, []).append(change["value"]["id"])
    for key, ids in created.items():
        conversations = sorted([*node(key)["conversations"], *ids])
        ops.append(("s", ("tasks", key, "conversations"), conversations))

    if not ops:
        return
    mount.value = apply_immutable(mount.value, ops)
    # 帧的上下文**不带**调用方的取消信号：观察者自己管自己的取消。
    frame_context = without_abort_signal(context)
    for observer in list(mount.observers):

        def deliver(obs: Any = observer) -> None:
            obs.advance(mount.value, ops, frame_context)

        contained(deliver, report)


def _node_of(record: dict[str, Any], conversations: list[int]) -> TaskGraphNode:
    """一条任务记录 → 图节点（``task-graph.ts:206-217``）。"""
    node: dict[str, Any] = {
        "id": record["id"],
        "kind": record["kind"],
        "conversationId": record["conversationId"],
        "background": record["background"],
        "abortRequested": record["abortRequested"],
        "state": _state_of(record),
        "conversations": conversations,
    }
    if record.get("owner") is not None:
        node["owner"] = record["owner"]
    return node


def _state_of(record: dict[str, Any]) -> TaskGraphState:
    """任务记录的状态 → 图里那个**不含负载**的状态（``task-graph.ts:219-232``）。"""
    state = record["state"]
    status = state["status"]
    if status in ("pending", "running"):
        return {"status": status, "phase": _phase_of(state["checkpoint"])}
    if status == "waiting":
        return {
            "status": "waiting",
            "phase": _phase_of(state["checkpoint"]),
            "on": list(state["on"]),
            "policy": state["policy"],
        }
    # 终局记录**永远不会到这里**：它们离开图。
    return {"status": "completing", "outcome": state["outcome"]["status"]}


def _phase_of(checkpoint: Any) -> str:
    """检查点里的 ``phase``（``task-graph.ts:234-236``）。"""
    return str(checkpoint["phase"])


def _options(session: SessionImpl) -> Any:
    from pi_chord.state import ReplicatedStateSourceOptions

    return ReplicatedStateSourceOptions(on_error=session.report)


def _abort_reason(signal: Any) -> BaseException:
    reason = getattr(signal, "reason", None)
    if isinstance(reason, BaseException):
        return reason
    from pi_chord.context import AbortError

    return AbortError("The operation was aborted")
