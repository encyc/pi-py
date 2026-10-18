"""输出截断工具。

对应上游 ``core/tools/truncate.ts``。

截断方向：
- ``truncate_head``：从头部保留（read/find/grep/ls 用，因为要看开头）。
- ``truncate_tail``：从尾部保留（bash 用，因为错误/结果在末尾）。
- ``truncate_middle``：保留首尾、中间省略（上游 v1.3.0 新增，MCP 工具输出用）。
- ``truncate_line``：单行截断（grep 用，长匹配行）。
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024  # 50KB
GREP_MAX_LINE_LENGTH = 500


@dataclass
class TruncationResult:
    content: str
    truncated: bool
    truncated_by: str | None  # "lines" | "bytes" | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _split_lines_for_counting(content: str) -> list[str]:
    """与上游 ``splitLinesForCounting`` 一致：空串 0 行，末尾换行不算新行。"""
    if not content:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def truncate_head(
    content: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """从头部保留。永不返回半行。"""
    total_bytes = _byte_len(content)
    lines = content.split("\n")
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content,
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

    # 首行就超字节限制
    first_line_bytes = _byte_len(lines[0])
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

    output_lines: list[str] = []
    output_bytes = 0
    truncated_by = "lines" if total_lines > max_lines else "bytes"

    for i, line in enumerate(lines):
        if i >= max_lines:
            truncated_by = "lines"
            break
        line_bytes = _byte_len(line)
        if output_bytes + line_bytes > max_bytes and output_lines:
            truncated_by = "bytes"
            break
        output_lines.append(line)
        output_bytes += line_bytes
        if i < len(lines) - 1:  # 换行符
            output_bytes += 1

    result_content = "\n".join(output_lines)
    return TruncationResult(
        content=result_content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output_lines),
        output_bytes=_byte_len(result_content),
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_tail(
    content: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """从尾部保留。若最后一行超字节且无其他行，返回该行尾部（半行）。"""
    total_bytes = _byte_len(content)
    lines = content.split("\n")
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content,
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

    output_lines: list[str] = []
    output_bytes = 0
    truncated_by = "lines" if total_lines > max_lines else "bytes"
    last_line_partial = False

    for line in reversed(lines):
        line_bytes = _byte_len(line)
        if len(output_lines) >= max_lines:
            truncated_by = "lines"
            break
        if output_bytes + line_bytes > max_bytes and output_lines:
            truncated_by = "bytes"
            break
        # 单行超字节且还没有任何输出 → 取尾部
        if line_bytes > max_bytes and not output_lines:
            truncated_by = "bytes"
            last_line_partial = True
            tail_bytes = line.encode("utf-8")[-max_bytes:].decode("utf-8", errors="ignore")
            output_lines.append(tail_bytes)
            break
        output_lines.append(line)
        output_bytes += line_bytes + (1 if output_lines else 0)

    output_lines.reverse()
    result_content = "\n".join(output_lines)
    return TruncationResult(
        content=result_content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output_lines),
        output_bytes=_byte_len(result_content),
        last_line_partial=last_line_partial,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> tuple[str, bool]:
    """单行截断。返回 (text, was_truncated)。"""
    if len(line) <= max_chars:
        return line, False
    return line[:max_chars] + "... [truncated]", True


@dataclass
class MiddleTruncationResult:
    """``truncate_middle`` 的结果（对应上游 ``MiddleTruncationResult``）。"""

    content: str
    truncated: bool
    removed_chars: int
    total_bytes: int
    total_lines: int


def truncate_middle(content: str, max_bytes: int) -> MiddleTruncationResult:
    """保留首尾各 max_bytes/2，中间用 ``…N chars truncated…`` 标记替换。

    对齐上游 v1.3.0 ``truncateMiddle()``（Codex 风格的中间省略）：只在字符边界切分，
    不会产生半个 UTF-8 字符。
    """
    buf = content.encode("utf-8")
    total_lines = len(_split_lines_for_counting(content))
    if len(buf) <= max_bytes:
        return MiddleTruncationResult(
            content=content,
            truncated=False,
            removed_chars=0,
            total_bytes=len(buf),
            total_lines=total_lines,
        )

    def is_boundary(index: int) -> bool:
        # 续字节（10xxxxxx）不是字符起点。
        return index >= len(buf) or (buf[index] & 0xC0) != 0x80

    head_end = max_bytes // 2
    while head_end > 0 and not is_boundary(head_end):
        head_end -= 1
    tail_start = len(buf) - (max_bytes - max_bytes // 2)
    while tail_start < len(buf) and not is_boundary(tail_start):
        tail_start += 1

    head = buf[:head_end].decode("utf-8")
    tail = buf[tail_start:].decode("utf-8")
    removed_chars = len(buf[head_end:tail_start].decode("utf-8"))
    return MiddleTruncationResult(
        content=f"{head}…{removed_chars} chars truncated…{tail}",
        truncated=True,
        removed_chars=removed_chars,
        total_bytes=len(buf),
        total_lines=total_lines,
    )


def format_size(num_bytes: int) -> str:
    """格式化字节大小。"""
    if num_bytes < 1024:
        return f"{num_bytes}B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f}KB"
    return f"{num_bytes / (1024 * 1024):.1f}MB"


__all__ = [
    "DEFAULT_MAX_LINES",
    "DEFAULT_MAX_BYTES",
    "GREP_MAX_LINE_LENGTH",
    "MiddleTruncationResult",
    "TruncationResult",
    "truncate_head",
    "truncate_tail",
    "truncate_middle",
    "truncate_line",
    "format_size",
]
