"""工具调用的展示辅助函数。

对应上游 ``core/tools/render-utils.ts``。

**有意偏离**：上游该文件里绝大多数是 TUI 渲染辅助（主题着色、``renderShell``、
``formatToolCallWithArgs`` / ``startToolTimer`` / ``toolTimerText`` 等），依赖
``@earendil-works/pi-tui``。本包不复刻 TUI（见 ``PORTING.md``），因此只移植
**无渲染依赖、且被上游包入口导出**的 ``format_read_line_range``。

上游把它用在 read 渲染器、session 树选择器和扩展示例（``subagent`` / ``minimal-mode``）里，
属于 SDK 公开面的一部分。
"""

from __future__ import annotations

import math

__all__ = ["format_read_line_range"]


def _parse_line_number(value: object) -> float | None:
    """对齐上游 ``parseLineNumber``：接受有限数字或数字字符串，其余返回 None。

    ``bool`` 在 Python 里是 ``int`` 的子类，但 JS 的 ``typeof`` 会把它判成 ``"boolean"``，
    因此这里显式排除。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if value.strip() == "":
            return None
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _js_number(value: float) -> str:
    """按 JS ``String(number)`` 的观感输出（整数不带 ``.0``）。"""
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    return repr(value)


def format_read_line_range(offset: object, limit: object) -> str:
    """把 read 的 offset/limit 格式化成 ``:start-end`` 行号区间。

    对齐上游 v1.3.0：修复 ``null`` 或数字字符串参数导致的行号标签错误
    （上游 #9887 / #9996）。返回空串表示无法构成标签。
    """
    # 严格 schema 会把省略的可选参数表示成 null。
    if offset is None and limit is None:
        return ""
    start = 1.0 if offset is None else _parse_line_number(offset)
    if start is None:
        return ""
    if limit is None:
        return f":{_js_number(start)}"
    count = _parse_line_number(limit)
    if count is None:
        return ""
    end = start + count - 1
    # 对齐上游 `end ? ... : ...`：end 为 0 时省略区间。
    return f":{_js_number(start)}{f'-{_js_number(end)}' if end else ''}"
