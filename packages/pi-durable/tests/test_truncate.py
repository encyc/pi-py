"""``pi_durable.truncate`` 的单元测试。

用例逐条对照上游 ``durable/test/env-truncate.test.ts``（100 行），
并补充 ``truncate_head_of`` 的契约与 JS ``toFixed(1)`` 的边界。
"""

from __future__ import annotations

import pytest

from pi_durable.truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    TruncationOptions,
    TruncationTotals,
    format_size,
    split_lines_for_counting,
    truncate_head,
    truncate_head_of,
    utf8_byte_length,
)


def test_defaults_match_upstream() -> None:
    assert DEFAULT_MAX_LINES == 2000
    assert DEFAULT_MAX_BYTES == 50 * 1024


# --- 对照上游 env-truncate.test.ts:13-21 ---------------------------------------


def test_reports_utf8_byte_counts() -> None:
    content = "aé🙂\nb"
    result = truncate_head(content, TruncationOptions(max_bytes=100, max_lines=10))

    assert result.truncated is False
    assert result.total_bytes == utf8_byte_length(content)
    assert result.output_bytes == utf8_byte_length(content)
    assert result.total_bytes == 9
    assert result.truncated_by is None


# --- 对照上游 env-truncate.test.ts:23-45（去掉 Buffer 缺失的运行时切换）---------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("ascii", 5),
        ("é", 2),
        ("中", 3),  # U+4E2D：BMP 且 > U+07FF → 3 字节
        ("\u0800", 3),
        ("\uffff", 3),
        ("🙂", 4),
        ("\ud83d", 3),  # 孤立高代理 → U+FFFD，3 字节
        ("\ude42", 3),  # 孤立低代理 → U+FFFD，3 字节
        ("a\ud83d\ude42b", 6),  # 未合并的代理对 → 按一个 4 字节字符算
        ("\u07ff", 2),
        ("\u07ff\u0800\uffff", 2 + 3 + 3),
    ],
)
def test_utf8_byte_length_matches_upstream(text: str, expected: int) -> None:
    """输入取自上游 ``env-truncate.test.ts:24``。

    那条用例（``env-truncate.test.ts:23-45``）断言长度等于
    ``new TextEncoder().encode(content).length``，即**真实 UTF-8 长度**。
    """
    assert utf8_byte_length(text) == expected


def test_truncate_head_counts_cjk_as_three_bytes() -> None:
    """CJK 按 3 字节计：30 字节上限下，10 个「中」正好 30 字节 → 不截断。"""
    result = truncate_head("中" * 10, TruncationOptions(max_bytes=30, max_lines=100))

    assert result.truncated is False
    assert result.total_bytes == 30


def test_truncate_head_counts_astral_plane_as_four_bytes() -> None:
    """非 BMP 字符（Python 里是**单个**码点）按 4 字节计。"""
    # 5 个 🙂 = 20 字节；上限 19 → 撞字节上限，但首行就超限 → 空内容。
    result = truncate_head("🙂" * 5, TruncationOptions(max_bytes=19, max_lines=100))

    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert result.first_line_exceeds_limit is True
    assert result.total_bytes == 20


def test_head_without_partial_lines_and_byte_reporting() -> None:
    # 上游 `utf8-byte-length-without-buffer` 断言 head 为 {content:"aé🙂", outputBytes:7, truncatedBy:"bytes"}
    result = truncate_head("aé🙂\nb", TruncationOptions(max_bytes=7, max_lines=10))

    assert result.content == "aé🙂"
    assert result.output_bytes == 7
    assert result.truncated_by == "bytes"


# --- 对照上游 env-truncate.test.ts:47-52 ---------------------------------------


def test_trailing_newline_is_not_an_extra_line() -> None:
    content = "\n".join(["line"] * 3) + "\n"
    head = truncate_head(content, TruncationOptions(max_bytes=100, max_lines=3))

    assert head.truncated is False
    assert head.total_lines == 3
    assert head.output_lines == 3


# --- 对照上游 env-truncate.test.ts:54-63 ---------------------------------------


def test_truncates_head_by_line_limit() -> None:
    result = truncate_head("one\ntwo\nthree\nfour", TruncationOptions(max_bytes=100, max_lines=2))

    assert result.content == "one\ntwo"
    assert result.truncated is True
    assert result.truncated_by == "lines"
    assert result.total_lines == 4
    assert result.output_lines == 2


# --- 对照上游 env-truncate.test.ts:65-73 ---------------------------------------


def test_reports_bytes_when_only_trailing_newline_exceeds_at_line_cap() -> None:
    result = truncate_head("hello\nworld\n", TruncationOptions(max_bytes=11, max_lines=2))

    assert result.content == "hello\nworld"
    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert result.total_lines == 2
    assert result.output_lines == 2


# --- 对照上游 env-truncate.test.ts:75-84 ---------------------------------------


def test_truncates_on_utf8_byte_limit_without_partial_lines() -> None:
    result = truncate_head("éé\nabc", TruncationOptions(max_bytes=4, max_lines=10))

    assert result.content == "éé"
    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert result.output_bytes == 4
    assert result.first_line_exceeds_limit is False


# --- 对照上游 env-truncate.test.ts:86-93 ---------------------------------------


def test_first_line_exceeding_byte_limit_yields_empty_content() -> None:
    result = truncate_head("éé\nabc", TruncationOptions(max_bytes=3, max_lines=10))

    assert result.content == ""
    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert result.first_line_exceeds_limit is True
    assert result.output_lines == 0


# --- 对照上游 env-truncate.test.ts:95-99 ---------------------------------------


def test_formats_sizes() -> None:
    assert format_size(1023) == "1023B"
    assert format_size(1536) == "1.5KB"
    assert format_size(3 * 1024 * 1024) == "3.0MB"


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [
        (0, "0B"),
        (1, "1B"),
        (1024, "1.0KB"),
        (1024 * 1024, "1.0MB"),
        (1536 * 1024, "1.5MB"),
    ],
)
def test_format_size_boundaries(num_bytes: int, expected: str) -> None:
    assert format_size(num_bytes) == expected


def test_format_size_uses_js_to_fixed_rounding() -> None:
    """JS ``toFixed(1)`` 在并列点上「取更大的 n」，Python ``f"{x:.1f}"`` 不是。

    1024 * 1.25 = 1280 字节 → 1.25KB。JS 给 ``"1.3"``，``f"{1.25:.1f}"`` 给 ``"1.2"``。
    """
    assert format_size(1280) == "1.3KB"
    assert f"{1280 / 1024:.1f}" == "1.2"  # 记录分叉，避免有人「顺手」改回 f-string


# --- split_lines_for_counting ---------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", []),
        ("a", ["a"]),
        ("a\n", ["a"]),
        ("a\nb", ["a", "b"]),
        ("a\n\n", ["a", ""]),
        ("\n", [""]),
    ],
)
def test_split_lines_for_counting(content: str, expected: list[str]) -> None:
    assert split_lines_for_counting(content) == expected


# --- truncate_head_of 契约（truncate.ts:86-89）----------------------------------


def test_truncate_head_of_equals_truncate_head_on_whole_text() -> None:
    content = "one\ntwo\nthree\nfour\nfive"
    options = TruncationOptions(max_bytes=12, max_lines=3)

    direct = truncate_head(content, options)
    via_prefix = truncate_head_of(
        content,
        TruncationTotals(
            lines=len(split_lines_for_counting(content)), bytes=utf8_byte_length(content)
        ),
        options,
    )

    assert direct == via_prefix


def test_truncate_head_of_uses_supplied_totals_not_prefix_length() -> None:
    """``totals`` 是整段文本的总量；``prefix`` 只是它的一个前缀。

    用前缀 + 真实总量，结果必须与对整段文本调用一致——这正是该 API 存在的理由。
    """
    whole = "alpha\nbeta\ngamma\ndelta"
    prefix = "alpha\nbeta"
    options = TruncationOptions(max_bytes=100, max_lines=2)

    result = truncate_head_of(
        prefix,
        TruncationTotals(lines=4, bytes=utf8_byte_length(whole)),
        options,
    )

    assert result.content == "alpha\nbeta"
    assert result.truncated is True
    assert result.truncated_by == "lines"
    assert result.total_lines == 4
    assert result.total_bytes == utf8_byte_length(whole)


def test_truncate_head_of_empty_prefix_does_not_raise() -> None:
    """上游在 ``lines`` 为空时会读 ``lines[0]``（``truncate.ts:120``）并抛 TypeError。

    这是契约外的输入；Python 侧定为「无首行 = 0 字节」，返回空内容而不是抛异常。
    """
    result = truncate_head_of("", TruncationTotals(lines=5000, bytes=100), TruncationOptions())

    assert result.content == ""
    assert result.truncated is True
    assert result.truncated_by == "lines"
    assert result.first_line_exceeds_limit is False


def test_explicit_zero_max_bytes_is_not_replaced_by_default() -> None:
    """``None`` 才算「用默认值」；显式 0 是合法上限（对应 JS 的 ``??`` 而非 ``||``）。"""
    result = truncate_head("abc", TruncationOptions(max_bytes=0, max_lines=10))

    assert result.max_bytes == 0
    assert result.content == ""
    assert result.first_line_exceeds_limit is True


def test_none_options_use_defaults() -> None:
    result = truncate_head("hello")

    assert result.max_lines == DEFAULT_MAX_LINES
    assert result.max_bytes == DEFAULT_MAX_BYTES
    assert result.truncated is False
