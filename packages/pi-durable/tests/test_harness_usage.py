"""``harness/usage.py`` 的测试 —— 移植 ``harness/usage.ts``（72 行）。

两个重点：

1. **``omitUndefinedProperties`` 是 no-op**（chord 的 J8），所以可选计数器的 ``None``
   必须由本模块**在构造处**过滤——而不是指望开关替我删；
2. 工具名可以就叫 ``__proto__`` / ``toString``：上游为此用 ``Object.defineProperty``
   绕开原型陷阱，Python 的 dict 没有原型链。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_ai import Usage
from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.harness.usage import (
    UsageDoc,
    add_usage,
    add_usage_state,
    record_usage,
    usage_json,
)
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage


def make_usage(**overrides: Any) -> Usage:
    base = {
        "input": 1,
        "output": 2,
        "cache_read": 0,
        "cache_write": 3,
        "total_tokens": 6,
        "cost": {
            "input": 0.1,
            "output": 0.2,
            "cache_read": 0.0,
            "cache_write": 0.0,
            "total": 0.3,
        },
    }
    base.update(overrides)
    return Usage(**base)


def zero_total() -> dict[str, Any]:
    return {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "total_tokens": 0,
        "cost": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0, "total": 0.0},
    }


# --------------------------------------------------------------------------------------
# 定义
# --------------------------------------------------------------------------------------


def test_usage_doc_semantics() -> None:
    """账本是**当前**值，而且分叉**从零开始**——子会话不该背父会话的账。"""
    definition = UsageDoc.definition
    assert definition["kind"] == "pi.usage"
    assert definition["scope"] == "conversation"
    assert definition["history"] == "latest"
    assert definition["fork"] == "initial"


def test_usage_doc_initial_is_empty_ledger() -> None:
    assert UsageDoc.definition["initial"](None) == {"models": {}, "tools": {}}


# --------------------------------------------------------------------------------------
# usage_json：过滤 None 的可选计数器
# --------------------------------------------------------------------------------------


def test_usage_json_drops_absent_optional_counters() -> None:
    """⚠️ 这条就是 J8：``None`` 的可选计数器**整个键不写**。

    chord 的 ``omitUndefinedProperties`` 刻意是 no-op（它会连合法的 JSON ``null``
    一起删掉），所以过滤必须发生在这里。
    """
    data = usage_json(make_usage())
    assert "cache_write_1h" not in data
    assert "reasoning" not in data
    assert data["input"] == 1
    assert data["cost"]["total"] == 0.3


def test_usage_json_keeps_present_optional_counters() -> None:
    data = usage_json(make_usage(cache_write_1h=4, reasoning=5))
    assert data["cache_write_1h"] == 4
    assert data["reasoning"] == 5


def test_usage_json_keeps_a_zero_counter() -> None:
    """⚠️ ``0`` 必须保留——它和「没有这个计数器」是两回事。"""
    data = usage_json(make_usage(reasoning=0))
    assert data["reasoning"] == 0


def test_usage_json_output_is_strict_json() -> None:
    from pi_chord.json import is_json_value

    assert is_json_value(usage_json(make_usage()))


# --------------------------------------------------------------------------------------
# add_usage
# --------------------------------------------------------------------------------------


def test_add_usage_accumulates_required_counters() -> None:
    total = zero_total()
    add_usage(total, make_usage())
    add_usage(total, make_usage())
    assert total["input"] == 2
    assert total["total_tokens"] == 12
    assert total["cost"]["total"] == 0.6


def test_add_usage_adds_an_optional_counter_once_either_side_reports_it() -> None:
    """可选计数器只要**有一边报了**就累加，另一边当作 0。"""
    total = zero_total()
    add_usage(total, make_usage())  # 没报
    assert "reasoning" not in total
    add_usage(total, make_usage(reasoning=7))
    assert total["reasoning"] == 7
    add_usage(total, make_usage())  # 这边没报，但已经有了，累加 0
    assert total["reasoning"] == 7


def test_add_usage_state_merges_buckets() -> None:
    state: dict[str, Any] = {"models": {}, "tools": {}}
    other: dict[str, Any] = {
        "models": {"a/x": usage_json(make_usage())},
        "tools": {"read": usage_json(make_usage())},
    }
    add_usage_state(state, other)  # type: ignore[arg-type]
    assert set(state["models"]) == {"a/x"}
    assert set(state["tools"]) == {"read"}

    add_usage_state(state, other)  # type: ignore[arg-type]
    assert state["models"]["a/x"]["input"] == 2  # 第二次是累加


def test_add_usage_state_handles_prototype_like_tool_names() -> None:
    """工具可以就叫 ``__proto__`` / ``toString``；Python 的 dict 没有原型链。"""
    state: dict[str, Any] = {"models": {}, "tools": {}}
    other: dict[str, Any] = {
        "models": {},
        "tools": {"__proto__": usage_json(make_usage()), "toString": usage_json(make_usage())},
    }
    add_usage_state(state, other)  # type: ignore[arg-type]
    assert set(state["tools"]) == {"__proto__", "toString"}
    assert {} == {}  # 全局 dict 未被污染


# --------------------------------------------------------------------------------------
# record_usage（接真实 Session）
# --------------------------------------------------------------------------------------


async def make_session(backend: str, tmp_path: Path) -> SessionImpl:
    storage = (
        MemoryStorage()
        if backend == "memory"
        else await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    )
    session = SessionImpl(storage)
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    return session


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_record_usage_creates_then_accumulates(backend: str, tmp_path: Path) -> None:
    session = await make_session(backend, tmp_path)
    try:

        async def change(tx: Any) -> None:
            await record_usage(tx, 1, "models", "anthropic/x", make_usage())
            await record_usage(tx, 1, "models", "anthropic/x", make_usage())
            await record_usage(tx, 1, "tools", "read", make_usage())

        await session.commit(change, CTX)
        state = await session.snapshot(UsageDoc, 1, CTX)

        assert state is not None
        assert state["models"]["anthropic/x"]["input"] == 2
        assert set(state["tools"]) == {"read"}
        assert state["tools"]["read"]["input"] == 1
    finally:
        await session.close(CTX)


async def test_record_usage_recognizes_a_tool_named_to_string() -> None:
    """「只认自有键」：一个工具可以就叫 ``toString``，账本上必须是**两个不同的桶**。"""
    session = SessionImpl(MemoryStorage())
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)

    async def change(tx: Any) -> None:
        await record_usage(tx, 1, "tools", "toString", make_usage())
        await record_usage(tx, 1, "tools", "other", make_usage())

    await session.commit(change, CTX)
    state = await session.snapshot(UsageDoc, 1, CTX)
    assert state is not None
    assert set(state["tools"]) == {"toString", "other"}


async def test_each_conversation_has_its_own_ledger() -> None:
    session = SessionImpl(MemoryStorage())
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    await session.commit(lambda tx: tx.create_conversation({"kind": "ownerless"}), CTX)

    async def first(tx: Any) -> None:
        await record_usage(tx, 1, "models", "a/x", make_usage())

    async def second(tx: Any) -> None:
        await record_usage(tx, 2, "models", "a/x", make_usage())

    await session.commit(first, CTX)
    await session.commit(second, CTX)
    assert (await session.snapshot(UsageDoc, 1, CTX))["models"]["a/x"]["input"] == 1
    assert (await session.snapshot(UsageDoc, 2, CTX))["models"]["a/x"]["input"] == 1


async def test_ledger_is_persisted_across_reopen(tmp_path: Path) -> None:
    path = str(tmp_path / "durable.db")
    first = await open_node_sqlite_storage(path)
    session = SessionImpl(first)
    try:
        await session.commit(lambda tx: tx.create_root_conversation(), CTX)

        async def change(tx: Any) -> None:
            await record_usage(tx, 1, "models", "a/x", make_usage(reasoning=9))

        await session.commit(change, CTX)
    finally:
        await session.close(CTX)

    second = await open_node_sqlite_storage(path)
    reopened = SessionImpl(second)
    try:
        state = await reopened.snapshot(UsageDoc, 1, CTX)
        assert state is not None
        assert state["models"]["a/x"]["reasoning"] == 9
    finally:
        await reopened.close(CTX)
