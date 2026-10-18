"""op 的应用：可变、不可变、以及跨批次单次 copy-on-write 回放。

对应上游 ``chord/src/delta/index.ts``:
- ``apply`` / ``applyOps``                  — 第 326-406 行
- ``applyImmutable``                        — 第 408-411 行
- ``applyImmutableBatches``                 — 第 413-441 行
- ``appendCopy``                            — 第 443-473 行
- ``copyContainers``                        — 第 475-521 行

三种 API 的语义差别（上游 ``index.ts:319-325`` / ``:413-418``）:

``apply``
    就地修改 ``target``；返回根（因为 ``r`` 会换根）。``r`` 的载荷被 **adopt**，
    不拷贝。上游注释（``index.ts:336-342``）：把同一个 batch 扇出给多个进程内
    消费者会让它们的副本**互相别名**——这是所有权规则，不是缺陷。
``apply_immutable``
    ``apply_immutable_batches(target, [ops])``。
``apply_immutable_batches``
    **一次 copy-on-write 作用域跨所有批次**，**不暴露任何中间修订**。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TypeVar, cast

from .errors import RESERVED_SEGMENTS, PathError, UnsafePathError
from .ops import (
    JsonValue,
    Op,
    Path,
    Seg,
    SetOp,
    SpliceOp,
    assert_index_in_range,
    assert_safe_path,
    assert_valid_op,
    has_own,
    is_int,
    resolve,
)
from .textdiff import u16_slice

__all__ = [
    "apply",
    "apply_immutable",
    "apply_immutable_batches",
]

T = TypeVar("T")

_Container = list[JsonValue] | dict[str, JsonValue]
_Owned = dict[int, object]
"""``id(容器) -> 容器``。

上游用 ``WeakSet``（``index.ts:421``）。Python 侧额外保留**强引用**：仅存 ``id``
的话，一个不再被树引用的容器可能被回收，其 ``id`` 被复用，从而让另一个对象被
误判成「已拷贝」。
"""


def _read(container: _Container, key: Seg) -> JsonValue:
    """JS ``Object.hasOwn(parent, key) ? parent[key] : undefined``（``index.ts:380``）。"""
    if isinstance(container, list):
        return container[key] if is_int(key) and 0 <= key < len(container) else None
    return container.get(key) if isinstance(key, str) else None


def _write(container: _Container, key: Seg, value: JsonValue) -> None:
    """JS ``Object.defineProperty(parent, key, {value, writable, enumerable, configurable})``。

    上游（``index.ts:375-379``）用 ``defineProperty`` 而不是赋值，是为了避免原型链
    上的 setter 被执行；Python 没有这个风险，直接赋值即可。数组的
    ``index == len`` 视为追加（与 JS 给数组下标赋值一致）。
    """
    if isinstance(container, list):
        index = cast(int, key)
        if index == len(container):
            container.append(value)
        else:
            container[index] = value
    else:
        container[cast(str, key)] = value


def _path_of(op: Op) -> Path:
    """取 op 的路径（``r`` 之外都有）。

    这里对每个动词单独 ``cast``：mypy 无法按 ``op[0]`` 的值把 ``Op`` 这个元组联合
    收窄，而逐个显式 cast 同时也把「哪个动词带哪些字段」写清楚了。
    """
    if op[0] == "r":
        raise TypeError("r has no path")
    return cast(SetOp, op)[1]


def _apply_ops(root: JsonValue, ops: Sequence[Op]) -> JsonValue:
    """上游 ``applyOps``（``index.ts:330-406``）。"""
    for op in ops:
        assert_valid_op(op)
        if op[0] == "r":
            # 上游 index.ts:336-342 —— adopt，不拷贝。
            root = op[1]
            continue
        path = _path_of(op)
        assert_safe_path(path)
        if op[0] == "p":
            _, _, index, remove, items = op
            target = root if len(path) == 0 else resolve(root, path)
            if not isinstance(target, list):
                raise PathError(path)
            # 上游 index.ts:354-357 按 10_000 分块插入，那是 JS 展开运算符的栈限制
            # workaround，无语义内容；Python 直接切片赋值即可。
            target[index : index + remove] = list(items)
            continue
        if op[0] == "m":
            permutation = op[2]
            target = root if len(path) == 0 else resolve(root, path)
            if not isinstance(target, list) or len(target) != len(permutation):
                raise PathError(path)
            previous = list(target)
            for index, source in enumerate(permutation):
                target[index] = previous[source]
            continue
        # s/d/a/t 永远不能指向根 —— 类型禁止（index.ts:368）。
        parent = resolve(root, path[:-1])
        key = path[-1]
        if isinstance(parent, list):
            if not is_int(key):
                raise UnsafePathError(key)
            assert_index_in_range(parent, key)
        if op[0] == "s":
            _write(parent, key, op[2])
        elif op[0] == "d":
            if isinstance(parent, list):
                if not is_int(key) or key >= len(parent):
                    raise PathError(path)
                del parent[key]
            else:
                # JS 的 `delete obj[k]` 对不存在的键是 no-op，不抛错。
                parent.pop(cast(str, key), None)
        elif op[0] == "a":
            text = op[2]
            current = _read(parent, key)
            if not isinstance(current, str):
                raise PathError(path)
            _write(parent, key, current + text)
        else:  # "t"
            count = op[2]
            current = _read(parent, key)
            if not isinstance(current, str):
                raise PathError(path)
            _write(parent, key, u16_slice(current, count))
    return root


def apply(target: T, ops: Sequence[Op]) -> T:
    """就地应用 op 批次（上游 ``apply``, ``index.ts:326-328``）。

    ⚠️ 会修改 ``target``；``r`` 的载荷会被 adopt（不拷贝）。
    """
    root = cast(JsonValue, cast(object, target))
    return cast(T, _apply_ops(root, ops))


def _copy_once(value: JsonValue, path: Path, owned: _Owned) -> _Container:
    """拷贝一个容器并登记为 owned（上游 ``index.ts:476-492`` 的 ``copy``）。"""
    existing = owned.get(id(value))
    if existing is not None:
        return cast(_Container, existing)
    if isinstance(value, list):
        copied: _Container = list(value)
    elif isinstance(value, dict):
        copied = dict(value)
    else:
        raise PathError(path)
    owned[id(copied)] = copied
    return copied


def _copy_containers(root: JsonValue, path: Path, owned: _Owned) -> JsonValue:
    """路径拷贝：沿 ``path`` 复制容器，未触及的兄弟子树直接共享（``index.ts:475-521``）。"""
    if not isinstance(root, (list, dict)):
        raise PathError(path)
    copied_root = _copy_once(root, path, owned)
    destination: _Container = copied_root
    for segment in path:
        if not has_own(destination, segment):
            raise PathError(path)
        if isinstance(destination, list) and not is_int(segment):
            raise UnsafePathError(segment)
        child = (
            destination[cast(int, segment)]
            if isinstance(destination, list)
            else destination[cast(str, segment)]
        )
        if not isinstance(child, (list, dict)):
            raise PathError(path)
        if id(child) in owned:
            destination = cast(_Container, owned[id(child)])
            continue
        copied_child = _copy_once(child, path, owned)
        _write(destination, segment, cast(JsonValue, copied_child))
        destination = copied_child
    return cast(JsonValue, copied_root)


def _append_copy(root: JsonValue, op: Op, owned: _Owned) -> JsonValue:
    """对「尚未拷贝过、且 ``index == len``、``delete_count == 0``」的数组追加做一次
    ``concat`` 构造，避免「先拷贝再增长」的二次拷贝（上游 ``index.ts:443-473``）。

    返回 ``None`` 表示不适用，交给通用路径。
    """
    _, path, index, delete_count, items = cast(SpliceOp, op)
    if delete_count != 0:
        return None
    target: JsonValue = root
    for segment in path:
        if isinstance(segment, str):
            valid = segment not in RESERVED_SEGMENTS and isinstance(target, dict)
        else:
            valid = is_int(segment) and segment >= 0 and isinstance(target, list)
        if not valid or not has_own(target, segment):
            return None
        if isinstance(target, list):
            target = target[cast(int, segment)]
        elif isinstance(target, dict):
            target = target[cast(str, segment)]
        else:  # pragma: no cover - `valid` 已经排除了非容器
            return None
    if not isinstance(target, list) or id(target) in owned or index != len(target):
        return None
    appended = list(target) + list(items)
    owned[id(appended)] = appended
    if len(path) == 0:
        return cast(JsonValue, appended)
    parent_path = path[:-1]
    copied_root = _copy_containers(root, parent_path, owned)
    _write(resolve(copied_root, parent_path), path[-1], cast(JsonValue, appended))
    return copied_root


def apply_immutable_batches(target: T, batches: Iterable[Sequence[Op]]) -> T:
    """把多个批次当作**一次只保留最终结果**的回放来应用（上游 ``index.ts:413-441``）。

    为早期批次拷贝的容器可能在应用后续批次时被就地修改，因此这里**刻意不暴露任何
    中间修订**。
    """
    root = cast(JsonValue, cast(object, target))
    owned: _Owned = {}
    for ops in batches:
        for op in ops:
            assert_valid_op(op)
            if op[0] == "r":
                root = op[1]
                continue
            if op[0] == "p":
                appended = _append_copy(root, op, owned)
                if appended is not None:
                    root = appended
                    continue
            parent_path = op[1] if op[0] in ("p", "m") else op[1][:-1]
            root = _copy_containers(root, parent_path, owned)
            root = _apply_ops(root, [op])
    return cast(T, root)


def apply_immutable(target: T, ops: Sequence[Op]) -> T:
    """应用单个批次而不修改先前的不可变值（上游 ``applyImmutable``, ``index.ts:408-411``）。"""
    return apply_immutable_batches(target, [ops])
