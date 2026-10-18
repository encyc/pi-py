"""``pi_chord.output`` 的 UTF-8 字节/字符边界工具。

对照上游 ``chord/src/output.ts``:
- ``characterEnd``   — 第 101-106 行
- ``utf8ByteLength`` — 第 275-294 行

⚠️ 这两个函数处理的是 **UTF-8 字节**，与 :mod:`pi_chord.delta.textdiff` 的 UTF-16
码元计数是两件不同的事。测试分开写就是为了防止混用。
"""

from __future__ import annotations

import pytest

from pi_chord.output import character_end, utf8_byte_length

EMOJI = "\U0001f600"  # U+1F600 😀 -> UTF-8: F0 9F 98 80
E_ACUTE = "\u00e9"  # U+00E9 é  -> UTF-8: C3 A9
CJK = "\u4e2d"  # U+4E2D 中 -> UTF-8: E4 B8 AD


def test_character_end_ascii_is_identity() -> None:
    data = b"hello"
    for index in range(len(data) + 1):
        assert character_end(data, index) == index


def test_character_end_backs_up_to_character_start() -> None:
    data = E_ACUTE.encode("utf-8")  # C3 A9
    assert len(data) == 2
    assert character_end(data, 2) == 2  # 末尾永远是边界
    assert character_end(data, 1) == 0  # 落在续字节上 -> 回到字符首


def test_character_end_inside_four_byte_character() -> None:
    data = EMOJI.encode("utf-8")  # F0 9F 98 80
    assert len(data) == 4
    for index in (1, 2, 3):
        assert character_end(data, index) == 0
    assert character_end(data, 4) == 4


def test_character_end_mixed_text() -> None:
    data = ("a" + E_ACUTE + CJK + EMOJI).encode("utf-8")
    # 逐字节回退到各自字符的起始处
    boundaries = {0, 1, 3, 6, 10}
    for index in range(len(data) + 1):
        assert character_end(data, index) in boundaries
    assert character_end(data, 2) == 1  # é 的续字节
    assert character_end(data, 5) == 3  # 中的续字节
    assert character_end(data, 9) == 6  # 😀 的续字节


def test_character_end_edge_indexes() -> None:
    assert character_end(b"", 0) == 0
    assert character_end(b"abc", 0) == 0
    # 越界读按 0 处理（上游 ``bytes[index] ?? 0``），因此不算续字节
    assert character_end(b"abc", 99) == 99
    assert character_end(bytearray(b"abc"), 3) == 3


def test_character_end_never_splits_a_character() -> None:
    """它的用途：把一个字节窗口的末端回退到不会切断字符的位置。"""
    text = "hello " + EMOJI + " world"
    data = text.encode("utf-8")
    cut = data.index(b"\xf0") + 1  # 故意切在 emoji 中间
    end = character_end(data, cut)
    assert data[:end].decode("utf-8") == "hello "
    assert data[:cut].decode("utf-8", errors="ignore") == "hello "


# ─── utf8_byte_length ────────────────────────────────────────────────────────


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
def test_utf8_byte_length_matches_encoding(text: str, expected: int) -> None:
    assert utf8_byte_length(text) == expected
    assert utf8_byte_length(text) == len(text.encode("utf-8"))


def test_utf8_byte_length_counts_lone_surrogate_as_replacement() -> None:
    """上游 ``output.ts:275``：*"a lone surrogate counts as U+FFFD, three bytes"*。"""
    assert utf8_byte_length("\ud800") == 3
    assert utf8_byte_length("a\udc00b") == 5
    # Python 无法直接编码孤立代理，这条正是本函数存在（而不是直接 encode）的理由
    with pytest.raises(UnicodeEncodeError):
        "\ud800".encode("utf-8")


def test_utf8_byte_length_merges_unpaired_surrogates_like_js() -> None:
    """**未合并**的代理对按一个字符计 4 字节 —— 与 JS 逐码元的语义一致。

    Python ``str`` 里 ``"\\ud83d\\ude00"`` 是 2 个字符；天真地逐字符计数会得到
    3+3=6。上游在 UTF-16 码元空间里把它当作一个 emoji（4 字节）。先合并相邻的
    高低代理再计数，两边就一致了。
    """
    unmerged = "\ud83d\ude00"
    assert len(unmerged) == 2  # 确实是两个 Python 字符
    assert utf8_byte_length(unmerged) == 4
    assert utf8_byte_length(unmerged) == utf8_byte_length(EMOJI)


def test_utf8_byte_length_high_surrogate_without_low_is_three() -> None:
    """高位代理后面不是低位代理时，按孤立代理计 3 字节。"""
    assert utf8_byte_length("\ud83da") == 4  # 3 + 1
    assert utf8_byte_length("\ud83d\ud83d") == 6  # 两个孤立高位代理


def test_utf8_byte_length_is_not_utf16_length() -> None:
    """守住「别把这两个计数混用」。"""
    from pi_chord.delta import u16_len

    assert u16_len(EMOJI) == 2
    assert utf8_byte_length(EMOJI) == 4
    assert u16_len(CJK) == 1
    assert utf8_byte_length(CJK) == 3
