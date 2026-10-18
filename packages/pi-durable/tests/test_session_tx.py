"""``session/tx.py`` —— 上游 ``Tx``（``types.ts:787-895``，31 个成员）的契约测试。

**这个 Protocol 是对既有实现的收口**：批次 B 的 ``Transaction`` 已经具备全部方法。
所以测试的重点是**两者对得上**（结构子类型），而不是新行为。
"""

from __future__ import annotations

from typing import Any

import pytest

from pi_durable.session.tx import Tx

#: 上游 ``Tx`` 的 31 个成员，按 ``types.ts:787-895`` 的顺序。
UPSTREAM_MEMBERS = [
    # 读（9）
    "conversation",
    "entry",
    "task",
    "scanConversations",
    "scanEntries",
    "latestHeadMarker",
    "scanTasks",
    "submissionByRequest",
    # 创建（5）
    "createConversation",
    "forkConversation",
    "appendEntry",
    "createTask",
    "createSubmission",
    # 结算与放置（2）
    "settleSubmission",
    "placeSubmission",
    # 文档（2 个名字，12 个重载）
    "doc",
    "retireDoc",
]


def test_tx_is_a_protocol_and_cannot_be_instantiated() -> None:
    """与 ``model_registry`` 同款：契约不是实现。"""
    assert getattr(Tx, "_is_protocol", False) is True
    with pytest.raises(TypeError):
        Tx()  # type: ignore[misc]


def test_tx_has_every_upstream_member_name() -> None:
    """**31 个成员 → 18 个 Python 名字**：只有 ``doc``/``retireDoc`` 那 12 个重载塌了。

    逐个对照，确保没有漏掉任何一个上游成员名。
    """
    snake = {
        "conversation",
        "entry",
        "task",
        "scanConversations",
        "scanEntries",
        "latestHeadMarker",
        "scanTasks",
        "submissionByRequest",
        "createConversation",
        "forkConversation",
        "appendEntry",
        "createTask",
        "createSubmission",
        "settleSubmission",
        "placeSubmission",
        "doc",
        "retireDoc",
    }
    present = {n for n in dir(Tx) if not n.startswith("_")}
    # 驼峰 → 蛇形（本包约定：函数 snake_case）
    expected = {
        "conversation",
        "entry",
        "task",
        "scan_conversations",
        "scan_entries",
        "latest_head_marker",
        "scan_tasks",
        "submission_by_request",
        "create_conversation",
        "fork_conversation",
        "append_entry",
        "create_task",
        "create_submission",
        "settle_submission",
        "place_submission",
        "doc",
        "retire_doc",
    }
    assert present == expected, f"多/少：{sorted(present ^ expected)}"
    assert len(UPSTREAM_MEMBERS) == 17  # 17 个名字覆盖 31 个成员（14 个重载塌成 2 个）


def test_entry_covers_the_typed_overload_with_one_name() -> None:
    """⚠️ 上游是**两个重载** ``entry(id)`` / ``entry<D>(token, id)``。

    本包把令牌做成**第二个可选参数**，所以**一个名字覆盖两个重载**——
    **不另造** ``entry_of`` 那样的新名字。这与 ``doc``/``retire_doc`` 把 12 个重载
    收成一个的做法是同一取舍（偏离 #48）。
    """
    import inspect

    parameters = list(inspect.signature(Tx.entry).parameters)
    assert parameters == ["self", "id", "token"]


def test_settle_and_place_are_synchronous() -> None:
    """⚠️ 这两个上游就是**同步**的（``types.ts`` 里没有 ``Promise``）：

    它们只**记账**，不读表——所以在提交回调里可以安全地在一串表写入之后调用。
    本包照抄为普通方法（不是 ``async``）。
    """
    import inspect

    for name in ("settle_submission", "place_submission"):
        method = getattr(Tx, name)
        assert not inspect.iscoroutinefunction(method), f"{name} 不应是协程"


def test_scan_and_read_members_are_asynchronous() -> None:
    import inspect

    for name in (
        "conversation",
        "entry",
        "task",
        "scan_conversations",
        "scan_entries",
        "latest_head_marker",
        "scan_tasks",
        "submission_by_request",
        "create_conversation",
        "fork_conversation",
        "append_entry",
        "create_task",
        "create_submission",
    ):
        assert inspect.iscoroutinefunction(getattr(Tx, name)), f"{name} 应是协程"


# --------------------------------------------------------------------------------------
# 实现是否满足契约（结构子类型）
# --------------------------------------------------------------------------------------


def test_transaction_class_satisfies_the_tx_contract() -> None:
    """⚠️ **本 Protocol 是对既有实现的收口**，所以这条最有价值：

    批次 B 的 ``Transaction``（1400 行）必须**结构上**满足它。
    ``runtime_checkable`` 的 ``isinstance`` 只查**方法存在性**，不查签名——
    但「方法齐不齐」正是这里最容易漏的（上游 31 个 vs 本包 18 个名字）。
    """
    from pi_durable.session.transaction import Transaction

    instance_methods = {name for name in dir(Transaction) if not name.startswith("_")}
    required = {n for n in dir(Tx) if not n.startswith("_")}
    missing = required - instance_methods
    assert missing == set(), f"Transaction 缺这些 Tx 成员：{sorted(missing)}"
    # 反过来也看一眼：多出来的都是批次 B 的内部/扩展面，不属于 Tx
    assert "create_root_conversation" in instance_methods


def test_transaction_is_runtime_checkable_as_tx() -> None:
    from pi_durable.session.transaction import Transaction

    class Fake(Transaction):  # type: ignore[misc]
        def __init__(self) -> None:  # pragma: no cover - 只做结构检查
            pass

    assert isinstance(Fake(), Tx)


def test_doc_collapses_six_overloads_into_one() -> None:
    """上游 ``doc`` 有 **6 个重载**（session / conversation+id / task+id，三个 family 版再带 key/seed）。

    本包收成**一个**：``address`` 是剩余实参，运行期由 ``token.definition["scope"]``
    决定地址形状。这不是"少做了"，而是**scope 在本包是运行期属性**——
    上游那六个 ``DocToken`` 变体在 Python 里本就塌成一个 ``DocToken``。
    """
    import inspect

    parameters = list(inspect.signature(Tx.doc).parameters)
    assert parameters == ["self", "token", "address"]

    from pi_durable.session.tx import Tx as _Tx

    retire = list(inspect.signature(_Tx.retire_doc).parameters)
    assert retire == ["self", "token", "address"]


def test_no_scope_specific_token_aliases_are_introduced() -> None:
    """⚠️ 上游有 ``SessionDocToken`` / ``ConversationDocToken`` / ``TaskDocToken``（+ 三个 family 版）。

    本包**不造这六个别名**：``DocToken`` 的 scope 放在 ``definition`` 里，
    由 ``check_record_scope()`` 在**运行期**校验。造六个只在类型上不同的别名
    会是**噪音，不是契约**（R28 的反面：形状已知，但**语言里没有对应物可表达**）。
    """
    import pi_durable.session.tx as module

    for name in (
        "SessionDocToken",
        "ConversationDocToken",
        "TaskDocToken",
        "SessionDocFamilyToken",
        "ConversationDocFamilyToken",
        "TaskDocFamilyToken",
    ):
        assert not hasattr(module, name), f"不该引入 {name}"


def test_typed_entry_aliases_are_documented_not_invented() -> None:
    """``TypedEntry<D>`` / ``TypedEntryDraft<D>`` 的类型参数在运行期**被擦除**，
    所以本包给的是**别名 + 说明**，不是新类型。"""
    from pi_durable.session.tx import TypedEntryRecord
    from pi_durable.types import EntryRecord

    assert TypedEntryRecord is EntryRecord
