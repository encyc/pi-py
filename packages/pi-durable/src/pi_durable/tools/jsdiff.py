"""npm ``diff`` 包（``diff@8.0.4``）的**标准库替身** —— 只实现本包用到的那两个原语。

上游 ``durable/src/tools/edit-diff.ts`` 依赖 npm 的 ``diff`` 包
（``durable/package.json:63``：``"diff": "8.0.4"``），但**整份 500 行里只有 2 处用到它**：

* :func:`diff_lines` —— 对应 ``Diff.diffLines``（``edit-diff.ts:382``）
* :func:`create_two_files_patch` —— 对应 ``Diff.createTwoFilesPatch``（``edit-diff.ts:367``）

其余约 360 行（fuzzy 匹配、BOM、替换应用、四个错误消息）是纯文本处理，零 diff 依赖。
把这个事实写下来很重要：**它把本批的风险面从 500 行缩到 2 个函数。**

实现方式：行级 diff 用标准库 :class:`difflib.SequenceMatcher`（与 jsdiff 的
``diffLines`` 同类的行级算法），**不自己重写 diff 算法**；只在「opcode → jsdiff 的
part 序列」和「patch 文本格式」这两处做映射。

对拍证据
--------

``tests/test_edit_diff_parity.py`` 用真正的 ``diff@8.0.4`` 生成的 golden 语料逐条比对
（语料与生成脚本见 ``tests/parity/``，可重跑）：

* ``diff_lines``：小语料 25/26、大语料（100–1200 行）9/9
* ``create_two_files_patch``：**字节等价 34/35**；**``applyPatch`` 往返 35/35**（主验收）
* 唯一字节差异是「移动行」的对齐歧义，**其 patch 仍能被正确应用**

``autojunk``：显式传 ``False`` 关掉 difflib 的高频元素启发式。实测在本次语料上
``True``/``False`` 结果相同（9/9 与 9/9），但该启发式会**按文档改变**超过 200 元素序列的
结果，所以关掉它以免依赖运气。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

__all__ = [
    "NO_NEWLINE_MARKER",
    "DiffPart",
    "create_two_files_patch",
    "diff_lines",
    "split_lines_with_endings",
]

#: 缺失行尾标记，与 GNU diff / jsdiff 的写法一致。
NO_NEWLINE_MARKER = "\\ No newline at end of file\n"

#: 上游 ``edit-diff.ts:54`` 的 ``/[^\n]*\n|[^\n]+/g``：切行并**保留**行尾
#: （``\r\n`` 也在内），末尾无换行的最后一行也算一行。
_LINE_PATTERN = re.compile(r"[^\n]*\n|[^\n]+")


def split_lines_with_endings(content: str) -> list[str]:
    """``edit-diff.ts:53-55`` 的 ``splitLinesWithEndings``。

    ``"a\\nb"`` → ``["a\\n", "b"]``；``""`` → ``[]``。**保留** ``\\r\\n``。
    """
    return _LINE_PATTERN.findall(content)


@dataclass(frozen=True, slots=True)
class DiffPart:
    """``diffLines`` 的一段（jsdiff ``ChangeObject``）。"""

    value: str
    #: 属于新内容（新增或替换后的新文本）。
    added: bool
    #: 属于旧内容（删除或替换前的旧文本）。
    removed: bool
    #: 行数。
    count: int


def _count_lines(value: str) -> int:
    """jsdiff 的 ``count``：换行数，末尾无换行时最后一行也算。"""
    return value.count("\n") or (1 if value else 0)


def diff_lines(old_content: str, new_content: str) -> list[DiffPart]:
    """``Diff.diffLines(oldContent, newContent)``（``edit-diff.ts:382``）。

    返回**极大**的连续段：``equal`` / ``delete`` / ``insert`` 各成一段，
    ``replace`` 拆成「先 removed、后 added」两段——与 jsdiff 的输出形状一致。

    **已知差异**：当改动是「把若干行移动/交换到别处」时，对齐存在歧义，
    ``SequenceMatcher``（Ratcliff-Obershelp）与 jsdiff（Myers）可能选出不同的、
    但**同样合法**的编辑脚本。实测 35 条语料里 1 条如此（``interleaved``）。
    """
    old_lines = split_lines_with_endings(old_content)
    new_lines = split_lines_with_endings(new_content)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)

    parts: list[DiffPart] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            parts.append(DiffPart("".join(old_lines[i1:i2]), False, False, 0))
        elif tag == "delete":
            parts.append(DiffPart("".join(old_lines[i1:i2]), False, True, 0))
        elif tag == "insert":
            parts.append(DiffPart("".join(new_lines[j1:j2]), True, False, 0))
        else:  # replace → removed 段 + added 段
            parts.append(DiffPart("".join(old_lines[i1:i2]), False, True, 0))
            parts.append(DiffPart("".join(new_lines[j1:j2]), True, False, 0))

    return [DiffPart(p.value, p.added, p.removed, _count_lines(p.value)) for p in parts]


def _format_range(start: int, count: int) -> str:
    """统一 diff 的区间记法：``count == 0`` 时用 0-based 起点（``@@ -0,0 +1,2 @@``）。"""
    return f"{start if count == 0 else start + 1},{count}"


def _emit(prefix: str, value: str) -> str:
    """给一行加前缀；该行没有行尾时补 ``\\ No newline at end of file``。

    与 jsdiff 一致：**行尾原样保留**，所以 CRLF 文件产出的 patch 里是 ``\\r\\n``。
    """
    if value.endswith("\n"):
        return prefix + value
    return prefix + value + "\n" + NO_NEWLINE_MARKER


def create_two_files_patch(
    path: str,
    old_content: str,
    new_content: str,
    context: int = 4,
) -> str:
    """``Diff.createTwoFilesPatch(path, path, old, new, …, {context, headerOptions: FILE_HEADERS_ONLY})``。

    对应 ``edit-diff.ts:366-371``：``context`` 默认 4，``FILE_HEADERS_ONLY`` 表示
    只写 ``--- path\\n+++ path\\n`` 两行头，不带 ``Index:`` 也不带时间戳。

    **内容相同时只返回两行头**（jsdiff 对空 patch 的行为，实测 ``identical`` 与
    ``both-empty`` 两个语料都是这样）。
    """
    old_lines = split_lines_with_endings(old_content)
    new_lines = split_lines_with_endings(new_content)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)

    out: list[str] = [f"--- {path}\n", f"+++ {path}\n"]
    for group in matcher.get_grouped_opcodes(context):
        old_start, old_end = group[0][1], group[-1][2]
        new_start, new_end = group[0][3], group[-1][4]
        out.append(
            f"@@ -{_format_range(old_start, old_end - old_start)} "
            f"+{_format_range(new_start, new_end - new_start)} @@\n"
        )
        for tag, i1, i2, j1, j2 in group:
            # 四种 opcode 各自出哪些行必须写清楚——用 `!= "insert"` / `!= "delete"`
            # 这种否定写法会把 equal 的新侧也当成新增输出（曾踩过）。
            if tag == "equal":
                for line in old_lines[i1:i2]:
                    out.append(_emit(" ", line))
            elif tag == "delete":
                for line in old_lines[i1:i2]:
                    out.append(_emit("-", line))
            elif tag == "insert":
                for line in new_lines[j1:j2]:
                    out.append(_emit("+", line))
            else:  # replace：旧行与新行都要出
                for line in old_lines[i1:i2]:
                    out.append(_emit("-", line))
                for line in new_lines[j1:j2]:
                    out.append(_emit("+", line))
    return "".join(out)
