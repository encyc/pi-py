"""``harness/inbox.py`` 的测试 —— 移植 ``harness/inbox.ts``（168 行）。

边界（boundary）按 **spec §6** 选择排队项：每个 write、第一个或全部 steer，
以及 **final** 边界上的第一个或全部 follow-up。一个被选中的 reset 把 ``postTools`` 升级成 final。

这些测试**接真实 Session**（`SessionImpl` + `Transaction`），因为边界的主要语义就是
「表读取必须早于第一次表写入」与「按位置移除」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.harness.inbox import (
    Boundary,
    InboxDoc,
    append_input,
    apply_boundary,
    has_queued_reset,
    is_display_only,
    is_stale,
    pick_input_entry,
    prepare_boundary,
    remove_inbox_item,
    withdraw_queued_inputs,
)
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage

ALL_MODES = {"steeringMode": "all", "followUpMode": "all"}
ONE_AT_A_TIME = {"steeringMode": "one-at-a-time", "followUpMode": "one-at-a-time"}


async def make_session(backend: str = "memory", tmp_path: Path | None = None) -> SessionImpl:
    storage = (
        MemoryStorage()
        if backend == "memory"
        else await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    )
    session = SessionImpl(storage)
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    return session


async def queue_submission(session: SessionImpl, **item: Any) -> int:
    """建一个真提交，并把对应的收件箱项排进去（``item["id"]`` 由它填）。"""

    async def change(tx: Any) -> None:
        submission = await tx.create_submission(
            {"conversationId": 1, "requestId": f"r{len(item)}", "type": "input", "status": "queued"}
        )
        item["id"] = submission["id"]
        draft = await tx.doc(InboxDoc, 1)
        draft["items"].append(item)

    await session.commit(change, CTX)
    return item["id"]


async def entries(session: SessionImpl) -> list[dict[str, Any]]:
    page = await session._storage.scan_entries(  # noqa: SLF001
        {"conversationId": 1, "order": "ascending"}, 50, None, CTX
    )
    return page["items"]


# --------------------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------------------


def test_is_display_only_counts_present_fields_even_when_empty() -> None:
    """``edits: []`` **算存在**——它仍然是一次（空的）编辑，不是展示专用。"""
    assert is_display_only({"data": {}})
    assert is_display_only({})
    assert not is_display_only({"model": []})
    assert not is_display_only({"head": "self"})
    assert not is_display_only({"edits": []})


def test_has_queued_reset() -> None:
    assert has_queued_reset([{"mode": "write", "entry": {"head": "self"}}])
    assert not has_queued_reset([{"mode": "write", "entry": {"head": 5}}])
    assert not has_queued_reset([{"mode": "steer", "entry": {"head": "self"}}])


def test_pick_input_entry_drops_an_absent_data() -> None:
    """按 **R23** 在构造处不写 ``data`` 键，而不是指望 no-op 的开关删它。"""
    assert pick_input_entry({"kind": "app.note"}) == {"kind": "app.note"}
    assert "data" not in pick_input_entry({"kind": "app.note"})
    assert pick_input_entry({"kind": "app.note", "data": {"a": 1}}) == {
        "kind": "app.note",
        "data": {"a": 1},
    }


def test_pick_input_entry_strips_other_fields() -> None:
    """只留 kind 与 data——未类型化的调用方可能多传字段。"""
    assert pick_input_entry({"kind": "k", "data": 1, "extra": "nope", "model": []}) == {
        "kind": "k",
        "data": 1,
    }


def test_is_stale_compares_against_the_active_range() -> None:
    boundary = Boundary(1, {"items": []}, "all", "all", head=10)
    assert is_stale(boundary, {"head": 5})
    assert not is_stale(boundary, {"head": 10})
    assert not is_stale(boundary, {"head": 11})
    assert not is_stale(boundary, {"head": "self"})  # 不是数字

    no_head = Boundary(1, {"items": []}, "all", "all", head=None)
    assert not is_stale(no_head, {"head": 5})  # 还没有活动范围


def test_inbox_doc_semantics() -> None:
    """队列空时才写 base；分叉从空队列开始。"""
    definition = InboxDoc.definition
    assert definition["kind"] == "pi.inbox"
    assert definition["fork"] == "initial"
    assert definition["initial"](None) == {"items": []}
    predicate = definition["checkpointWhen"]
    assert predicate({"items": []}, [], {"deltasSinceBase": 0}) is True
    assert predicate({"items": [{"id": 1}]}, [], {"deltasSinceBase": 0}) is False


# --------------------------------------------------------------------------------------
# 边界选择
# --------------------------------------------------------------------------------------


async def test_prepare_boundary_reads_the_head_marker() -> None:
    session = await make_session()
    await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.reset", "head": "self"}), CTX)

    async def change(tx: Any) -> None:
        boundary = await prepare_boundary(tx, 1, ALL_MODES)  # type: ignore[arg-type]
        assert boundary.head == 2  # 那个 head 标记
        assert boundary.steering_mode == "all"
        assert boundary.inbox["items"] == []

    await session.commit(change, CTX)


async def test_final_boundary_selects_both_modes_and_places_in_inbox_order() -> None:
    """⚠️ **选择**按 mode（steer + final 时的 follow-up），但**放置按收件箱位置**。

    上游把挑出来的下标 ``sort`` 过（``inbox.ts:96-97``），所以先排队的那一项先落地——
    **不是**「steer 一律排在 follow-up 前面」。这条我先按直觉写错了测试。
    """
    session = await make_session()
    follow_id = await queue_submission(session, mode="followUp", content=[{"text": "f"}])
    steer_id = await queue_submission(session, mode="steer", content=[{"text": "s"}])

    placed: list[int] = []

    async def change(tx: Any) -> None:
        boundary = await prepare_boundary(tx, 1, ALL_MODES)  # type: ignore[arg-type]
        result = await apply_boundary(tx, boundary, "final", 1.0)
        placed.extend(result["users"])
        assert result["reset"] is False

    await session.commit(change, CTX)
    # 两种 mode 都被选中，但按**收件箱位置**放置：follow-up 先排，所以它先落地。
    assert placed == [follow_id, steer_id]
    assert sorted(placed) == sorted([follow_id, steer_id])
    assert (await session.snapshot(InboxDoc, 1, CTX))["items"] == []


async def test_post_tools_boundary_leaves_follow_ups_queued() -> None:
    session = await make_session()
    steer_id = await queue_submission(session, mode="steer", content=[{"text": "s"}])
    await queue_submission(session, mode="followUp", content=[{"text": "f"}])

    async def change(tx: Any) -> None:
        boundary = await prepare_boundary(tx, 1, ALL_MODES)  # type: ignore[arg-type]
        result = await apply_boundary(tx, boundary, "postTools", 1.0)
        assert result["users"] == [steer_id]

    await session.commit(change, CTX)
    remaining = (await session.snapshot(InboxDoc, 1, CTX))["items"]
    assert [item["mode"] for item in remaining] == ["followUp"]


async def test_queue_mode_one_at_a_time_takes_only_the_first() -> None:
    session = await make_session()
    first = await queue_submission(session, mode="steer", content=[{"text": "1"}])
    await queue_submission(session, mode="steer", content=[{"text": "2"}])

    async def change(tx: Any) -> None:
        boundary = await prepare_boundary(tx, 1, ONE_AT_A_TIME)  # type: ignore[arg-type]
        result = await apply_boundary(tx, boundary, "final", 1.0)
        assert result["users"] == [first]

    await session.commit(change, CTX)
    assert len((await session.snapshot(InboxDoc, 1, CTX))["items"]) == 1


async def test_writes_are_placed_before_user_items() -> None:
    """**顺序保证**：write 先放，用户项后放——这样在 reset 之前排队的用户项在新上下文里跑。"""
    session = await make_session()
    await queue_submission(session, mode="steer", content=[{"text": "s"}])
    await queue_submission(session, mode="write", entry={"kind": "app.note", "data": {"n": 1}})

    order: list[str] = []

    async def change(tx: Any) -> None:
        boundary = await prepare_boundary(tx, 1, ALL_MODES)  # type: ignore[arg-type]
        await apply_boundary(tx, boundary, "final", 1.0)

    await session.commit(change, CTX)
    kinds = [entry["kind"] for entry in await entries(session)]
    order.extend(kinds)
    assert order == ["app.note", "pi.user"]


async def test_a_queued_reset_upgrades_post_tools_to_final() -> None:
    """``head: "self"`` 的 write 排着队时，``postTools`` 边界也放 follow-up。"""
    session = await make_session()
    await queue_submission(session, mode="followUp", content=[{"text": "f"}])
    await queue_submission(session, mode="write", entry={"kind": "pi.reset", "head": "self"})

    async def change(tx: Any) -> None:
        boundary = await prepare_boundary(tx, 1, ALL_MODES)  # type: ignore[arg-type]
        result = await apply_boundary(tx, boundary, "postTools", 1.0)
        assert result["reset"] is True
        assert len(result["users"]) == 1  # follow-up 被放了

    await session.commit(change, CTX)
    assert (await session.snapshot(InboxDoc, 1, CTX))["items"] == []


async def test_a_stale_write_is_unanswered_and_not_placed() -> None:
    """head 指向活动范围**之前**的 write：结算成 ``unanswered`` / ``stale``，**不**放置。"""
    session = await make_session()
    # 先建一个 head 标记，把活动范围推到 2
    await session.commit(lambda tx: tx.append_entry(1, {"kind": "pi.reset", "head": "self"}), CTX)
    stale_id = await queue_submission(session, mode="write", entry={"kind": "app.note", "head": 1})

    async def change(tx: Any) -> None:
        boundary = await prepare_boundary(tx, 1, ALL_MODES)  # type: ignore[arg-type]
        await apply_boundary(tx, boundary, "final", 1.0)

    await session.commit(change, CTX)

    settled = await session._storage.submission(stale_id, CTX)  # noqa: SLF001
    assert settled is not None
    assert settled["status"] == "unanswered"
    assert settled["reason"] == "stale"
    # 没有追加那个条目
    assert [entry["kind"] for entry in await entries(session)] == ["pi.reset"]


async def test_placement_settles_the_submission() -> None:
    session = await make_session()
    steer_id = await queue_submission(session, mode="steer", content=[{"text": "s"}])

    async def change(tx: Any) -> None:
        boundary = await prepare_boundary(tx, 1, ALL_MODES)  # type: ignore[arg-type]
        await apply_boundary(tx, boundary, "final", 1.0)

    await session.commit(change, CTX)
    placed = await session._storage.submission(steer_id, CTX)  # noqa: SLF001
    assert placed is not None
    assert placed["status"] == "placed"
    assert placed["entry"] is not None


async def test_items_are_removed_positionally_from_the_end() -> None:
    """同时选中的 write 与用户项混在一起时，移除必须从尾部做，否则下标会错位。"""
    session = await make_session()
    await queue_submission(session, mode="steer", content=[{"text": "1"}])
    await queue_submission(session, mode="write", entry={"kind": "app.a"})
    await queue_submission(session, mode="steer", content=[{"text": "2"}])
    await queue_submission(session, mode="write", entry={"kind": "app.b"})

    async def change(tx: Any) -> None:
        boundary = await prepare_boundary(tx, 1, ALL_MODES)  # type: ignore[arg-type]
        result = await apply_boundary(tx, boundary, "final", 1.0)
        assert len(result["users"]) == 2

    await session.commit(change, CTX)
    assert (await session.snapshot(InboxDoc, 1, CTX))["items"] == []
    # write 先放、且按原顺序
    assert [entry["kind"] for entry in await entries(session)] == [
        "app.a",
        "app.b",
        "pi.user",
        "pi.user",
    ]


async def test_append_input_uses_the_entry_kind_and_data() -> None:
    session = await make_session()

    async def change(tx: Any) -> None:
        entry = await append_input(
            tx, 1, [{"text": "x"}], {"kind": "app.note", "data": {"n": 1}}, 5.0
        )
        assert entry["kind"] == "app.note"
        assert entry["data"] == {"n": 1}
        assert entry["model"][0]["role"] == "user"
        assert entry["model"][0]["timestamp"] == 5.0

    await session.commit(change, CTX)


async def test_append_input_defaults_to_pi_user() -> None:
    session = await make_session()

    async def change(tx: Any) -> None:
        entry = await append_input(tx, 1, [{"text": "x"}], None, 0.0)
        assert entry["kind"] == "pi.user"

    await session.commit(change, CTX)


# --------------------------------------------------------------------------------------
# 撤回
# --------------------------------------------------------------------------------------


async def test_remove_inbox_item() -> None:
    session = await make_session()
    id = await queue_submission(session, mode="steer", content=[{"text": "s"}])
    await queue_submission(session, mode="steer", content=[{"text": "keep"}])

    async def change(tx: Any) -> None:
        await remove_inbox_item(tx, 1, id)

    await session.commit(change, CTX)
    remaining = (await session.snapshot(InboxDoc, 1, CTX))["items"]
    assert len(remaining) == 1
    assert remaining[0]["content"] == [{"text": "keep"}]


async def test_withdraw_queued_inputs_leaves_writes() -> None:
    """撤回每个排队**输入**；排队中的**写入**留着等以后放置。"""
    session = await make_session()
    steer_id = await queue_submission(session, mode="steer", content=[{"text": "s"}])
    follow_id = await queue_submission(session, mode="followUp", content=[{"text": "f"}])
    await queue_submission(session, mode="write", entry={"kind": "app.note"})

    async def change(tx: Any) -> None:
        await withdraw_queued_inputs(tx, 1)

    await session.commit(change, CTX)

    remaining = (await session.snapshot(InboxDoc, 1, CTX))["items"]
    assert [item["mode"] for item in remaining] == ["write"]

    for id in (steer_id, follow_id):
        settled = await session._storage.submission(id, CTX)  # noqa: SLF001
        assert settled is not None
        assert settled["status"] == "unanswered"
        assert settled["reason"] == "aborted"


# --------------------------------------------------------------------------------------
# 真实 SQLite
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_boundary_round_trip(backend: str, tmp_path: Path) -> None:
    session = await make_session(backend, tmp_path)
    try:
        steer_id = await queue_submission(session, mode="steer", content=[{"text": "s"}])
        write_id = await queue_submission(
            session, mode="write", entry={"kind": "app.note", "data": {"n": 1}}
        )

        async def change(tx: Any) -> None:
            boundary = await prepare_boundary(tx, 1, ALL_MODES)  # type: ignore[arg-type]
            result = await apply_boundary(tx, boundary, "final", 1.0)
            assert result["users"] == [steer_id]

        await session.commit(change, CTX)

        assert [entry["kind"] for entry in await entries(session)] == ["app.note", "pi.user"]
        assert (await session.snapshot(InboxDoc, 1, CTX))["items"] == []
        for id in (steer_id, write_id):
            placed = await session._storage.submission(id, CTX)  # noqa: SLF001
            assert placed is not None and placed["status"] == "placed"
    finally:
        await session.close(CTX)
