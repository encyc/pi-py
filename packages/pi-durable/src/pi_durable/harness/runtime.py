"""`TaskRuntime` —— 任务定义与调度器之间的**中枢契约** —— 移植上游 ``types.ts:170-240``
（约 24 个成员，外加两个父接口）。

**为什么它单独一个模块，而不在 `types.py`**（R21 第三面在**落位**上的应用）：

它同时需要 ``session/tx.py`` 的 ``DocumentObserver`` 与 ``harness/types.py`` 的
``HookRunner`` / ``ConversationHandle`` / ``RegistrySnapshot`` / ``Agent`` / ``Settings`` /
``DocumentReader``。所以它必须住在**两者之后**——**契约住在它依赖的类型之后，
而不是「看起来该住的地方」**（按名字归类会把它放进 `types.py`，那是错的）。

**它为什么必须在 `tool.ts` 之前落地**：`TaskRuntime` 是**每一个任务定义**
（`ToolTask` / `GenerationTask` / `CompactionTask`）拿到的那个参数。
它的**实现在 `scheduler.ts`**（最后一步），但它的**形状要在所有任务之前定**——
**契约要早定，实现要晚写**（R21 第三面表现 B）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pi_ai import Models
from pi_chord.context import AbortSignal, Context
from pi_durable.harness.types import (
    Agent,
    ConversationHandle,
    DocumentReader,
    HookRunner,
    RegistrySnapshot,
    Settings,
    SettledTask,
)
from pi_durable.session.tx import DocumentObserver
from pi_durable.types import (
    EntryRecord,
    TaskOutcome,
    TaskRecord,
)

__all__ = ["TaskRuntime"]


#: ``memo()`` 的「只读」哨兵。
#:
#: ⚠️ **不能用 `None` 当哨兵**：上游的 ``candidate: T`` 里 ``T extends JsonValue``，
#: 而 ``null`` 是合法的 JSON 值——用 ``None`` 会让「存一个 null」被当成「只读」。
class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


@runtime_checkable
class TaskRuntime(DocumentObserver, DocumentReader, Protocol):
    """一次任务调用的运行时（``types.ts:170-240``）。

    它继承两个父接口：

    * :class:`~pi_durable.session.tx.DocumentObserver`（非创建式文档观察）
    * :class:`~pi_durable.harness.types.DocumentReader`（``snapshot`` / ``snapshot_as_of``）

    ⚠️ **每一次调用结束后，所有操作都拒绝**；通过它取得的观察也在调用结束时停。
    """

    # ── 身份与取消 ────────────────────────────────────────────────────────────────────

    @property
    def task_id(self) -> int:
        """本任务的 ID。"""
        ...

    @property
    def conversation_id(self) -> int:
        """本任务所在对话的 ID。"""
        ...

    @property
    def signal(self) -> AbortSignal:
        """被 ``abortTask()``、Harness 关闭、或**调用结束**时中止。

        ⚠️ 调用结束后**仍在用它**的活（比如一个脱离的等待）会被取消——
        反正它那时也写不进任何东西。
        """
        ...

    # ── 阶段环境 ──────────────────────────────────────────────────────────────────────

    @property
    def registry(self) -> RegistrySnapshot:
        """**当前阶段**的注册表快照；每个阶段边界刷新。"""
        ...

    async def agent(self, context: Context) -> Agent[Any]:
        """本任务所在对话的 agent。

        每个阶段**至多解析一次**、在**首次使用**时解析，并在该阶段内固定。
        """
        ...

    @property
    def settings(self) -> Settings:
        """``HarnessOptions.settings``，**每次访问**都重新解析。"""
        ...

    @property
    def models(self) -> Models:
        """模型门面。

        ⚠️ 本端 `pi_ai.Models` 目前是 **9/24**：``stream`` / ``complete`` / ``checkAuth`` /
        ``getProviders`` 等 15 个成员**未声明**（被上游-ai 的架构裁决挡着）。
        durable 只用到 5 个（``get_model`` / ``stream_simple`` / ``complete_simple`` /
        ``fetch_deferred`` / ``cancel_deferred``），**全在**。
        **若哪天真需要那 15 个里的一个，先报告，不要自己扩、也不要退回 `Any`。**
        """
        ...

    async def env(self, context: Context) -> Any:
        """为本任务所在对话调用一次 ``HarnessOptions.env``；**用它的错误拒绝**。"""
        ...

    @property
    def hooks(self) -> HookRunner:
        """本任务**名字**对应的钩子处理器，来自它所在对话选中的扩展，**按扩展序**。"""
        ...

    # ── 提交与备忘 ────────────────────────────────────────────────────────────────────

    async def commit(self, change: Any, context: Context) -> None:
        """**重读任务之后**在 Session 线上提交。

        在下列情形拒绝：任务已终局、调用已结束、Harness 正在关闭、
        或（在 run 调用里）任务带中止标记。
        返回的 state **在同一次提交里**替换任务状态；**不返回**则保持不变。
        ``tx.create_task()`` 默认用本任务的对话。

        ⚠️ **回调里只通过 ``tx`` 读**：``snapshot()`` / ``document_state()`` / ``watch_doc()`` /
        ``get_task()`` 以及其它 Session 与运行时读**可能排队在回调自己持有的那条线上而永不返回**。
        """
        ...

    async def memo(self, name: str, context: Context, candidate: Any = UNSET) -> Any:
        """读一个本任务的**持久备忘**；给了 ``candidate`` 就是「不存在才存，返回持久赢家」。

        上游是两个重载 ``memo(name, context)`` / ``memo(name, candidate, context)``。
        本包把 ``candidate`` 做成**带哨兵的第三个参数**——
        **不能用 `None` 当哨兵**，因为 ``null`` 是合法的 JSON 值（见 :data:`UNSET`）。
        """
        ...

    # ── 任务 ──────────────────────────────────────────────────────────────────────────

    async def get_task(self, id: int, context: Context) -> TaskRecord | None:
        """已提交的任务记录。"""
        ...

    async def wait_for_task(self, id: int, context: Context) -> SettledTask:
        """等一个任务的**终局回执**；调用结束时拒绝。"""
        ...

    async def abort_owned(self, id: int, context: Context) -> None:
        """中止一个**本任务拥有**的任务（语义同 ``Harness.abortTask()``），并等到它终局。

        对**别人拥有**的任务拒绝；对已终局的任务**立刻返回**。
        """
        ...

    async def owned_tasks(self, context: Context) -> list[TaskRecord]:
        """本任务**直接拥有**的活任务，按 ID 序——**也包括更早一次调用创建的**（重启前那些）。

        **不含**它拥有的对话里的任务。返回的是已提交记录的**副本**。
        """
        ...

    async def outcomes(self, ids: Any, context: Context) -> list[TaskOutcome]:
        """终局任务的结果，按序。

        任一个**缺失或不是终局**时拒绝。用在一次等待**之后**。
        """
        ...

    # ── 对话与条目 ────────────────────────────────────────────────────────────────────

    async def conversation(self, id: int, context: Context) -> ConversationHandle | None:
        """一个**已存在**对话的调用期句柄（比如本任务拥有的那个）；不存在时 ``None``。

        它的操作与它返回的提交在**调用结束后拒绝**；已准入的工作**保持持久**。
        """
        ...

    async def entry(self, id: int, context: Context, token: Any = None) -> EntryRecord | None:
        """本任务所在对话里**可见**的已提交条目。

        给了 ``token`` 就按它的种类收窄：条目不存在、**不可见**、或**种类不符**时返回 ``None``。
        （上游是两个重载 ``entry(id, context)`` / ``entry(token, id, context)``；
        本包把令牌做成第三个可选参数，与 :meth:`Tx.entry` 同一取舍。）
        """
        ...

    async def context(
        self, conversation_id: int, context: Context, options: dict[str, Any] | None = None
    ) -> Any:
        """已提交的**原始**活动 transcript 与模型上下文，可选地在可见条目 ``at`` 处切掉。"""
        ...

    # ── 时钟与上报 ────────────────────────────────────────────────────────────────────

    def now(self) -> float:
        """Harness 的时钟。"""
        ...

    def report(self, error: BaseException) -> None:
        """把一个**非致命**失败转给 ``HarnessOptions.onReport``。"""
        ...

    async def sleep(self, until: float, context: Context) -> None:
        """等 Harness 时钟走到 ``until``；调用或 ``context`` 被取消时拒绝。"""
        ...
