"""chord delta 的错误类型与保留路径段。

对应上游 ``chord/src/delta/index.ts``:
- ``RESERVED_SEGMENTS`` — 第 131 行
- ``UnsafePathError``   — 第 133-149 行
- ``PathError``         — 第 310-317 行
"""

from __future__ import annotations

import json

__all__ = [
    "RESERVED_SEGMENTS",
    "PathError",
    "Seg",
    "UnsafePathError",
]

Seg = str | int
"""路径段：对象键（``str``）或数组下标（非负 ``int``）。"""

RESERVED_SEGMENTS: frozenset[str] = frozenset({"__proto__", "constructor", "prototype"})
"""上游 ``index.ts:131``。

tracker 绝不把这三个键作为路径段发射；``apply`` / ``apply_immutable`` 遇到即拒绝。
Python 没有原型链，但这三个键仍必须保留：**它们参与「折叠到哪个祖先」的判定**，
丢掉会让含这些键的文档产出的 op 与上游分叉（详见 ``PORTING.md``）。
"""


class UnsafePathError(Exception):
    """路径段本身不可能安全地寻址（上游 ``index.ts:133-149``）。

    ``index.ts:128-130`` 的原文：op 来自 facet、插件隔间或工具，其内容可能回显模型输出，
    因此全部按不可信输入处理。
    """

    def __init__(self, segment: object) -> None:
        super().__init__(f"unsafe path segment: {segment!s}")
        self.segment: object = segment


class PathError(Exception):
    """路径在目标值上无法解析（上游 ``index.ts:310-317``）。"""

    def __init__(self, path: object) -> None:
        super().__init__(f"unresolvable path: {json.dumps(path, default=str)}")
        self.path: object = path
