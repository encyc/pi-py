"""工具输出的共享截断工具 —— 移植 ``durable/src/truncate.ts``（174 行）。

截断基于两个**互相独立**的上限，先撞到哪个就用哪个（``truncate.ts:4-5``）：

* 行数上限，默认 2000 行（``truncate.ts:12``）
* 字节上限，默认 50 KB（``truncate.ts:13``）

**永不返回半行**（``truncate.ts:8``）。工具输出的流式有界缓冲是另一条路径
（上游 ``OutputBuffer``，``truncate.ts:9``），不在本模块。

与 ``pi_coding_agent.truncate`` 的区别
--------------------------------------

那是 **coding-agent** 的 ``core/tools/truncate.ts``，另一份上游文件、另一套 API
（``truncate_tail`` / ``truncate_middle`` / ``truncate_line``）。本模块是 **durable**
的 ``truncate.ts``：只有 ``truncate_head`` / ``truncate_head_of`` / ``format_size``，
并再导出 ``utf8_byte_length``（``truncate.ts:48``）。两者**不合并**，因为上游就是两份。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pi_chord.output import utf8_byte_length

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "TruncationOptions",
    "TruncationResult",
    "TruncationTotals",
    "format_size",
    "truncate_head",
    "truncate_head_of",
    "utf8_byte_length",
]

#: 上游 ``DEFAULT_MAX_LINES``（``truncate.ts:12``）。
DEFAULT_MAX_LINES = 2000
#: 上游 ``DEFAULT_MAX_BYTES``（``truncate.ts:13``）。
DEFAULT_MAX_BYTES = 50 * 1024

#: 撞到的是哪一个上限；未截断时是 ``None``（``truncate.ts:21``）。
TruncatedBy = Literal["lines", "bytes"]


@dataclass(frozen=True, slots=True)
class TruncationOptions:
    """上游 ``TruncationOptions``（``truncate.ts:40-45``）。

    ``None`` 表示「用默认值」，对应 JS 的 ``?? DEFAULT_*``（``truncate.ts:95-96``）——
    注意这与「显式传 0」不同：``max_bytes=0`` 是合法上限，不会被默认值替换。
    """

    max_lines: int | None = None
    max_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class TruncationTotals:
    """整段文本的总量（``truncate.ts:92`` 的内联对象类型）。"""

    lines: int
    bytes: int


@dataclass(frozen=True, slots=True)
class TruncationResult:
    """上游 ``TruncationResult``（``truncate.ts:15-38``），字段一一对应。"""

    #: 截断后的内容。
    content: str
    #: 是否发生了截断。
    truncated: bool
    #: 撞到的是哪个上限；未截断时为 ``None``。
    truncated_by: TruncatedBy | None
    #: 原文总行数。
    total_lines: int
    #: 原文总字节数。
    total_bytes: int
    #: 截断结果里的完整行数。
    output_lines: int
    #: 截断结果的字节数。
    output_bytes: int
    #: 最后一行是否被部分截断（只在尾部截断的边界情况出现；头部截断恒为 ``False``）。
    last_line_partial: bool
    #: 首行是否就超过字节上限（头部截断时内容为空）。
    first_line_exceeds_limit: bool
    #: 实际生效的行数上限。
    max_lines: int
    #: 实际生效的字节上限。
    max_bytes: int


def split_lines_for_counting(content: str) -> list[str]:
    """按行切分用于计数：空串 0 行，末尾换行不算新行（``truncate.ts:50-55``）。

    对应 JS ``content.split("\\n")`` + 末尾 pop：

    * ``""`` → ``[]``（上游提前返回，``truncate.ts:51``）
    * ``"a"`` → ``["a"]``
    * ``"a\\n"`` → ``["a"]``（pop 掉末尾空串）
    * ``"a\\nb"`` → ``["a", "b"]``
    * ``"a\\n\\n"`` → ``["a", ""]``（只 pop 一个）

    上游没有导出它；这里导出以便测试直接对照（线上公开面不变）。
    """
    if len(content) == 0:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def _js_to_fixed_1(value: float) -> str:
    """JS ``toFixed(1)`` 的等价实现。

    差异点：JS 规范规定「取最接近的 ``n/10``，**并列时取更大的 n**」，即十进制的
    四舍五入；Python 的 ``f"{x:.1f}"`` 走的是二进制值的 round-half-even。两者在
    ``x = 1.25`` 这类**可被 double 精确表示**的并列点上会分叉
    （JS ``"1.3"``，Python ``"1.2"``）。

    ``Decimal(float)`` 取的是该 double 的**精确**十进制值，再配 ``ROUND_HALF_UP``，
    与 JS 的「并列取大」逐位一致。
    """
    return str(Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def format_size(num_bytes: int) -> str:
    """把字节数格式化成人类可读的尺寸（``truncate.ts:60-68``）。

    ``< 1024`` → ``"123B"``；``< 1 MiB`` → ``"1.5KB"``；否则 ``"2.0MB"``。
    小数点后一位用 :func:`_js_to_fixed_1`，与 JS ``toFixed(1)`` 一致。
    """
    if num_bytes < 1024:
        return f"{num_bytes}B"
    if num_bytes < 1024 * 1024:
        return f"{_js_to_fixed_1(num_bytes / 1024)}KB"
    return f"{_js_to_fixed_1(num_bytes / (1024 * 1024))}MB"


def truncate_head(content: str, options: TruncationOptions | None = None) -> TruncationResult:
    """从头部截断（保留前 N 行 / 字节），适合「要看开头」的文件读取。

    对应上游 ``truncateHead()``（``truncate.ts:77-83``）：先算出整段的总行数与总字节数，
    再交给 :func:`truncate_head_of`。**永不返回半行**；首行就超过字节上限时返回空内容并置
    ``first_line_exceeds_limit=True``。
    """
    return truncate_head_of(
        content,
        TruncationTotals(
            lines=len(split_lines_for_counting(content)), bytes=utf8_byte_length(content)
        ),
        options,
    )


def truncate_head_of(
    prefix: str,
    totals: TruncationTotals,
    options: TruncationOptions | None = None,
) -> TruncationResult:
    """已知前缀与总量时的头部截断（``truncate.ts:90-174``）。

    上游契约（``truncate.ts:86-89``）：``prefix`` 必须是整段文本，或者**长于**
    ``max_bytes + 1`` 个 UTF-8 字节，或者**至少含 ``max_lines`` 个换行**；
    满足任一条时，结果与对整段文本调用 ``truncate_head`` 相同。

    这是给「只需读文件头部就能判定截断结果」的调用方省一次全量读取用的。
    """
    max_lines = (
        DEFAULT_MAX_LINES if options is None or options.max_lines is None else options.max_lines
    )
    max_bytes = (
        DEFAULT_MAX_BYTES if options is None or options.max_bytes is None else options.max_bytes
    )

    total_bytes = totals.bytes
    lines = split_lines_for_counting(prefix)
    total_lines = totals.lines

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=prefix,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    # 上游这里读 `lines[0]`（``truncate.ts:120``）。按 `truncateHeadOf` 的契约，
    # 越界调用方不存在；JS 在 `lines` 为空时会抛 TypeError。Python 侧取「无首行 =
    # 0 字节」，让它走完下面的循环并返回空内容，行为更明确、也不改变合法调用的结果。
    first_line_bytes = utf8_byte_length(lines[0]) if lines else 0
    if first_line_bytes > max_bytes:
        return TruncationResult(
            content="",
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=0,
            output_bytes=0,
            last_line_partial=False,
            first_line_exceeds_limit=True,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output: list[str] = []
    output_bytes_count = 0
    truncated_by: TruncatedBy = "lines"

    for index in range(min(len(lines), max_lines)):
        line_bytes = utf8_byte_length(lines[index]) + (1 if index > 0 else 0)
        if output_bytes_count + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        output.append(lines[index])
        output_bytes_count += line_bytes

    # 没有因字节中断时，只有「确实少了几行」才证明撞的是行数上限；
    # 否则就是末尾那个换行字节超了（``truncate.ts:155-156``）。
    if truncated_by != "bytes":
        truncated_by = "lines" if len(output) < total_lines else "bytes"

    output_content = "\n".join(output)

    return TruncationResult(
        content=output_content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output),
        # 与上游一致：这里对拼接后的文本**重新**计算字节数，而不是复用循环里的累加值
        # ——循环给非首行都加了 1 个换行字节，最后一行若没有换行就会多算（``truncate.ts:159``）。
        output_bytes=utf8_byte_length(output_content),
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )
