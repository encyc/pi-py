"""``pi_durable.tools.image`` / ``image_processor`` 的单元测试。

上游 ``durable/src/tools/image.ts``（182 行）与 ``image-processor.ts``（58 行）。
测试用的字节串都是**按容器格式手工拼出来的最小结构**，不是随手编的魔法字节——
这样每个断言失败时都能对着格式规范看出是哪一段错了。
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError

import pytest

from pi_durable.tools.image import (
    BLOCK_BYTES,
    HEADER_BYTES,
    detect_supported_image_mime_type,
    detect_supported_image_mime_type_of,
    image_dimensions,
)
from pi_durable.tools.image_processor import (
    DEFAULT_IMAGE_LIMITS,
    INLINE_IMAGE_TYPES,
    ImageLimits,
    ImageSize,
    PreparedImage,
    Resized,
    base64_length,
    to_base64,
)

# --------------------------------------------------------------------------------------
# 夹具构造
# --------------------------------------------------------------------------------------


def png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload))
    )


def make_png(width: int, height: int, *, animated: bool = False) -> bytes:
    """签名 + IHDR [+ acTL] + IDAT + IEND 的最小合法 PNG。"""
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    body = png_chunk(b"IHDR", header)
    if animated:
        body += png_chunk(b"acTL", struct.pack(">II", 2, 0))
    body += png_chunk(b"IDAT", zlib.compress(b"\x00" * (width * 4 + 1) * height))
    return b"\x89PNG\r\n\x1a\n" + body + png_chunk(b"IEND", b"")


def make_gif(width: int, height: int, *, version: bytes = b"89a") -> bytes:
    return b"GIF" + version + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def make_bmp(width: int, height: int, *, bits_per_pixel: int = 24) -> bytes:
    dib_header_size = 40
    pixel_data_offset = 14 + dib_header_size
    pixel_bytes = ((width * bits_per_pixel + 31) // 32) * 4 * height
    file_size = pixel_data_offset + pixel_bytes
    header = b"BM" + struct.pack("<IHHI", file_size, 0, 0, pixel_data_offset)
    dib = struct.pack(
        "<IiiHHIIiiII", dib_header_size, width, height, 1, bits_per_pixel, 0, 0, 0, 0, 0, 0
    )
    return header + dib + b"\x00" * pixel_bytes


def make_webp_vp8x(width: int, height: int) -> bytes:
    """``VP8X`` 变体：宽高各存 24 位、从 1 起算（``image.ts:96-99``）。"""
    canvas = (
        struct.pack("<I", 0)
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    chunk = b"VP8X" + struct.pack("<I", len(canvas)) + canvas
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def make_webp_vp8(width: int, height: int) -> bytes:
    """``VP8 `` 变体：宽高在偏移 26 / 28，各 14 位（``image.ts:90-91``）。"""
    payload = b"\x00" * 6 + struct.pack("<HH", width & 0x3FFF, height & 0x3FFF) + b"\x00" * 4
    chunk = b"VP8 " + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def make_jpeg(width: int, height: int, *, marker: int = 0xC0) -> bytes:
    """SOI + SOF0 + EOI。SOF 段：长度(2) 精度(1) 高(2) 宽(2)。"""
    sof = (
        b"\xff"
        + bytes([marker])
        + struct.pack(">HBHH", 17, 8, height, width)
        + b"\x03"
        + b"\x00" * 9
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


class BytesSource:
    """:class:`~pi_durable.tools.image.ByteSource` 的测试替身，记录读取位置。"""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.reads: list[tuple[int, int]] = []

    @property
    def size(self) -> int:
        return len(self._data)

    async def read(self, offset: int, length: int) -> bytes:
        self.reads.append((offset, length))
        return self._data[offset : offset + length]


# --------------------------------------------------------------------------------------
# detect_supported_image_mime_type
# --------------------------------------------------------------------------------------


def test_detects_png() -> None:
    assert detect_supported_image_mime_type(make_png(4, 4)) == "image/png"


def test_detects_jpeg() -> None:
    assert detect_supported_image_mime_type(make_jpeg(4, 4)) == "image/jpeg"


def test_rejects_jpeg_f7_variant() -> None:
    """``image.ts:47``：``buffer[3] === 0xf7`` 的 JPEG 判为不支持。"""
    assert detect_supported_image_mime_type(b"\xff\xd8\xff\xf7rest") is None


def test_detects_gif_both_versions() -> None:
    assert detect_supported_image_mime_type(make_gif(4, 4, version=b"87a")) == "image/gif"
    assert detect_supported_image_mime_type(make_gif(4, 4, version=b"89a")) == "image/gif"


def test_detects_webp() -> None:
    assert detect_supported_image_mime_type(make_webp_vp8x(4, 4)) == "image/webp"


def test_detects_bmp() -> None:
    assert detect_supported_image_mime_type(make_bmp(4, 4)) == "image/bmp"


def test_rejects_animated_png() -> None:
    """APNG 会让 provider 拿到意料之外的帧数（``image.ts:61-73``）。"""
    assert detect_supported_image_mime_type(make_png(4, 4, animated=True)) is None


def test_rejects_png_without_ihdr_first_chunk() -> None:
    """``is_png`` 要求签名后第一块是长度 13 的 ``IHDR``（``image.ts:55-59``）。"""
    bogus = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 4) + b"IHDR" + b"\x00" * 4
    assert detect_supported_image_mime_type(bogus) is None


def test_rejects_bmp_with_bad_dib_header() -> None:
    good = make_bmp(4, 4)
    # 把 DIB 头大小改成 39（不在 12 / 40..124 之内）→ 判为假 BMP。
    bad = good[:14] + struct.pack("<I", 39) + good[18:]
    assert detect_supported_image_mime_type(bad) is None


def test_rejects_bmp_with_odd_bits_per_pixel() -> None:
    assert detect_supported_image_mime_type(make_bmp(4, 4, bits_per_pixel=7)) is None


def test_rejects_unknown_bytes() -> None:
    assert detect_supported_image_mime_type(b"not an image at all") is None


def test_handles_truncated_input() -> None:
    """极短输入不能抛异常（``_byte_at`` 模拟 JS 的 ``?? 0``）。"""
    for length in range(0, 40):
        detect_supported_image_mime_type(make_png(4, 4)[:length])


# --------------------------------------------------------------------------------------
# detect_supported_image_mime_type_of
# --------------------------------------------------------------------------------------


async def test_detect_of_reads_only_header_for_non_png() -> None:
    source = BytesSource(make_jpeg(4, 4))
    assert await detect_supported_image_mime_type_of(source) == "image/jpeg"
    assert source.reads == [(0, HEADER_BYTES)]


async def test_detect_of_walks_png_chunks() -> None:
    source = BytesSource(make_png(4, 4))
    assert await detect_supported_image_mime_type_of(source) == "image/png"
    # 第一次读头部，第二次从偏移 8 开始分块读 PNG 块。
    assert source.reads[0] == (0, HEADER_BYTES)
    assert source.reads[1][0] == 8
    assert source.reads[1][1] == BLOCK_BYTES


async def test_detect_of_rejects_animated_png() -> None:
    source = BytesSource(make_png(4, 4, animated=True))
    assert await detect_supported_image_mime_type_of(source) is None


async def test_detect_of_truncated_png_still_reports_png() -> None:
    """PNG 在块遍历够不到 ``IDAT`` 时按**静图**处理（``image.ts:34-43`` 的循环自然退出）。

    这不是遗漏：遍历的语义是「走到 ``acTL`` 就是动图」，走不到就只能当作没看到动图标记。
    真正读不到东西（头都不全）时由 :func:`is_png` 先挡掉。
    """
    source = BytesSource(make_png(4, 4)[:33])
    assert await detect_supported_image_mime_type_of(source) == "image/png"


async def test_detect_of_reports_non_png_formats() -> None:
    """非 PNG 时只读一次头，判定沿用同步版本（``image.ts:18``）。"""
    source = BytesSource(b"\xff\xd8\xff\xe0" + b"\x00" * 40)
    assert await detect_supported_image_mime_type_of(source) == "image/jpeg"


async def test_detect_of_returns_none_for_unknown_bytes() -> None:
    source = BytesSource(b"this is definitely not an image" + b"\x00" * 32)
    assert await detect_supported_image_mime_type_of(source) is None


# --------------------------------------------------------------------------------------
# image_dimensions
# --------------------------------------------------------------------------------------


def test_png_dimensions() -> None:
    assert image_dimensions(make_png(123, 45), "image/png") == (123, 45)


def test_gif_dimensions() -> None:
    assert image_dimensions(make_gif(123, 45), "image/gif") == (123, 45)


def test_webp_vp8x_dimensions() -> None:
    assert image_dimensions(make_webp_vp8x(123, 45), "image/webp") == (123, 45)


def test_webp_vp8_dimensions() -> None:
    assert image_dimensions(make_webp_vp8(123, 45), "image/webp") == (123, 45)


def test_jpeg_dimensions() -> None:
    assert image_dimensions(make_jpeg(123, 45), "image/jpeg") == (123, 45)


def test_jpeg_dimensions_skips_non_sof_markers() -> None:
    """跳过 DHT(0xC4) / JPG(0xC8) / DAC(0xCC)，它们与 SOF0..SOF15 共用编号段。

    真实 JPEG 里这些段出现在 SOF 之前，所以「跳过」是必须的。
    """
    for marker in (0xC4, 0xC8, 0xCC):
        # 段长字段含它自己那 2 字节，所以 4 表示段内还有 2 字节载荷。
        segment = b"\xff" + bytes([marker]) + struct.pack(">H", 4) + b"\x00\x00"
        data = b"\xff\xd8" + segment + make_jpeg(123, 45)[2:]
        assert image_dimensions(data, "image/jpeg") == (123, 45), f"marker=0x{marker:02X}"


@pytest.mark.parametrize("mime_type", ["image/png", "image/gif", "image/webp", "image/jpeg"])
def test_dimensions_return_none_when_too_short(mime_type: str) -> None:
    assert image_dimensions(b"", mime_type) is None
    assert image_dimensions(b"\x00" * 5, mime_type) is None


def test_dimensions_unknown_mime_type() -> None:
    assert image_dimensions(make_png(4, 4), "image/bmp") is None


# --------------------------------------------------------------------------------------
# image_processor
# --------------------------------------------------------------------------------------


def test_default_limits_match_upstream() -> None:
    assert ImageLimits(max_width=2000, max_height=2000, max_bytes=4718592) == DEFAULT_IMAGE_LIMITS
    assert DEFAULT_IMAGE_LIMITS.max_bytes == int(4.5 * 1024 * 1024) == 4_718_592


def test_inline_image_types_match_upstream() -> None:
    assert {"image/png", "image/jpeg", "image/gif", "image/webp"} == INLINE_IMAGE_TYPES
    assert "image/bmp" not in INLINE_IMAGE_TYPES


def test_to_base64_matches_stdlib() -> None:
    import base64

    for payload in (b"", b"a", b"ab", b"abc", b"\x00\xff\xfe", bytes(range(256))):
        assert to_base64(payload) == base64.b64encode(payload).decode("ascii")


def test_to_base64_handles_large_payload() -> None:
    """上游为「3 MiB 图像曾经吃 140 MB 堆」专门改过分块（``CHANGELOG.md:16``）。

    Python 侧直接走 ``base64.b64encode``，这里确认大输入也正确。
    """
    payload = bytes(range(256)) * (3 * 1024 * 1024 // 256)
    assert len(to_base64(payload)) == base64_length(len(payload))


def test_base64_length() -> None:
    assert base64_length(0) == 0
    assert base64_length(1) == 4
    assert base64_length(3) == 4
    assert base64_length(4) == 8
    assert base64_length(6) == 8
    assert base64_length(7) == 12


def test_prepared_image_defaults() -> None:
    image = PreparedImage(data="AAAA", mime_type="image/png")
    assert image.resized is None
    assert image.converted_from is None


def test_prepared_image_is_frozen() -> None:
    image = PreparedImage(data="AAAA", mime_type="image/png")
    with pytest.raises(FrozenInstanceError):
        image.data = "BBBB"  # type: ignore[misc]


def test_resized_shape() -> None:
    resized = Resized(from_=ImageSize(4000, 3000), to=ImageSize(2000, 1500))
    assert resized.from_.width == 4000
    assert resized.to.height == 1500


async def _empty_processor_stream() -> AsyncIterator[None]:  # pragma: no cover - 仅用于类型演示
    yield None
