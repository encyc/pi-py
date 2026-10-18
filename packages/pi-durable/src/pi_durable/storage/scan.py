"""内置后端共享的游标解析 —— 移植 ``durable/src/storage/scan.ts``（40 行）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pi_durable.errors import StorageRequestError
from pi_durable.types import Id, ScanOrder

__all__ = ["ScanStart", "next_cursor", "scan_start"]

_VALID_ORDERS = ("ascending", "descending")


@dataclass(frozen=True, slots=True)
class ScanStart:
    """一次内置扫描的起点：方向，以及上一页返回的最后一个 ID（``scan.ts:4-8``）。"""

    order: ScanOrder
    after: int | None


def _is_safe_integer(value: object) -> bool:
    """JS ``Number.isSafeInteger``。"""
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) <= 2**53 - 1


def scan_start(
    requested: ScanOrder | None,
    cursor: dict[str, Any] | None,
    fallback: ScanOrder,
) -> ScanStart:
    """解析一次扫描的方向与位置（``scan.ts:10-35``）。

    游标**带着它被创建时的方向**继续：查询重复该方向或省略 ``order`` 都可以；
    要求另一个方向则抛 :class:`~pi_durable.errors.StorageRequestError`。
    没有方向字段的游标（在扫描有方向之前写下的）按本次扫描的默认方向继续。
    """
    if requested is not None and requested not in _VALID_ORDERS:
        raise StorageRequestError(f"Invalid scan order: {requested}")
    if cursor is None:
        return ScanStart(requested or fallback, None)

    after = cursor.get("after")
    stored = cursor.get("order")
    if not _is_safe_integer(after):
        raise StorageRequestError("Invalid storage cursor")
    if stored is not None and stored not in _VALID_ORDERS:
        raise StorageRequestError("Invalid storage cursor")

    order: ScanOrder = stored if stored is not None else fallback
    if requested is not None and requested != order:
        raise StorageRequestError(
            f"The cursor continues a {order} scan; the query asks for {requested}"
        )
    return ScanStart(order, int(after))  # type: ignore[arg-type]


def next_cursor(id: Id, order: ScanOrder) -> dict[str, Any]:
    """上一页最后一个条目是 ``id`` 时的续扫状态（``scan.ts:37-40``）。"""
    return {"after": id, "order": order}
