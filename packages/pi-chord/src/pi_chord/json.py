"""严格 JSON 值的类型、拷贝与判定。

对应上游 ``chord/src/json.ts``（135 行）:
- ``JsonValue``           — 第 1 行
- ``JsonRepresentation``  — 第 5-16 行（**近似**，见下）
- ``CopyJsonOptions``     — 第 25-28 行
- ``copyJson``            — 第 30-80 行
- ``isJsonValue``         — 第 88-135 行

``copyJson`` 把任意输入拷成一棵**无别名**的严格 JSON 树（调用方拥有），并在拷贝
过程中做校验：拒绝循环、非有限数、非普通对象/数组、访问器、非字符串键。
tracker 的 draft 放置（placement）走的正是这条校验。
"""

from __future__ import annotations

import math
from typing import Any, TypeAlias, TypeGuard

__all__ = [
    "CopyJsonOptions",
    "JsonRepresentation",
    "JsonValue",
    "copy_json",
    "is_json_value",
]

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
"""严格 JSON 值（上游 ``json.ts:1``）。

注意 Python 与 JS 的一处根本差别：上游的 ``JsonValue`` 里 **没有 ``undefined``**
（JS 的 ``undefined`` 不在联合类型里），而 Python 的 ``None`` 对应的是 JSON ``null``。
所以 Python 侧不存在「undefined 属性」这个概念，见 :data:`CopyJsonOptions`。
"""

JsonRepresentation: TypeAlias = JsonValue
"""应用数据类型的**严格 JSON 表示**（上游 ``json.ts:5-16``）—— **近似**。

上游是一个 TS 的**条件类型 + 同态映射类型**，在类型层做一次递归变换::

    type JsonRepresentation<T> =
        IsAny<T> extends true            ? JsonValue          // any
      : unknown extends T                ? JsonValue          // unknown
      : T extends null|boolean|number|string ? T              // 基本类型原样
      : T extends readonly (infer I)[]   ? JsonRepresentation<I>[]      // 数组逐元素
      : T extends object                 ? { [K in keyof T]: JsonRepresentation<T[K]> }  // 对象逐键
      : never;                                               // 函数/symbol 等

**Python 的类型系统没有条件类型，也没有映射类型**（PEP 695 的 ``type X[T] = ...``
需要 3.12+，本包 ``requires-python >= 3.11``；且 ``TypeAliasType`` 要引入
``typing_extensions``，本包零依赖）。因此这里给出的是：

- **上界（sound）**：``JsonRepresentation<T>`` 在上游**恒为**某种 ``JsonValue``
  （前两个分支直接就是 ``JsonValue``，其余分支都是 JSON 可表达的递归结构）。
  所以 ``JsonRepresentation = JsonValue`` **永远不会把非法的东西判成合法**。
- **不精确之处**：上游对具体 ``T`` 会**收窄**（``JsonRepresentation<number>`` 是
  ``number``；``JsonRepresentation<Item>`` 保留 ``Item`` 的键），而 Python 侧一律
  是 ``JsonValue`` —— 即我的类型**更宽**，会接受上游会拒绝的赋值。
- **不可下标**：``JsonRepresentation[SomeType]`` 在 Python 里是 ``TypeError``
  （``JsonValue`` 是 ``Union``，不是泛型）。移植时把 ``JsonRepresentation<X>``
  直接写成 ``JsonRepresentation``。

需要**更精确**时，下游应直接用目标类型自己的 JSON 形态（例如 ``TypedDict`` 镜像），
而不是指望这层变换 —— 见 ``PORTING.md`` 的 J7。
"""


class CopyJsonOptions:
    """上游 ``CopyJsonOptions``（``json.ts:25-28``）。

    ``omit_undefined_properties``
        JavaScript 里 ``{a: undefined}`` 的 ``a`` 会被 JSON 序列化丢弃，
        上游据此提供该开关。**Python 没有 ``undefined``**：``None`` 是 JSON 的
        ``null``，是一个合法且必须保留的值，不存在「值为 undefined 的属性」。
        因此本实现接受该参数仅为保持调用点可平移，**它对结果没有任何影响**。

        ⚠️ 上游有 **16 处**调用点传了这个开关（``durable`` 的 ``harness/tool.ts``、
        ``codemode.ts``、``session/transaction.ts`` 等），移植时的正确做法是
        **在构造处直接不写入该键**，而不是指望这个开关删掉 ``None`` —— 那会把
        合法的 JSON ``null`` 一起删掉。见 ``PORTING.md`` 的 J8。
    """

    __slots__ = ("omit_undefined_properties",)

    def __init__(self, omit_undefined_properties: bool = False) -> None:
        self.omit_undefined_properties = omit_undefined_properties


def _is_plain_dict(value: object) -> TypeGuard[dict[str, Any]]:
    """上游要求 ``Object.getPrototypeOf(value)`` 是 ``Object.prototype`` 或 ``null``。

    Python 的对应物是「类型恰好是 ``dict``」——子类（``OrderedDict``、``defaultdict``
    等）都算非普通对象，与 JS 里「非 Object.prototype 原型」一致。
    """
    return type(value) is dict


def _is_plain_list(value: object) -> TypeGuard[list[Any]]:
    """上游要求 ``Object.getPrototypeOf(value) === Array.prototype``。"""
    return type(value) is list


def copy_json(value: Any, options: CopyJsonOptions | None = None) -> JsonValue:
    """把 ``value`` 拷成一棵无别名的严格 JSON 树（上游 ``copyJson``, ``json.ts:30-33``）。

    ``options`` 仅为调用点平移而保留，见 :class:`CopyJsonOptions`。
    """
    return _copy(value, None)


def _copy(value: Any, ancestors: set[int] | None) -> JsonValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            # 上游 json.ts:39 —— 非有限数不是严格 JSON
            raise TypeError("Value contains a non-finite number and is not strict JSON")
        return value
    if not isinstance(value, (dict, list)):
        # 上游 json.ts:41-42 —— 函数、bigint、自定义对象等都不是严格 JSON
        raise TypeError(f"Value contains a non-JSON {type(value).__name__}; expected strict JSON")
    active = ancestors if ancestors is not None else set()
    marker = id(value)
    if marker in active:
        raise TypeError("Value contains cycles and is not strict JSON")
    active.add(marker)
    try:
        if _is_plain_list(value):
            result: list[JsonValue] = []
            for item in value:
                result.append(_copy(item, active))
            return result
        if not _is_plain_dict(value):
            # 上游 json.ts:62-65 —— 只有普通对象与 null 原型对象
            raise TypeError("Value must contain strict JSON plain objects or arrays")
        result_dict: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                # 上游 json.ts:67-68 —— symbol 键被拒绝；Python 里非 str 键同理
                raise TypeError("Value contains a non-string key and is not strict JSON")
            result_dict[key] = _copy(item, active)
        return result_dict
    finally:
        active.discard(marker)


def is_json_value(value: Any) -> bool:
    """``value`` 是否是无循环、有限、普通容器的严格 JSON（上游 ``isJsonValue``）。"""
    return _check(value, set())


def _check(value: Any, ancestors: set[int]) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, (int, float)):
        return not isinstance(value, float) or math.isfinite(value)
    if not isinstance(value, (dict, list)):
        return False
    marker = id(value)
    if marker in ancestors:
        return False
    ancestors.add(marker)
    try:
        if _is_plain_list(value):
            return all(_check(item, ancestors) for item in value)
        if not _is_plain_dict(value):
            return False
        for key, item in value.items():
            if not isinstance(key, str) or not _check(item, ancestors):
                return False
        return True
    finally:
        ancestors.discard(marker)


def json_equal(left: JsonValue, right: JsonValue) -> bool:
    """严格 JSON 的深相等。

    上游用 ``equalTrustedJson``（``tracker.ts:2158``）做同样的事，并明确
    「Equality ignores key order and prototype」（``delta/README.md:141``）。
    Python 的 ``==`` 对 ``dict`` 本来就忽略键序、对 ``list`` 按序比较，语义一致；
    这里把它具名，方便 tracker 表达「相等即无 op」。
    """
    return left == right
