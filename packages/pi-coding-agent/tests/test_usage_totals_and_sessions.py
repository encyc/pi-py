"""Tier-B④-2a 小件测试：四个 `execute.ts` 前置（两个已存在 / 两个新增）。

* `format_size` —— 上游 ``core/tools/truncate.ts`` 的 ``formatSize``：**本仓早已有**，无需移植
* `save_output_file` —— 上游 ``utils/output-files.ts``：提到共享模块，MCP 与 codemode 共用
* `combine_usage` —— ``core/usage-totals.ts`` 的 ``combineUsage``：**部分移植**
* `SessionEntry` —— ``core/session-manager.ts`` 的联合类型：**类型专用切分**
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from pi_coding_agent.output_files import save_output_file
from pi_coding_agent.session_manager import SessionEntry, SessionEntryBase
from pi_coding_agent.truncate import format_size
from pi_coding_agent.usage_totals import combine_usage

# ============================================================
# format_size：已存在，锁行为等价
# ============================================================


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [
        (0, "0B"),
        (1023, "1023B"),
        (1024, "1.0KB"),
        (1536, "1.5KB"),
        (1048575, "1024.0KB"),
        (1048576, "1.0MB"),
        (1572864, "1.5MB"),
    ],
)
def test_format_size_matches_upstream_format_size(num_bytes: int, expected: str):
    """上游分支：``<1024``→B / ``<1024²``→``toFixed(1)``KB / 否则 MB。"""
    assert format_size(num_bytes) == expected


# ============================================================
# save_output_file：共享模块
# ============================================================


async def test_save_output_file_is_user_only_readable(tmp_path):
    path = await save_output_file("hello", ".txt")
    try:
        assert await asyncio.to_thread(os.path.isfile, path)
        assert await asyncio.to_thread(Path(path).read_text, encoding="utf-8") == "hello"
        # 上游 v1.0.3 起「readable only by the user」
        mode = (await asyncio.to_thread(os.stat, path)).st_mode
        assert (mode & 0o777) == 0o600
    finally:
        os.unlink(path)


async def test_save_output_file_accepts_bytes_and_prefix():
    path = await save_output_file(b"\x00\x01", ".bin", prefix="pi-codemode-")
    try:
        assert await asyncio.to_thread(Path(path).read_bytes) == b"\x00\x01"
        assert await asyncio.to_thread(os.path.basename, path) is not None
        assert os.path.basename(path).startswith("pi-codemode-")
    finally:
        os.unlink(path)


def test_mcp_saver_delegates_to_the_shared_module():
    """MCP 的实现已提到共享模块 —— 同一段代码，只留一份（避免各自漂移）。

    保留 MCP 自己的前缀，行为与之前完全一致。
    """
    import inspect

    from pi_coding_agent.extensions.mcp import tools

    source = inspect.getsource(tools.save_to_temp_file)
    assert "save_output_file(data, extension, prefix=" in source
    # 实现细节（mkstemp/chmod）不得再出现在 MCP 里 —— 只看**代码体**，
    # docstring 里为说明历史会提到 `0o600`，那不是实现。
    body = source.split('"""', 2)[2]
    assert "mkstemp" not in body
    assert "0o600" not in body


# ============================================================
# combine_usage：部分移植
# ============================================================


def _usage(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "input": 1,
        "output": 2,
        "cacheRead": 3,
        "cacheWrite": 4,
        "totalTokens": 10,
        "cost": {"input": 0.1, "output": 0.2, "cacheRead": 0.3, "cacheWrite": 0.4, "total": 1.0},
    }
    base.update(overrides)
    return base


def test_combine_usage_adds_every_field():
    combined = combine_usage(_usage(), _usage())
    assert combined["input"] == 2
    assert combined["output"] == 4
    assert combined["cacheRead"] == 6
    assert combined["cacheWrite"] == 8
    assert combined["totalTokens"] == 20
    assert combined["cost"] == {
        "input": pytest.approx(0.2),
        "output": pytest.approx(0.4),
        "cacheRead": pytest.approx(0.6),
        "cacheWrite": pytest.approx(0.8),
        "total": pytest.approx(2.0),
    }


def test_combine_usage_keeps_optional_keys_absent_when_both_sides_lack_them():
    """可选键「保持缺席」而不是加出 0 —— 上游用条件展开。"""
    combined = combine_usage(_usage(), _usage())
    assert "cacheWrite1h" not in combined
    assert "reasoning" not in combined


def test_combine_usage_includes_optional_keys_when_either_side_has_them():
    combined = combine_usage(_usage(cacheWrite1h=5), _usage())
    assert combined["cacheWrite1h"] == 5
    assert "reasoning" not in combined

    combined2 = combine_usage(_usage(), _usage(reasoning=7))
    assert combined2["reasoning"] == 7


# ============================================================
# SessionEntry：类型专用切分
# ============================================================


def test_session_entry_union_has_all_eleven_variants():
    """上游 11 个变体一个不少（少了就会出现「读不到某类条目」的静默问题）。"""
    variants = SessionEntry.__args__  # type: ignore[attr-defined]
    assert len(variants) == 11
    # TypedDict 不支持 issubclass（会 TypeError），改用注解断言继承关系：
    # 每个变体都必须带上基类的四个公共字段。
    base_keys = set(SessionEntryBase.__annotations__)
    assert base_keys == {"id", "parentId", "timestamp"}
    for variant in variants:
        assert base_keys <= set(variant.__annotations__), variant
        assert "type" in variant.__annotations__, variant  # 各变体自己声明判别字段


def test_session_manager_module_is_type_only():
    """类型专用切分：**没有运行期行为**，也没有悄悄补上未移植的会话读写。"""
    import pi_coding_agent.session_manager as module

    for absent in ("SessionManager", "read_session", "write_session", "load_session"):
        assert not hasattr(module, absent), absent


def test_custom_entry_carries_the_codemode_store_shape():
    """codemode 的 ``store()`` 落成 ``type="custom"`` + ``customType="codemode-store"``。"""
    import typing

    hints = typing.get_type_hints(
        __import__("pi_coding_agent.session_manager", fromlist=["x"]).CustomEntry
    )
    assert hints["type"] == typing.Literal["custom"]
    assert hints["customType"] is str
