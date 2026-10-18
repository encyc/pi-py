"""一个对话自身花费的账本 —— 移植 ``harness/usage.ts``（72 行）。

两个键空间：

* ``models``：助手条目与摘要尝试，键是 ``provider/modelId``；
* ``tools``：工具结果，键是工具名——它们的用量没有模型身份。

⚠️ 两处必须写清的移植决定
--------------------------

**1. ``omitUndefinedProperties`` 在 chord 里是 no-op（chord 的 J8）**

JS 只丢 ``undefined``、**保留 ``null``**；Python 的 ``None`` 同时表示两者。
实现成「删掉值为 ``None`` 的键」会把**合法的 JSON ``null`` 一起删掉**——那是语义分叉。
所以本模块**在构造处就过滤掉值为 ``None`` 的可选计数器**（:func:`_usage_json`），
而不是指望 ``copy_json`` 的开关替我删。

**2. ``Object.defineProperty`` 那处不需要对应物**

上游 ``addUsageState`` 用 ``Object.defineProperty`` 而不是直接赋值，注释是
「assigning a tool named ``__proto__`` would set the prototype」。
Python 的 ``dict[key] = value`` 没有原型链，``__proto__`` 就是普通键，
所以这里是**直接赋值**，并且有一条同名工具（``__proto__`` / ``toString``）的测试钉住它。
"""

from __future__ import annotations

from typing import Any, TypedDict, cast

from pi_ai import Usage
from pi_chord.json import CopyJsonOptions, JsonRepresentation, copy_json
from pi_durable.documents import define_doc

__all__ = [
    "UsageDoc",
    "UsageState",
    "add_usage",
    "add_usage_state",
    "record_usage",
    "usage_json",
]


class UsageState(TypedDict):
    """一个对话的花费账本（``usage.ts:12-18``）。

    ``models`` 按 ``provider/modelId`` 键；``tools`` 按工具名键。
    两边存的是 ``Usage`` 的 **JSON 表示**——也就是直接可以落进文档的值。
    """

    models: dict[str, JsonRepresentation]
    tools: dict[str, JsonRepresentation]


#: 把用量转成「严格 JSON」用的选项（``usage.ts:34``）。
#:
#: ⚠️ chord 把它们**可以**实现成「删掉 ``None`` 的键」，但它**刻意保持 no-op**
#: （见模块文档）。所以本模块**不依赖**它，而是自己先过滤。
_USAGE_JSON_COPY_OPTIONS = CopyJsonOptions(omit_undefined_properties=True)

#: 可选计数器：**只在提供方报了它们时**才写进账本（``usage.ts:41-43``）。
_OPTIONAL_COUNTERS = ("cache_write_1h", "reasoning")


#: 一个对话的 ``pi.usage`` 文档（``usage.ts:20-28``）。
#:
#: 与提供方身份一样是 ``history: "latest"`` + ``fork: "initial"``：
#: 账本是**当前**值，而且分叉**从零开始**——子会话的花费不该背父会话的账。
#: ``checkpointWhen: () => true`` 每次都写 base。
UsageDoc = define_doc(
    {
        "kind": "pi.usage",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "initial": lambda seed=None: {"models": {}, "tools": {}},
        "checkpointWhen": lambda value, ops, info: True,
    }
)


def usage_json(usage: Usage | dict[str, Any]) -> JsonRepresentation:
    """一份 ``Usage`` 的严格 JSON 形式：**可选计数器缺失时整个键不写**。

    这就是 chord ``omitUndefinedProperties`` 在上游想要的效果，
    但由调用方在**构造处**完成（见模块文档第 1 条）。
    """
    raw: dict[str, Any] = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    kept: dict[str, Any] = {}
    for name, value in raw.items():
        if value is None:
            # ⚠️ 只对**可选计数器**丢弃；其它字段为 None 是数据问题，照原样留着。
            continue
        kept[name] = value
    return copy_json(kept, _USAGE_JSON_COPY_OPTIONS)


def add_usage(total: Any, usage: Usage | dict[str, Any]) -> None:
    """把 ``usage`` 的每个计数器加到 ``total`` 上（``usage.ts:47-63``）。

    可选计数器**只要有一边报了**就累加：另一边当作 0。
    """
    raw: dict[str, Any] = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    cost = raw["cost"]
    total["input"] += raw["input"]
    total["output"] += raw["output"]
    total["cache_read"] += raw["cache_read"]
    total["cache_write"] += raw["cache_write"]
    total["total_tokens"] += raw["total_tokens"]
    for name in _OPTIONAL_COUNTERS:
        value = raw.get(name)
        if value is not None:
            total[name] = (total.get(name) or 0) + value
    total["cost"]["input"] += cost["input"]
    total["cost"]["output"] += cost["output"]
    total["cost"]["cache_read"] += cost["cache_read"]
    total["cost"]["cache_write"] += cost["cache_write"]
    total["cost"]["total"] += cost["total"]


def add_usage_state(state: UsageState, other: UsageState) -> None:
    """把 ``other`` 的每个桶加进 ``state``（``usage.ts:66-81``）。"""
    for bucket in ("models", "tools"):
        for key, usage in other[bucket].items():
            totals = state[bucket]
            total = totals.get(key) if key in totals else None
            if total is not None:
                add_usage(total, cast(dict[str, Any], usage))
            else:
                # 上游用 ``Object.defineProperty`` 绕开 ``__proto__`` 的原型陷阱；
                # Python 的 dict 没有原型链，直接赋值即可（见模块文档第 2 条）。
                totals[key] = copy_json(cast(Any, usage))


async def record_usage(
    tx: Any,
    conversation_id: int,
    bucket: str,
    key: str,
    usage: Usage | dict[str, Any],
) -> None:
    """把 ``usage`` 加进该对话 ``pi.usage`` 的某一个桶，**就在记录这次响应的那次提交里**
    （``usage.ts:30-45``）。

    ``bucket`` 是 ``"models"`` 或 ``"tools"``。
    """
    document = await tx.doc(UsageDoc, conversation_id)
    totals = document[bucket]
    # 只认自有键：一个工具可以就叫 `toString`。
    total = totals.get(key) if key in totals else None
    if total is None:
        totals[key] = usage_json(usage)
    else:
        add_usage(total, usage)
