"""``edit-diff`` / ``jsdiff`` 与真 ``diff@8.0.4`` 的对拍。

上游 ``generateUnifiedPatch`` / ``generateDiffString`` 的唯一外部依赖是 npm 的 ``diff``
包（``edit-diff.ts:367,382``）。本仓用 stdlib ``difflib`` 替它，因此**必须证明替得对**。

语料 ``tests/parity/golden.json`` 由 ``tests/parity/generate-golden.js`` 用真
``diff@8.0.4`` 生成（重跑方式见该文件头部注释，也写在 ``PORTING.md``）：

* 小语料 27 条：存全量期望值，逐条比对
* 大语料 9 条（100–1200 行，含 >200 行触发 ``autojunk`` 的边界）：输入由**确定性配方**
  重建，只比对输出的 sha256，免得仓库背几百 KB 字符串

**主验收**是「patch 能正确应用」（上游 ``test/tools.test.ts:369`` 就是
``expect(applyPatch(original, details.patch)).toBe(...)``），字节等价是记录项。
本文件自带一个最小 unified-diff 应用器来做这个往返验证——不依赖 node。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pi_durable.tools.jsdiff import create_two_files_patch, diff_lines, split_lines_with_endings

GOLDEN = json.loads((Path(__file__).parent / "parity" / "golden.json").read_text(encoding="utf-8"))

#: 对齐有歧义的语料：改动是「把若干行搬到别处」时，``SequenceMatcher``（Ratcliff-Obershelp）
#: 与 jsdiff（Myers）可以选出不同但同样合法的编辑脚本。这些用例只断言
#: 「patch 能正确应用」，不断言字节相同。详见 PORTING.md 的差异清单。
#:
#: 实测只有 1 条（小语料 ``interleaved``）。大语料的 ``big-moved-line``（把第 10 行搬到
#: 第 200 行）**与 jsdiff 完全一致**——所以这里没有把它算作歧义，而是让它走字节比对。
ALIGNMENT_AMBIGUITY = {"interleaved"}


# --------------------------------------------------------------------------------------
# 最小 unified-diff 应用器（仅用于验证本文件产出的 patch）
# --------------------------------------------------------------------------------------


def apply_unified_patch(original: str, patch: str) -> str:
    """把 ``patch`` 应用到 ``original``。

    与真实 patch 工具同构：正文每行是 ``标记 + 原行内容``，而**原行内容自带行尾**，
    所以这里必须按「保留行尾」的方式切分——否则 ``a\\n\\n\\nb\\n`` 这种含空行的内容
    还原不出来。
    """
    old_lines = split_lines_with_endings(original)
    patch_lines = split_lines_with_endings(patch)

    out: list[str] = []
    cursor = 0
    for position, line in enumerate(patch_lines):
        # 只跳过开头的 `---` / `+++` 两行：正文里的一行完全可能以 `---` 开头。
        if position < 2 or line.startswith("\\"):
            continue
        if line.startswith("@@"):
            header = line[line.index("-", 2) :]
            old_start = int(header.split(",")[0][1:])
            start = 0 if old_start == 0 else old_start - 1
            out.extend(old_lines[cursor:start])
            cursor = start
            continue
        marker, value = line[:1], line[1:]
        # unified diff 是**行式**格式：正文行照例带 `\n`，「源行没有换行」这件事由紧跟其后的
        # `\ No newline at end of file` 标记表达。所以这里要按标记把行尾去掉。
        if position + 1 < len(patch_lines) and patch_lines[position + 1].startswith("\\"):
            value = value[:-1] if value.endswith("\n") else value
        if marker == " ":
            out.append(value)
            cursor += 1
        elif marker == "-":
            cursor += 1
        elif marker == "+":
            out.append(value)
    out.extend(old_lines[cursor:])
    return "".join(out)


def _js_json(value: object) -> str:
    """与 JS ``JSON.stringify`` 逐字节一致的序列化，供 sha256 比对。

    两处必须对齐：**无空格分隔符**，以及**不转义非 ASCII**。
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------------------
# 大语料配方 —— 必须与 tests/parity/generate-golden.js 的 bigCases() 逐字一致
# --------------------------------------------------------------------------------------


def big_cases() -> list[tuple[str, str, str]]:
    cases: list[tuple[str, str, str]] = []

    def numbered(n: int) -> list[str]:
        return [f"line {i} some repeated words here" for i in range(n)]

    for n in (100, 201, 250, 500, 1200):
        a = numbered(n)
        b = list(a)
        b[n // 2] = "CHANGED LINE"
        cases.append((f"big-{n}-one-change", "\n".join(a) + "\n", "\n".join(b) + "\n"))

    n = 300
    a = numbered(n)
    b = list(a)
    b[0] = "HEAD CHANGED"
    b[n - 1] = "TAIL CHANGED"
    cases.append(("big-300-ends", "\n".join(a) + "\n", "\n".join(b) + "\n"))

    repeated = "\n".join("same" if i % 2 == 0 else f"uniq{i}" for i in range(400)) + "\n"
    cases.append(("big-repeat-heavy", repeated, repeated.replace("uniq201", "UNIQ201")))

    lines = [f"l{i}" for i in range(250)]
    middle = "\n".join(f"NEW{i}" for i in range(50))
    cases.append(
        (
            "big-bulk-insert",
            "\n".join(lines) + "\n",
            "\n".join(lines[:120]) + "\n" + middle + "\n" + "\n".join(lines[120:]),
        )
    )

    moved = [f"m{i}" for i in range(220)]
    shuffled = list(moved)
    shuffled.insert(200, shuffled.pop(10))
    cases.append(("big-moved-line", "\n".join(moved) + "\n", "\n".join(shuffled) + "\n"))

    return cases


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# 小语料：逐字节
# --------------------------------------------------------------------------------------


SMALL = GOLDEN["small"]
SMALL_BY_NAME = {case["name"]: case for case in SMALL}


@pytest.mark.parametrize("case", SMALL, ids=[c["name"] for c in SMALL])
def test_diff_lines_matches_jsdiff(case: dict[str, object]) -> None:
    """``diff_lines`` 与 ``Diff.diffLines`` 的 parts 完全一致。"""
    name = str(case["name"])
    got = [
        {"value": p.value, "added": p.added, "removed": p.removed, "count": p.count}
        for p in diff_lines(str(case["old"]), str(case["new"]))
    ]

    if name in ALIGNMENT_AMBIGUITY:
        # 合法但不同的编辑脚本：只要求「应用后得到新内容」，不要求形状相同。
        assert _apply_parts(str(case["old"]), got) == str(case["new"])
        return

    assert got == case["diffLines"]


@pytest.mark.parametrize("case", SMALL, ids=[c["name"] for c in SMALL])
def test_patch_matches_jsdiff_or_round_trips(case: dict[str, object]) -> None:
    """patch 字节等价；有歧义的用例退化为「必须能正确应用」。"""
    name = str(case["name"])
    old, new = str(case["old"]), str(case["new"])
    patch = create_two_files_patch("f.txt", old, new)

    # 主验收：patch 应用后必须得到新内容（上游 test/tools.test.ts:369 的口径）。
    assert apply_unified_patch(old, patch) == new

    if name in ALIGNMENT_AMBIGUITY:
        assert patch != case["patch"]
    else:
        assert patch == case["patch"]


def test_only_one_small_case_differs_byte_wise() -> None:
    """把「有几条字节不同」钉成断言——差异变多会立刻红。"""
    differing = [
        case["name"]
        for case in SMALL
        if create_two_files_patch("f.txt", str(case["old"]), str(case["new"])) != case["patch"]
    ]
    assert differing == ["interleaved"]


def test_only_one_small_case_differs_in_diff_lines() -> None:
    differing = []
    for case in SMALL:
        got = [
            {"value": p.value, "added": p.added, "removed": p.removed, "count": p.count}
            for p in diff_lines(str(case["old"]), str(case["new"]))
        ]
        if got != case["diffLines"]:
            differing.append(case["name"])
    assert differing == ["interleaved"]


def _apply_parts(old: str, parts: list[dict[str, object]]) -> str:
    """把 diffLines 的 parts 应用回旧内容，得到新内容（验证编辑脚本合法）。"""
    out: list[str] = []
    for part in parts:
        if not part["removed"]:
            out.append(str(part["value"]))
    return "".join(out)


# --------------------------------------------------------------------------------------
# 大语料：sha256（输入由配方重建）
# --------------------------------------------------------------------------------------


BIG = GOLDEN["big"]
BIG_EXPECTED = {case["name"]: case for case in BIG}
BIG_CASES = big_cases()


def test_big_case_recipe_matches_generator() -> None:
    """配方与 JS 生成器一致性的自检：名字集合必须对得上。"""
    assert [name for name, _, _ in BIG_CASES] == [case["name"] for case in BIG]


@pytest.mark.parametrize("case", BIG_CASES, ids=[c[0] for c in BIG_CASES])
def test_big_diff_lines_hash(case: tuple[str, str, str]) -> None:
    name, old, new = case
    got = _js_json(
        [
            {"value": p.value, "added": p.added, "removed": p.removed, "count": p.count}
            for p in diff_lines(old, new)
        ]
    )
    assert _sha(got) == BIG_EXPECTED[name]["diffLinesSha256"]


@pytest.mark.parametrize("case", BIG_CASES, ids=[c[0] for c in BIG_CASES])
def test_big_patch_hash_or_round_trip(case: tuple[str, str, str]) -> None:
    name, old, new = case
    patch = create_two_files_patch("f.txt", old, new)

    # 主验收对**所有**大语料都成立，包括移动行那条。
    assert apply_unified_patch(old, patch) == new

    if name in ALIGNMENT_AMBIGUITY:
        assert _sha(patch) != BIG_EXPECTED[name]["patchSha256"]
    else:
        assert _sha(patch) == BIG_EXPECTED[name]["patchSha256"]


def test_large_inputs_do_not_use_difflibs_autojunk_heuristic() -> None:
    """``autojunk`` 会按文档改变 >200 元素序列的结果；本实现显式关掉它。

    这条断言用「行为」表达：对 1200 行里只有一行变化的输入，结果必须是
    **恰好 1 处删除 + 1 处新增**——若 autojunk 把它当垃圾，改动会变大。
    """
    _, old, new = next(c for c in BIG_CASES if c[0] == "big-1200-one-change")
    parts = diff_lines(old, new)

    removed = sum(p.count for p in parts if p.removed)
    added = sum(p.count for p in parts if p.added)
    assert (removed, added) == (1, 1)
