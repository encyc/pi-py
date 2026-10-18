"""``pi_durable.tools.path_utils`` 的单元测试（上游 ``durable/src/tools/path-utils.ts``，30 行）。"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest

from pi_chord.context import BACKGROUND_CONTEXT
from pi_durable.env import get_or_throw
from pi_durable.env.node import NodeExecutionEnv, NodeExecutionEnvOptions
from pi_durable.tools.path_utils import (
    NARROW_NO_BREAK_SPACE,
    normalize_tool_path,
    resolve_read_tool_path,
    resolve_tool_path,
)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[NodeExecutionEnv]:
    yield NodeExecutionEnv(NodeExecutionEnvOptions(cwd=str(tmp_path)))


# --- normalize_tool_path（path-utils.ts:7-10）-----------------------------------


def test_strips_leading_at_sign() -> None:
    assert normalize_tool_path("@src/main.py") == "src/main.py"


def test_leaves_paths_without_at_sign() -> None:
    assert normalize_tool_path("src/main.py") == "src/main.py"


def test_collapses_unicode_spaces() -> None:
    # U+00A0 不换行空格、U+2009 窄空格、U+3000 全角空格都折成普通空格。
    assert normalize_tool_path("a\u00a0b\u2009c\u3000d") == "a b c d"


def test_keeps_narrow_no_break_space_distinct_from_plain_space() -> None:
    """U+202F 也在折叠表里（``path-utils.ts:4``），所以它同样变成普通空格。"""
    assert normalize_tool_path(f"a{NARROW_NO_BREAK_SPACE}b") == "a b"


def test_at_sign_inside_path_is_kept() -> None:
    assert normalize_tool_path("src/@scope/pkg.py") == "src/@scope/pkg.py"


def test_only_the_first_at_sign_is_stripped() -> None:
    assert normalize_tool_path("@@a") == "@a"


# --- resolve_tool_path（path-utils.ts:12-14）-----------------------------------


async def test_resolve_tool_path_makes_absolute(env: NodeExecutionEnv, tmp_path: Path) -> None:
    resolved = await resolve_tool_path(env, "sub/file.txt", BACKGROUND_CONTEXT)
    assert Path(resolved) == tmp_path / "sub" / "file.txt"


async def test_resolve_tool_path_normalizes_first(env: NodeExecutionEnv, tmp_path: Path) -> None:
    resolved = await resolve_tool_path(env, "@sub/file.txt", BACKGROUND_CONTEXT)
    assert Path(resolved) == tmp_path / "sub" / "file.txt"


async def test_resolve_tool_path_absolute_input_passes_through(
    env: NodeExecutionEnv, tmp_path: Path
) -> None:
    target = str(tmp_path / "x.txt")
    assert await resolve_tool_path(env, target, BACKGROUND_CONTEXT) == target


# --- resolve_read_tool_path（path-utils.ts:16-29）------------------------------


async def test_returns_plain_path_when_it_exists(env: NodeExecutionEnv, tmp_path: Path) -> None:
    (tmp_path / "plain.txt").write_text("hi", encoding="utf-8")
    resolved = await resolve_read_tool_path(env, "plain.txt", BACKGROUND_CONTEXT)
    assert Path(resolved) == tmp_path / "plain.txt"


async def test_returns_resolved_path_when_nothing_exists(
    env: NodeExecutionEnv, tmp_path: Path
) -> None:
    """所有变体都不存在时退回最初解析出的路径（``path-utils.ts:29``）。"""
    resolved = await resolve_read_tool_path(env, "missing.txt", BACKGROUND_CONTEXT)
    assert Path(resolved) == tmp_path / "missing.txt"


async def test_prefers_the_curly_apostrophe_variant(env: NodeExecutionEnv, tmp_path: Path) -> None:
    """直引号写成右单引号的文件名：模型给直引号，磁盘上是弯引号。"""
    target = tmp_path / "it\u2019s.txt"
    target.write_text("hi", encoding="utf-8")

    resolved = await resolve_read_tool_path(env, "it's.txt", BACKGROUND_CONTEXT)

    assert resolved == str(target)


async def test_nfd_variant_is_considered_but_composed_form_wins_first(
    env: NodeExecutionEnv, tmp_path: Path
) -> None:
    """NFD 变体只在前面的变体都不存在时才会被选中（``path-utils.ts:18-27`` 的顺序）。

    注意 macOS 的 APFS/HFS+ 对文件名做 Unicode 归一化且查找不区分 NFC/NFD：
    用 NFC 名字去查一个以 NFD 存盘的文件也会命中。所以这里**不能**断言「返回 NFD 形式」——
    可观察到的、且与平台无关的不变式是：返回的路径真实存在且内容正确。
    """
    decomposed = unicodedata.normalize("NFD", "café.txt")
    target = tmp_path / decomposed
    target.write_text("hi", encoding="utf-8")

    composed = unicodedata.normalize("NFC", "café.txt")
    assert composed != decomposed  # 前提：两种码点序列确实不同

    resolved = await resolve_read_tool_path(env, composed, BACKGROUND_CONTEXT)

    assert get_or_throw(await env.exists(resolved, BACKGROUND_CONTEXT)) is True
    assert get_or_throw(await env.read_text_file(resolved, BACKGROUND_CONTEXT)) == "hi"
    # 归一化后的路径指向同一个文件。
    assert unicodedata.normalize("NFC", resolved) == unicodedata.normalize("NFC", str(target))


async def test_prefers_the_narrow_no_break_space_variant(
    env: NodeExecutionEnv, tmp_path: Path
) -> None:
    """截图时间戳形如 ``Screenshot 2026-01-01 at 1.30.00 PM.png``——
    macOS 把 ``AM/PM`` 前的空格写成 U+202F。"""
    target = tmp_path / "at 1.30.00\u202fPM.png"
    target.write_text("hi", encoding="utf-8")

    resolved = await resolve_read_tool_path(env, "at 1.30.00 PM.png", BACKGROUND_CONTEXT)

    assert resolved == str(target)


async def test_am_pm_replacement_is_case_insensitive(env: NodeExecutionEnv, tmp_path: Path) -> None:
    """上游正则带 ``i`` 标志（``path-utils.ts:20``），且 ``$1`` 保留原大小写。"""
    target = tmp_path / "at 9.00.00\u202fpm.png"
    target.write_text("hi", encoding="utf-8")

    resolved = await resolve_read_tool_path(env, "at 9.00.00 pm.png", BACKGROUND_CONTEXT)

    assert resolved == str(target)


async def test_variants_are_deduplicated(env: NodeExecutionEnv, tmp_path: Path) -> None:
    """没有任何变体存在时也能正常返回，不会因为重复项多做无用功。"""
    (tmp_path / "plain.txt").write_text("hi", encoding="utf-8")
    # `plain.txt` 的 NFD 形式与自身相同，变体去重后只剩一个。
    assert await resolve_read_tool_path(env, "plain.txt", BACKGROUND_CONTEXT) == str(
        tmp_path / "plain.txt"
    )


async def test_write_helpers_agree_on_resolution(env: NodeExecutionEnv, tmp_path: Path) -> None:
    """``resolve_tool_path`` 与 ``resolve_read_tool_path`` 对已存在文件给同一答案。"""
    (tmp_path / "same.txt").write_text("hi", encoding="utf-8")
    write_path = await resolve_tool_path(env, "same.txt", BACKGROUND_CONTEXT)
    read_path = await resolve_read_tool_path(env, "same.txt", BACKGROUND_CONTEXT)
    assert write_path == read_path
    assert get_or_throw(await env.read_text_file(read_path, BACKGROUND_CONTEXT)) == "hi"
