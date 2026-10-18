"""事务性 JSON 追踪器：不可变修订、overlay draft、精确 op 批次。

对应上游 ``chord/src/delta/tracker.ts``（2,239 行）与 ``chord/src/delta/README.md``。

公开契约只有 4 个类型 + 5 个方法（上游 ``tracker.ts:121-141``）::

    Tracker:  value, revision, begin_change(), prepare_replace(value), adopt(prepared)
    Change:   state(Draft), prepare(), abort()
    Prepared: base, value, ops, base_revision, abort()

生命周期（``delta/README.md:124-136``）
--------------------------------------

- ``begin_change()`` 在当前修订上开一个 overlay draft，**绝不修改 base**。
- ``prepare()`` 物化候选与 ops，但**不改变权威**（authority）。
- ``adopt()`` 校验并**指针交换**。
- 同一修订上可有多个 open/prepared change；**adopt 其中一个会让其余全部 stale**。

契约边界（``delta/README.md:94``）
----------------------------------

**「只有结果值有契约意义」**（*Don't infer meaning from op shape. Only the resulting
value is contractual.*）。本实现保证::

    apply_immutable(tracker.value, prepared.ops) == prepared.value

但**不保证 op 形态与上游逐条相同**。差异见 ``PORTING.md`` 的 T 系列，最大的一处是
**数组**：上游用 piece/permutation 机制产出最小 ``p``/``m``，本实现用「公共前缀 +
公共后缀」的 splice —— **正确但不最小**。

实现说明
--------

上游用 ``Proxy`` 包一个空对象，把全部读写转发到 overlay 节点（``tracker.ts:382-463``）。
Python 没有 Proxy，这里用一个真实的句柄类 :class:`Draft`，内部维护
「base 容器 + 首次写入时的浅拷贝 + 子句柄表」。对外行为（嵌套读写、放置校验、
settle 后不可用）与上游对齐。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Generic, TypeVar, cast

from pi_chord.json import JsonValue, copy_json

from .apply import apply_immutable
from .ops import Op, Path, assert_valid_op, is_int
from .textdiff import attach_path, emit_string_ops

__all__ = [
    "MAX_DELTA_OPERATIONS",
    "MAX_SIMPLE_OBJECT_NODES",
    "Change",
    "Draft",
    "Prepared",
    "Tracker",
    "TrackerError",
    "track",
]

MAX_DELTA_OPERATIONS = 4_096
"""超出预算就整体折叠成 ``[("r", value)]``（上游 ``tracker.ts:112``，用点 ``:1666,:1707``）。"""

MAX_SIMPLE_OBJECT_NODES = 128
"""上游 ``tracker.ts:113`` 的「简单对象」快速路径阈值。

本实现**不使用**它 —— 那是纯性能优化，不影响结果值（``PORTING.md`` T4）。
"""

T = TypeVar("T")

_STATUS_OPEN = "open"
_STATUS_PREPARED = "prepared"
_STATUS_CONSUMED = "consumed"
_STATUS_ABORTED = "aborted"
_STATUS_STALE = "stale"
_SETTLED = (_STATUS_CONSUMED, _STATUS_ABORTED, _STATUS_STALE)


class TrackerError(Exception):
    """tracker 生命周期错误。

    上游用裸 ``Error``（``tracker.ts:256-264``）；这里给一个具名类型方便调用方区分
    「用法错误」与「值错误」。**消息与上游逐字一致**，便于对拍。
    """


def _is_container(value: object) -> bool:
    return isinstance(value, (dict, list))


class _Context:
    """一次 change 的状态（上游 ``OverlayContext``）。"""

    __slots__ = (
        "base_revision",
        "base_value",
        "overlay_released",
        "owner",
        "replacement",
        "replacement_value",
        "root",
        "status",
    )

    def __init__(
        self,
        owner: object,
        base_revision: int,
        base_value: JsonValue,
        *,
        replacement: bool = False,
        replacement_value: JsonValue = None,
    ) -> None:
        self.owner = owner
        self.base_revision = base_revision
        self.base_value: JsonValue = base_value
        self.status = _STATUS_OPEN
        self.replacement = replacement
        self.replacement_value: JsonValue = replacement_value
        # prepare 之后 overlay 即被释放（上游 materializePrepared 里的
        # releaseOverlayReferences / clearContext, tracker.ts:338-339）：
        # `delta/README.md:33` —— prepare/abort/adopt 之后任何 draft 使用都抛错。
        self.overlay_released = False
        self.root: Draft | None = None

    def is_settled(self) -> bool:
        return self.status in _SETTLED


def _assert_readable(context: _Context) -> None:
    if context.is_settled() or context.overlay_released:
        raise TypeError("Cannot use a settled overlay")


class Draft:
    """一个 draft 容器句柄（上游用 ``Proxy``，``tracker.ts:382-430``）。

    读到的容器返回**子句柄**，所以 ``draft["a"]["b"] = 1`` 这类嵌套写入自然落到同一棵
    overlay 上。

    ⚠️ 与上游的一处行为差异（``PORTING.md`` T6）：父列表发生**结构性变更**
    （插入/删除/重排）时，已下发的子句柄会被烘焙进父级并标记 ``detached``；此后对它的
    写入**抛 ``TypeError``**，而不是静默丢失。上游让持有的句柄跟随重排
    （``delta/README.md:151``）。
    """

    __slots__ = ("_children", "_data", "_detached", "base", "context", "is_list")

    def __init__(self, context: _Context, base: Any) -> None:
        self.context = context
        self.base: Any = base
        self.is_list = isinstance(base, list)
        self._data: Any = None
        self._children: dict[Any, Draft] = {}
        self._detached = False

    # ─── 内部 ──────────────────────────────────────────────────────────────

    def _assert_live(self) -> None:
        if self._detached:
            # 父级结构性变更后本句柄已不在 overlay 树上。静默丢弃写入会让调用方
            # 以为改动生效；这里选择大声失败。
            raise TypeError("Cannot use a detached overlay")
        _assert_readable(self.context)

    def _source(self) -> Any:
        return self._data if self._data is not None else self.base

    def _ensure_data(self) -> Any:
        if self._data is None:
            self._data = list(self.base) if self.is_list else dict(self.base)
        return self._data

    def _normalize_index(self, key: Any, length: int, *, allow_end: bool) -> int:
        if not is_int(key):
            raise TypeError(f"list indices must be integers, not {type(key).__name__}")
        index = key
        if index < 0:
            index += length
        limit = length if allow_end else length - 1
        if index < 0 or index > limit:
            raise IndexError(f"list index out of range: {key}")
        return index

    def _read_child(self, key: Any, value: Any) -> Any:
        if not _is_container(value):
            return value
        child = self._children.get(key)
        if child is None:
            child = Draft(self.context, value)
            self._children[key] = child
        return child

    def _bake_children(self) -> None:
        """把子句柄内容写回本层 ``_data``，并把它们标记为 detached。"""
        if not self._children:
            return
        data = self._ensure_data()
        for key, child in self._children.items():
            data[key] = child._materialize()
            child._detached = True
        self._children.clear()

    def _materialize(self) -> JsonValue:
        """当前内容的**独立深拷贝**（结构性变更烘焙子句柄、以及 ``to_json`` 用）。"""
        self._assert_live()
        source = self._source()
        if self.is_list:
            out: list[JsonValue] = []
            for index in range(len(source)):
                child = self._children.get(index)
                out.append(child._materialize() if child is not None else copy_json(source[index]))
            return out
        result: dict[str, JsonValue] = {}
        for key in source:
            child = self._children.get(key)
            result[key] = child._materialize() if child is not None else copy_json(source[key])
        return result

    # ─── 读 ────────────────────────────────────────────────────────────────

    def __getitem__(self, key: Any) -> Any:
        self._assert_live()
        source = self._source()
        if self.is_list:
            index = self._normalize_index(key, len(source), allow_end=False)
            return self._read_child(index, source[index])
        if key not in source:
            raise KeyError(key)
        return self._read_child(key, source[key])

    def __contains__(self, key: object) -> bool:
        self._assert_live()
        source = self._source()
        if self.is_list:
            return is_int(key) and 0 <= key < len(source)
        return key in source

    def __iter__(self) -> Iterator[Any]:
        self._assert_live()
        source = self._source()
        return iter(list(source) if self.is_list else list(source.keys()))

    def __len__(self) -> int:
        self._assert_live()
        return len(self._source())

    def keys(self) -> list[str]:
        self._assert_live()
        if self.is_list:
            raise TypeError("a list draft has no keys()")
        return list(self._source().keys())

    def values(self) -> list[Any]:
        return [self[key] for key in self]

    def items(self) -> list[Any]:
        return [(key, self[key]) for key in self]

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except (KeyError, IndexError):
            return default

    # ─── 写 ────────────────────────────────────────────────────────────────

    def __setitem__(self, key: Any, value: Any) -> None:
        self._assert_live()
        data = self._ensure_data()
        if self.is_list:
            index = self._normalize_index(key, len(data), allow_end=True)
            self._detach_child(index)
            data[index] = _placement(value)
            return
        self._detach_child(key)
        data[key] = _placement(value)

    def __delitem__(self, key: Any) -> None:
        self._assert_live()
        data = self._ensure_data()
        if self.is_list:
            index = self._normalize_index(key, len(data), allow_end=False)
            del data[index]
            self._shift_children(index, -1, drop_at=index)
            return
        if key not in data:
            raise KeyError(key)
        del data[key]
        self._detach_child(key)

    def _detach_child(self, key: Any) -> None:
        child = self._children.pop(key, None)
        if child is not None:
            child._detached = True

    def _shift_children(self, index: int, delta: int, *, drop_at: int | None = None) -> None:
        """元素搬移后重映射子句柄下标；被删掉的那个标记 detached。"""
        shifted: dict[Any, Draft] = {}
        for key, child in self._children.items():
            if drop_at is not None and key == drop_at:
                child._detached = True
                continue
            shifted[key + delta if key >= index else key] = child
        self._children = shifted

    # ─── 列表变更 ──────────────────────────────────────────────────────────

    def _structural(self) -> list[JsonValue]:
        self._assert_live()
        if not self.is_list:
            raise TypeError("this draft is not a list")
        self._bake_children()
        return cast(list[JsonValue], self._ensure_data())

    def append(self, value: Any) -> None:
        self._structural().append(_placement(value))

    def extend(self, values: Any) -> None:
        data = self._structural()
        for value in values:
            data.append(_placement(value))

    def insert(self, index: int, value: Any) -> None:
        data = self._structural()
        data.insert(index, _placement(value))
        self._shift_children(index, 1)

    def pop(self, index: int = -1) -> Any:
        self._assert_live()
        data = self._ensure_data()
        normalized = self._normalize_index(index, len(data), allow_end=False)
        item = data.pop(normalized)
        self._shift_children(normalized, -1, drop_at=normalized)
        return item

    def remove(self, value: Any) -> None:
        self._assert_live()
        data = self._ensure_data()
        index = list(data).index(value)
        del data[index]
        self._shift_children(index, -1, drop_at=index)

    def clear(self) -> None:
        self._assert_live()
        data = self._structural() if self.is_list else self._ensure_data()
        for child in self._children.values():
            child._detached = True
        self._children.clear()
        data.clear()

    def reverse(self) -> None:
        self._structural().reverse()

    def sort(self, *, key: Any = None, reverse: bool = False) -> None:
        self._structural().sort(key=key, reverse=reverse)

    def __iadd__(self, values: Any) -> Draft:
        self.extend(values)
        return self

    # ─── 比较与展示 ────────────────────────────────────────────────────────

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Draft):
            return self._materialize() == other._materialize()
        return self._materialize() == other

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        if self._detached:
            return "<Draft detached>"
        if self.context.is_settled() or self.context.overlay_released:
            return "<Draft settled>"
        return f"<Draft {self._materialize()!r}>"

    def to_json(self) -> JsonValue:
        """当前内容的独立快照（上游无对应物；供调用方与调试使用）。"""
        return self._materialize()


def _placement(value: Any) -> JsonValue:
    """把一个值放进 draft：draft 句柄取其当前内容，其余走严格 JSON 深拷贝+校验。

    上游 ``delta/README.md:105-113``：放置会被拷贝，拷贝过程同时校验；非严格 JSON
    在 draft 变化**之前**抛错。
    """
    if isinstance(value, Draft):
        return value._materialize()
    return copy_json(value)


# ─── op 发射 ─────────────────────────────────────────────────────────────────


def _emit_value(ops: list[Op], path: Path, value: JsonValue) -> None:
    """在 ``path`` 处放置 ``value``；根路径没有 ``s``，只能用 ``r``。"""
    if len(path) == 0:
        ops.append(("r", value))
    else:
        ops.append(("s", path, value))


_ELEMENTWISE_LIMIT = 64
"""等长数组改动不超过这个宽度时逐元素发射（``s`` / 字符串 ``a``/``t``），
超过则用一个 splice。纯启发式，不影响结果值。"""


def _emit_change(ops: list[Op], path: Path, old: JsonValue, new: JsonValue) -> None:
    """两个**普通 JSON 值**之间的差异（不涉及 overlay）。

    字符串走 :func:`~pi_chord.delta.textdiff.emit_string_ops`，因此 transcript 这类
    「往尾部追加」的变更会得到 ``("a", path, text)`` 而不是整个字符串的 ``("s", ...)``
    —— 那正是 delta 存在的意义（上游 ``tracker.ts:2047-2060``）。
    """
    if old == new:
        return
    if isinstance(old, str) and isinstance(new, str):
        string_ops = emit_string_ops(old, new)
        if string_ops is not None:
            ops.extend(attach_path(string_ops, path))
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in old:
            if key not in new:
                ops.append(("d", path + (key,)))
        for key, value in new.items():
            if key not in old:
                _emit_value(ops, path + (key,), value)
            else:
                _emit_change(ops, path + (key,), old[key], value)
        return
    if isinstance(old, list) and isinstance(new, list):
        _diff_list(old, new, path, ops)
        return
    _emit_value(ops, path, new)


def _diff_list(base: list[JsonValue], value: list[JsonValue], path: Path, ops: list[Op]) -> None:
    """数组差异。

    **等长**且改动区不宽时逐元素递归（保留字符串的 ``a``/``t``）；否则用「公共前缀 +
    公共后缀」的一个 splice。

    **正确但不最小**（``PORTING.md`` T3）：上游用 piece/permutation 机制产出最小
    ``p``/``m``；这里只保证 ``apply_immutable(base, ops) == value``，正是
    ``delta/README.md:94`` 允许的自由度。
    """
    if base == value:
        return
    total_base, total_value = len(base), len(value)
    prefix = 0
    while prefix < total_base and prefix < total_value and base[prefix] == value[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < total_base - prefix
        and suffix < total_value - prefix
        and base[total_base - 1 - suffix] == value[total_value - 1 - suffix]
    ):
        suffix += 1
    changed = total_value - prefix - suffix
    if total_base == total_value and changed <= _ELEMENTWISE_LIMIT:
        for index in range(prefix, total_value - suffix):
            _emit_change(ops, path + (index,), base[index], value[index])
        return
    ops.append(
        ("p", path, prefix, total_base - prefix - suffix, value[prefix : total_value - suffix])
    )


def _emit_child(child: Draft, base_value: JsonValue, path: Path, ops: list[Op]) -> None:
    """子句柄相对其 base 子树的差异。"""
    source = child._source()
    if isinstance(base_value, str) and isinstance(source, str):
        string_ops = emit_string_ops(base_value, source)
        if string_ops is not None:
            ops.extend(attach_path(string_ops, path))
        return
    if isinstance(base_value, list) and isinstance(source, list):
        _emit_node(child, base_value, path, ops)
        return
    if isinstance(base_value, dict) and isinstance(source, dict):
        _emit_node(child, base_value, path, ops)
        return
    _emit_change(ops, path, base_value, child._materialize())


def _emit_node(node: Draft, base_value: JsonValue, path: Path, ops: list[Op]) -> None:
    """把一个 overlay 节点相对其 base 子树的差异发射成 op。"""
    source = node._source()
    if node.is_list:
        if node._data is not None:
            _diff_list(cast(list[JsonValue], base_value), cast(list[JsonValue], source), path, ops)
            return
        # 未发生结构性变化：只可能是子句柄上的元素级改动。
        base_list = cast(list[JsonValue], base_value)
        for index, child in node._children.items():
            _emit_child(child, base_list[index], path + (index,), ops)
        return
    base_dict = cast(dict[str, JsonValue], base_value)
    for key in base_dict:
        if key not in source:
            ops.append(("d", path + (key,)))
    for key, value in cast(dict[str, JsonValue], source).items():
        child_node = node._children.get(key)
        if child_node is not None:
            _emit_child(child_node, base_dict.get(key), path + (key,), ops)
        elif key not in base_dict:
            _emit_value(ops, path + (key,), value)
        else:
            _emit_change(ops, path + (key,), base_dict[key], value)


def _emit_operations(context: _Context) -> list[Op]:
    """从 overlay 生成 op 批次（上游 ``emitOperations``, ``tracker.ts:1602``）。"""
    base = context.base_value
    if context.replacement:
        # prepare_replace 是整根操作，不是 diff（``delta/README.md:145-148``）。
        value = context.replacement_value
        return [] if base == value else [("r", value)]
    root = context.root
    if root is None:  # pragma: no cover - 非 replacement 的上下文一定有 root
        return []
    source = root._source()
    if not (_is_container(base) and _is_container(source)):
        return [] if base == source else [("r", source)]
    if isinstance(base, list) != isinstance(source, list):
        return [("r", source)]
    ops: list[Op] = []
    _emit_node(root, base, (), ops)
    if len(ops) > MAX_DELTA_OPERATIONS:
        # 上游 tracker.ts:1707 —— 超预算整体折叠成根替换。
        return [("r", root._materialize())]
    return ops


# ─── Prepared / Change / Tracker ─────────────────────────────────────────────


class Prepared(Generic[T]):
    """一次已物化但**尚未采纳**的候选修订（上游 ``Prepared``, ``tracker.ts:121-127``）。"""

    __slots__ = ("_context", "base", "base_revision", "ops", "value")

    def __init__(
        self, context: _Context, base: T, value: T, ops: list[Op], base_revision: int
    ) -> None:
        self._context = context
        self.base = base
        self.value = value
        self.ops = ops
        self.base_revision = base_revision

    def abort(self) -> None:
        """阻止被采纳；候选本身仍可读（``delta/README.md:134-136``）。"""
        if self._context.status == _STATUS_PREPARED:
            self._context.status = _STATUS_ABORTED

    def __repr__(self) -> str:
        return f"<Prepared base_revision={self.base_revision} ops={len(self.ops)}>"


class Change(Generic[T]):
    """一次打开的变更（上游 ``Change``, ``tracker.ts:129-133``）。"""

    __slots__ = ("_context",)

    def __init__(self, context: _Context) -> None:
        self._context = context

    @property
    def state(self) -> Draft:
        """可变 draft（上游 ``Change.state``）。改动只落在 overlay 上。"""
        _assert_readable(self._context)
        root = self._context.root
        if root is None:  # pragma: no cover
            raise TypeError("this change has no overlay")
        return root

    def prepare(self) -> Prepared[Any]:
        """物化候选与 ops；**不改变权威**（上游 ``tracker.ts:181-201``）。"""
        context = self._context
        if context.status != _STATUS_OPEN:
            raise TypeError("Cannot prepare a settled change")
        base = context.base_value
        ops = _emit_operations(context)
        for op in ops:
            assert_valid_op(op)
        value: JsonValue = base if len(ops) == 0 else apply_immutable(base, ops)
        context.status = _STATUS_PREPARED
        # 物化完成，overlay 不再可用（候选本身通过 prepared.value 保持可读）。
        context.overlay_released = True
        return Prepared[Any](context, base, value, ops, context.base_revision)

    def abort(self) -> None:
        """幂等放弃这次变更（上游 ``tracker.ts:202-213``）。"""
        if self._context.status in (_STATUS_OPEN, _STATUS_PREPARED):
            self._context.status = _STATUS_ABORTED

    def __repr__(self) -> str:
        return f"<Change {self._context.status}>"


class Tracker(Generic[T]):
    """一个修订序列（上游 ``Tracker``, ``tracker.ts:135-141``）。"""

    __slots__ = ("_contexts", "_owner", "_revision", "_value")

    def __init__(self, initial: T) -> None:
        self._owner = object()
        self._value: T = initial
        self._revision = 0
        self._contexts: list[_Context] = []

    @property
    def value(self) -> T:
        """最新已采纳的修订。**契约上不可变**：不冻结、不做防御性拷贝。"""
        return self._value

    @property
    def revision(self) -> int:
        return self._revision

    def begin_change(self) -> Change[T]:
        """在当前修订上开一个 overlay（上游 ``tracker.ts:235-240``）。"""
        base = self._value
        context = _Context(self._owner, self._revision, cast(JsonValue, base))
        if not _is_container(base):
            raise TypeError("track() requires a plain object or array root")
        context.root = Draft(context, base)
        self._contexts.append(context)
        return Change[T](context)

    def prepare_replace(self, value: T) -> Prepared[T]:
        """整根替换，**不是 diff**（上游 ``tracker.ts:241-252`` + ``README.md:145-148``）。

        所有权转移：调用方此后不得再修改 ``value``。与原值深相等时 ``ops`` 为空且保留原根。
        """
        context = _Context(
            self._owner,
            self._revision,
            cast(JsonValue, self._value),
            replacement=True,
            replacement_value=cast(JsonValue, value),
        )
        self._contexts.append(context)
        ops = _emit_operations(context)
        for op in ops:
            assert_valid_op(op)
        base = self._value
        context.status = _STATUS_PREPARED
        return Prepared[T](context, base, value if ops else base, ops, context.base_revision)

    def adopt(self, prepared: Prepared[T]) -> None:
        """校验并指针交换（上游 ``tracker.ts:254-277``）。"""
        context = prepared._context
        if context.owner is not self._owner:
            raise TrackerError("Prepared change belongs to a different tracker")
        if context.status == _STATUS_CONSUMED:
            raise TrackerError("Prepared change has already been used")
        if context.status == _STATUS_ABORTED:
            raise TrackerError("Prepared change has been aborted")
        if context.status == _STATUS_STALE:
            raise TrackerError("Prepared change is stale")
        if context.status != _STATUS_PREPARED:
            raise TrackerError("Prepared change is not ready")
        if prepared.base_revision != self._revision:
            context.status = _STATUS_STALE
            raise TrackerError("Prepared change is stale")
        if self._value is not prepared.base:
            context.status = _STATUS_STALE
            raise TrackerError("Prepared change is stale")
        # 物化已在 prepare 期间完成；采纳是一次不会失败的指针交换，好让存储失败能
        # 丢弃候选而不触碰权威（上游 tracker.ts:271-272）。
        self._value = prepared.value
        self._revision += 1
        context.status = _STATUS_CONSUMED
        self._invalidate(context)

    def _invalidate(self, winner: _Context) -> None:
        """采纳一个候选会让同修订上其余 change 全部 stale（``README.md:131-136``）。"""
        remaining: list[_Context] = []
        for context in self._contexts:
            if context is winner or context.is_settled():
                continue
            context.status = _STATUS_STALE
            remaining.append(context)
        self._contexts = remaining

    def __repr__(self) -> str:
        return f"<Tracker revision={self._revision}>"


def track(initial: T) -> Tracker[T]:
    """建立追踪器；``initial`` 的所有权转移给 tracker（上游 ``tracker.ts:321-323``）。"""
    if not _is_container(initial):
        raise TypeError("track() requires a plain object or array root")
    return Tracker(initial)
