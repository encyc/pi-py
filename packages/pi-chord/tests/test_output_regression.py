"""**回归测试**：``pi_chord.output.character_end`` / ``utf8_byte_length`` 的既有契约。

这两个函数**已被 `pi-durable` 使用**（`durable/src/truncate.py` 直接
``from pi_chord.output import utf8_byte_length``，`tools/read.py` 用 ``character_end``）。
本模块把它们的**签名与语义**钉死：任何改动都会在这里失败，从而在破坏下游之前被拦住。

覆盖范围是 task-15 交付时的那两个函数（`output.ts:101-106` / `:275-294`），
**不含** task-21 新增的 ``bound_output`` / ``OutputBuffer`` / ``Progress``
（那些在 ``test_output.py``）。
"""

from __future__ import annotations

import inspect

import pytest

from pi_chord.output import character_end, utf8_byte_length

# ─── 签名冻结点 ──────────────────────────────────────────────────────────────


def test_character_end_signature_is_unchanged() -> None:
    """签名是下游的接口契约。"""
    signature = inspect.signature(character_end)
    assert list(signature.parameters) == ["data", "index"]
    assert signature.return_annotation in ("int", int)
    for name in ("data", "index"):
        assert signature.parameters[name].default is inspect.Parameter.empty


def test_utf8_byte_length_signature_is_unchanged() -> None:
    signature = inspect.signature(utf8_byte_length)
    assert list(signature.parameters) == ["content"]
    assert signature.return_annotation in ("int", int)
    assert signature.parameters["content"].default is inspect.Parameter.empty


def test_both_are_still_exported_from_the_module() -> None:
    from pi_chord import output

    assert "character_end" in output.__all__
    assert "utf8_byte_length" in output.__all__


# ─── character_end 语义冻结点 ────────────────────────────────────────────────


EMOJI = "\U0001f600"  # U+1F600 -> F0 9F 98 80
E_ACUTE = "\u00e9"  # U+00E9  -> C3 A9
CJK = "\u4e2d"  # U+4E2D  -> E4 B8 AD


def test_character_end_ascii_is_identity() -> None:
    data = b"hello"
    for index in range(len(data) + 1):
        assert character_end(data, index) == index


def test_character_end_backs_up_over_continuation_bytes() -> None:
    data = E_ACUTE.encode("utf-8")  # C3 A9
    assert character_end(data, 2) == 2
    assert character_end(data, 1) == 0


def test_character_end_inside_a_four_byte_character() -> None:
    data = EMOJI.encode("utf-8")  # F0 9F 98 80
    for index in (1, 2, 3):
        assert character_end(data, index) == 0
    assert character_end(data, 4) == 4


def test_character_end_accepts_bytes_and_bytearray() -> None:
    """下游可能传 ``bytes``；``bytearray`` 也必须继续工作。"""
    assert character_end(b"abc", 3) == 3
    assert character_end(bytearray(b"abc"), 3) == 3
    assert character_end(bytearray(E_ACUTE.encode()), 1) == 0


def test_character_end_edge_indexes() -> None:
    assert character_end(b"", 0) == 0
    assert character_end(b"abc", 0) == 0
    # 越界读按 0 处理（JS ``bytes[index] ?? 0``），因此不算续字节
    assert character_end(b"abc", 99) == 99


def test_character_end_never_splits_a_character() -> None:
    """它的用途：把字节窗口末端回退到不会切断字符的位置。"""
    data = ("hello " + EMOJI + " world").encode("utf-8")
    cut = data.index(b"\xf0") + 1
    assert data[: character_end(data, cut)].decode("utf-8") == "hello "


# ─── utf8_byte_length 语义冻结点 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", 0),
        ("abc", 3),
        (E_ACUTE, 2),
        (CJK, 3),
        (EMOJI, 4),
        ("a" + EMOJI + "b", 6),
        ("hello " + EMOJI + " world", 16),
    ],
)
def test_utf8_byte_length_matches_real_utf8(text: str, expected: int) -> None:
    assert utf8_byte_length(text) == expected
    assert utf8_byte_length(text) == len(text.encode("utf-8"))


def test_utf8_byte_length_lone_surrogate_counts_three() -> None:
    """上游 ``output.ts:275``：*a lone surrogate counts as U+FFFD, three bytes*。"""
    assert utf8_byte_length("\ud800") == 3
    assert utf8_byte_length("a\udc00b") == 5


def test_utf8_byte_length_merges_unpaired_surrogates() -> None:
    """未合并的代理对按一个 emoji 计 4 字节（与 JS 逐码元语义一致）。"""
    unmerged = "\ud83d\ude00"
    assert len(unmerged) == 2
    assert utf8_byte_length(unmerged) == 4
    assert utf8_byte_length(unmerged) == utf8_byte_length(EMOJI)


def test_utf8_byte_length_of_bmp_is_three_not_four() -> None:
    """**task-15 修过的那个缺陷**：U+0800..U+FFFF 是 3 字节，不是 4。

    这条测试的存在就是为了防止它回归 —— 它曾让 CJK 多算一个字节，
    而 ``pi-durable`` 的 ``truncate.py`` 直接依赖这个计数。
    """
    for char in ["\u0800", "\u4e2d", "\uffff", "\u00e9"]:
        expected = len(char.encode("utf-8"))
        assert utf8_byte_length(char) == expected, hex(ord(char))


def test_utf8_byte_length_is_not_utf16_length() -> None:
    """守住「别把这两个计数混用」。"""
    from pi_chord.delta import u16_len

    assert u16_len(EMOJI) == 2
    assert utf8_byte_length(EMOJI) == 4
    assert u16_len(CJK) == 1
    assert utf8_byte_length(CJK) == 3


# ─── 下游实际用法冻结点 ──────────────────────────────────────────────────────


def test_pi_durable_truncate_import_path_still_works() -> None:
    """``pi-durable`` 的 ``truncate.py`` 就是这一行导入。"""
    from pi_chord.output import utf8_byte_length as imported

    assert imported is utf8_byte_length


def test_truncation_workflow_used_by_downstream() -> None:
    """下游的典型组合：先算总字节，再在字节窗口里回退到字符边界。"""
    text = "line one\nline two " + EMOJI + " end\n"
    data = text.encode("utf-8")
    assert utf8_byte_length(text) == len(data)
    window = 10
    start = character_end(data, len(data) - window)
    kept = data[start:]
    # 切片能干净解码，且一定是原文的后缀
    decoded = kept.decode("utf-8")
    assert text.endswith(decoded)
    # 只可能比窗口多出「被回退掉的那一个字符」的字节数
    assert len(kept) <= window + 4
