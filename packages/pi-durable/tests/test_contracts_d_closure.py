"""``TaskRuntime`` 闭包里**不涉及 `Models`** 的 7 个契约（批次 D，R28 情形二）。

这些是 `TaskRuntime` 的连带依赖，但它们**彼此独立、形状已知**，所以能在
`task-42` 的 `Models` 到位之前先做完。

⚠️ **契约测试与行为测试是两种东西**：这里断言的是「**形状齐全且不可实例化**」，
不是「行为正确」。混在一起会让人以为契约测试是多余的。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from pi_durable.harness import ConversationHandle, HookRunner
from pi_durable.session.tx import DocumentObserver
from pi_durable.types import (
    DocumentWatch,
    NextTaskState,
    RunningTask,
    TypedEntry,
    TypedEntryDraft,
    WatchHandle,
)

PROTOCOLS = [
    ("RunningTask", RunningTask),
    ("DocumentObserver", DocumentObserver),
    ("HookRunner", HookRunner),
    ("ConversationHandle", ConversationHandle),
]


@pytest.mark.parametrize(("name", "contract"), PROTOCOLS)
def test_contract_is_a_protocol_and_cannot_be_instantiated(name: str, contract: Any) -> None:
    assert getattr(contract, "_is_protocol", False) is True, name
    with pytest.raises(TypeError):
        contract()


# --------------------------------------------------------------------------------------
# DocumentObserver
# --------------------------------------------------------------------------------------


def test_document_observer_collapses_six_overloads_into_one() -> None:
    """上游 ``watchDoc`` 有 **6 个重载**（session / conversation+id / task+id，
    三个 family 版再带 ``key``），本包收成一个：``token, *address``。

    与 :meth:`Tx.doc` 同一取舍——scope 在本包是**运行期属性**。
    """
    parameters = list(inspect.signature(DocumentObserver.watch_doc).parameters)
    assert parameters == ["self", "token", "address"]


def test_document_observer_is_non_creating() -> None:
    """⚠️ 它是**非创建式**的：文档不存在时返回 ``None``。

    这正是 ``watch_doc`` 与 ``doc`` 的区别——``doc`` 会把文档建出来，
    而「观察一个还没建的文档」不该有副作用。
    """
    source = inspect.getsource(DocumentObserver.watch_doc)
    assert "None" in source


# --------------------------------------------------------------------------------------
# HookRunner
# --------------------------------------------------------------------------------------


def test_hook_runner_has_one_method() -> None:
    assert {n for n in dir(HookRunner) if not n.startswith("_")} == {"each"}


def test_hook_runner_documents_the_report_and_continue_rule() -> None:
    """⚠️ **``invoke`` 普通抛出 → 报告后继续下一个；调用本身被取消 → 向上传播。**

    本包的 ``ToolTask`` 靠这个语义实现「第一个 block 生效、其余不再跑」，
    所以这条语义不能只写在 TS 注释里。
    """
    doc = HookRunner.each.__doc__ or ""
    assert "报告" in doc and "下一个" in doc
    assert "取消" in doc and "传播" in doc


# --------------------------------------------------------------------------------------
# ConversationHandle
# --------------------------------------------------------------------------------------


def test_conversation_handle_members() -> None:
    """四个成员：``id`` / ``submit`` / ``abort`` / ``waitForIdle``。"""
    assert {n for n in dir(ConversationHandle) if not n.startswith("_")} == {
        "abort",
        "id",
        "submit",
        "wait_for_idle",
    }


def test_conversation_handle_abort_waits_for_idle() -> None:
    """``abort()`` 不只是发信号——它**等到会话空闲**（撤回排队输入 + 中止普通作用域）。"""
    doc = ConversationHandle.abort.__doc__ or ""
    assert "空闲" in doc


# --------------------------------------------------------------------------------------
# 别名型契约（不是新类型，是**擦除后的别名 + 说明**）
# --------------------------------------------------------------------------------------


def test_typed_entry_is_an_alias_not_a_new_type() -> None:
    """``TypedEntry<D>`` 的类型参数在 Python 里**被擦除**。

    上游是 ``Omit<EntryRecord, "data"> & ({data: D} | {data?: never})``。
    本包给**别名 + 说明**，收窄交给 ``EntryToken.is_()`` / ``as_typed()``。
    **造一个新类只会得到"看起来更贴合上游、实际不提供任何保证"的东西。**
    """
    from pi_durable.types import EntryDraft, EntryRecord

    assert TypedEntry is EntryRecord
    assert TypedEntryDraft is EntryDraft


def test_typed_entry_narrowing_is_runtime_not_static() -> None:
    """所以收窄是**运行期**的事，做它的是 ``as_typed()``。"""
    from pi_durable.entries import as_typed, define_entry

    token = define_entry("pi.demo")
    assert as_typed(token, None) is None
    assert as_typed(token, {"id": 1, "kind": "other"}) is None  # 种类不符 → None
    matching = {"id": 2, "kind": "pi.demo"}
    assert as_typed(token, matching) is matching


def test_document_watch_is_a_watch_handle_of_nullable_value() -> None:
    """``DocumentWatch<T>`` = ``WatchHandle[Readonly<T> | None]``。

    **值是 ``None`` 表示文档不存在**——这与「存在但为空的对象」不同。
    """
    assert DocumentWatch is WatchHandle
    doc = WatchHandle.__doc__ or ""
    assert "WatchHandle" in doc or "观察" in doc


def test_next_task_state_is_documented_not_typed() -> None:
    """⚠️ 上游是 ``Extract<TaskState<S,R>, {status: "running"|"waiting"|"terminal"}>``。

    本包给 ``dict[str, Any]`` + 说明，因为那个 ``Extract`` 在 Python 里没有对应物。
    说明里必须留住**最要紧的那条语义**：返回 ``terminal`` 时，
    如果任务下面还有普通的活在跑，**存进去的是 ``completing``**（spec §5.5）——
    所以要读回来看，不能假定存进去的就是终局。
    """
    assert NextTaskState is dict or NextTaskState == dict[str, Any]


# --------------------------------------------------------------------------------------
# TaskRuntime —— 任务定义与调度器之间的中枢契约（types.ts:170-240）
# --------------------------------------------------------------------------------------


def test_task_runtime_is_a_protocol_and_cannot_be_instantiated() -> None:
    from pi_durable.harness import TaskRuntime

    assert getattr(TaskRuntime, "_is_protocol", False) is True
    with pytest.raises(TypeError):
        TaskRuntime()


def test_task_runtime_inherits_both_parent_interfaces() -> None:
    """⚠️ 上游是 ``interface TaskRuntime extends DocumentObserver, DocumentReader``。

    **我第一版漏了这两个父接口**（只写了 ``class TaskRuntime(Protocol)``），
    于是 ``watch_doc`` / ``snapshot`` / ``snapshot_as_of`` **一个都没继承到**——
    而 ``tool.ts`` 正是要调 ``runtime.entry()`` 与 ``runtime.commit()``，
    ``generation.ts`` 要调 ``runtime.context()``。漏父接口不会报错，只会让**使用方拿不到方法**。
    """
    from pi_durable.harness import TaskRuntime
    from pi_durable.harness.types import DocumentReader
    from pi_durable.session.tx import DocumentObserver

    members = {n for n in dir(TaskRuntime) if not n.startswith("_")}
    assert {"watch_doc"} <= members, "缺 DocumentObserver 的成员"
    assert {"snapshot", "snapshot_as_of"} <= members, "缺 DocumentReader 的成员"
    # ⚠️ 用 `__mro__` 查继承关系，不用 `issubclass`：后者要求**两个**父协议都是
    # `@runtime_checkable`（`DocumentReader` 不是），而这里要验的是**继承**本身。
    assert DocumentObserver in TaskRuntime.__mro__
    assert DocumentReader in TaskRuntime.__mro__


def test_task_runtime_has_every_upstream_member() -> None:
    """上游 24 个成员 → 本包 **22 个名字 + 3 个继承来的 = 25**。

    塌掉的两个：``memo`` 的两个重载、``entry`` 的两个重载
    （各由一个「带哨兵/可选参数」的方法覆盖）。
    """
    from pi_durable.harness import TaskRuntime

    own = {
        "task_id",
        "conversation_id",
        "signal",
        "registry",
        "agent",
        "settings",
        "models",
        "env",
        "hooks",
        "commit",
        "memo",
        "get_task",
        "wait_for_task",
        "abort_owned",
        "owned_tasks",
        "outcomes",
        "conversation",
        "entry",
        "context",
        "now",
        "report",
        "sleep",
    }
    present = {n for n in dir(TaskRuntime) if not n.startswith("_")}
    assert own <= present, f"缺：{sorted(own - present)}"
    assert present == own | {"watch_doc", "snapshot", "snapshot_as_of"}
    assert len(own) == 22  # 上游 24 − 2 个被塌掉的重载


def test_memo_uses_a_sentinel_not_none() -> None:
    """⚠️ **不能用 ``None`` 当「只读」哨兵**：上游 ``candidate: T`` 里 ``T extends JsonValue``，
    而 ``null`` 是**合法的 JSON 值**——用 ``None`` 会让「存一个 null」被当成「只读」。

    这是 R29（JS 三态不能压成两态）的又一个实例：`undefined` 与 `null` 是两回事。
    """
    from pi_durable.harness import UNSET, TaskRuntime

    assert UNSET is not None
    parameters = list(inspect.signature(TaskRuntime.memo).parameters)
    assert parameters == ["self", "name", "context", "candidate"]
    assert inspect.signature(TaskRuntime.memo).parameters["candidate"].default is UNSET


def test_entry_token_is_an_optional_third_parameter() -> None:
    """与 ``Tx.entry`` 同一取舍：令牌做成可选参数，**不另造名字**。"""
    from pi_durable.harness import TaskRuntime

    parameters = list(inspect.signature(TaskRuntime.entry).parameters)
    assert parameters == ["self", "id", "context", "token"]


def test_task_runtime_documents_the_models_limit() -> None:
    """⚠️ `pi_ai.Models` 目前 **9/24**——15 个成员未声明（架构裁决挡着）。

    `TaskRuntime.models` 只做**类型标注**，durable 只用那 5 个方法，所以不受影响。
    但这条限制必须写在契约旁边，**否则后人会以为门面是完整的**。
    """
    from pi_durable.harness import TaskRuntime

    doc = TaskRuntime.models.__doc__ or ""
    assert "9/24" in doc
    assert "不要自己扩" in doc


def test_task_runtime_documents_the_commit_reentrancy_rule() -> None:
    """⚠️ ``commit()`` 的回调里**只通过 ``tx`` 读**——否则会在它自己持有的线上排队而**永不返回**。

    这是「不会报错、只会挂住」的那类坑，必须写在契约上。
    """
    from pi_durable.harness import TaskRuntime

    doc = TaskRuntime.commit.__doc__ or ""
    assert "永不返回" in doc
