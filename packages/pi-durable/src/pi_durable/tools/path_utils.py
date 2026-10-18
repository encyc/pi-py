"""工具路径归一化 —— 移植 ``durable/src/tools/path-utils.ts``（30 行）。

模型给出的路径里常混进窄空格、不换行空格等 Unicode 空白（从网页或文档里复制来的），
以及 macOS 截图上时间戳用的窄不换行空格（U+202F）。
:func:`resolve_read_tool_path` 会依次尝试几种「看起来一样但码点不同」的变体，
命中哪个就用哪个。
"""

from __future__ import annotations

import re
import unicodedata

from pi_chord.context import Context
from pi_durable.env import ExecutionEnv, get_or_throw

__all__ = [
    "NARROW_NO_BREAK_SPACE",
    "UNICODE_SPACES",
    "normalize_tool_path",
    "resolve_read_tool_path",
    "resolve_tool_path",
]

#: 会被折叠成普通空格的各种 Unicode 空白（``path-utils.ts:4``）。
UNICODE_SPACES = re.compile(r"[\u00A0\u2000-\u200A\u202F\u205F\u3000]")
#: macOS 截图时间戳里用的是窄不换行空格（``path-utils.ts:5``）。
NARROW_NO_BREAK_SPACE = "\u202f"

_AM_PM = re.compile(r" (AM|PM)\.", re.IGNORECASE)
_APOSTROPHE = "'"


def normalize_tool_path(path: str) -> str:
    """把 Unicode 空白折成普通空格，并去掉开头的 ``@``（``path-utils.ts:7-10``）。

    开头的 ``@`` 是模型常见的多余前缀（``@src/main.py``）。
    """
    normalized = UNICODE_SPACES.sub(" ", path)
    return normalized[1:] if normalized.startswith("@") else normalized


async def resolve_tool_path(env: ExecutionEnv, path: str, context: Context) -> str:
    """归一化后取绝对路径（``path-utils.ts:12-14``）。"""
    return get_or_throw(await env.absolute_path(normalize_tool_path(path), context))


async def resolve_read_tool_path(env: ExecutionEnv, path: str, context: Context) -> str:
    """在几个等价变体里挑第一个真实存在的路径（``path-utils.ts:16-29``）。

    变体顺序与上游一致，且**去重后保持首次出现顺序**（上游 ``new Set(variants)``）：
    原样、``AM/PM`` 前的空格换成 U+202F、NFD 分解、直引号换成右单引号、
    以及 NFD + 右单引号。都不存在时退回最初解析出的路径。
    """
    resolved = await resolve_tool_path(env, path, context)
    variants = [
        resolved,
        _AM_PM.sub(f"{NARROW_NO_BREAK_SPACE}\\1.", resolved),
        unicodedata.normalize("NFD", resolved),
        resolved.replace(_APOSTROPHE, "\u2019"),
        unicodedata.normalize("NFD", resolved).replace(_APOSTROPHE, "\u2019"),
    ]

    for variant in dict.fromkeys(variants):
        if get_or_throw(await env.exists(variant, context)):
            return variant
    return resolved
