"""``pi_chord.delta`` 的 op 应用与校验 —— 对照上游 ``chord/src/delta/index.ts``。

向量来源（``recon/08-chord-delta-spec.md`` §5）:
- ``delta.test.ts``
- ``delta-apply-immutable.test.ts``
- ``delta-tracker/tracker.test.ts``（生命周期部分留给下一代 tracker）

上游断言可观察的**结构共享**（``delta-apply-immutable.test.ts:69-70`` 用 ``toBe``），
所以这里也用 ``is`` 断言身份，而不是只比值。
"""

from __future__ import annotations

from typing import Any

import pytest

from pi_chord.delta import (
    RESERVED_SEGMENTS,
    JsonValue,
    Op,
    PathError,
    UnsafePathError,
    apply,
    apply_immutable,
    apply_immutable_batches,
    assert_index_in_range,
    assert_permutation,
    assert_safe_path,
    assert_valid_op,
    is_base,
    is_replace,
    resolve,
    resolve_value,
)

# ─── 应用语义向量 ─────────────────────────────────────────────────────────────


def test_append_then_rolling_window() -> None:
    """上游 ``delta.test.ts:163-177``。"""
    base: dict[str, Any] = {"text": "abcdefgh"}
    assert apply(base, [("a", ("text",), "ij")]) == {"text": "abcdefghij"}
    assert apply(base, [("t", ("text",), 3), ("a", ("text",), "xyz")]) == {"text": "defghijxyz"}


def test_mutable_and_immutable_agree_and_share() -> None:
    """上游 ``delta.test.ts:188-198``。"""
    original = {"text": "a", "values": [1, 2], "nested": {"value": 1}, "stable": {"value": 9}}
    base = {"text": "a", "values": [1, 2], "nested": {"value": 1}, "stable": {"value": 9}}
    operations: list[Op] = [
        ("a", ("text",), "b"),
        ("p", ("values",), 1, 1, [3, 4]),
        ("s", ("nested", "value"), 2),
    ]
    immutable = apply_immutable(base, operations)
    assert immutable == {
        "text": "ab",
        "values": [1, 3, 4],
        "nested": {"value": 2},
        "stable": {"value": 9},
    }
    # 输入未被修改
    assert base == original
    # 未触及的子树**共享**（上游用 toBe 断言同一件事）
    assert immutable["stable"] is base["stable"]
    # 可变路径得到相同结果
    assert apply(_clone(base), operations) == immutable


def test_root_replacement_root_splice_and_permutation() -> None:
    """上游 ``delta.test.ts:201-208``。"""
    base: list[Op] = [("r", [1, 2, 3])]
    assert is_base(base)
    assert apply(None, base) == [1, 2, 3]
    value: Any = apply(None, base)
    value = apply(value, [("p", (), 1, 1, [4])])
    value = apply(value, [("m", (), [2, 0, 1])])
    assert value == [3, 1, 4]


def test_array_root_ops() -> None:
    """上游 ``delta-apply-immutable.test.ts:97-104``。"""
    array = [1, 2, 3]
    result = apply_immutable(array, [("p", (), 1, 1, [4, 5]), ("m", (), [3, 0, 1, 2]), ("d", (1,))])
    assert result == [3, 4, 5]
    assert array == [1, 2, 3]


def test_touched_containers_copied_once_payload_ownership() -> None:
    """上游 ``delta-apply-immutable.test.ts:24-82`` —— 全部 14 条身份断言。"""
    shared = {"nested": {"value": 1}}
    untouched = {"value": 9}
    row_payload = {"id": 4, "label": "placed"}
    base: dict[str, Any] = {
        "text": "abcdef",
        "stable": {"value": 7},
        "branch": {"value": 1},
        "copy": None,
        "placed": None,
        "untouched": None,
        "left": None,
        "right": None,
        "meta": {"count": 0, "obsolete": True},
        "rows": [
            {"id": 1, "label": "one"},
            {"id": 2, "label": "two"},
            {"id": 3, "label": "three"},
        ],
    }
    operations: list[Op] = [
        ("t", ("text",), 2),
        ("a", ("text",), "!"),
        ("s", ("meta", "count"), 1),
        ("s", ("meta", "count"), 2),
        ("d", ("meta", "obsolete")),
        ("s", ("copy",), base["branch"]),
        ("s", ("copy", "value"), 2),
        ("s", ("placed",), shared),
        ("s", ("placed", "nested", "value"), 2),
        ("s", ("untouched",), untouched),
        ("s", ("left",), shared),
        ("s", ("right",), shared),
        ("s", ("left", "nested", "value"), 3),
        ("p", ("rows",), 1, 1, [row_payload]),
        ("s", ("rows", 1, "label"), "edited"),
        ("m", ("rows",), [1, 0, 2]),
        ("s", ("rows", 0, "label"), "moved"),
    ]
    result = apply_immutable(base, operations)

    assert result["text"] == "cdef!"
    assert result["stable"] is base["stable"]
    assert result["untouched"] is untouched
    assert result["copy"] is not base["branch"]
    assert result["copy"] == {"value": 2}
    assert result["placed"] is not shared
    assert result["placed"] == {"nested": {"value": 2}}
    assert result["left"] is not shared
    assert result["right"] is shared
    assert result["rows"][0] is not row_payload
    assert result["rows"][0] == {"id": 4, "label": "moved"}
    # 输入与载荷都未被改动
    assert base["branch"]["value"] == 1
    assert shared["nested"]["value"] == 1
    assert row_payload["label"] == "placed"


def test_root_replacement_payload_protected_from_later_edits() -> None:
    """上游 ``delta-apply-immutable.test.ts:84-95``。"""
    replacement = {"nested": {"value": 1}, "values": [1, 2, 3]}
    result = apply_immutable_batches(
        None,
        [
            [("r", replacement)],
            [("s", ("nested", "value"), 2)],
            [("p", ("values",), 1, 1, [4, 5]), ("m", ("values",), [3, 0, 1, 2])],
        ],
    )
    assert result == {"nested": {"value": 2}, "values": [3, 1, 4, 5]}
    assert replacement == {"nested": {"value": 1}, "values": [1, 2, 3]}


def test_immutable_batches_equal_sequential_and_flattened() -> None:
    """上游 ``delta-apply-immutable.test.ts:107-140``。"""
    base: dict[str, Any] = {
        "text": "abcdef",
        "meta": {"count": 0},
        "values": [{"id": 1, "value": 1}, {"id": 2, "value": 2}, {"id": 3, "value": 3}],
    }
    batches: list[list[Op]] = [
        [("s", ("meta", "count"), 1), ("p", ("values",), 1, 1, [{"id": 4, "value": 4}])],
        [],
        [("m", ("values",), [2, 0, 1]), ("s", ("values", 2, "value"), 40)],
        [("t", ("text",), 2), ("a", ("text",), "!")],
    ]
    streamed = apply_immutable_batches(base, batches)
    sequential = base
    for batch in batches:
        sequential = apply_immutable(sequential, batch)
    flattened = apply_immutable(base, [op for batch in batches for op in batch])
    assert streamed == sequential
    assert streamed == flattened
    assert base == {
        "text": "abcdef",
        "meta": {"count": 0},
        "values": [{"id": 1, "value": 1}, {"id": 2, "value": 2}, {"id": 3, "value": 3}],
    }


def test_apply_mutates_target_in_place() -> None:
    """``apply`` 是可变 API（上游 ``index.ts:319-325``）。"""
    target: dict[str, Any] = {"text": "a"}
    result = apply(target, [("a", ("text",), "b")])
    assert result is target
    assert target == {"text": "ab"}


def test_replacement_payload_is_adopted_not_copied() -> None:
    """``r`` 的载荷被 adopt（上游 ``index.ts:336-342``）。"""
    payload: list[JsonValue] = [1, 2, 3]
    result = apply(None, [("r", payload)])
    assert result is payload


def test_delete_missing_object_key_is_a_noop() -> None:
    """JS 的 ``delete obj[k]`` 对不存在的键不抛错（``index.ts:389``）。"""
    assert apply({"a": 1}, [("d", ("missing",))]) == {"a": 1}


def test_set_at_array_length_appends() -> None:
    """``index == len`` 允许（``index.ts:289-306``）。"""
    assert apply({"xs": [1, 2]}, [("s", ("xs", 2), 3)]) == {"xs": [1, 2, 3]}
    assert apply({"xs": []}, [("s", ("xs", 0), 1)]) == {"xs": [1]}


def test_delete_on_array_shifts() -> None:
    assert apply({"xs": [1, 2, 3]}, [("d", ("xs", 1))]) == {"xs": [1, 3]}


def test_splice_forms() -> None:
    assert apply({"xs": [1, 2, 3]}, [("p", ("xs",), 1, 1, [9])]) == {"xs": [1, 9, 3]}
    assert apply({"xs": [1, 2, 3]}, [("p", ("xs",), 1, 0, [9])]) == {"xs": [1, 9, 2, 3]}
    assert apply({"xs": [1, 2, 3]}, [("p", ("xs",), 1, 2, [])]) == {"xs": [1]}
    # 索引越界时 JS splice 会钳到末尾：不删除，只在末尾插入
    assert apply({"xs": [1]}, [("p", ("xs",), 99, 5, [2])]) == {"xs": [1, 2]}


def test_permutation_semantics() -> None:
    """``new[i] = old[permutation[i]]``（``index.ts:360-365``）。"""
    assert apply({"xs": ["a", "b", "c"]}, [("m", ("xs",), [2, 0, 1])]) == {"xs": ["c", "a", "b"]}
    assert apply({"xs": [1, 2]}, [("m", ("xs",), [0, 1])]) == {"xs": [1, 2]}


# ─── 校验 / 错误向量 ─────────────────────────────────────────────────────────


def test_reserved_segment_is_rejected() -> None:
    """上游 ``delta.test.ts:211``。"""
    with pytest.raises(UnsafePathError):
        apply({}, [("s", ("constructor", "prototype", "x"), True)])


def test_index_beyond_end_is_rejected() -> None:
    """上游 ``delta.test.ts:212`` 与 ``index.ts:289-306`` 的 DoS 理由。"""
    with pytest.raises(UnsafePathError):
        apply({"values": [1]}, [("s", ("values", 3), 2)])
    # 4.29e9 的索引不能分配出数组
    with pytest.raises(UnsafePathError):
        apply({"values": [1]}, [("s", ("values", 4294967290), 2)])


def test_append_to_non_string_is_rejected() -> None:
    """上游 ``delta.test.ts:213``。"""
    with pytest.raises(PathError):
        apply({"value": 1}, [("a", ("value",), "x")])


def test_truncate_non_string_is_rejected() -> None:
    with pytest.raises(PathError):
        apply({"value": 1}, [("t", ("value",), 1)])


def test_string_path_is_not_a_path() -> None:
    """上游 ``delta.test.ts:214`` 与 ``index.ts:219-221`` 的注释。"""
    with pytest.raises(TypeError):
        assert_valid_op(("s", "value", 1))


def test_permutation_must_be_a_bijection() -> None:
    """上游 ``delta.test.ts:215``。"""
    with pytest.raises(TypeError, match="bijection"):
        assert_valid_op(("m", (), [0, 0]))
    with pytest.raises(TypeError, match="bijection"):
        assert_valid_op(("m", (), [0, 2]))


def test_negative_truncate_count_is_rejected() -> None:
    """上游 ``delta.test.ts:466-467``。"""
    with pytest.raises(TypeError):
        apply({"value": "abc"}, [("t", ("value",), -1)])


def test_unknown_verb_is_rejected() -> None:
    """上游 ``index.ts:187-189``：静默跳过未知动词，正是新生产者的 op 悄悄消失的方式。"""
    with pytest.raises(TypeError, match="unknown op verb"):
        assert_valid_op(("z", (), 1))


def test_validation_precedes_traversal() -> None:
    """上游 ``delta.test.ts:218-229``：校验必须先于遍历，一个 getter 都不能触发。"""
    reads = _CountingDict({"trap": {}})
    with pytest.raises(TypeError):
        apply_immutable(reads, [("s", "bad-path", 1)])  # type: ignore[list-item]
    assert reads.accesses == 0


def test_arity_is_enforced() -> None:
    for op in [
        ("r",),
        ("s", ("a",)),
        ("d",),
        ("a", ("a",)),
        ("t", ("a",)),
        ("p", (), 0, 0),
        ("m", ()),
    ]:
        with pytest.raises(TypeError):
            assert_valid_op(op)


def test_non_empty_path_required_for_scalar_verbs() -> None:
    """``s``/``d``/``a``/``t`` 不能指向根——类型禁止（``index.ts:25-26``）。"""
    for op in [("s", (), 1), ("d", ()), ("a", (), "x"), ("t", (), 1)]:
        with pytest.raises(TypeError, match="path is empty"):
            assert_valid_op(op)
    # p / m 允许空路径
    assert_valid_op(("p", (), 0, 0, []))
    assert_valid_op(("m", (), []))


def test_reserved_segments_constant() -> None:
    assert frozenset({"__proto__", "constructor", "prototype"}) == RESERVED_SEGMENTS


def test_safe_path_rejects_negative_and_non_integer() -> None:
    for bad in [(-1,), (1.5,), (True,), (None,)]:
        with pytest.raises(UnsafePathError):
            assert_safe_path(bad)


def test_bool_is_not_an_integer_index() -> None:
    """⚠️ Python 的 ``bool`` 是 ``int`` 子类，JS 的 ``Number.isInteger(true)`` 是 false。

    漏掉这一步会让 ``("s", ("xs", True), 1)`` 通过校验——上游会抛
    ``UnsafePathError``（``index.ts:283-284``）。
    """
    with pytest.raises(UnsafePathError):
        assert_valid_op(("s", ("xs", True), 1))
    with pytest.raises(UnsafePathError):
        assert_safe_path((True,))
    with pytest.raises(TypeError):
        assert_valid_op(("p", (), True, 0, []))
    with pytest.raises(TypeError):
        assert_valid_op(("t", ("v",), True))
    assert_index_in_range([1], 1)  # 合法
    with pytest.raises(UnsafePathError):
        assert_index_in_range([1], 2)


def test_assert_permutation_accepts_tuples_and_lists() -> None:
    assert_permutation([2, 0, 1])
    assert_permutation((2, 0, 1))
    assert_permutation([])


# ─── 路径解析 ────────────────────────────────────────────────────────────────


def test_resolve_walks_own_properties_only() -> None:
    root: dict[str, Any] = {"a": {"b": [1, 2]}}
    assert resolve_value(root, ("a", "b", 1)) == 2
    assert resolve(root, ("a", "b")) == [1, 2]
    with pytest.raises(PathError):
        resolve_value(root, ("missing",))
    with pytest.raises(PathError):
        resolve_value(root, ("a", "b", 5))
    with pytest.raises(PathError):
        # resolve 要求结果是容器
        resolve(root, ("a", "b", 0))


def test_array_path_requires_integer_segment() -> None:
    with pytest.raises(UnsafePathError):
        resolve_value({"xs": [1]}, ("xs", "0"))


def test_path_through_scalar_is_rejected() -> None:
    with pytest.raises(PathError):
        resolve_value({"a": 1}, ("a", "b"))


# ─── 分类谓词 ────────────────────────────────────────────────────────────────


def test_is_base_and_is_replace() -> None:
    assert is_base([("r", 1)])
    assert not is_base([("s", ("a",), 1)])
    assert not is_base([])
    assert is_replace(("r", 1))
    assert not is_replace(("s", ("a",), 1))


class _CountingDict(dict[str, Any]):
    """记录任何读取尝试的 dict，用于验证「校验先于遍历」。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.accesses = 0

    def __contains__(self, key: object) -> bool:
        self.accesses += 1
        return super().__contains__(key)

    def __getitem__(self, key: str) -> Any:
        self.accesses += 1
        return super().__getitem__(key)


def _clone(value: Any) -> Any:
    """仅用于测试的深拷贝（上游测试同样用 JSON 往返）。"""
    import copy

    return copy.deepcopy(value)
