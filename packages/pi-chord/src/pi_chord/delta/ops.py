"""chord delta 的 op 词表、类型与静态校验。

对应上游 ``chord/src/delta/index.ts``:
- ``Op`` / ``Path`` / ``Seg`` / ``NonEmptyPath`` — 第 14-38 行
- ``isReplace`` / ``isBase``                  — 第 70-76 行
- ``assertValidOp`` / ``assertPathArg`` /
  ``assertPermutation``                      — 第 152-208 行
- ``assertSafePath``                          — 第 279-287 行
- ``assertIndexInRange``                      — 第 289-306 行
- ``resolveValue`` / ``resolve``              — 第 523-540 行

上游把 op 定义为元组（``index.ts:19-20``：「Tuples are the form — in memory, on the
wire, on disk」），Python 侧保持同样的表示：``tuple`` 而不是 ``list``，因为它是
**不可变**的、可哈希的、可直接比较的。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, TypeAlias, TypeGuard

from pi_chord.json import JsonValue

from .errors import RESERVED_SEGMENTS, PathError, Seg, UnsafePathError

__all__ = [
    "AppendOp",
    "DeleteOp",
    "JsonValue",
    "NonEmptyPath",
    "Op",
    "Path",
    "PermuteOp",
    "ReplaceOp",
    "Seg",
    "SetOp",
    "SpliceOp",
    "TruncateOp",
    "assert_index_in_range",
    "assert_permutation",
    "assert_safe_path",
    "assert_valid_op",
    "has_own",
    "is_base",
    "is_replace",
    "resolve",
    "resolve_value",
]

# `JsonValue` 的真身在 `pi_chord.json`（上游 `chord/src/json.ts:1`）。这里从上面
# 导入并保留在本模块的命名空间与 `__all__` 里，让既有的
# `from pi_chord.delta import JsonValue` / `from pi_chord.delta.ops import JsonValue`
# 继续可用。

Path: TypeAlias = tuple[Seg, ...]
NonEmptyPath: TypeAlias = tuple[Seg, ...]
"""上游用 ``readonly [Seg, ...Seg[]]`` 在类型上强制非空；Python 无法表达，
改用运行时校验（``assert_valid_op`` 对 ``s``/``d``/``a``/``t`` 强制非空，见 ``index.ts:160,164,168,172``）。
"""

ReplaceOp: TypeAlias = tuple[Literal["r"], JsonValue]
SetOp: TypeAlias = tuple[Literal["s"], Path, JsonValue]
DeleteOp: TypeAlias = tuple[Literal["d"], Path]
AppendOp: TypeAlias = tuple[Literal["a"], Path, str]
TruncateOp: TypeAlias = tuple[Literal["t"], Path, int]
SpliceOp: TypeAlias = tuple[Literal["p"], Path, int, int, list[JsonValue]]
PermuteOp: TypeAlias = tuple[Literal["m"], Path, list[int]]

Op: TypeAlias = ReplaceOp | SetOp | DeleteOp | AppendOp | TruncateOp | SpliceOp | PermuteOp
"""7 个动词，元数固定（``index.ts:24-38``）。

``r`` 是**唯一**替换整个值的 op；``s``/``d``/``a``/``t`` 不能指向根（类型禁止）；
``p``/``m`` 可以，因为被追踪的值本身可能是数组。
"""


def is_int(value: object) -> TypeGuard[int]:
    """JS ``Number.isInteger`` 的对应物。

    ⚠️ 不能直接用 ``isinstance(value, int)``：Python 的 ``bool`` 是 ``int`` 的子类，
    而 JS 里 ``Number.isInteger(true)`` 是 ``false``。漏掉这一步会让
    ``("s", (True,), 1)`` 这类非法 op 通过校验（上游会抛 ``UnsafePathError``，
    见 ``index.ts:283-284``）。
    """
    return isinstance(value, int) and not isinstance(value, bool)


def is_replace(op: Op) -> bool:
    """上游 ``index.ts:70``。"""
    return op[0] == "r"


def is_base(ops: Sequence[Op]) -> bool:
    """批次是否以替换开头（上游 ``index.ts:76``）。

    上游注释（``index.ts:72-75``）：flush 保证 ``r`` 要么在索引 0，要么不存在，
    所以这是**精确**判定而不是启发式。
    """
    return len(ops) > 0 and ops[0][0] == "r"


def assert_safe_path(path: Sequence[object]) -> None:
    """上游 ``index.ts:279-287``。"""
    for segment in path:
        if isinstance(segment, str):
            if segment in RESERVED_SEGMENTS:
                raise UnsafePathError(segment)
        elif not (is_int(segment) and segment >= 0):
            raise UnsafePathError(segment)


def assert_index_in_range(parent: Sequence[object], index: int) -> None:
    """数组索引必须指向已有元素，或**恰好越界一个位置**（上游 ``index.ts:289-306``）。

    上游注释（``index.ts:290-302``）给了两条理由，都必须保留：

    1. **这保证值仍是 ``JsonValue``**——稀疏数组过不了 JSON 往返：洞会序列化成
       ``null`` 并作为真属性回来，于是 ``arr[7] = x``（长度 3 的数组）产生的状态
       副本无法匹配。
    2. **消除 DoS**——否则 ``("s", ("xs", 4294967290), 1)`` 一个 op 就能分配
       42.9 亿个元素的数组。
    """
    if index > len(parent):
        raise UnsafePathError(index)


def assert_permutation(value: object) -> None:
    """``m`` 的置换必须是双射（上游 ``index.ts:199-208``）。"""
    if not isinstance(value, (list, tuple)):
        raise TypeError("m permutation is not an array")
    seen = [False] * len(value)
    for index in value:
        if not is_int(index) or index < 0 or index >= len(value) or seen[index]:
            raise TypeError("m permutation is not a bijection")
        seen[index] = True


def _assert_path_arg(path: object, non_empty: bool = False) -> None:
    """上游 ``assertPathArg``（``index.ts:193-197``）。"""
    if not isinstance(path, (list, tuple)):
        raise TypeError("path is not an array")
    if non_empty and len(path) == 0:
        raise TypeError("path is empty")
    assert_safe_path(path)


def assert_valid_op(op: object) -> None:
    """校验 **decoded** 形态的 op：路径内联、无 ``#``、无短形态（上游 ``index.ts:152-191``）。

    上游注释（``index.ts:143-151``）：用 wire 语法校验 ``Op`` 会比类型更宽松——
    两元素的 ``["s", value]`` 会通过，然后 ``apply`` 会把那个值当成路径读。两套
    词表各自用各自的校验器。

    wire 词表（``assertValidWireOp``, ``index.ts:211-277``）本包**不移植**：单进程
    不需要 path interning，且 ``pi_durable`` 对 ``encoder``/``decoder`` 零引用。
    """
    if not isinstance(op, (list, tuple)) or len(op) == 0:
        raise TypeError("op is not a tuple")
    verb = op[0]
    if verb == "r":
        if len(op) != 2:
            raise TypeError("r arity")
    elif verb == "s":
        if len(op) != 3:
            raise TypeError("s arity")
        _assert_path_arg(op[1], True)
    elif verb == "d":
        if len(op) != 2:
            raise TypeError("d arity")
        _assert_path_arg(op[1], True)
    elif verb == "a":
        if len(op) != 3 or not isinstance(op[2], str):
            raise TypeError("a shape")
        _assert_path_arg(op[1], True)
    elif verb == "t":
        if len(op) != 3 or not is_int(op[2]) or op[2] < 0:
            raise TypeError("t shape")
        _assert_path_arg(op[1], True)
    elif verb == "p":
        if len(op) != 5:
            raise TypeError("p arity")
        _assert_path_arg(op[1])
        if not is_int(op[2]) or op[2] < 0:
            raise TypeError("p index")
        if not is_int(op[3]) or op[3] < 0:
            raise TypeError("p remove")
        if not isinstance(op[4], (list, tuple)):
            raise TypeError("p items")
    elif verb == "m":
        if len(op) != 3:
            raise TypeError("m arity")
        _assert_path_arg(op[1])
        assert_permutation(op[2])
    else:
        # 上游 (index.ts:187-189): 静默跳过未知动词，正是新生产者的 op 悄悄消失的方式。
        raise TypeError(f"unknown op verb: {verb!s}")


def _has_own(container: object, segment: Seg) -> bool:
    """JS ``Object.hasOwn`` 的对应物——只走自有属性，不逃逸到原型链。"""
    if isinstance(container, list):
        return is_int(segment) and 0 <= segment < len(container)
    if isinstance(container, dict):
        return segment in container
    return False


def resolve_value(root: JsonValue, path: Path) -> JsonValue:
    """沿 ``path`` 取值（上游 ``resolveValue``, ``index.ts:523-534``）。"""
    node: JsonValue = root
    for segment in path:
        if not isinstance(node, (list, dict)):
            raise PathError(path)
        if isinstance(node, list) and not is_int(segment):
            # 上游 index.ts:527 —— 数组下标必须是数字
            raise UnsafePathError(segment)
        if not _has_own(node, segment):
            raise PathError(path)
        node = node[segment]  # type: ignore[index]
    return node


def resolve(root: JsonValue, path: Path) -> list[JsonValue] | dict[str, JsonValue]:
    """``resolveValue`` + 结果必须是容器（上游 ``resolve``, ``index.ts:536-540``）。"""
    node = resolve_value(root, path)
    if not isinstance(node, (list, dict)):
        raise PathError(path)
    return node


def has_own(container: object, segment: Seg) -> bool:
    """``_has_own`` 的公开别名，供 ``apply`` 复用。"""
    return _has_own(container, segment)
