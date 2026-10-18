"""从字节直接读 JPEG / WebP 的 EXIF 方向 —— 移植 ``durable/src/images/exif.ts``（70 行）。

不解码图像，只在字节里走一遍容器结构找到 TIFF 头，再找标签 ``0x0112``（Orientation）。
读不到时返回 ``1``（上游 ``exif.ts:4``）。被 ``images/index.ts`` 的 Photon processor 用来
决定是否需要把图像转正（``images/index.ts:37-62``）。
"""

from __future__ import annotations

__all__ = ["exif_orientation"]

_ORIENTATION_TAG = 0x0112


def exif_orientation(data: bytes | bytearray) -> int:
    """JPEG 或 WebP 的 EXIF 方向，取值 1..8；缺失或不可读时为 1（``exif.ts:4-27``）。

    返回 1 表示「无需转正」。5..8 是**交换宽高**的四个方向
    （``images/index.ts:120-121`` 的注释）。
    """
    tiff = _tiff_start(data)
    if tiff < 0 or tiff + 8 > len(data):
        return 1

    little = _byte_at(data, tiff) == 0x49 and _byte_at(data, tiff + 1) == 0x49

    def u16(at: int) -> int:
        if little:
            return _byte_at(data, at) | (_byte_at(data, at + 1) << 8)
        return (_byte_at(data, at) << 8) | _byte_at(data, at + 1)

    def u32(at: int) -> int:
        # 上游两处都用 `>>> 0` 归一到无符号 32 位（``exif.ts:10-13``）。
        # Python 整数无上限，按位与掩码即可得到同样的无符号值。
        if little:
            return (
                _byte_at(data, at)
                | (_byte_at(data, at + 1) << 8)
                | (_byte_at(data, at + 2) << 16)
                | (_byte_at(data, at + 3) << 24)
            ) & 0xFFFFFFFF
        return (
            (_byte_at(data, at) << 24)
            | (_byte_at(data, at + 1) << 16)
            | (_byte_at(data, at + 2) << 8)
            | _byte_at(data, at + 3)
        ) & 0xFFFFFFFF

    directory = tiff + u32(tiff + 4)
    if directory + 2 > len(data):
        return 1
    entries = u16(directory)
    for index in range(entries):
        entry = directory + 2 + index * 12
        if entry + 12 > len(data):
            return 1
        # 标签 0x0112 是 Orientation，SHORT，值就放在条目里（``exif.ts:20-24``）。
        if u16(entry) == _ORIENTATION_TAG:
            value = u16(entry + 8)
            return value if 1 <= value <= 8 else 1
    return 1


def _tiff_start(data: bytes | bytearray) -> int:
    """JPEG 的 APP1 或 WebP 的 EXIF 块里 TIFF 头的起点；没有则 -1（``exif.ts:30-59``）。"""
    if _byte_at(data, 0) == 0xFF and _byte_at(data, 1) == 0xD8:
        offset = 2
        while offset + 4 <= len(data):
            if _byte_at(data, offset) != 0xFF:
                return -1
            marker = _byte_at(data, offset + 1)
            if marker == 0xFF:
                offset += 1
                continue
            if marker == 0xE1 and _is_exif_header(data, offset + 4):
                return offset + 10
            offset += 2 + ((_byte_at(data, offset + 2) << 8) | _byte_at(data, offset + 3))
        return -1
    if _ascii(data, 0, "RIFF") and _ascii(data, 8, "WEBP"):
        offset = 12
        while offset + 8 <= len(data):
            size = (
                _byte_at(data, offset + 4)
                | (_byte_at(data, offset + 5) << 8)
                | (_byte_at(data, offset + 6) << 16)
                | (_byte_at(data, offset + 7) << 24)
            ) & 0xFFFFFFFF
            # 有些 WebP 会在 TIFF 头前写 "Exif\0\0"（``exif.ts:52``）。
            if _ascii(data, offset, "EXIF"):
                return offset + 14 if _is_exif_header(data, offset + 8) else offset + 8
            # RIFF 块按偶数对齐（``exif.ts:54-55``）。
            offset += 8 + size + (size % 2)
    return -1


def _is_exif_header(data: bytes | bytearray, offset: int) -> bool:
    return (
        _ascii(data, offset, "Exif")
        and _byte_at(data, offset + 4) == 0
        and _byte_at(data, offset + 5) == 0
    )


def _ascii(data: bytes | bytearray, offset: int, text: str) -> bool:
    return all(_byte_at(data, offset + index) == ord(char) for index, char in enumerate(text))


def _byte_at(data: bytes | bytearray, index: int) -> int:
    """JS 的 ``bytes[index] ?? 0``；负下标在 Python 里是从尾部数，必须挡住。"""
    return data[index] if 0 <= index < len(data) else 0
