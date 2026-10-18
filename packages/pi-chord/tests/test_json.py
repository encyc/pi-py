"""``pi_chord.json`` 测试 —— 对照上游 ``chord/src/json.ts``。

上游没有独立的 ``json.test.ts`` 深度覆盖（只有 65 行的 ``test/json.test.ts``，
主要走 tracker 的 placement 校验），所以这里按 ``json.ts:30-135`` 的语义逐条构造。
"""

from __future__ import annotations

import math

import pytest

from pi_chord.json import CopyJsonOptions, copy_json, is_json_value, json_equal


def test_scalars_pass_through() -> None:
    for value in [None, True, False, 0, 1, -1, 3.5, "", "abc"]:
        assert copy_json(value) == value
        assert is_json_value(value)


def test_containers_are_deep_copied() -> None:
    source = {"a": [1, {"b": 2}]}
    copied = copy_json(source)
    assert copied == source
    assert copied is not source
    assert copied["a"] is not source["a"]  # type: ignore[index]
    assert copied["a"][1] is not source["a"][1]  # type: ignore[index]


def test_copy_is_independent_of_source() -> None:
    source = {"xs": [1, 2]}
    copied = copy_json(source)
    source["xs"].append(3)
    assert copied == {"xs": [1, 2]}


def test_non_finite_numbers_are_rejected() -> None:
    for bad in [math.nan, math.inf, -math.inf]:
        with pytest.raises(TypeError, match="non-finite"):
            copy_json({"x": bad})
        assert not is_json_value({"x": bad})


def test_non_json_scalars_are_rejected() -> None:
    for bad in [b"bytes", {1, 2}, (1, 2), object()]:
        with pytest.raises(TypeError, match="non-JSON"):
            copy_json({"x": bad})
        assert not is_json_value({"x": bad})


def test_cycles_are_rejected() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(TypeError, match="cycles"):
        copy_json(cyclic)
    assert not is_json_value(cyclic)


def test_shared_subtree_is_not_a_cycle() -> None:
    """同一个容器出现在两处是合法的（别名），不是循环。"""
    shared = {"v": 1}
    assert copy_json({"a": shared, "b": shared}) == {"a": {"v": 1}, "b": {"v": 1}}
    assert is_json_value({"a": shared, "b": shared})


def test_list_subclass_is_rejected() -> None:
    """上游要求 ``getPrototypeOf(value) === Array.prototype``。"""

    class MyList(list):  # type: ignore[type-arg]
        pass

    with pytest.raises(TypeError, match="plain objects or arrays"):
        copy_json(MyList([1]))
    assert not is_json_value(MyList([1]))


def test_dict_subclass_is_rejected() -> None:
    class MyDict(dict):  # type: ignore[type-arg]
        pass

    with pytest.raises(TypeError, match="plain objects or arrays"):
        copy_json(MyDict(a=1))
    assert not is_json_value(MyDict(a=1))


def test_non_string_keys_are_rejected() -> None:
    with pytest.raises(TypeError, match="non-string key"):
        copy_json({1: "a"})
    assert not is_json_value({1: "a"})


def test_nested_depth_is_fine() -> None:
    value = {"a": [{"b": [{"c": [1, 2, 3]}]}]}
    assert copy_json(value) == value
    assert is_json_value(value)


def test_copy_json_options_are_accepted_but_inert() -> None:
    """``omit_undefined_properties`` 在 Python 里没有对应语义。

    上游的 ``{a: undefined}`` 会在 JSON 序列化时丢掉 ``a``；Python 的 ``None`` 是
    JSON 的 ``null``，是**必须保留**的合法值。所以该开关只用于保持调用点可平移。
    """
    options = CopyJsonOptions(omit_undefined_properties=True)
    assert copy_json({"a": None, "b": 1}, options) == {"a": None, "b": 1}
    assert options.omit_undefined_properties is True


def test_json_equal_ignores_key_order() -> None:
    """上游 ``delta/README.md:141``：*Equality ignores key order and prototype*。"""
    assert json_equal({"a": 1, "b": 2}, {"b": 2, "a": 1})
    assert not json_equal({"a": 1}, {"a": 2})
    assert json_equal([1, 2], [1, 2])
    assert not json_equal([1, 2], [2, 1])  # 数组按序


def test_is_json_value_accepts_the_strict_json_union() -> None:
    assert is_json_value({"a": [None, True, 1, 1.5, "s", {}, []]})
    assert not is_json_value(object())
