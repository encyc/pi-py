"""``pi_chord.delta.tracker`` 测试 —— 对照上游 ``chord/src/delta/tracker.ts`` 与
``chord/src/delta/README.md``，并沿用上游 ``test/delta-tracker/tracker.test.ts`` 的语义。

**核心不变量**（``delta/README.md:94``「只有结果值有契约意义」）::

    apply_immutable(prepared.base, prepared.ops) == prepared.value

每一项产生 op 的测试都断言这条，op 的具体形态只在**它同时也是契约时**才断言
（字符串 ``a``/``t``、对象的 ``s``/``d``）。
"""

from __future__ import annotations

from typing import Any

import pytest

from pi_chord.delta import (
    MAX_DELTA_OPERATIONS,
    Prepared,
    TrackerError,
    apply_immutable,
    track,
)
from pi_chord.json import copy_json


def round_trips(prepared: Prepared[Any]) -> bool:
    """本实现保证的**唯一**强不变量。"""
    return apply_immutable(prepared.base, prepared.ops) == prepared.value


BASE: dict[str, Any] = {
    "text": "abcdefgh",
    "count": 0,
    "rows": [{"id": 1, "n": 1}, {"id": 2, "n": 2}, {"id": 3, "n": 3}],
    "nested": {"deep": {"value": 1}},
    "stable": {"untouched": True},
}


def fresh() -> Any:
    return track(copy_json(BASE))


# ─── 基本契约 ────────────────────────────────────────────────────────────────


def test_track_starts_at_revision_zero() -> None:
    tracker = fresh()
    assert tracker.revision == 0
    assert tracker.value == BASE


def test_adopt_swaps_the_root_and_advances_revision() -> None:
    tracker = fresh()
    change = tracker.begin_change()
    change.state["count"] = 5
    prepared = change.prepare()
    assert tracker.value["count"] == 0  # prepare 不改变权威
    tracker.adopt(prepared)
    assert tracker.value["count"] == 5
    assert tracker.revision == 1


def test_prepare_does_not_apply_the_shadow_value_but_shadow_is_readable() -> None:
    """上游 ``tracker.test.ts:224``：aborted / stale 的已物化候选仍可读。"""
    tracker = fresh()
    change = tracker.begin_change()
    change.state["count"] = 7
    prepared = change.prepare()
    prepared.abort()
    assert prepared.value["count"] == 7  # 候选可读
    assert tracker.value["count"] == 0  # 权威未动


def test_untouched_subtrees_are_shared_between_revisions() -> None:
    tracker = fresh()
    change = tracker.begin_change()
    change.state["count"] = 1
    prepared = change.prepare()
    assert prepared.value["stable"] is prepared.base["stable"]  # type: ignore[index]
    assert prepared.value["rows"] is prepared.base["rows"]  # type: ignore[index]


def test_caller_value_is_never_mutated() -> None:
    tracker = fresh()
    snapshot = copy_json(BASE)
    change = tracker.begin_change()
    change.state["count"] = 1
    change.state["rows"][0]["n"] = 99
    prepared = change.prepare()
    tracker.adopt(prepared)
    assert tracker.value != snapshot
    # 原始 base 对象（第一次 track 的那个）保持不变
    assert prepared.base == snapshot


# ─── op 形态：契约内的部分 ───────────────────────────────────────────────────


def test_string_append_emits_append_op() -> None:
    """上游 ``delta.test.ts:163-169``。"""
    tracker = fresh()
    change = tracker.begin_change()
    change.state["text"] += "ij"
    prepared = change.prepare()
    assert prepared.ops == [("a", ("text",), "ij")]
    assert round_trips(prepared)


def test_string_rolling_window_emits_truncate_then_append() -> None:
    """上游 ``delta.test.ts:171-178``。"""
    tracker = fresh()
    change = tracker.begin_change()
    change.state["text"] = change.state["text"][3:] + "xyz"
    prepared = change.prepare()
    assert prepared.ops == [("t", ("text",), 3), ("a", ("text",), "xyz")]
    assert round_trips(prepared)


def test_object_set_and_delete_emit_s_and_d() -> None:
    tracker = fresh()
    change = tracker.begin_change()
    change.state["count"] = 42
    del change.state["text"]
    change.state["added"] = {"new": True}
    prepared = change.prepare()
    assert ("s", ("count",), 42) in prepared.ops
    assert ("d", ("text",)) in prepared.ops
    assert ("s", ("added",), {"new": True}) in prepared.ops
    assert round_trips(prepared)


def test_nested_write_targets_the_deep_path() -> None:
    tracker = fresh()
    change = tracker.begin_change()
    change.state["nested"]["deep"]["value"] = 2
    prepared = change.prepare()
    assert prepared.ops == [("s", ("nested", "deep", "value"), 2)]
    assert round_trips(prepared)


def test_array_append_emits_splice() -> None:
    tracker = fresh()
    change = tracker.begin_change()
    change.state["rows"].append({"id": 4, "n": 4})
    prepared = change.prepare()
    assert prepared.ops == [("p", ("rows",), 3, 0, [{"id": 4, "n": 4}])]
    assert round_trips(prepared)


def test_array_element_edit_stays_element_wise() -> None:
    """等长数组的小改动逐元素发射，于是嵌套字符串仍能拿到 ``a``。"""
    tracker = fresh()
    change = tracker.begin_change()
    change.state["rows"][1]["n"] = 20
    prepared = change.prepare()
    assert prepared.ops == [("s", ("rows", 1, "n"), 20)]
    assert round_trips(prepared)


def test_transcript_style_append_then_edit() -> None:
    """durable 的主用例：会话 transcript 追加一条，再改它的正文。"""
    tracker = track({"messages": []})
    change = tracker.begin_change()
    change.state["messages"].append({"role": "user", "content": "hi"})
    prepared = change.prepare()
    assert prepared.ops == [("p", ("messages",), 0, 0, [{"role": "user", "content": "hi"}])]
    assert round_trips(prepared)
    tracker.adopt(prepared)

    change = tracker.begin_change()
    change.state["messages"][0]["content"] += " there"
    prepared = change.prepare()
    assert prepared.ops == [("a", ("messages", 0, "content"), " there")]
    assert round_trips(prepared)


def test_dict_root_can_be_cleared_and_rebuilt() -> None:
    tracker = track({"a": 1, "b": 2})
    change = tracker.begin_change()
    change.state.clear()
    assert len(change.state) == 0
    change.state["z"] = 9
    prepared = change.prepare()
    assert prepared.value == {"z": 9}
    assert round_trips(prepared)


def test_root_kind_change_is_only_reachable_through_prepare_replace() -> None:
    """overlay 的根容器类型恒定；换根类型只能走 ``prepare_replace``。"""
    tracker = track({"a": 1})
    prepared = tracker.prepare_replace([1, 2])
    assert prepared.ops == [("r", [1, 2])]
    assert round_trips(prepared)


# ─── no-op 归一化（上游不保证，本实现做得到，但不承诺） ──────────────────────


def test_write_restored_to_original_normalizes_to_empty() -> None:
    """把值写回原样 → 空 ops（``delta/README.md:140`` 的「usually」）。"""
    tracker = fresh()
    change = tracker.begin_change()
    change.state["count"] = 1
    change.state["count"] = 0
    prepared = change.prepare()
    assert prepared.ops == []
    assert prepared.value is prepared.base


def test_untouched_change_is_a_noop() -> None:
    tracker = fresh()
    prepared = tracker.begin_change().prepare()
    assert prepared.ops == []
    assert prepared.value is prepared.base


def test_deeply_equal_container_assignment_normalizes_to_empty() -> None:
    tracker = fresh()
    change = tracker.begin_change()
    change.state["stable"] = {"untouched": True}
    prepared = change.prepare()
    assert prepared.ops == []
    assert round_trips(prepared)


def test_equality_ignores_key_order() -> None:
    """``delta/README.md:141``：*Equality ignores key order*。"""
    tracker = track({"a": 1, "b": 2})
    change = tracker.begin_change()
    change.state.clear()
    change.state["b"] = 2
    change.state["a"] = 1
    prepared = change.prepare()
    assert prepared.ops == []
    assert round_trips(prepared)


def test_adopting_a_noop_still_advances_revision_and_stales_competitors() -> None:
    """``delta/README.md:143``。"""
    tracker = fresh()
    first = tracker.begin_change()
    second = tracker.begin_change()
    prepared = first.prepare()
    tracker.adopt(prepared)
    assert prepared.ops == []
    assert tracker.revision == 1  # 空 ops 也推进修订
    with pytest.raises(TypeError, match="settled"):
        second.prepare()


# ─── 生命周期 / 竞争 ─────────────────────────────────────────────────────────


def test_adopting_one_change_stales_its_competitors() -> None:
    """``delta/README.md:131-136``。"""
    tracker = fresh()
    first = tracker.begin_change()
    second = tracker.begin_change()
    first.state["count"] = 1
    second.state["count"] = 2
    tracker.adopt(first.prepare())
    with pytest.raises(TypeError, match="settled"):
        second.state["count"]
    with pytest.raises(TypeError, match="settled"):
        second.prepare()


def test_prepared_competitor_is_rejected_as_stale() -> None:
    """上游 ``tracker.test.ts:142``。"""
    tracker = fresh()
    first = tracker.begin_change()
    second = tracker.begin_change()
    first.state["count"] = 1
    second.state["count"] = 2
    second_prepared = second.prepare()
    tracker.adopt(first.prepare())
    with pytest.raises(TrackerError, match="stale"):
        tracker.adopt(second_prepared)


def test_prepared_cannot_be_adopted_twice() -> None:
    tracker = fresh()
    change = tracker.begin_change()
    change.state["count"] = 1
    prepared = change.prepare()
    tracker.adopt(prepared)
    with pytest.raises(TrackerError, match="already been used"):
        tracker.adopt(prepared)


def test_aborted_prepared_cannot_be_adopted() -> None:
    tracker = fresh()
    change = tracker.begin_change()
    change.state["count"] = 1
    prepared = change.prepare()
    prepared.abort()
    with pytest.raises(TrackerError, match="has been aborted"):
        tracker.adopt(prepared)


def test_change_abort_is_idempotent_and_revokes_the_draft() -> None:
    """上游 ``tracker.test.ts:118``。"""
    tracker = fresh()
    change = tracker.begin_change()
    change.abort()
    change.abort()  # 幂等
    with pytest.raises(TypeError, match="settled"):
        change.state["count"] = 1


def test_adopt_from_a_foreign_tracker_is_rejected() -> None:
    first = fresh()
    second = fresh()
    change = first.begin_change()
    change.state["count"] = 1
    prepared = change.prepare()
    with pytest.raises(TrackerError, match="different tracker"):
        second.adopt(prepared)


def test_prepared_revision_mismatch_is_rejected() -> None:
    tracker = fresh()
    change = tracker.begin_change()
    change.state["count"] = 1
    prepared = change.prepare()
    # 用另一个 change 推进修订，让 prepared 的 base_revision 过期
    other = tracker.begin_change()
    other.state["count"] = 2
    tracker.adopt(other.prepare())
    with pytest.raises(TrackerError, match="stale"):
        tracker.adopt(prepared)


def test_draft_is_unusable_after_prepare() -> None:
    tracker = fresh()
    change = tracker.begin_change()
    change.state["count"] = 1
    change.prepare()
    with pytest.raises(TypeError, match="settled"):
        change.state["count"] = 2


# ─── prepare_replace ─────────────────────────────────────────────────────────


def test_prepare_replace_with_equal_value_is_a_noop() -> None:
    """``delta/README.md:145-148``。"""
    tracker = fresh()
    prepared = tracker.prepare_replace(copy_json(BASE))
    assert prepared.ops == []
    assert prepared.value is prepared.base


def test_prepare_replace_with_new_value_emits_root_replacement() -> None:
    tracker = fresh()
    replacement = {"only": "this"}
    prepared = tracker.prepare_replace(replacement)
    assert prepared.ops == [("r", replacement)]
    assert prepared.value is replacement
    assert round_trips(prepared)
    tracker.adopt(prepared)
    assert tracker.value is replacement


def test_prepare_replace_can_change_the_root_kind() -> None:
    tracker = track({"a": 1})
    prepared = tracker.prepare_replace([1, 2, 3])
    assert prepared.ops == [("r", [1, 2, 3])]
    assert round_trips(prepared)


# ─── 折叠阈值 ────────────────────────────────────────────────────────────────


def test_exceeding_the_operation_budget_folds_to_a_root_replacement() -> None:
    """上游 ``tracker.ts:1707``：超预算整体折叠成 ``[("r", value)]``。"""
    # 预算约束的是 **op 条数**，不是载荷大小：往数组尾部追加无论多长都只是一个
    # splice。所以这里用大量互不相同的键来撑爆条数。
    tracker = track({"m": {}})
    change = tracker.begin_change()
    target = change.state["m"]
    for index in range(MAX_DELTA_OPERATIONS + 10):
        target[f"k{index}"] = index
    prepared = change.prepare()
    assert len(prepared.ops) == 1
    assert prepared.ops[0][0] == "r"
    assert round_trips(prepared)
    assert len(prepared.value["m"]) == MAX_DELTA_OPERATIONS + 10


def test_below_the_budget_keeps_incremental_ops() -> None:
    tracker = track({"items": []})
    change = tracker.begin_change()
    for index in range(10):
        change.state["items"].append({"i": index})
    prepared = change.prepare()
    assert len(prepared.ops) == 1
    assert prepared.ops[0][0] == "p"
    assert round_trips(prepared)


# ─── 放置（placement）语义 ───────────────────────────────────────────────────


def test_placed_containers_are_copied_so_later_source_mutation_is_invisible() -> None:
    """``delta/README.md:105-113``：放置会被拷贝。"""
    tracker = track({"holder": None})
    payload = {"v": 1}
    change = tracker.begin_change()
    change.state["holder"] = payload
    payload["v"] = 999
    prepared = change.prepare()
    assert prepared.value == {"holder": {"v": 1}}
    assert round_trips(prepared)


def test_invalid_placement_raises_before_the_draft_changes() -> None:
    """非严格 JSON 在 draft 变化**之前**抛错（``delta/README.md:108-110``）。"""
    tracker = track({"holder": None})
    change = tracker.begin_change()
    with pytest.raises(TypeError, match="non-JSON"):
        change.state["holder"] = {"bad": object()}
    assert change.state["holder"] is None  # draft 未被改动


def test_cyclic_placement_is_rejected() -> None:
    tracker = track({"holder": None})
    change = tracker.begin_change()
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    with pytest.raises(TypeError, match="cycles"):
        change.state["holder"] = cyclic


def test_placing_a_draft_handle_clones_its_content() -> None:
    """``delta/README.md:111-112``。"""
    tracker = track({"a": {"v": 1}, "b": None})
    change = tracker.begin_change()
    change.state["a"]["v"] = 2
    change.state["b"] = change.state["a"]
    prepared = change.prepare()
    assert prepared.value == {"a": {"v": 2}, "b": {"v": 2}}
    assert round_trips(prepared)


def test_placing_a_large_tuple_is_rejected() -> None:
    tracker = track({"holder": None})
    change = tracker.begin_change()
    with pytest.raises(TypeError):
        change.state["holder"] = (1, 2, 3)


# ─── draft 行为 ──────────────────────────────────────────────────────────────


def test_nested_reads_return_live_sub_drafts() -> None:
    tracker = fresh()
    change = tracker.begin_change()
    rows = change.state["rows"]
    rows.append({"id": 4, "n": 4})
    assert len(change.state["rows"]) == 4  # 通过父级也能看到
    prepared = change.prepare()
    assert len(prepared.value["rows"]) == 4
    assert round_trips(prepared)


def test_draft_supports_mapping_helpers() -> None:
    tracker = fresh()
    change = tracker.begin_change()
    assert "text" in change.state
    assert "missing" not in change.state
    assert set(change.state.keys()) == set(BASE.keys())
    assert change.state.get("missing", "dflt") == "dflt"
    assert change.state.get("text") == "abcdefgh"


def test_draft_supports_list_mutators() -> None:
    tracker = track({"xs": [1, 2, 3]})
    change = tracker.begin_change()
    xs = change.state["xs"]
    xs.append(4)
    xs.extend([5, 6])
    xs.insert(0, 0)
    assert list(xs) == [0, 1, 2, 3, 4, 5, 6]
    assert xs.pop() == 6
    xs.remove(0)
    assert list(xs) == [1, 2, 3, 4, 5]
    xs.reverse()
    assert list(xs) == [5, 4, 3, 2, 1]
    prepared = change.prepare()
    assert prepared.value == {"xs": [5, 4, 3, 2, 1]}
    assert round_trips(prepared)


def test_draft_sort() -> None:
    tracker = track({"xs": [3, 1, 2]})
    change = tracker.begin_change()
    change.state["xs"].sort()
    prepared = change.prepare()
    assert prepared.value == {"xs": [1, 2, 3]}
    assert round_trips(prepared)


def test_draft_clear() -> None:
    tracker = track({"xs": [1, 2]})
    change = tracker.begin_change()
    change.state["xs"].clear()
    prepared = change.prepare()
    assert prepared.value == {"xs": []}
    assert round_trips(prepared)


def test_delete_on_array_shifts() -> None:
    tracker = track({"xs": [1, 2, 3]})
    change = tracker.begin_change()
    del change.state["xs"][1]
    prepared = change.prepare()
    assert prepared.value == {"xs": [1, 3]}
    assert round_trips(prepared)


def test_array_index_out_of_range() -> None:
    tracker = track({"xs": [1]})
    change = tracker.begin_change()
    with pytest.raises(IndexError):
        change.state["xs"][5]
    with pytest.raises(IndexError):
        change.state["xs"][5] = 1


def test_detached_handle_raises_instead_of_losing_writes() -> None:
    """父列表结构性变更后，旧子句柄大声失败（``PORTING.md`` T6）。

    上游让持有的句柄跟随重排；本实现选择「烘焙 + detached + 抛错」，
    因为静默丢写入比抛错危险得多。
    """
    tracker = track({"rows": [{"n": 1}, {"n": 2}]})
    change = tracker.begin_change()
    held = change.state["rows"][0]
    held["n"] = 100
    change.state["rows"].append({"n": 3})  # 结构性变更 → 烘焙 + detach
    with pytest.raises(TypeError, match="detached"):
        held["n"] = 5
    prepared = change.prepare()
    # 烘焙发生在 detach 之前，100 保住了
    assert prepared.value["rows"][0] == {"n": 100}
    assert round_trips(prepared)


def test_draft_repr_is_safe_after_settle() -> None:
    """settle 之后 ``repr`` 不能去碰已经不可用的 overlay。"""
    tracker = fresh()
    change = tracker.begin_change()
    draft = change.state  # 先拿到句柄（prepare 之后 change.state 本身就会抛）
    draft["count"] = 1
    prepared = change.prepare()
    assert "settled" in repr(draft)
    tracker.adopt(prepared)
    assert "settled" in repr(draft)


def test_root_must_be_a_container() -> None:
    with pytest.raises(TypeError, match="plain object or array"):
        track("scalar")  # type: ignore[arg-type]


# ─── 差分正确性（fuzz 护栏） ─────────────────────────────────────────────────


@pytest.mark.parametrize("seed", range(12))
def test_random_mutations_always_round_trip(seed: int) -> None:
    """随机变更后 ``apply_immutable(base, ops) == value`` 必须成立。

    这是本模块最高性价比的护栏：op 形态允许不同，但**值必须一致**。
    """
    import random

    rng = random.Random(seed)
    tracker: Any = track({"xs": [], "n": 0, "s": "start", "o": {"k": 1}})
    for _ in range(40):
        change = tracker.begin_change()
        root = change.state
        op = rng.randrange(6)
        if op == 0:
            root["n"] = rng.randrange(100)
        elif op == 1:
            root["s"] = root["s"] + rng.choice(["a", "bb", "\U0001f600", ""])
        elif op == 2:
            root["xs"].append({"i": len(root["xs"])})
        elif op == 3 and len(root["xs"]) > 0:
            del root["xs"][rng.randrange(len(root["xs"]))]
        elif op == 4:
            root["o"]["k"] = rng.randrange(100)
        else:
            root["xs"].reverse()
        prepared = change.prepare()
        assert round_trips(prepared), (seed, op, prepared.ops)
        tracker.adopt(prepared)
    assert tracker.revision > 0
