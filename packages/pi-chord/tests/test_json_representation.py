"""``JsonRepresentation`` 的测试 —— 对照上游 ``chord/src/json.ts:5-16``。

上游是 TS 的**条件类型 + 同态映射类型**（一个类型层的递归变换），Python 无法表达。
本包给出的是**sound 上界** ``JsonRepresentation = JsonValue``。

因此这里断言的是三件事：

1. **可导入**（两个落点：``pi_chord.json`` 与根 ``pi_chord``）；
2. **与预期类型等价** —— ``is`` 同一性，即它确实就是 ``JsonValue``；
3. **忠实度缺口被钉住** —— 它**不可下标**（``JsonRepresentation[X]`` 会抛
   ``TypeError``），且它比上游**更宽**。这样将来若有人换了实现，测试会指出差异。
"""

from __future__ import annotations

from typing import Any

import pytest

from pi_chord.json import JsonRepresentation, JsonValue


def test_importable_from_its_home_module() -> None:
    """落点 1：``pi_chord.json``（上游 ``json.ts`` 的位置）。"""
    import pi_chord.json as module

    assert module.JsonRepresentation is JsonRepresentation
    assert "JsonRepresentation" in module.__all__


def test_importable_from_the_package_root() -> None:
    """落点 2：根入口。上游 ``index.ts:44`` 也把它从根再导出。"""
    import pi_chord

    assert pi_chord.JsonRepresentation is JsonRepresentation  # type: ignore[attr-defined]
    assert "JsonRepresentation" in pi_chord.__all__


def test_equivalent_to_json_value() -> None:
    """**忠实度核心**：Python 侧的等价物就是 ``JsonValue``（sound 上界）。

    上游的 ``JsonRepresentation<T>`` 恒为某种 ``JsonValue``：前两个分支直接是
    ``JsonValue``，其余分支都是 JSON 可表达的递归结构。所以把整个变换近似成
    ``JsonValue`` 永远不会把非法的东西判成合法 —— 只会更宽。
    """
    assert JsonRepresentation is JsonValue


@pytest.mark.parametrize(
    "value",
    [None, True, 1, 1.5, "text", [], {}, [1, "a", None], {"nested": {"deep": [1, 2]}}],
)
def test_accepts_every_strict_json_shape(value: Any) -> None:
    """用 ``JsonRepresentation`` 标注的变量必须能吃下任何严格 JSON 值。"""
    representation: JsonRepresentation = value
    assert representation == value


def test_is_not_subscriptable() -> None:
    """**忠实度缺口**：Python 侧不能写 ``JsonRepresentation[SomeType]``。

    ``JsonValue`` 是 ``Union``，不是泛型；``Union[...]`` 不支持下标。
    移植时把上游的 ``JsonRepresentation<Usage>`` 直接写成 ``JsonRepresentation``。

    这条断言是刻意的：若将来有人把它换成可下标的泛型别名，这里会失败，
    从而提醒同步更新 PORTING.md 的忠实度说明。
    """
    with pytest.raises(TypeError):
        JsonRepresentation[int]  # type: ignore[valid-type]


def test_approximation_is_wider_than_upstream() -> None:
    """钉住「更宽」这一方向：上游会收窄，Python 侧不会。

    上游 ``JsonRepresentation<number>`` 是 ``number``；侧把标量也一概当作
    ``JsonRepresentation``（即 ``JsonValue``）接受。所以下列赋值在 Python 侧**合法**，
    而在上游会产生类型错误 —— 这是近似的代价，必须有测试记录在案。
    """
    scalar: JsonRepresentation = 42  # 上游：JsonRepresentation<number> 也接受
    assert scalar == 42
    # 反过来，Python 侧接受一切 JsonValue，包括上游会 ``never`` 的形态之外的东西
    mixed: JsonRepresentation = {"a": [1, {"b": None}]}
    assert mixed == {"a": [1, {"b": None}]}


def test_works_as_a_typed_dict_value_annotation() -> None:
    """下游 ``usage.ts`` 的形态：``Record<string, JsonRepresentation<Usage>>``。

    Python 侧写成 ``dict[str, JsonRepresentation]``（即 ``dict[str, JsonValue]``）。
    """
    from typing import TypedDict

    class UsageState(TypedDict):
        models: dict[str, JsonRepresentation]
        tools: dict[str, JsonRepresentation]

    state: UsageState = {"models": {"anthropic/claude": {"input": 10}}, "tools": {}}
    assert state["models"]["anthropic/claude"] == {"input": 10}
    assert state["tools"] == {}


def test_runtime_work_is_done_by_copy_json() -> None:
    """类型是纯静态的；真正的「表示」转换由 ``copy_json`` 完成。

    上游 ``usage.ts:37`` 就是 ``copyJson(usage, {omitUndefinedProperties: true})
    as JsonRepresentation<Usage>`` —— 转换在运行时，类型只是标注。
    """
    from pi_chord.json import CopyJsonOptions, copy_json

    source = {"input": 10, "cost": {"total": 0.5}}
    representation: JsonRepresentation = copy_json(source, CopyJsonOptions())
    assert representation == source
    assert representation is not source  # 无别名拷贝
