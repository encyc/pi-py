"""存储后端 —— ``durable/src/storage/``。

契约与记录类型在 :mod:`pi_durable.types`（对应上游 ``types.ts`` 的存储子集）；
本包提供内置后端的共享扫描助手与具体实现。
"""

from __future__ import annotations

from pi_durable.storage.scan import ScanStart, next_cursor, scan_start

__all__ = ["ScanStart", "next_cursor", "scan_start"]
