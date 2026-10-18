"""逐叶写入 JSON —— 移植 ``harness/json.ts``（31 行）。"""

from __future__ import annotations

from typing import Any, TypeGuard

from pi_chord.json import JsonValue

__all__ = ["assign_json"]

#: 可以逐叶写入的 JSON 容器（``json.ts:3``）。
JsonContainer = dict[str, Any] | list[Any]


def _is_record(value: Any) -> TypeGuard[dict[str, Any]]:
    """``typeof value === "object" && value !== null && !Array.isArray(value)``（``json.ts:30-32``）。

    ``TypeGuard`` 是必要的：mypy 不会从一个返回 ``bool`` 的函数推断收窄，
    而下面两处递归都依赖「这里已经确定是 dict 了」。
    """
    return isinstance(value, dict)


def _get(target: JsonContainer, key: str | int) -> Any:
    if isinstance(target, list):
        index = int(key)
        return target[index] if -len(target) <= index < len(target) else None
    return target.get(str(key))


def _set(target: JsonContainer, key: str | int, value: Any) -> None:
    if isinstance(target, list):
        target[int(key)] = value
    else:
        target[str(key)] = value


def assign_json(target: JsonContainer, key: str | int, value: JsonValue) -> None:
    """把 ``value`` **逐叶**写到 ``target[key]``（``json.ts:10-28``）。

    **为什么不能直接整体赋值**（上游注释，``json.ts:5-9``）：chord 把一次容器赋值记成**一个完整的
    set**，只有在「字符串叶子被换成更长的字符串」时才额外发一个 append。
    所以整体写一个部分值，会让**每一次 flush 都把整条消息存下来并发布出去**。
    逐叶写则只产生真正变化的那几个操作。

    递归规则：

    * 两边都是对象 → 先删掉 ``value`` 里没有的键，再逐键递归；
    * 两边都是数组且 ``len(current) <= len(value)`` → 按下标递归，多出来的 **append**；
    * 其它 → 值不同才写。

    ⚠️ 上游的 ``current !== value`` 是**引用比较**；Python 侧用 ``!=``。
    对 JSON 值两者在**判定「要不要写」**上一致；差别只在「同一个对象」时上游也会走
    ``slots[key] = value``（写回自己，无副作用），而这里会跳过——结果相同。
    ⚠️ 上游的 ``Object.hasOwn(value, name)`` 是**自有属性**判定；Python 的 ``in``
    对 dict 就是自有键，语义一致。
    """
    current = _get(target, key)

    if _is_record(current) and _is_record(value):
        # value 里没有的键要删掉：逐叶写入必须是**完整替换**的语义。
        for name in list(current):
            if name not in value:
                del current[name]
        for name, child in value.items():
            assign_json(current, name, child)
        return

    if isinstance(current, list) and isinstance(value, list) and len(current) <= len(value):
        for index in range(len(value)):
            if index < len(current):
                assign_json(current, index, value[index])
            else:
                current.append(value[index])
        return

    if current != value:
        _set(target, key, value)
