"""``pi_durable.images.exif`` 的单元测试（上游 ``durable/src/images/exif.ts``，70 行）。

夹具按容器格式手工拼装：JPEG 的 APP1 / WebP 的 EXIF 块 + TIFF 头 + IFD0。
这样每个断言失败时都能直接对着字节看出是哪一段偏移错了。
"""

from __future__ import annotations

import struct

import pytest

from pi_durable.images.exif import exif_orientation

# --------------------------------------------------------------------------------------
# TIFF / EXIF 夹具
# --------------------------------------------------------------------------------------


def tiff_with_orientation(orientation: int, *, little: bool = True) -> bytes:
    """一个含单个 Orientation 条目的最小 TIFF 头。"""
    order = b"II" if little else b"MM"
    endian = "<" if little else ">"
    header = order + struct.pack(f"{endian}H", 42) + struct.pack(f"{endian}I", 8)
    entry = (
        struct.pack(f"{endian}HHI", 0x0112, 3, 1)
        + struct.pack(f"{endian}H", orientation)
        + b"\x00\x00"
    )
    ifd = struct.pack(f"{endian}H", 1) + entry + struct.pack(f"{endian}I", 0)
    return header + ifd


def jpeg_with_exif(tiff: bytes) -> bytes:
    """SOI + APP1("Exif\\0\\0" + TIFF) + EOI。

    ``exif.ts:39`` 在 APP1 里看到 ``Exif\\0\\0`` 时返回 ``offset + 10``：
    ``offset`` 指向 ``0xFF``，跳过 2 字节标记 + 2 字节长度 + 6 字节 ``Exif\\0\\0``。
    """
    payload = b"Exif\x00\x00" + tiff
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    return b"\xff\xd8" + app1 + b"\xff\xd9"


def webp_with_exif(tiff: bytes, *, with_exif_prefix: bool = True) -> bytes:
    """RIFF/WEBP 容器里一个 ``EXIF`` 块。

    ``with_exif_prefix=True`` 时块内以 ``Exif\\0\\0`` 开头（``exif.ts:53`` 返回 ``offset+14``），
    否则 TIFF 头直接跟在块头后（返回 ``offset+8``）。
    """
    payload = (b"Exif\x00\x00" + tiff) if with_exif_prefix else tiff
    chunk = b"EXIF" + struct.pack("<I", len(payload)) + payload
    chunk += b"\x00" * (len(chunk) % 2)  # RIFF 块偶数对齐
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


# --------------------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("orientation", [1, 2, 3, 4, 5, 6, 7, 8])
def test_reads_every_valid_orientation(orientation: int) -> None:
    assert exif_orientation(jpeg_with_exif(tiff_with_orientation(orientation))) == orientation


@pytest.mark.parametrize("orientation", [0, 9, 65535])
def test_out_of_range_orientation_falls_back_to_one(orientation: int) -> None:
    """``exif.ts:23``：只有 1..8 才算数，其余一律 1。"""
    assert exif_orientation(jpeg_with_exif(tiff_with_orientation(orientation))) == 1


def test_big_endian_tiff() -> None:
    """``MM`` 字节序（``exif.ts:7`` 的 ``little`` 判定）。"""
    assert exif_orientation(jpeg_with_exif(tiff_with_orientation(6, little=False))) == 6


def test_webp_exif_chunk_with_prefix() -> None:
    assert exif_orientation(webp_with_exif(tiff_with_orientation(8))) == 8


def test_webp_exif_chunk_without_prefix() -> None:
    """有些实现不写 ``Exif\\0\\0`` 前缀，TIFF 头直接跟在块头后（``exif.ts:53``）。"""
    assert exif_orientation(webp_with_exif(tiff_with_orientation(3), with_exif_prefix=False)) == 3


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\xff\xd8",  # 只有 SOI
        b"\xff\xd8\xff\xd9",  # SOI + EOI，没有 APP1
        b"\x89PNG\r\n\x1a\n",  # 不是 JPEG 也不是 WebP
        b"RIFF\x00\x00\x00\x00WEBP",  # WebP 头但没有块
        b"\x00" * 64,
    ],
)
def test_absent_exif_returns_one(data: bytes) -> None:
    assert exif_orientation(data) == 1


def test_clears_when_tiff_offset_points_past_end() -> None:
    """TIFF 头声明的 IFD 偏移越界时返回 1，不抛异常（``exif.ts:15``）。"""
    endian = "<"
    header = b"II" + struct.pack(f"{endian}H", 42) + struct.pack(f"{endian}I", 0xFFFFFF)
    assert exif_orientation(jpeg_with_exif(header)) == 1


def test_clears_when_entry_runs_past_end() -> None:
    """条目数声明得比实际字节多时返回 1（``exif.ts:19``）。"""
    endian = "<"
    header = b"II" + struct.pack(f"{endian}H", 42) + struct.pack(f"{endian}I", 8)
    ifd = struct.pack(f"{endian}H", 5)  # 声称 5 个条目，实际一个都没有
    assert exif_orientation(jpeg_with_exif(header + ifd)) == 1


def test_skips_non_orientation_tags() -> None:
    """IFD 里有别的标签时继续找 0x0112（``exif.ts:17-25``）。"""
    endian = "<"
    header = b"II" + struct.pack(f"{endian}H", 42) + struct.pack(f"{endian}I", 8)
    other = struct.pack(f"{endian}HHI", 0x011A, 3, 1) + struct.pack(f"{endian}H", 72) + b"\x00\x00"
    orientation = (
        struct.pack(f"{endian}HHI", 0x0112, 3, 1) + struct.pack(f"{endian}H", 6) + b"\x00\x00"
    )
    ifd = struct.pack(f"{endian}H", 2) + other + orientation + struct.pack(f"{endian}I", 0)
    assert exif_orientation(jpeg_with_exif(header + ifd)) == 6


def test_jpeg_with_other_app_segment_before_app1() -> None:
    """APP0（JFIF）在 APP1 之前时仍能找到 EXIF（``exif.ts:32-42`` 的段遍历）。"""
    app0_payload = b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00"
    app0 = b"\xff\xe0" + struct.pack(">H", len(app0_payload) + 2) + app0_payload
    app1_payload = b"Exif\x00\x00" + tiff_with_orientation(6)
    app1 = b"\xff\xe1" + struct.pack(">H", len(app1_payload) + 2) + app1_payload
    assert exif_orientation(b"\xff\xd8" + app0 + app1 + b"\xff\xd9") == 6


def test_stops_at_non_marker_byte() -> None:
    """段遍历遇到非 ``0xFF`` 就放弃（``exif.ts:33``），返回 1 而不是继续瞎找。"""
    broken = b"\xff\xd8\xff\xe0" + struct.pack(">H", 4) + b"\x00\x00" + b"\x42" + b"\xff\xe1"
    assert exif_orientation(broken) == 1
