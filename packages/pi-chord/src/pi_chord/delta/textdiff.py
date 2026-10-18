"""字符串级 delta 发射与应用 —— **UTF-16 code unit 约定**。

对应上游:
- ``chord/src/delta/tracker.ts:2039-2063`` (``emitChangedValue`` 的字符串分支)
- ``chord/src/delta/diff.ts:63-76`` (``emitString``，同一逻辑的第二处)
- ``chord/src/delta/index.ts:87-112`` (``overlap``)
- ``chord/src/delta/index.ts:391-402`` (``apply`` 的 ``a`` / ``t`` 分支)

为什么这一层单独成模块
----------------------

JS 的 ``String.prototype.length`` 与 ``String.prototype.slice`` 都按 **UTF-16 code
unit** 计数，而 Python ``str`` 是 **code point** 序列。二者只在一种 op 上分叉：

- ``("a", path, text)`` 携带的是**文本本身**，不是计数 —— 两种约定下**永远相同**
  （30,000 例 fuzz 验证：``a``-op 差异 = 0）。
- ``("t", path, count)`` 的 ``count`` 是 **UTF-16 码元数**（``index.ts:400`` 的
  ``current.slice(op[2])``）—— 这是唯一的真实分叉点。

本包按 Lead 决定采用 **UTF-16 约定**，理由见 ``PORTING.md``。派生结论：
``n_utf16 - n_cp`` 恰好等于被删前缀中非 BMP 码点的个数。

实现要点
--------

内部统一用 ``utf-16-be`` 打包成 ``bytes`` 做比较与查找：每个码元恰好 2 字节，
子串边界恒在偶数偏移，因此可以直接用原生的 ``bytes.find`` / 切片，既快又不会
在半个码元上切开。

⚠️ 解码回 ``str`` 时**不能**用 ``data.decode("utf-16-be", errors="surrogatepass")``：
那会把一个合法代理对拆成**两个孤立代理**（长度 2 的 ``str``），与源的 1 个码点
不等价。必须手写合并循环（``from_units``）。
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from typing import Literal, TypeAlias

from .ops import Op, Path

__all__ = [
    "MAX_OVERLAP_SCAN",
    "AppendStringOp",
    "ReplaceStringOp",
    "StringOp",
    "TruncateStringOp",
    "apply_string_ops",
    "attach_path",
    "emit_string_ops",
    "from_units",
    "overlap",
    "to_units",
    "u16_len",
    "u16_slice",
]

MAX_OVERLAP_SCAN = 65_536
"""上游 ``tracker.ts:2054`` 的字面量与 ``diff.ts:4`` 的 ``DEFAULT_OVERLAP_SCAN`` 同为 65,536。"""

_SURROGATE_HIGH_START = 0xD800
_SURROGATE_HIGH_END = 0xDBFF
_SURROGATE_LOW_START = 0xDC00
_SURROGATE_LOW_END = 0xDFFF
_SURROGATE_OFFSET = 0x10000 - (_SURROGATE_HIGH_START << 10) - _SURROGATE_LOW_START

AppendStringOp: TypeAlias = tuple[Literal["a"], str]
TruncateStringOp: TypeAlias = tuple[Literal["t"], int]
ReplaceStringOp: TypeAlias = tuple[Literal["s"], str]

StringOp: TypeAlias = AppendStringOp | TruncateStringOp | ReplaceStringOp
"""**字符串级** op：``("a", text)`` / ``("t", count)`` / ``("s", text)``。

这一层刻意不带路径 —— 它是被测的**原语**，直接对应
``recon/08-chord-delta-spec.md`` §2.3 的 20 条向量（那 20 条就是路径无关的字符串级
形态）。要落到真实 :data:`~pi_chord.delta.ops.Op` 上用 :func:`attach_path`。
"""


def to_units(text: str) -> list[int]:
    """``str`` -> UTF-16 码元序列（JS 字符串的等价表示）。"""
    data = text.encode("utf-16-be", errors="surrogatepass")
    return list(struct.unpack(f">{len(data) // 2}H", data))


def from_units(units: Sequence[int]) -> str:
    """UTF-16 码元序列 -> ``str``：**合并合法代理对**，保留真正的孤立代理。"""
    out: list[str] = []
    index = 0
    count = len(units)
    while index < count:
        code = units[index]
        if (
            _SURROGATE_HIGH_START <= code <= _SURROGATE_HIGH_END
            and index + 1 < count
            and _SURROGATE_LOW_START <= units[index + 1] <= _SURROGATE_LOW_END
        ):
            out.append(chr((code << 10) + units[index + 1] + _SURROGATE_OFFSET))
            index += 2
        else:
            out.append(chr(code))
            index += 1
    return "".join(out)


def _text_to_bytes(text: str) -> bytes:
    return text.encode("utf-16-be", errors="surrogatepass")


def _bytes_to_text(data: bytes) -> str:
    return from_units(list(struct.unpack(f">{len(data) // 2}H", data)))


def u16_len(text: str) -> int:
    """UTF-16 码元数（等价于 JS ``text.length``）。"""
    return len(_text_to_bytes(text)) // 2


def u16_slice(text: str, start: int, end: int | None = None) -> str:
    """等价于 JS ``text.slice(start[, end])`` —— **按 UTF-16 码元**切片。"""
    data = _text_to_bytes(text)
    if end is None:
        return _bytes_to_text(data[start * 2 :])
    return _bytes_to_text(data[start * 2 : end * 2])


def _find_aligned(haystack: bytes, needle: bytes, start: int) -> int:
    """在 ``haystack`` 中找 ``needle``，只接受**码元对齐**（偶数偏移）的匹配。

    ``bytes.find`` 是原生实现；未对齐的命中必须跳过，否则会把错开半个码元的
    位置当成真匹配。
    """
    if not needle:
        return start if start <= len(haystack) else -1
    found = haystack.find(needle, start)
    while found != -1:
        if found % 2 == 0:
            return found
        found = haystack.find(needle, found + 1)
    return -1


def overlap(a: str, b: str, scan: int, probe: int = 64, max_candidates: int = 8) -> int:
    """``a`` 的最长后缀同时是 ``b`` 的前缀的长度，单位是 **UTF-16 码元**。

    上游 ``index.ts:87-112``。原文注释（``index.ts:89-93``）：

    > 先用 ``indexOf`` 探测再验证精确子串相等，好让热循环留在原生实现里。
    > 手写 KMP 渐进等价但实践中慢得多。始终正确：返回的 n 满足
    > ``a.slice(a.length - n) === b.slice(0, n)``。

    先用长 head（候选少，且能抓住滚动窗口产生的大重叠），再退化到单字符
    （候选变多但能找到任何重叠）。候选数有上限，因为重复输出（构建日志、或任意
    单字符长串）会让长 head 在数千个位置命中；放弃时返回 0，那会发射一个
    ``set`` —— 更大，但**永远不会错**。
    """
    if scan == 0:
        return 0
    hi = _text_to_bytes(a)
    hb = _text_to_bytes(b)
    a_units = len(hi) // 2
    b_units = len(hb) // 2
    if a_units == 0 or b_units == 0:
        return 0
    tail = hi[(a_units - scan) * 2 :] if a_units > scan else hi
    for head_units in (min(probe, b_units), 1):
        head = hb[: head_units * 2]
        tried = 0
        found = _find_aligned(tail, head, 0)
        while found != -1:
            tried += 1
            if tried > max_candidates:
                break
            n = (len(tail) - found) // 2
            if n <= b_units and tail[found:] == hb[: n * 2]:
                return n
            found = _find_aligned(tail, head, found + 1)
        if head_units == 1:
            break
    return 0


def emit_string_ops(before: str, after: str) -> list[StringOp] | None:
    """为一次字符串变更发射字符串级 op；``None`` 表示没有变更。

    上游 ``tracker.ts:2047-2062``（``diff.ts:63-76`` 是同一逻辑）：

    1. 若 ``after`` 比 ``before`` 长且以 ``before`` 开头 —— 单发追加
       （``tracker.ts:2050-2053``，走 V8 的 rope 快路径）。
    2. 否则求 :func:`overlap`；有重叠则先 ``t`` 再按需 ``a``（``tracker.ts:2054-2059``）。
    3. 都没有则整体 ``s``（``tracker.ts:2061``）。

    规则 1 的切片在两种约定下都落在同一字符边界上，因此追加文本恒等 —— 30,000 例
    fuzz 中 ``a``-op 差异为 0。
    """
    if before == after:
        return None
    hb = _text_to_bytes(before)
    ha = _text_to_bytes(after)
    if len(ha) > len(hb) and ha[: len(hb)] == hb:
        return [("a", _bytes_to_text(ha[len(hb) :]))]
    shared = overlap(before, after, MAX_OVERLAP_SCAN)
    if shared > 0:
        ops: list[StringOp] = [("t", len(hb) // 2 - shared)]
        if len(ha) // 2 > shared:
            ops.append(("a", _bytes_to_text(ha[shared * 2 :])))
        return ops
    return [("s", after)]


def apply_string_ops(text: str, ops: Sequence[StringOp]) -> str:
    """把字符串级 op 应用到 ``text``（上游 ``index.ts:391-402`` 的 ``a`` / ``t``）。"""
    current = text
    for op in ops:
        if op[0] == "a":
            current = current + op[1]
        elif op[0] == "t":
            current = u16_slice(current, op[1])
        else:
            current = op[1]
    return current


def attach_path(ops: Sequence[StringOp], path: Path) -> list[Op]:
    """把字符串级 op 提升为带路径的真实 :data:`~pi_chord.delta.ops.Op`。

    ``tracker`` 在发射时会用这个函数把字符串级结果挂到具体路径上。
    """
    out: list[Op] = []
    for op in ops:
        if op[0] == "a":
            out.append(("a", path, op[1]))
        elif op[0] == "t":
            out.append(("t", path, op[1]))
        else:
            out.append(("s", path, op[1]))
    return out
