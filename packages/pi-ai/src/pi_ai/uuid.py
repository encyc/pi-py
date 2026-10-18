"""时间有序的 UUIDv7 生成。

对应上游 ``packages/ai/src/utils/uuid.ts``（v1.3.0，45 行；v0.85.1 → v1.3.0 期间
**未变更**）。

位布局（与上游逐位一致）：

======  ==================================================================
字节     内容
======  ==================================================================
0–5     48 位 Unix 毫秒时间戳（大端）
6       高 4 位 = 版本 ``0x7``；低 4 位 = 序列 bit[40:37]
7       序列 bit[36:29]
8       高 2 位 = 变体 ``0b10``；低 6 位 = 序列 bit[28:23]
9–10    序列 bit[22:15]、bit[14:7]
11      序列 bit[6:0] 左移 1 位，最低位保留**首次随机**的 bit0
======  ==================================================================

**单调性**：模块级 ``_sequence`` 每次调用**递增 1**（不只是同毫秒内），配合
普通调用（不传时间戳）时时间戳取 ``max(now, 上次)``，使 (时间戳‖序列) 这个
89 位整数**严格递增**。

**传显式时间戳时不做钳制**——上游注释 "A supplied timestamp is preserved for
follower ids"，即显式时间戳按原样写入，此时若它早于前一次普通调用，整体不再
单调。这是上游的**有意语义**，本端对齐。

与 JS 的差异（均为语言层，语义等价）：

- ``RangeError`` → :class:`ValueError`（Python 对越界值的惯用异常）
- ``globalThis.crypto.getRandomValues`` → :func:`os.urandom`（同为密码学安全随机源）
- ``Number.isInteger`` → :func:`_is_integer_like`（见其文档：``bool`` 不算整数，
  整数值的 ``float`` 算整数——与 JS 一致）
"""

from __future__ import annotations

import math
import os
import time

__all__ = ["uuidv7"]

#: UUIDv7 时间戳字段的上限：48 位全 1。
MAX_UUID_V7_TIMESTAMP = 0xFFFFFFFFFFFF

#: 序列字段上限：41 位全 1。
MAX_SEQUENCE = (1 << 41) - 1

#: 上次**普通调用**（未传时间戳）的有效时间戳；-1 表示尚未产生过。
_last_ordinary_timestamp = -1

#: 单调序列。首次调用时从随机字节初始化，此后每次调用递增 1。
#: 模块级可变状态——上游如此，故 `pi_ai.uuid` 与 `pi_ai.utils.uuid` 必须解析到
#: **同一批对象**（后者只是转发），否则会出现两个独立序列。
_sequence: int | None = None


def _is_integer_like(value: object) -> bool:
    """镜像 JS 的 ``Number.isInteger``。

    - ``bool`` **不算**整数（JS 里 ``typeof true !== "number"``；Python 的 ``bool``
      是 ``int`` 子类，故必须显式排除）
    - 整数值的 ``float`` **算**整数（``Number.isInteger(1.0) === true``）
    - ``NaN`` / ``inf`` / 非整数 float / 其他类型 → False
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value) and value.is_integer()
    return False


def uuidv7(timestamp_ms: int | None = None) -> str:
    """生成一个时间有序的 UUIDv7 字符串。

    对应上游 ``uuidv7``（``utils/uuid.ts:8-45``）。

    :param timestamp_ms: 可选的 Unix 毫秒时间戳。省略时取当前时间，并**向上钳制**
        到不小于上次普通调用的值（保证单调）。显式传入时**按原样使用**、不钳制，
        也不更新内部"上次时间戳"（上游语义："A supplied timestamp is preserved for
        follower ids"）。
    :raises ValueError: ``timestamp_ms`` 不是整数（``bool`` 不算），或超出
        ``[0, 2**48 - 1]``；或 41 位序列耗尽（需连续调用 2**41 次，实际不可达）。
    :returns: 形如 ``"0190f2c1-8a3b-7c4d-9e5f-0123456789ab"`` 的标准 UUID 字符串。

    典型用法（与下游 ``durable/src/harness/provider.ts:18`` 一致）::

        session_id = uuidv7()
    """
    requested = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms

    if not _is_integer_like(requested) or requested < 0 or requested > MAX_UUID_V7_TIMESTAMP:
        raise ValueError(
            f"UUIDv7 timestamp must be an integer between 0 and {MAX_UUID_V7_TIMESTAMP}"
        )

    global _last_ordinary_timestamp, _sequence

    if timestamp_ms is None:
        effective_timestamp = max(int(requested), _last_ordinary_timestamp)
        _last_ordinary_timestamp = effective_timestamp
    else:
        effective_timestamp = int(requested)

    payload = bytearray(os.urandom(16))

    if _sequence is None:
        _sequence = (
            (payload[1] << 32)
            | (payload[2] << 24)
            | (payload[3] << 16)
            | (payload[4] << 8)
            | payload[5]
        )
    else:
        if _sequence == MAX_SEQUENCE:
            raise ValueError("UUIDv7 generator sequence exhausted")
        _sequence += 1

    sequence = _sequence

    # 48 位时间戳（大端）覆盖字节 0–5
    for index in range(5, -1, -1):
        payload[index] = (effective_timestamp >> ((5 - index) * 8)) & 0xFF

    payload[6] = 0x70 | ((sequence >> 37) & 0x0F)
    payload[7] = (sequence >> 29) & 0xFF
    payload[8] = 0x80 | ((sequence >> 23) & 0x3F)
    payload[9] = (sequence >> 15) & 0xFF
    payload[10] = (sequence >> 7) & 0xFF
    # 最低位保留首次随机的 bit0（RHS 先求值，故读到的是原始随机字节）
    payload[11] = ((sequence & 0x7F) << 1) | (payload[11] & 0x01)

    hex_digits = [f"{byte:02x}" for byte in payload]
    return (
        f"{''.join(hex_digits[0:4])}-{''.join(hex_digits[4:6])}-"
        f"{''.join(hex_digits[6:8])}-{''.join(hex_digits[8:10])}-"
        f"{''.join(hex_digits[10:])}"
    )


def _reset_for_testing() -> None:
    """测试专用：重置模块级状态（序列与"上次时间戳"）。

    上游没有这个口子——其状态同样全局且不可重置。此处提供**私有**钩子，是为了让
    测试可确定性验证"首次调用从随机字节初始化序列"等行为；命名与 ``models.py``
    的 ``_reset_for_testing`` 一致。不属于公开 API。
    """
    global _last_ordinary_timestamp, _sequence
    _last_ordinary_timestamp = -1
    _sequence = None
