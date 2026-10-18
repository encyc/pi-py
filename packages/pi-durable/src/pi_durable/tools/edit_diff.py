"""编辑工具的 diff 与模糊匹配工具 —— 移植 ``durable/src/tools/edit-diff.ts``（500 行）。

整份文件里**只有 2 处**依赖 npm ``diff`` 包，其余约 360 行是纯文本处理：

======================  ==========================================  ==========================
上游函数                 作用                                        依赖
======================  ==========================================  ==========================
``generateUnifiedPatch``  ``edit-diff.ts:366-371``                    ``Diff.createTwoFilesPatch``
``generateDiffString``    ``edit-diff.ts:377-500``                    ``Diff.diffLines``
其余 10 个函数            fuzzy 匹配 / BOM / 替换应用 / 错误消息         **零 diff 依赖**
======================  ==========================================  ==========================

两个 diff 原语由 :mod:`pi_durable.tools.jsdiff` 提供（stdlib 实现，已与真
``diff@8.0.4`` 对拍，见那个模块的文档）。

错误消息逐字保留——上游测试用正则断言它们
（``test/tools.test.ts:400,407,410``：``/overlap/``、``/Could not find the exact text/``、
``/Found 3 occurrences/``）。
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from pi_durable.tools.jsdiff import create_two_files_patch, diff_lines

__all__ = [
    "AppliedEditsResult",
    "DiffStringResult",
    "Edit",
    "FuzzyMatchResult",
    "LineSpan",
    "TextReplacement",
    "apply_edits_to_normalized_content",
    "apply_replacements_preserving_unchanged_lines",
    "detect_line_ending",
    "fuzzy_find_text",
    "generate_diff_string",
    "generate_unified_patch",
    "normalize_for_fuzzy_match",
    "normalize_to_lf",
    "prepare_edit_arguments",
    "restore_line_endings",
    "split_lines_with_endings",
    "strip_bom",
]


# --------------------------------------------------------------------------------------
# 行尾与归一化（edit-diff.ts:7-55）
# --------------------------------------------------------------------------------------


def detect_line_ending(content: str) -> str:
    """内容用的是 ``\\r\\n`` 还是 ``\\n``（``edit-diff.ts:7-12``）。

    取**第一个**出现的换行符所属的类型；没有换行时是 ``\\n``。
    """
    crlf_index = content.find("\r\n")
    lf_index = content.find("\n")
    if lf_index == -1:
        return "\n"
    if crlf_index == -1:
        return "\n"
    return "\r\n" if crlf_index < lf_index else "\n"


def normalize_to_lf(text: str) -> str:
    """把所有行尾折成 ``\\n``（``edit-diff.ts:14-16``）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, ending: str) -> str:
    """把 ``\\n`` 还原成原本的行尾（``edit-diff.ts:18-20``）。"""
    return text.replace("\n", "\r\n") if ending == "\r\n" else text


#: 智能单引号 → ``'``（``edit-diff.ts:37``）。
_SMART_SINGLE = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'"})
#: 智能双引号 → ``"``（``edit-diff.ts:41``）。
_SMART_DOUBLE = str.maketrans({"\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"'})
#: 各种连字符/破折号 → ``-``（``edit-diff.ts:45``）。
_DASHES = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)
#: 特殊空白 → 普通空格（``edit-diff.ts:49``）。
_SPACES = str.maketrans(
    {chr(code): " " for code in (0x00A0, *range(0x2002, 0x200B), 0x202F, 0x205F, 0x3000)}
)


def normalize_for_fuzzy_match(text: str) -> str:
    """模糊匹配前的归一化（``edit-diff.ts:27-51``）。

    顺序与上游一致：``NFKC`` → 逐行去尾空白 → 智能引号 → 连字符 → 特殊空白。
    **NFKC 在前**很关键：它会把全角字符、兼容字符先折成标准形式。
    """
    normalized = unicodedata.normalize("NFKC", text)
    stripped = "\n".join(line.rstrip() for line in normalized.split("\n"))
    return (
        stripped.translate(_SMART_SINGLE)
        .translate(_SMART_DOUBLE)
        .translate(_DASHES)
        .translate(_SPACES)
    )


def split_lines_with_endings(content: str) -> list[str]:
    """``edit-diff.ts:53-55``；由 :mod:`pi_durable.tools.jsdiff` 提供同一实现。"""
    from pi_durable.tools.jsdiff import split_lines_with_endings as _impl

    return _impl(content)


# --------------------------------------------------------------------------------------
# 替换应用（edit-diff.ts:57-169）
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineSpan:
    """一行在字符串里的半开区间 ``[start, end)``（``edit-diff.ts:57-60``）。"""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class TextReplacement:
    """一次替换：在 base 里的起点、长度，以及新文本（``edit-diff.ts:69``）。"""

    match_index: int
    match_length: int
    new_text: str


@dataclass(frozen=True, slots=True)
class _MatchedEdit(TextReplacement):
    """``edit-diff.ts:62-67``：额外记住它来自第几个 edit，用于错误消息。"""

    edit_index: int = 0


@dataclass(frozen=True, slots=True)
class _Group:
    """一串互相重叠、因而必须一起重写的行（``edit-diff.ts:139``）。"""

    start_line: int
    end_line: int
    replacements: tuple[TextReplacement, ...]


def _get_line_spans(content: str) -> list[LineSpan]:
    """每行的半开区间（``edit-diff.ts:71-78``）。"""
    spans: list[LineSpan] = []
    offset = 0
    for line in split_lines_with_endings(content):
        spans.append(LineSpan(offset, offset + len(line)))
        offset += len(line)
    return spans


def _get_replacement_line_range(
    lines: Sequence[LineSpan], replacement: TextReplacement
) -> tuple[int, int]:
    """这次替换覆盖到第几行到第几行（``edit-diff.ts:80-105``）。

    返回 ``[start_line, end_line)``。替换区间落在内容之外时抛错。
    """
    replacement_start = replacement.match_index
    replacement_end = replacement.match_index + replacement.match_length

    start_line = -1
    for index, line in enumerate(lines):
        if line.start <= replacement_start < line.end:
            start_line = index
            break
    if start_line == -1:
        raise ValueError("Replacement range is outside the base content.")

    end_line = start_line
    while end_line < len(lines) and lines[end_line].end < replacement_end:
        end_line += 1
    if end_line >= len(lines):
        raise ValueError("Replacement range is outside the base content.")

    return start_line, end_line + 1


def _apply_replacements(
    content: str, replacements: Sequence[TextReplacement], offset: int = 0
) -> str:
    """**逆序**应用替换，这样前面的偏移不会失效（``edit-diff.ts:107-116``）。"""
    result = content
    for replacement in reversed(list(replacements)):
        match_index = replacement.match_index - offset
        result = (
            result[:match_index]
            + replacement.new_text
            + result[match_index + replacement.match_length :]
        )
    return result


def apply_replacements_preserving_unchanged_lines(
    original_content: str,
    base_content: str,
    replacements: Sequence[TextReplacement],
) -> str:
    """把针对 ``base_content`` 的替换应用到 ``original_content``，**未改动的行原样保留**
    （``edit-diff.ts:118-169``）。

    用在 base 是原文的归一化视图时：每次替换先**加宽到它实际触碰的整行**，这些行从归一化
    base 重写，其余行从原文拷回。用真实替换区间驱动保留，所以重复的归一化行不会被对错位置。
    """
    original_lines = split_lines_with_endings(original_content)
    base_lines = _get_line_spans(base_content)
    if len(original_lines) != len(base_lines):
        raise ValueError(
            "Cannot preserve unchanged lines because the base content has a different line count."
        )

    groups: list[_Group] = []
    for replacement in sorted(replacements, key=lambda item: item.match_index):
        start_line, end_line = _get_replacement_line_range(base_lines, replacement)
        current = groups[-1] if groups else None
        if current is not None and start_line < current.end_line:
            groups[-1] = _Group(
                current.start_line,
                max(current.end_line, end_line),
                (*current.replacements, replacement),
            )
            continue
        groups.append(_Group(start_line, end_line, (replacement,)))

    original_line_index = 0
    parts: list[str] = []
    for group in groups:
        parts.append("".join(original_lines[original_line_index : group.start_line]))
        group_start = base_lines[group.start_line].start
        group_end = base_lines[group.end_line - 1].end
        parts.append(
            _apply_replacements(
                base_content[group_start:group_end], group.replacements, group_start
            )
        )
        original_line_index = group.end_line
    parts.append("".join(original_lines[original_line_index:]))
    return "".join(parts)


# --------------------------------------------------------------------------------------
# 模糊查找（edit-diff.ts:171-252）
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FuzzyMatchResult:
    """``fuzzy_find_text`` 的结果（``edit-diff.ts:171-185``）。"""

    found: bool
    #: 匹配起点——**在应当用于替换的那份内容里**的偏移。
    index: int
    match_length: int
    #: 是否用了模糊匹配（``False`` 表示精确匹配）。
    used_fuzzy_match: bool
    #: 应据以做替换的内容：精确匹配时是原文，模糊匹配时是归一化后的文本。
    content_for_replacement: str


@dataclass(frozen=True, slots=True)
class Edit:
    """一次精确文本替换（``edit-diff.ts:187-190``）。"""

    old_text: str
    new_text: str


@dataclass(frozen=True, slots=True)
class AppliedEditsResult:
    """``apply_edits_to_normalized_content`` 的结果（``edit-diff.ts:192-195``）。"""

    base_content: str
    new_content: str


def fuzzy_find_text(content: str, old_text: str) -> FuzzyMatchResult:
    """先精确找，再模糊找（``edit-diff.ts:197-241``）。

    模糊匹配**全程在归一化空间里做**：返回的 ``index`` 是归一化内容里的偏移，
    调用方据此算替换，再决定归一化结果里有多少该写回原文。
    """
    exact_index = content.find(old_text)
    if exact_index != -1:
        return FuzzyMatchResult(True, exact_index, len(old_text), False, content)

    fuzzy_content = normalize_for_fuzzy_match(content)
    fuzzy_old_text = normalize_for_fuzzy_match(old_text)
    fuzzy_index = fuzzy_content.find(fuzzy_old_text)
    if fuzzy_index == -1:
        return FuzzyMatchResult(False, -1, 0, False, content)

    return FuzzyMatchResult(True, fuzzy_index, len(fuzzy_old_text), True, fuzzy_content)


@dataclass(frozen=True, slots=True)
class BomResult:
    """``strip_bom`` 的结果（``edit-diff.ts:244-246``）。"""

    bom: str
    text: str


def strip_bom(content: str) -> BomResult:
    """去掉开头的 UTF-8 BOM，并把 BOM 与去掉后的文本一起返回（``edit-diff.ts:243-246``）。"""
    if content.startswith("\ufeff"):
        return BomResult("\ufeff", content[1:])
    return BomResult("", content)


def _count_occurrences(content: str, old_text: str) -> int:
    """在**归一化空间**里数出现次数（``edit-diff.ts:248-252``）。

    上游是 ``fuzzyContent.split(fuzzyOldText).length - 1``。Python 要写成
    ``len(split(...)) - 1``——**不能**用 ``list.count("")``，那数的是空元素个数，
    对 ``"foo foo foo".split("foo")`` 会得到 2 而不是 3。
    """
    fuzzy_content = normalize_for_fuzzy_match(content)
    fuzzy_old_text = normalize_for_fuzzy_match(old_text)
    if fuzzy_old_text == "":
        return 0
    return len(fuzzy_content.split(fuzzy_old_text)) - 1


# --------------------------------------------------------------------------------------
# 错误消息：逐字保留（edit-diff.ts:254-290）
# --------------------------------------------------------------------------------------


def _not_found_error(path: str, edit_index: int, total_edits: int) -> ValueError:
    if total_edits == 1:
        return ValueError(
            f"Could not find the exact text in {path}. "
            "The old text must match exactly including all whitespace and newlines."
        )
    return ValueError(
        f"Could not find edits[{edit_index}] in {path}. "
        "The oldText must match exactly including all whitespace and newlines."
    )


def _duplicate_error(path: str, edit_index: int, total_edits: int, occurrences: int) -> ValueError:
    if total_edits == 1:
        return ValueError(
            f"Found {occurrences} occurrences of the text in {path}. "
            "The text must be unique. Please provide more context to make it unique."
        )
    return ValueError(
        f"Found {occurrences} occurrences of edits[{edit_index}] in {path}. "
        "Each oldText must be unique. Please provide more context to make it unique."
    )


def _empty_old_text_error(path: str, edit_index: int, total_edits: int) -> ValueError:
    if total_edits == 1:
        return ValueError(f"oldText must not be empty in {path}.")
    return ValueError(f"edits[{edit_index}].oldText must not be empty in {path}.")


def _no_change_error(path: str, total_edits: int) -> ValueError:
    if total_edits == 1:
        return ValueError(
            f"No changes made to {path}. The replacement produced identical content. "
            "This might indicate an issue with special characters or the text not existing as expected."
        )
    return ValueError(f"No changes made to {path}. The replacements produced identical content.")


def apply_edits_to_normalized_content(
    normalized_content: str,
    edits: Sequence[Edit],
    path: str,
) -> AppliedEditsResult:
    """对 LF 归一化后的内容施加一组精确文本替换（``edit-diff.ts:292-363``）。

    所有 edit 都**针对同一份原始内容**匹配，然后逆序应用，偏移才稳定。只要有一个 edit 需要
    模糊匹配，整次操作就在归一化空间里跑，再把行级改动叠回原文，未改动的行保持原字节。
    """
    normalized_edits = [
        Edit(normalize_to_lf(e.old_text), normalize_to_lf(e.new_text)) for e in edits
    ]

    for index, edit in enumerate(normalized_edits):
        if len(edit.old_text) == 0:
            raise _empty_old_text_error(path, index, len(normalized_edits))

    initial_matches = [
        fuzzy_find_text(normalized_content, edit.old_text) for edit in normalized_edits
    ]
    used_fuzzy_match = any(match.used_fuzzy_match for match in initial_matches)
    replacement_base = (
        normalize_for_fuzzy_match(normalized_content) if used_fuzzy_match else normalized_content
    )

    matched: list[_MatchedEdit] = []
    for index, edit in enumerate(normalized_edits):
        match = fuzzy_find_text(replacement_base, edit.old_text)
        if not match.found:
            raise _not_found_error(path, index, len(normalized_edits))

        occurrences = _count_occurrences(replacement_base, edit.old_text)
        if occurrences > 1:
            raise _duplicate_error(path, index, len(normalized_edits), occurrences)

        matched.append(
            _MatchedEdit(
                match_index=match.index,
                match_length=match.match_length,
                new_text=edit.new_text,
                edit_index=index,
            )
        )

    matched.sort(key=lambda item: item.match_index)
    for previous, current in zip(matched, matched[1:], strict=False):
        if previous.match_index + previous.match_length > current.match_index:
            raise ValueError(
                f"edits[{previous.edit_index}] and edits[{current.edit_index}] overlap in {path}. "
                "Merge them into one edit or target disjoint regions."
            )

    base_content = normalized_content
    new_content = (
        apply_replacements_preserving_unchanged_lines(normalized_content, replacement_base, matched)
        if used_fuzzy_match
        else _apply_replacements(replacement_base, matched)
    )

    if base_content == new_content:
        raise _no_change_error(path, len(normalized_edits))

    return AppliedEditsResult(base_content, new_content)


# --------------------------------------------------------------------------------------
# diff 输出（edit-diff.ts:365-500）
# --------------------------------------------------------------------------------------


def generate_unified_patch(
    path: str,
    old_content: str,
    new_content: str,
    context_lines: int = 4,
) -> str:
    """标准统一 patch（``edit-diff.ts:365-371``）。

    上游传 ``context: 4`` 与 ``headerOptions: FILE_HEADERS_ONLY``，即只写
    ``--- path`` / ``+++ path`` 两行头，不带 ``Index:`` 与时间戳。
    """
    return create_two_files_patch(path, old_content, new_content, context_lines)


@dataclass(frozen=True, slots=True)
class DiffStringResult:
    """``generate_diff_string`` 的结果（``edit-diff.ts:377-381``）。"""

    diff: str
    #: 新文件里第一处改动的行号；没有改动时 ``None``。
    first_changed_line: int | None


def generate_diff_string(
    old_content: str,
    new_content: str,
    context_lines: int = 4,
) -> DiffStringResult:
    """带行号与上下文的、面向展示的 diff（``edit-diff.ts:377-500``）。

    格式（每行前缀一个字符）：

    * ``+{新行号} {内容}`` —— 新增
    * ``-{旧行号} {内容}`` —— 删除
    * `` {行号} {内容}`` —— 上下文
    * `` {空格} ...`` —— 省略了若干行上下文

    行号宽度按两侧最大行数取；上下文**只在改动前后各保留 ``context_lines`` 行**
    （紧邻改动的那一段最多保留 ``2 * context_lines`` 行）。
    """
    parts = diff_lines(old_content, new_content)
    output: list[str] = []

    old_lines = old_content.split("\n")
    new_lines = new_content.split("\n")
    max_line_num = max(len(old_lines), len(new_lines))
    line_num_width = len(str(max_line_num))

    old_line_num = 1
    new_line_num = 1
    last_was_change = False
    first_changed_line: int | None = None

    for index, part in enumerate(parts):
        raw = part.value.split("\n")
        if raw and raw[-1] == "":
            raw.pop()

        if part.added or part.removed:
            if first_changed_line is None:
                first_changed_line = new_line_num
            for line in raw:
                if part.added:
                    output.append(f"+{str(new_line_num).rjust(line_num_width)} {line}")
                    new_line_num += 1
                else:
                    output.append(f"-{str(old_line_num).rjust(line_num_width)} {line}")
                    old_line_num += 1
            last_was_change = True
        else:
            next_part_is_change = index < len(parts) - 1 and (
                parts[index + 1].added or parts[index + 1].removed
            )
            if last_was_change and next_part_is_change:
                if len(raw) <= context_lines * 2:
                    for line in raw:
                        output.append(f" {str(old_line_num).rjust(line_num_width)} {line}")
                        old_line_num += 1
                        new_line_num += 1
                else:
                    leading = raw[:context_lines]
                    trailing = raw[len(raw) - context_lines :]
                    skipped = len(raw) - len(leading) - len(trailing)
                    for line in leading:
                        output.append(f" {str(old_line_num).rjust(line_num_width)} {line}")
                        old_line_num += 1
                        new_line_num += 1
                    output.append(f" {' ' * line_num_width} ...")
                    old_line_num += skipped
                    new_line_num += skipped
                    for line in trailing:
                        output.append(f" {str(old_line_num).rjust(line_num_width)} {line}")
                        old_line_num += 1
                        new_line_num += 1
            elif last_was_change:
                shown = raw[:context_lines]
                skipped = len(raw) - len(shown)
                for line in shown:
                    output.append(f" {str(old_line_num).rjust(line_num_width)} {line}")
                    old_line_num += 1
                    new_line_num += 1
                if skipped > 0:
                    output.append(f" {' ' * line_num_width} ...")
                    old_line_num += skipped
                    new_line_num += skipped
            elif next_part_is_change:
                skipped = max(0, len(raw) - context_lines)
                if skipped > 0:
                    output.append(f" {' ' * line_num_width} ...")
                    old_line_num += skipped
                    new_line_num += skipped
                for line in raw[skipped:]:
                    output.append(f" {str(old_line_num).rjust(line_num_width)} {line}")
                    old_line_num += 1
                    new_line_num += 1
            else:
                old_line_num += len(raw)
                new_line_num += len(raw)

            last_was_change = False

    return DiffStringResult("\n".join(output), first_changed_line)


# --------------------------------------------------------------------------------------
# 参数修复（edit.ts:39-71，随 edit 工具一起来）
# --------------------------------------------------------------------------------------


def _is_single_edit_input(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("oldText"), str)
        and isinstance(value.get("newText"), str)
    )


def prepare_edit_arguments(raw: object) -> object:
    """修复模型常见的几种参数形状（``edit.ts:45-71``）。

    处理：``edits`` 是 JSON 字符串、``edits`` 是单个 edit 对象、以及顶层直接给
    ``oldText``/``newText``。**在副本上操作**，调用方的实参不被改动。
    """
    if not isinstance(raw, dict):
        return raw
    args: dict[str, object] = dict(raw)
    edits = args.get("edits")
    if isinstance(edits, str):
        try:
            parsed = json.loads(edits)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            args["edits"] = parsed
        elif _is_single_edit_input(parsed):
            args["edits"] = [parsed]
    elif _is_single_edit_input(edits):
        args["edits"] = [edits]

    old_text = args.get("oldText")
    new_text = args.get("newText")
    if not isinstance(old_text, str) or not isinstance(new_text, str):
        return args

    existing = args.get("edits")
    merged: list[object] = list(existing) if isinstance(existing, list) else []
    merged.append({"oldText": old_text, "newText": new_text})
    rest = {key: value for key, value in args.items() if key not in ("oldText", "newText")}
    return {**rest, "edits": merged}
