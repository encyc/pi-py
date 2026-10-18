"""用量的合并（**部分移植**）。

> ⚠️ 上游 ``core/usage-totals.ts``(97) **未整体移植**；这里只提供 codemode 需要的那一个函数
> :func:`combine_usage`（``execute.ts:459``）。该模块其余部分（``totalUsage`` 等）**未移植**。

上游出处：``core/usage-totals.ts``
"""

from __future__ import annotations

from typing import Any


def combine_usage(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """把两次用量相加（对应上游 ``combineUsage``）。

    ``cacheWrite1h`` 与 ``reasoning`` 是**可选**的：上游只在**任一侧有值**时才输出该键
    （展开一个条件对象），否则整个键不出现 —— 这不是「加出 0」，是「保持缺席」。
    未知键原样保留第一条的取值，以免丢掉上游将来新增的字段。
    """
    combined: dict[str, Any] = {
        "input": first.get("input", 0) + second.get("input", 0),
        "output": first.get("output", 0) + second.get("output", 0),
        "cacheRead": first.get("cacheRead", 0) + second.get("cacheRead", 0),
        "cacheWrite": first.get("cacheWrite", 0) + second.get("cacheWrite", 0),
    }
    if first.get("cacheWrite1h") is not None or second.get("cacheWrite1h") is not None:
        combined["cacheWrite1h"] = (first.get("cacheWrite1h") or 0) + (
            second.get("cacheWrite1h") or 0
        )
    if first.get("reasoning") is not None or second.get("reasoning") is not None:
        combined["reasoning"] = (first.get("reasoning") or 0) + (second.get("reasoning") or 0)
    combined["totalTokens"] = first.get("totalTokens", 0) + second.get("totalTokens", 0)
    first_cost = first.get("cost") or {}
    second_cost = second.get("cost") or {}
    combined["cost"] = {
        "input": first_cost.get("input", 0) + second_cost.get("input", 0),
        "output": first_cost.get("output", 0) + second_cost.get("output", 0),
        "cacheRead": first_cost.get("cacheRead", 0) + second_cost.get("cacheRead", 0),
        "cacheWrite": first_cost.get("cacheWrite", 0) + second_cost.get("cacheWrite", 0),
        "total": first_cost.get("total", 0) + second_cost.get("total", 0),
    }
    return combined


__all__ = ["combine_usage"]
