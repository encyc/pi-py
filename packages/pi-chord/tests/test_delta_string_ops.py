"""字符串级 delta 的 UTF-16 行为 —— 表驱动向量与边界。

对照上游:
- ``chord/src/delta/tracker.ts:2039-2063``（``emitChangedValue`` 字符串分支）
- ``chord/src/delta/diff.ts:63-76``（``emitString``）
- ``chord/src/delta/index.ts:87-112``（``overlap``）
- ``chord/src/delta/index.ts:391-402``（``apply`` 的 ``a``/``t``）

向量来自 ``recon/08-chord-delta-spec.md`` §2.3。最后一项 ``agree`` 记录「UTF-16
约定与 code point 约定在该向量上是否一致」——刻意把**分歧集合**也固化成断言，这样
约定不会被无意间改动。上游对非 BMP 字符零测试覆盖
（``grep -rn 'emoji|surrogate|utf16' delta*.test.ts`` 无命中），所以这些向量是
本移植**新增**的覆盖，不是从上游抄来的。
"""

from __future__ import annotations

import random

import pytest

from pi_chord.delta import (
    apply_string_ops,
    emit_string_ops,
    overlap,
    to_units,
    u16_len,
    u16_slice,
)
from pi_chord.delta.textdiff import StringOp

# (id, before, after, ops_utf16, 两种约定是否一致)
STRING_VECTORS: list[tuple[str, str, str, list[StringOp] | None, bool]] = [
    ("V1", "abc", "abcdef", [("a", "def")], True),
    ("V2", "", "hello", [("a", "hello")], True),
    ("V3", "hello", "", [("s", "")], True),
    ("V4", "a\U0001f600", "a\U0001f600b", [("a", "b")], True),
    ("V5", "\U0001f600abc", "abc", [("t", 2)], False),
    ("V6", "xxx\U0001f600", "xxxxx\U0001f600", [("s", "xxxxx\U0001f600")], True),
    (
        "V7",
        "\U0001f600\U0001f600\U0001f600",
        "\U0001f600\U0001f600\U0001f600\U0001f600\U0001f600",
        [("a", "\U0001f600\U0001f600")],
        True,
    ),
    ("V8", "log: \U0001f600", "log: \U0001f600 done", [("a", " done")], True),
    ("V9", "e\u0301tail", "tail", [("t", 2)], True),
    ("V10", "\u00e9tail", "tail", [("t", 1)], True),
    (
        "V11",
        "ab",
        "ab\U0001f468\u200d\U0001f469\u200d\U0001f467",
        [("a", "\U0001f468\u200d\U0001f469\u200d\U0001f467")],
        True,
    ),
    ("V12", "\U0001f600\U0001f600", "\U0001f600", [("t", 2)], False),
    (
        "V13",
        "start\U0001f1e8\U0001f1f3",
        "start\U0001f1e8\U0001f1f3!",
        [("a", "!")],
        True,
    ),
    ("V14", "aaaa", "aaa", [("t", 1)], True),
    ("V15", "\ud800abc", "abc", [("t", 1)], True),
    ("V16", "abcdefghij", "defghijxyz", [("t", 3), ("a", "xyz")], True),
    ("V17", "abc", "xyz", [("s", "xyz")], True),
    ("V18", "abcXYZ", "XYZdef", [("t", 3), ("a", "def")], True),
    ("V19", "same", "same", None, True),
    (
        "V20",
        "\U0001f468\u200d\U0001f469\u200d\U0001f467x",
        "x",
        [("t", 8)],
        False,
    ),
]

VECTOR_IDS = [vector[0] for vector in STRING_VECTORS]


def _codepoint_emit(before: str, after: str) -> list[StringOp] | None:
    """**对照组**：按 code point 计数的发射器，仅供分歧断言使用。

    这不是产品代码 —— 它存在的唯一目的是证明「哪些向量两种约定不同」。
    """
    if before == after:
        return None
    if len(after) > len(before) and after[: len(before)] == before:
        return [("a", after[len(before) :])]
    shared = _codepoint_overlap(before, after)
    if shared > 0:
        ops: list[StringOp] = [("t", len(before) - shared)]
        if len(after) > shared:
            ops.append(("a", after[shared:]))
        return ops
    return [("s", after)]


def _codepoint_overlap(a: str, b: str) -> int:
    if not a or not b:
        return 0
    for size in range(min(len(a), len(b)), 0, -1):
        if a[len(a) - size :] == b[:size]:
            return size
    return 0


@pytest.mark.parametrize("cid,before,after,ops_utf16,agree", STRING_VECTORS, ids=VECTOR_IDS)
def test_string_ops_utf16(
    cid: str, before: str, after: str, ops_utf16: list[StringOp] | None, agree: bool
) -> None:
    """UTF-16 约定：发射结果与上游一致，且能往返。"""
    emitted = emit_string_ops(before, after)
    assert emitted == ops_utf16, cid
    if emitted is None:
        assert before == after
        return
    assert apply_string_ops(before, emitted) == after, cid


@pytest.mark.parametrize("cid,before,after,ops_utf16,agree", STRING_VECTORS, ids=VECTOR_IDS)
def test_convention_divergence_is_pinned(
    cid: str, before: str, after: str, ops_utf16: list[StringOp] | None, agree: bool
) -> None:
    """分歧集合本身也是契约：把「哪 3 条两种约定不同」固化下来。

    上游 ``index.ts:400`` 用 ``String.prototype.slice``，因此 ``t`` 的计数单位是
    UTF-16 码元。若有人把实现改成 code point，这个测试会指出**恰好**是哪几条向量
    变了（V5 / V12 / V20，都是「截断边界落在非 BMP 字符之后」）。
    """
    emitted = emit_string_ops(before, after)
    assert (emitted == _codepoint_emit(before, after)) is agree, cid


def test_exactly_three_vectors_diverge() -> None:
    """冗余但明确的守卫：分歧向量恰好是 V5 / V12 / V20。"""
    diverging = [cid for cid, before, after, _ops, agree in STRING_VECTORS if not agree]
    assert diverging == ["V5", "V12", "V20"]


def test_identical_strings_emit_nothing() -> None:
    assert emit_string_ops("same", "same") is None


# ─── UTF-16 边界：emoji / ZWJ / 区域指示符 / 组合字符 / 空串 / 孤立代理 ──────────


def test_u16_len_counts_code_units_not_code_points() -> None:
    assert u16_len("abc") == 3
    assert u16_len("\U0001f600") == 2  # 😀 一个码点，两个码元
    assert len("\U0001f600") == 1
    assert u16_len("\U0001f468\u200d\U0001f469\u200d\U0001f467") == 8  # ZWJ 家庭
    assert u16_len("\U0001f1e8\U0001f1f3") == 4  # 🇨🇳 区域指示符
    assert u16_len("e\u0301") == 2  # 组合字符：2 个码点，2 个码元
    assert u16_len("") == 0
    assert u16_len("\ud800") == 1  # 孤立代理


def test_u16_slice_splits_by_code_units() -> None:
    assert u16_slice("\U0001f600abc", 2) == "abc"
    assert u16_slice("\U0001f600abc", 1) != "abc"  # 切在代理对中间
    assert u16_slice("hello", 0) == "hello"
    assert u16_slice("hello", 99) == ""
    assert u16_slice("hello", 1, 3) == "el"


def test_empty_string_edges() -> None:
    assert emit_string_ops("", "") is None
    assert emit_string_ops("", "x") == [("a", "x")]
    assert emit_string_ops("x", "") == [("s", "")]
    # 从空串追加 emoji 走的是「前缀匹配」分支，payload 是 emoji 本身
    assert emit_string_ops("", "\U0001f600") == [("a", "\U0001f600")]


def test_emoji_truncation_uses_utf16_counts() -> None:
    """``t`` 的计数单位是 UTF-16 码元，不是码点。"""
    assert emit_string_ops("\U0001f600abc", "abc") == [("t", 2)]
    assert emit_string_ops("\U0001f600\U0001f600", "\U0001f600") == [("t", 2)]
    assert emit_string_ops("\U0001f600", "") == [("s", "")]


def test_zwj_family_truncation() -> None:
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert emit_string_ops(family + "x", "x") == [("t", 8)]
    assert emit_string_ops("x", "x" + family) == [("a", family)]


def test_regional_indicator_pair() -> None:
    flag = "\U0001f1e8\U0001f1f3"
    assert u16_len(flag) == 4  # 两个区域指示符，各占一个代理对
    assert emit_string_ops("start" + flag, "start" + flag + "!") == [("a", "!")]
    # ⚠️ 前端截断只能表达「保留一个**后缀**」。`start` 是 `start🇨🇳` 的**前缀**
    # 而非后缀，所以这里无法用 `t` 表达，只能整体 `s`。
    assert emit_string_ops("start" + flag, "start") == [("s", "start")]
    # 保留后缀时才能用 `t`：丢掉前面的 "x"，留下旗帜（4 个码元）
    assert emit_string_ops("x" + flag, flag) == [("t", 1)]


def test_combining_characters_are_ordinary_code_points() -> None:
    """组合字符在 UTF-16 里也是普通码元——**不做字形簇归一化**。

    上游只做码元级的字符串运算，从不按 grapheme cluster 处理；``e`` + U+0301 是
    两个码元，与预组合的 ``é``（一个码元）行为不同。这条测试固化该差异。
    """
    decomposed = "e\u0301"
    precomposed = "\u00e9"
    assert u16_len(decomposed) == 2
    assert u16_len(precomposed) == 1
    assert emit_string_ops(decomposed + "tail", "tail") == [("t", 2)]
    assert emit_string_ops(precomposed + "tail", "tail") == [("t", 1)]
    # 二者不相等：不做 Unicode 归一化
    assert emit_string_ops(decomposed, precomposed) == [("s", precomposed)]


def test_lone_surrogate_is_preserved_not_replaced() -> None:
    """孤立代理在 Python ``str`` 里合法，且必须原样保留（与 JS 一致）。"""
    lone = "\ud800abc"
    assert u16_len(lone) == 4
    assert emit_string_ops(lone, "abc") == [("t", 1)]
    assert emit_string_ops("x", "x\udc00") == [("a", "\udc00")]


def test_to_units_round_trips_through_from_units() -> None:
    """码元往返必须恒等——包括代理对合并与孤立代理保留。

    ⚠️ 这里守着两个真实的坑：

    1. 用 ``data.decode("utf-16-be", errors="surrogatepass")`` 实现反解，会把一个
       合法代理对拆成**两个**孤立代理，于是 ``len()`` 从 1 变 2。
    2. 反向地，**未合并**的代理对（``"\\ud83d\\ude00"``，2 个孤立代理）经
       ``to_units`` → ``from_units`` 之后会被**合并**成一个 😀。这不是往返恒等，
       而是刻意的规范化：与 JS 的语义对齐（JS 里那本来就是一个 emoji）。
    """
    from pi_chord.delta import from_units

    identity_samples = [
        "abc",
        "",
        "\U0001f600",
        "a\U0001f600b",
        "\U0001f468\u200d\U0001f469\u200d\U0001f467",
        "\U0001f1e8\U0001f1f3",
        "e\u0301",
        "\ud800abc",  # 真正的孤立代理：必须原样保留
        "\udc00",
    ]
    for sample in identity_samples:
        assert from_units(to_units(sample)) == sample, repr(sample)

    # 未合并的代理对是**唯一**不满足往返恒等的输入：它会被规范化成单个码点。
    unmerged = "\ud83d\ude00"
    assert from_units(to_units(unmerged)) == "\U0001f600"
    assert u16_len(unmerged) == u16_len("\U0001f600") == 2


def test_overlap_matches_upstream_vectors() -> None:
    """上游 ``delta.test.ts:181-182``。"""
    assert overlap("abcdefgh", "defghxyz", 65_536) == 5
    assert overlap("abcdef", "defghi", 0) == 0


def test_overlap_scan_window_is_bounded() -> None:
    """``scan`` 之外的尾巴不参与候选（``index.ts:91``：``a.length > scan ? a.slice(a.length - scan) : a``）。"""
    assert overlap("abcXYZ", "XYZdef", 2) == 0  # 窗口只剩 "YZ"
    assert overlap("abcXYZ", "XYZdef", 3) == 3
    assert overlap("abcXYZ", "XYZdef", 65_536) == 3


def test_overlap_is_a_bounded_probe_not_the_true_longest() -> None:
    """``overlap`` 是**有界探测**，不是「最长重叠」的精确算法。

    上游 ``index.ts:98-101`` 明确接受这一点：候选数有上限，放弃时返回 0，
    那会发射一个 ``set`` —— **更大，但永远不会错**。所以返回 0 **不代表**
    「没有重叠」，只代表「这次不利用重叠」。
    """
    # "abc" 确实是 a 的后缀、b 的前缀，但探测从 k=0 起算得 n=13 > len(b)=6，
    # 且 tail 中再无 "a"，于是放弃 → 0。
    assert overlap("abc" + "x" * 10, "abcdef", 65_536) == 0
    # 放弃是安全的：退化成整体 set，值仍然正确。
    emitted = emit_string_ops("abc" + "x" * 10, "abcdef")
    assert emitted == [("s", "abcdef")]
    assert apply_string_ops("abc" + "x" * 10, emitted) == "abcdef"


def test_overlap_returns_utf16_units() -> None:
    """单位是码元：``x😀`` 与 ``😀y`` 的重叠是 2 个码元，不是 1 个码点。"""
    assert overlap("x\U0001f600", "\U0001f600y", 65_536) == 2
    assert overlap("\U0001f600abc", "abcZZZ", 65_536) == 3


def test_random_round_trip_fuzz() -> None:
    """带**固定种子**的确定性 fuzz：任意 (before, after) 对都必须往返成功。

    这是最高性价比的正确性护栏 —— 上游对应的思路见 ``state-fuzz.test.ts``。
    固定种子而非 ``hash()``：后者受 ``PYTHONHASHSEED`` 影响，会让测试不可复现。
    """
    alphabet = ["a", "b", "Z", "0", " ", "\n", "\u00e9", "\u4e2d", "\U0001f600", "\u0301", "\u200d"]
    rng = random.Random(20261018)
    for _ in range(2000):
        before = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 12)))
        after = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 12)))
        emitted = emit_string_ops(before, after)
        if emitted is None:
            assert before == after
            continue
        assert apply_string_ops(before, emitted) == after, (before, after, emitted)


def test_internal_encoding_is_two_bytes_per_unit() -> None:
    """实现依赖「每个码元恰好 2 字节」来做偶数偏移切片（``_text_to_bytes``）。"""
    assert len("a".encode("utf-16-be")) == 2
    assert len("\U0001f600".encode("utf-16-be")) == 4
    assert len("\U0001f600abc".encode("utf-16-be")) == 10
    assert u16_len("\U0001f600abc") == len("\U0001f600abc".encode("utf-16-be")) // 2
