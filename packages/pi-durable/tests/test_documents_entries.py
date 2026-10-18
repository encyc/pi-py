"""文档定义层与条目种类的测试 —— ``documents.ts``(208) + ``entries.ts``(34)。

含一条**真实 SQLite 后端**的端到端往返：定义 → 创建 → 增量 → 读回 → 物化，
证明这一层与存储层的接缝是通的。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.documents import (
    address_id,
    check_record_scope,
    check_record_version,
    define_doc,
    define_doc_family,
    document_create,
    materialize_document,
    materialize_document_value,
    resolve_address,
)
from pi_durable.entries import (
    AssistantEntry,
    CompactionEntry,
    ResetEntry,
    SystemEntry,
    ToolResultEntry,
    UserEntry,
    as_typed,
    compaction_reason,
    define_entry,
    tool_result_diagnostics,
)
from pi_durable.storage.sqlite.node import open_node_sqlite_storage

pytestmark = pytest.mark.asyncio


def notes_definition(**extra: Any) -> dict[str, Any]:
    return {
        "kind": "pi.notes",
        "version": 1,
        "scope": "session",
        "initial": lambda seed=None: {"items": [], "n": 0},
        **extra,
    }


# --------------------------------------------------------------------------------------
# 1. 定义校验与地址解析
# --------------------------------------------------------------------------------------


async def test_define_doc_rejects_bad_version() -> None:
    for bad in (0, -1, 1.5, True, "1", None):
        with pytest.raises(TypeError, match="must be a positive integer"):
            define_doc({"kind": "bad", "version": bad, "scope": "session"})


async def test_resolve_address_per_scope() -> None:
    session = define_doc(notes_definition())
    assert resolve_address(session.definition, []).address == {
        "kind": "pi.notes",
        "scope": {"kind": "session"},
    }
    assert resolve_address(session.definition, []).next_argument == 0

    conversation = define_doc(
        {
            "kind": "pi.cache",
            "version": 2,
            "scope": "conversation",
            "history": "latest",
            "fork": "current",
        }
    )
    resolved = resolve_address(conversation.definition, [7, "ctx"])
    assert resolved.address == {
        "kind": "pi.cache",
        "scope": {"kind": "conversation", "conversationId": 7},
    }
    # 下标停在下一位——也就是 Context 参数所在处
    assert resolved.next_argument == 1

    task = define_doc({"kind": "pi.state", "version": 1, "scope": "task"})
    assert resolve_address(task.definition, [9]).address == {
        "kind": "pi.state",
        "scope": {"kind": "task", "taskId": 9},
    }


async def test_resolve_address_family_consumes_key() -> None:
    family = define_doc_family(
        {
            "kind": "pi.fam",
            "version": 1,
            "scope": "task",
            "family": True,
            "initial": lambda seed=None: {"seed": seed},
        }
    )
    resolved = resolve_address(family.definition, [9, "member", "ctx"])
    assert resolved.address == {
        "kind": "pi.fam",
        "scope": {"kind": "task", "taskId": 9},
        "key": "member",
    }
    assert resolved.next_argument == 2


async def test_resolve_address_rejects_non_integer_owner() -> None:
    conversation = define_doc(
        {
            "kind": "pi.c",
            "version": 1,
            "scope": "conversation",
            "history": "latest",
            "fork": "current",
        }
    )
    for bad in ("7", 1.5, None, True):
        with pytest.raises(TypeError, match="requires a conversation ID"):
            resolve_address(conversation.definition, [bad])


async def test_unknown_scope_is_rejected() -> None:
    token = define_doc({"kind": "pi.x", "version": 1, "scope": "galaxy"})
    with pytest.raises(TypeError, match="unknown scope"):
        resolve_address(token.definition, [])


# --------------------------------------------------------------------------------------
# 2. 地址身份
# --------------------------------------------------------------------------------------


async def test_address_id_matches_upstream_json_stringify() -> None:
    """与 JS 的 ``JSON.stringify([kind, scopeKind, owner, key ?? null])`` 逐字节一致。"""
    assert address_id({"kind": "pi.notes", "scope": {"kind": "session"}}) == (
        '["pi.notes","session",null,null]'
    )
    assert (
        address_id({"kind": "pi.cache", "scope": {"kind": "conversation", "conversationId": 7}})
        == '["pi.cache","conversation",7,null]'
    )
    assert (
        address_id({"kind": "pi.fam", "scope": {"kind": "task", "taskId": 9}, "key": "member"})
        == '["pi.fam","task",9,"member"]'
    )


async def test_address_id_distinguishes_prototype_like_and_empty_keys() -> None:
    """``__proto__`` / ``constructor`` 这些键必须彼此可区分（一致性套件同款风险面）。"""

    def with_key(key: str | None) -> str:
        address: dict[str, Any] = {"kind": "cache", "scope": {"kind": "session"}}
        if key is not None:
            address["key"] = key
        return address_id(address)

    ids = [with_key(k) for k in ("__proto__", "constructor", "toString", "", None)]
    assert len(set(ids)) == len(ids)


async def test_address_id_distinguishes_scopes() -> None:
    session = address_id({"kind": "k", "scope": {"kind": "session"}})
    conversation = address_id({"kind": "k", "scope": {"kind": "conversation", "conversationId": 0}})
    task = address_id({"kind": "k", "scope": {"kind": "task", "taskId": 0}})
    assert len({session, conversation, task}) == 3


# --------------------------------------------------------------------------------------
# 3. 创建记录与语义校验
# --------------------------------------------------------------------------------------


async def test_document_create_only_stamps_history_for_conversation() -> None:
    conversation = define_doc(
        {
            "kind": "pi.cache",
            "version": 2,
            "scope": "conversation",
            "history": "rewindable",
            "fork": "asOf",
        }
    )
    resolved = resolve_address(conversation.definition, [7])
    record = document_create(conversation.definition, resolved.address, 42)
    assert record["id"] == 42
    assert record["history"] == "rewindable"
    assert record["fork"] == "asOf"

    session = define_doc(notes_definition())
    session_record = document_create(
        session.definition, resolve_address(session.definition, []).address, 43
    )
    assert "history" not in session_record
    assert "fork" not in session_record
    assert "key" not in session_record


async def test_document_create_keeps_family_key() -> None:
    family = define_doc_family(
        {
            "kind": "pi.fam",
            "version": 1,
            "scope": "session",
            "family": True,
            "initial": lambda s=None: {},
        }
    )
    address = resolve_address(family.definition, ["member"]).address
    assert document_create(family.definition, address, 5)["key"] == "member"


async def test_check_record_scope_rejects_mismatched_semantics() -> None:
    definition = notes_definition()
    good = {"id": 1, "kind": "pi.notes", "scope": {"kind": "session"}}
    check_record_scope(definition, good)

    with pytest.raises(TypeError, match="does not match the supplied definition semantics"):
        check_record_scope(
            definition, {"id": 1, "kind": "pi.notes", "scope": {"kind": "task", "taskId": 1}}
        )


async def test_check_record_scope_compares_history_and_fork() -> None:
    rewindable = {
        "kind": "pi.cache",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",
    }
    record = {
        "id": 1,
        "kind": "pi.cache",
        "scope": {"kind": "conversation", "conversationId": 7},
        "history": "rewindable",
        "fork": "asOf",
    }
    check_record_scope(rewindable, record)
    with pytest.raises(TypeError, match="does not match"):
        check_record_scope(rewindable, {**record, "fork": "current"})


async def test_check_record_version() -> None:
    definition = notes_definition()
    record = {"id": 1, "kind": "pi.notes", "scope": {"kind": "session"}}

    check_record_version(definition, record, 1)
    with pytest.raises(RuntimeError, match="has newer version 2 than 1"):
        check_record_version(definition, record, 2)
    with pytest.raises(RuntimeError, match="requires migration from version 0"):
        check_record_version(definition, record, 0)

    # 给了 migrate 就允许低版本
    migratable = notes_definition(
        version=2, migrate=lambda value, from_version: {**value, "migrated": from_version}
    )
    check_record_version(migratable, record, 1)


# --------------------------------------------------------------------------------------
# 4. 物化与迁移
# --------------------------------------------------------------------------------------


async def test_materialize_returns_same_value_at_current_version() -> None:
    value = {"items": [], "n": 0}
    out = materialize_document_value(
        notes_definition(), {"id": 1, "kind": "pi.notes", "scope": {"kind": "session"}}, 1, value
    )
    assert out is value  # 同版本原样返回，不复制


async def test_materialize_migrates_older_versions() -> None:
    definition = {
        "kind": "pi.notes",
        "version": 3,
        "scope": "session",
        "migrate": lambda value, from_version: {
            **value,
            "n": value.get("n", 0) + 1,
            "from": from_version,
        },
    }
    record = {"id": 1, "kind": "pi.notes", "scope": {"kind": "session"}}
    assert materialize_document_value(definition, record, 3, {"n": 0}) == {"n": 0}
    assert materialize_document_value(definition, record, 2, {"n": 0}) == {"n": 1, "from": 2}


async def test_materialize_document_detaches_migrated_value() -> None:
    """迁移产生的值必须是**解除关联**的：改返回值的嵌套结构不能影响入参。"""
    source = {"nested": {"count": 1}}

    def migrate(value: dict[str, Any], from_version: int) -> dict[str, Any]:
        del from_version
        return value  # 故意返回同一个对象

    definition = {"kind": "pi.m", "version": 2, "scope": "session", "migrate": migrate}
    record = {"id": 1, "kind": "pi.m", "scope": {"kind": "session"}}
    out = materialize_document_value(definition, record, 1, source)
    out["nested"]["count"] = 99
    assert source["nested"]["count"] == 1


async def test_materialize_document_from_stored() -> None:
    stored = {
        "record": {"id": 1, "kind": "pi.notes", "scope": {"kind": "session"}},
        "version": 1,
        "value": {"items": ["a"], "n": 1},
        "deltasSinceBase": 0,
    }
    assert materialize_document(notes_definition(), stored) == {"items": ["a"], "n": 1}


# --------------------------------------------------------------------------------------
# 5. 条目种类
# --------------------------------------------------------------------------------------


async def test_define_entry_rejects_empty_kind() -> None:
    for bad in ("", None, 1):
        with pytest.raises(TypeError, match="non-empty string"):
            define_entry(bad)  # type: ignore[arg-type]


async def test_builtin_entry_kinds_match_upstream() -> None:
    assert [
        t.kind
        for t in (
            UserEntry,
            AssistantEntry,
            SystemEntry,
            ToolResultEntry,
            ResetEntry,
            CompactionEntry,
        )
    ] == [
        "pi.user",
        "pi.assistant",
        "pi.system",
        "pi.tool-result",
        "pi.reset",
        "pi.compaction",
    ]


async def test_entry_token_narrows_by_kind() -> None:
    user = {"id": 1, "conversationId": 1, "kind": "pi.user"}
    result = {"id": 2, "conversationId": 1, "kind": "pi.tool-result", "data": {"diagnostics": []}}

    assert UserEntry.is_(user)
    assert not UserEntry.is_(result)
    assert not UserEntry.is_(None)
    assert ToolResultEntry(result)
    assert as_typed(UserEntry, user) is user
    assert as_typed(UserEntry, result) is None


async def test_typed_entry_accessors() -> None:
    result = {
        "id": 1,
        "conversationId": 1,
        "kind": "pi.tool-result",
        "data": {"diagnostics": [{"severity": "error"}]},
    }
    assert tool_result_diagnostics(result) == [{"severity": "error"}]
    assert tool_result_diagnostics({"kind": "pi.user"}) == []
    assert tool_result_diagnostics(None) == []
    assert (
        compaction_reason({"kind": "pi.compaction", "data": {"reason": "threshold"}}) == "threshold"
    )
    assert compaction_reason({"kind": "pi.user"}) is None


# --------------------------------------------------------------------------------------
# 6. 真实 SQLite 端到端：定义 → 存储 → 读回 → 物化
# --------------------------------------------------------------------------------------


async def test_definition_layer_round_trips_through_real_sqlite(tmp_path: Path) -> None:
    """定义层的输出直接喂给真实 SQLite 后端，再读回来物化——接缝必须是通的。"""
    definition = {
        "kind": "pi.notes",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",
        "initial": lambda seed=None: {"items": [], "n": 0},
    }
    token = define_doc(definition)
    storage = await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    try:
        root = 1
        await storage.commit([{"type": "conversation", "value": {"id": root}}], CTX)

        resolved = resolve_address(token.definition, [root, CTX])
        assert resolved.next_argument == 1  # 第二位就是 Context

        document_id = await storage.mint_id()
        created_at = await storage.commit(
            [
                {
                    "type": "document.create",
                    "record": document_create(token.definition, resolved.address, document_id),
                    "content": {"kind": "base", "version": 1, "value": {"items": ["a"], "n": 1}},
                }
            ],
            CTX,
        )

        stored = await storage.document(document_id, "current", CTX)
        assert stored is not None
        # 定义层的语义校验 + 物化，跑在真实读回的记录上
        assert materialize_document(token.definition, stored) == {"items": ["a"], "n": 1}

        changed_at = await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": document_id,
                    "content": {
                        "kind": "delta",
                        "version": 1,
                        "ops": [("p", ("items",), 1, 0, ["b"]), ("s", ("n",), 2)],
                    },
                }
            ],
            CTX,
        )
        current = await storage.document(document_id, "current", CTX)
        assert current is not None
        assert materialize_document(token.definition, current) == {"items": ["a", "b"], "n": 2}

        # as-of 读回旧点，仍然物化出旧值
        historical = await storage.document(document_id, created_at, CTX)
        assert historical is not None
        assert materialize_document(token.definition, historical) == {"items": ["a"], "n": 1}
        assert changed_at > created_at

        # check_record_scope 能吃真实记录
        check_record_scope(token.definition, current["record"])
    finally:
        await storage.close(CTX)


async def test_definition_materializes_value_read_from_sqlite(tmp_path: Path) -> None:
    """版本迁移路径也要在真实存储上跑通：存 v1，按 v2 定义读。"""
    storage = await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    try:
        root = 1
        await storage.commit([{"type": "conversation", "value": {"id": root}}], CTX)
        document_id = await storage.mint_id()
        await storage.commit(
            [
                {
                    "type": "document.create",
                    "record": {
                        "id": document_id,
                        "kind": "pi.settings",
                        "scope": {"kind": "conversation", "conversationId": root},
                        "history": "rewindable",
                        "fork": "asOf",
                    },
                    "content": {"kind": "base", "version": 1, "value": {"theme": "dark"}},
                }
            ],
            CTX,
        )
        stored = await storage.document(document_id, "current", CTX)
        assert stored is not None

        upgraded = {
            "kind": "pi.settings",
            "version": 2,
            "scope": "conversation",
            "history": "rewindable",
            "fork": "asOf",
            "migrate": lambda value, from_version: {**value, "schema": from_version},
        }
        assert materialize_document(upgraded, stored) == {"theme": "dark", "schema": 1}
    finally:
        await storage.close(CTX)
