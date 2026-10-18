"""provider 请求头归一化。

对应上游 ``utils/headers.ts``。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["headers_to_record", "provider_headers_to_record"]


def headers_to_record(headers: Any) -> dict[str, str]:
    """把 SDK 的 headers 对象转成普通 dict（丢失多值）。对应上游 ``headersToRecord``。"""
    if headers is None:
        return {}
    result: dict[str, str] = {}
    items = headers.items() if isinstance(headers, Mapping) else None
    if items is None:
        return {}
    for key, value in items:
        result[str(key)] = str(value)
    return result


def provider_headers_to_record(
    *sources: Mapping[str, str | None] | None,
) -> dict[str, str] | None:
    """合并多个 provider header 来源，按**小写名**折叠，后者覆盖前者。

    对应上游 v1.3.0 ``providerHeadersToRecord``（utils/headers.ts:11-24）。

    v0.85.1 的签名是单参数 ``(headers)``；v1.3.0 改为**变参**并引入小写折叠，
    使"模型级 / 调用方 headers 覆盖内置 ``originator`` 与 ``User-Agent``"成为可能
    （上游 issue #10429）。值为 ``None`` 表示**删除**该键。

    返回 ``None``（而非空 dict）以保持上游"无 header 即无值"的语义。
    """
    merged: dict[str, tuple[str, str]] = {}
    for source in sources:
        if not source:
            continue
        for name, value in source.items():
            key = name.lower()
            merged.pop(key, None)
            if value is not None:
                merged[key] = (name, value)
    if not merged:
        return None
    return {name: value for name, value in merged.values()}
