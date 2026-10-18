"""不解码图像就认出它的格式与尺寸 —— 移植 ``durable/src/tools/image-processor`` 的同级文件
``durable/src/tools/image.ts``（182 行）。

三件事：

* :func:`detect_supported_image_mime_type` —— 从**头部字节**判定 MIME，认得出
  JPEG / PNG / GIF / WebP / BMP；动图 PNG（APNG）与 JPEG 的 0xF7 变体返回 ``None``
* :func:`detect_supported_image_mime_type_of` —— 同一判定，但只读一个 :class:`ByteSource`
  的头部与 PNG 的块头，避免为一张大图把整个文件读进内存
* :func:`image_dimensions` —— 从头部读出宽高（PNG 的 IHDR、GIF 的 screen、
  WebP 的 VP8/VP8L/VP8X、JPEG 的第一个 SOF），不解码

全部是纯函数，除 :class:`ByteSource` 这一个协议外没有依赖。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

__all__ = [
    "BLOCK_BYTES",
    "HEADER_BYTES",
    "ByteSource",
    "detect_supported_image_mime_type",
    "detect_supported_image_mime_type_of",
    "image_dimensions",
]

_PNG_SIGNATURE = (0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)
#: 除 APNG 块遍历外所有检查都够用的字节数：BMP 最多读到偏移 29（``image.ts:3``）。
HEADER_BYTES = 32
#: APNG 块遍历一次读多少（``image.ts:4``）。
BLOCK_BYTES = 64 * 1024


class ByteSource(Protocol):
    """一个 ``size`` 字节文件的定位读取（``image.ts:7-10``）。"""

    @property
    def size(self) -> int: ...

    async def read(self, offset: int, length: int) -> bytes: ...


def _byte_at(data: bytes | bytearray, index: int) -> int:
    """JS 的 ``bytes[index] ?? 0``（``image.ts`` 全程使用）。

    ``Uint8Array`` 越界读返回 ``undefined``，上游一律用 ``?? 0`` 兜成 0；
    负下标同理。Python 里负下标是**从尾部数**，必须显式挡住，否则行为完全不同。
    """
    return data[index] if 0 <= index < len(data) else 0


def _read_uint16_le(buffer: bytes | bytearray, offset: int) -> int:
    return _byte_at(buffer, offset) + (_byte_at(buffer, offset + 1) << 8)


def _read_uint16_be(buffer: bytes | bytearray, offset: int) -> int:
    return (_byte_at(buffer, offset) << 8) + _byte_at(buffer, offset + 1)


def _read_uint32_be(buffer: bytes | bytearray, offset: int) -> int:
    # 上游用 `* 0x1000000` 而不是 `<< 24`，避免 JS 的 32 位有符号溢出（``image.ts:155``）。
    # Python 整数无上限，两种写法等价，这里保持上游写法以便逐行对照。
    return (
        _byte_at(buffer, offset) * 0x1000000
        + (_byte_at(buffer, offset + 1) << 16)
        + (_byte_at(buffer, offset + 2) << 8)
        + _byte_at(buffer, offset + 3)
    )


def _read_uint32_le(buffer: bytes | bytearray, offset: int) -> int:
    return (
        _byte_at(buffer, offset)
        + (_byte_at(buffer, offset + 1) << 8)
        + (_byte_at(buffer, offset + 2) << 16)
        + _byte_at(buffer, offset + 3) * 0x1000000
    )


def _starts_with(buffer: bytes | bytearray, prefix: Sequence[int]) -> bool:
    if len(buffer) < len(prefix):
        return False
    return all(_byte_at(buffer, index) == byte for index, byte in enumerate(prefix))


def _starts_with_ascii(buffer: bytes | bytearray, offset: int, text: str) -> bool:
    if len(buffer) < offset + len(text):
        return False
    return all(_byte_at(buffer, offset + index) == ord(char) for index, char in enumerate(text))


async def detect_supported_image_mime_type_of(source: ByteSource) -> str | None:
    """整个文件的格式判定，只读头部；PNG 额外读到第一个 ``acTL`` 或 ``IDAT``（``image.ts:16-20``）。"""
    header = await source.read(0, HEADER_BYTES)
    if not _starts_with(header, _PNG_SIGNATURE):
        return detect_supported_image_mime_type(header)
    return "image/png" if _is_png(header) and not await _is_animated_png_of(source) else None


async def _is_animated_png_of(source: ByteSource) -> bool:
    """``is_animated_png`` 的分块读取版本（``image.ts:23-44``）。"""
    block: bytes = b""
    block_start = 0

    async def bytes_at(offset: int, length: int) -> bytes:
        nonlocal block, block_start
        if offset < block_start or offset + length > block_start + len(block):
            block_start = offset
            block = await source.read(offset, BLOCK_BYTES)
        return block[offset - block_start : offset - block_start + length]

    offset = len(_PNG_SIGNATURE)
    while offset + 8 <= source.size:
        chunk_header = await bytes_at(offset, 8)
        chunk_length = _read_uint32_be(chunk_header, 0)
        if _starts_with_ascii(chunk_header, 4, "acTL"):
            return True
        if _starts_with_ascii(chunk_header, 4, "IDAT"):
            return False
        next_offset = offset + 8 + chunk_length + 4
        if next_offset <= offset or next_offset > source.size:
            return False
        offset = next_offset
    return False


def detect_supported_image_mime_type(buffer: bytes | bytearray) -> str | None:
    """从头部字节判定 MIME；不支持或无法判定时 ``None``（``image.ts:46-53``）。

    * JPEG 的 ``0xF7`` 变体（无 SOI 后的 DHT）判为不支持
    * PNG 只认签名 + 第一块是 13 字节的 ``IHDR``，且不是 APNG
    * GIF 认 ``GIF87a`` / ``GIF89a``
    * WebP 认 ``RIFF....WEBP``
    * BMP 还要过 :func:`_is_bmp` 的 DIB 头校验
    """
    if _starts_with(buffer, (0xFF, 0xD8, 0xFF)):
        return None if _byte_at(buffer, 3) == 0xF7 else "image/jpeg"
    if _starts_with(buffer, _PNG_SIGNATURE):
        return "image/png" if _is_png(buffer) and not _is_animated_png(buffer) else None
    if _starts_with_ascii(buffer, 0, "GIF87a") or _starts_with_ascii(buffer, 0, "GIF89a"):
        return "image/gif"
    if _starts_with_ascii(buffer, 0, "RIFF") and _starts_with_ascii(buffer, 8, "WEBP"):
        return "image/webp"
    if _starts_with_ascii(buffer, 0, "BM") and _is_bmp(buffer):
        return "image/bmp"
    return None


def _is_png(buffer: bytes | bytearray) -> bool:
    """签名之后第一块必须是长度 13 的 ``IHDR``（``image.ts:55-59``）。"""
    return (
        len(buffer) >= 16
        and _read_uint32_be(buffer, len(_PNG_SIGNATURE)) == 13
        and _starts_with_ascii(buffer, 12, "IHDR")
    )


def _is_animated_png(buffer: bytes | bytearray) -> bool:
    """走到第一个 ``acTL``（动图）或 ``IDAT``（静图）为止（``image.ts:61-73``）。"""
    offset = len(_PNG_SIGNATURE)
    while offset + 8 <= len(buffer):
        chunk_length = _read_uint32_be(buffer, offset)
        chunk_type_offset = offset + 4
        if _starts_with_ascii(buffer, chunk_type_offset, "acTL"):
            return True
        if _starts_with_ascii(buffer, chunk_type_offset, "IDAT"):
            return False
        next_offset = offset + 8 + chunk_length + 4
        if next_offset <= offset or next_offset > len(buffer):
            return False
        offset = next_offset
    return False


def image_dimensions(
    buffer: bytes | bytearray,
    mime_type: str,
) -> tuple[int, int] | None:
    """头部声明的宽高，不解码；读不到时 ``None``（``image.ts:79-119``）。

    返回 ``(width, height)``。上游返回 ``{width, height} | undefined``。
    """
    if mime_type == "image/png":
        if len(buffer) < 24:
            return None
        return (_read_uint32_be(buffer, 16), _read_uint32_be(buffer, 20))
    if mime_type == "image/gif":
        if len(buffer) < 10:
            return None
        return (_read_uint16_le(buffer, 6), _read_uint16_le(buffer, 8))
    if mime_type == "image/webp":
        if len(buffer) < 30:
            return None
        if _starts_with_ascii(buffer, 12, "VP8 "):
            return (_read_uint16_le(buffer, 26) & 0x3FFF, _read_uint16_le(buffer, 28) & 0x3FFF)
        if _starts_with_ascii(buffer, 12, "VP8L"):
            bits = _read_uint32_le(buffer, 21)
            return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        if _starts_with_ascii(buffer, 12, "VP8X"):

            def uint24(offset: int) -> int:
                return _read_uint16_le(buffer, offset) + (_byte_at(buffer, offset + 2) << 16)

            return (uint24(24) + 1, uint24(27) + 1)
        return None
    if mime_type == "image/jpeg":
        offset = 2
        while offset + 9 <= len(buffer):
            if _byte_at(buffer, offset) != 0xFF:
                return None
            marker = _byte_at(buffer, offset + 1)
            if marker == 0xFF:
                offset += 1
                continue
            # SOF0..SOF15，但 DHT(0xC4) / JPG(0xC8) / DAC(0xCC) 与它们共用这一段编号。
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (_read_uint16_be(buffer, offset + 7), _read_uint16_be(buffer, offset + 5))
            offset += 2 + _read_uint16_be(buffer, offset + 2)
        return None
    return None


def _is_bmp(buffer: bytes | bytearray) -> bool:
    """校验 BMP 的 DIB 头，排除只以 ``BM`` 开头的其它数据（``image.ts:121-143``）。"""
    if len(buffer) < 26:
        return False
    declared_file_size = _read_uint32_le(buffer, 2)
    pixel_data_offset = _read_uint32_le(buffer, 10)
    dib_header_size = _read_uint32_le(buffer, 14)
    if declared_file_size != 0 and declared_file_size < 26:
        return False
    if pixel_data_offset < 14 + dib_header_size:
        return False
    if declared_file_size != 0 and pixel_data_offset >= declared_file_size:
        return False

    if dib_header_size == 12:
        color_planes = _read_uint16_le(buffer, 22)
        bits_per_pixel = _read_uint16_le(buffer, 24)
    elif 40 <= dib_header_size <= 124:
        if len(buffer) < 30:
            return False
        color_planes = _read_uint16_le(buffer, 26)
        bits_per_pixel = _read_uint16_le(buffer, 28)
    else:
        return False
    return color_planes == 1 and bits_per_pixel in (1, 4, 8, 16, 24, 32)
