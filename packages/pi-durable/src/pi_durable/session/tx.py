"""一次 Session 提交回调的**事务面** —— 移植上游 ``Tx``（``types.ts:787-895``，**31 个成员**）。

**为什么单独一个模块**：``Tx`` 的签名引用 ``DocToken`` / ``DocFamilyToken``，
而 ``documents.py`` **运行时导入** ``types.py``（L0）。把 ``Tx`` 放进 ``types.py``
会造成**运行时循环导入**。所以它必须住在 ``documents.py`` **之后**——
这正是 **R21 第三面**那条判据（「排顺序看谁需要它的形状」）在**落位**上的应用：
契约住在**它依赖的类型之后**，而不是「看起来该住的地方」。

**契约与实现分离**（上游也是这样）：这里是**形状**，实现在 ``session/transaction.py``。
本包的 ``Transaction`` 已经具备这 31 个方法，所以这个 Protocol 是**对既有实现的收口**，
不是新行为。

⚠️ **三种 scope 令牌在 Python 里塌成一个**：上游有 ``SessionDocToken`` /
``ConversationDocToken`` / ``TaskDocToken``（以及三个 family 版），
靠**类型参数**把 scope 编进类型。本包的 ``DocToken`` 把 scope 放在 ``definition`` 里、
由 :func:`~pi_durable.documents.check_record_scope` 在**运行期**校验——
所以这里不再造六个只在类型上不同的别名（那会是**噪音，不是契约**）。
同理 ``TypedEntry<D>`` / ``TypedEntryDraft<D>`` 在 Python 里就是 ``EntryRecord`` /
``EntryDraft``（类型参数被擦除，运行期靠 ``EntryToken.is_()`` 收窄）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pi_durable.documents import DocFamilyToken, DocToken
from pi_durable.entries import EntryToken
from pi_durable.types import (
    ConversationRecord,
    EntryDraft,
    EntryQuery,
    EntryRecord,
    SubmissionCreate,
    SubmissionId,
    SubmissionRecord,
    SubmissionSettlement,
    TaskId,
    TaskOptions,
    TaskQuery,
    TaskRecord,
)

__all__ = ["DocumentObserver", "Tx"]

#: ``Tx.scan_*`` 的游标。
Cursor = Any

#: ``Tx.task()`` 返回的**擦除后**的任务记录（类型参数在运行期不存在）。
AnyTaskRecord = TaskRecord

#: 一条**已收窄**的条目记录；上游是 ``TypedEntry<D>``，Python 里类型参数被擦除。
TypedEntryRecord = EntryRecord


@runtime_checkable
class DocumentObserver(Protocol):
    """**非创建式**的文档观察获取，Session 与后续的调用期 API 共用（``types.ts:914-935``）。

    它也是 ``TaskRuntime`` 继承的两个父接口之一（另一个是 ``DocumentReader``）。

    上游有 **6 个重载**（session / conversation+id / task+id，三个 family 版再带 ``key``），
    都返回 ``Promise<DocumentWatch<T> | undefined>``——**``None`` 表示文档不存在**
    （它是非创建式的：不会顺手把文档建出来）。

    本包把它们收成**一个**：``address`` 是剩余实参、``context`` 恒在最后
    （与 :meth:`Tx.doc` 同一取舍）。**「文档不存在返回 ``None``」这一点照抄**——
    否则「观察一个还没建的文档」会把文档建出来，那正是 ``watch`` 与 ``doc`` 的区别。
    """

    async def watch_doc(self, token: DocToken, *address: Any) -> Any:
        """观察一个已提交的文档；**不存在时返回 ``None``**。

        ``address`` 的形状由 ``token.definition["scope"]`` 决定，
        ``context`` 是**最后一个**实参（上游 6 个重载里它都在末尾）。
        """
        ...


@runtime_checkable
class Tx(Protocol):
    """一次 Session 提交回调的事务面（``types.ts:787-895``）。

    表读与创建结果是**可信的不可变值**，可以与提交内部的共享状态共享。

    ⚠️ 只在提交回调里用：**回调持有会话线**，所以在里面做 Session 与运行时的读
    可能排队在它自己持有的那条线上而**永不返回**。
    """

    # ── 读 ────────────────────────────────────────────────────────────────────────────

    async def conversation(self, id: int) -> ConversationRecord | None:
        """一个对话记录。"""
        ...

    async def entry(self, id: int, token: EntryToken | None = None) -> EntryRecord | None:
        """一条条目记录；给了 ``token`` 就**按它的种类收窄**（种类不符返回 ``None``）。

        上游是两个重载 ``entry(id)`` / ``entry<D>(token, id)``。本包把令牌做成
        **第二个可选参数**（PORTING.md 偏离 #48 的同一取舍），
        所以**一个名字覆盖两个重载**——不另造 ``entry_of`` 那样的新名字。
        """
        ...

    async def task(self, id: int) -> AnyTaskRecord | None:
        """一个任务记录。"""
        ...

    async def scan_conversations(self, query: Any, limit: int, cursor: Cursor | None = None) -> Any:
        """分页扫对话。"""
        ...

    async def scan_entries(
        self, query: EntryQuery, limit: int, cursor: Cursor | None = None
    ) -> Any:
        """分页扫条目。"""
        ...

    async def latest_head_marker(self, conversation_id: int) -> EntryRecord | None:
        """该对话**最新可见**的、带 ``head`` 的条目。"""
        ...

    async def scan_tasks(self, query: TaskQuery, limit: int, cursor: Cursor | None = None) -> Any:
        """分页扫任务。"""
        ...

    async def submission_by_request(
        self, conversation_id: int, request_id: str
    ) -> SubmissionRecord | None:
        """按**对话作用域**的请求 ID 取已提交的提交记录。"""
        ...

    # ── 创建 ──────────────────────────────────────────────────────────────────────────

    async def create_conversation(self, options: Any) -> ConversationRecord:
        """以**显式选定**的归属创建一个对话。"""
        ...

    async def fork_conversation(
        self, parent_conversation_id: int, at: int, options: Any
    ) -> ConversationRecord:
        """在一个**具体的可见条目**处创建历史分叉，归属同样显式选定。"""
        ...

    async def append_entry(self, conversation_id: int, value: EntryDraft) -> EntryRecord:
        """追加一条条目。返回的记录是 **Session 拥有的不可变值**，可以与提交监听器共享。"""
        ...

    async def create_task(self, task: Any, input: Any, options: TaskOptions) -> TaskId:
        """创建一个任务。"""
        ...

    async def create_submission(self, create: SubmissionCreate) -> SubmissionRecord:
        """用**新的 ID** 创建一条原始提交记录。

        ⚠️ **不施加任何准入规则**：不检查忙碌、不进收件箱排队、不放置。
        除非调用方自己实现准入，否则请用 ``Conversation.submit()`` 或对话句柄。
        """
        ...

    # ── 结算与放置（**同步**：它们只记账，不读表）────────────────────────────────────

    def settle_submission(self, id: SubmissionId, settlement: SubmissionSettlement) -> None:
        """结算一条排队中或已放置的提交。

        只有**已放置**的输入才能被应答；**已结算的提交保持不变**。
        它针对**本次事务最新的**提交记录解析，所以在表写入之后仍然有效。
        运行任务结算它们应答的输入。
        """
        ...

    def place_submission(self, id: SubmissionId, entry: int) -> None:
        """把一条排队中的提交放置在 ``entry``：输入变 ``placed``、写入变 ``done``。

        解析方式同 :meth:`settle_submission`。收件箱边界放置它们选中的提交。
        """
        ...

    # ── 文档 ──────────────────────────────────────────────────────────────────────────

    async def doc(self, token: DocToken, *address: Any) -> Any:
        """取一个文档草稿（``Draft``）。

        上游有 **6 个重载**（session / conversation+id / task+id，以及三个 family 版带 ``key``/``seed``）。
        本包把它们收成一个：``address`` 是**剩余实参**——
        session 无、conversation 与 task 一个（id）、family 版两个（id 与 key）或三个（key、seed）。
        运行期由 ``token.definition["scope"]`` 决定地址的形状。
        """
        ...

    async def retire_doc(self, token: DocToken | DocFamilyToken, *address: Any) -> None:
        """让一个文档（或文档族的一个成员）退役。

        地址约定同 :meth:`doc`。上游同样有 **6 个重载**。
        """
        ...
