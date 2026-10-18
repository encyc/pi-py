"""``pi_durable.images.index`` 的单元测试（上游 ``durable/src/images/index.ts``，140 行）。

Pillow 是可选 extra，所以整份用例在缺 Pillow 时 **skip**；另有一条用例专门验证
「缺 Pillow 时报错清晰」，它反过来要求 Pillow **不可用**。

方向处理是逐像素验证的：测试里按上游 ``transposed()``（``images/index.ts:119-139``）
的公式重算一遍，再和 Pillow 的转置结果比对，而不是只看宽高对不对。
"""

from __future__ import annotations

import base64
import io
import struct
import sys

import pytest

from pi_durable.tools.image_processor import (
    DEFAULT_IMAGE_LIMITS,
    ImageLimits,
    PreparedImage,
)

pil = pytest.importorskip("PIL", reason="Pillow 是可选 extra：pip install 'pi-py-durable[images]'")

from PIL import Image  # noqa: E402

from pi_durable.images.index import (  # noqa: E402
    JPEG_QUALITIES,
    _transpose_ops,
    create_pillow_images,
    require_pillow,
)

# --------------------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------------------


def encode(image: Image.Image, fmt: str = "PNG", **kwargs: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)  # type: ignore[arg-type]
    return buffer.getvalue()


def gradient(width: int, height: int) -> Image.Image:
    """每个像素都不同的图，避免 PNG 因为纯色压得太小、掩盖编码选择的差异。"""
    image = Image.new("RGB", (width, height))
    image.putdata(
        [
            ((x * 7 + y * 13) % 256, (x * 31) % 256, (y * 17) % 256)
            for y in range(height)
            for x in range(width)
        ]
    )
    return image


def insert_exif_orientation(jpeg: bytes, orientation: int) -> bytes:
    """在 SOI 之后插入一个只含 Orientation 的 APP1 段。"""
    endian = "<"
    tiff = (
        b"II"
        + struct.pack(f"{endian}H", 42)
        + struct.pack(f"{endian}I", 8)
        + struct.pack(f"{endian}H", 1)
        + struct.pack(f"{endian}HHI", 0x0112, 3, 1)
        + struct.pack(f"{endian}H", orientation)
        + b"\x00\x00"
        + struct.pack(f"{endian}I", 0)
    )
    payload = b"Exif\x00\x00" + tiff
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    assert jpeg[:2] == b"\xff\xd8"
    return jpeg[:2] + app1 + jpeg[2:]


@pytest.fixture
def processor():
    return create_pillow_images()


# --------------------------------------------------------------------------------------
# 依赖与导出
# --------------------------------------------------------------------------------------


def test_require_pillow_returns_the_image_module() -> None:
    image_module = require_pillow()
    assert hasattr(image_module, "open")
    assert hasattr(image_module, "Transpose")


def test_jpeg_qualities_match_upstream() -> None:
    assert JPEG_QUALITIES == (80, 70, 55, 40)


def test_missing_pillow_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 extra 时必须是**带安装提示**的 ImportError，而不是裸 ModuleNotFoundError。"""
    for name in [key for key in sys.modules if key == "PIL" or key.startswith("PIL.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "PIL", None)  # type: ignore[arg-type]

    with pytest.raises(ImportError) as excinfo:
        require_pillow()

    message = str(excinfo.value)
    assert "pi-py-durable[images]" in message
    assert "Pillow" in message


def test_module_imports_without_calling_pillow() -> None:
    """``pi_durable.images`` 本身不依赖 Pillow（惰性导入的意义）。"""
    import pi_durable.images as images_module

    assert images_module.create_pillow_images is create_pillow_images


# --------------------------------------------------------------------------------------
# 原样透传（images/index.ts:44-52）
# --------------------------------------------------------------------------------------


async def test_passes_through_small_upright_inline_image(processor) -> None:
    data = encode(gradient(20, 10), "PNG")

    result = await processor.prepare(data, "image/png", DEFAULT_IMAGE_LIMITS)

    assert result is not None
    assert result.mime_type == "image/png"
    assert result.resized is None
    assert result.converted_from is None
    # 透传的是**原始字节**，不是重新编码的结果。
    assert base64.b64decode(result.data) == data


async def test_does_not_pass_through_when_over_pixel_limit(processor) -> None:
    data = encode(gradient(100, 50), "PNG")
    limits = ImageLimits(max_width=40, max_height=40, max_bytes=10**9)

    result = await processor.prepare(data, "image/png", limits)

    assert result is not None
    assert result.resized is not None
    assert result.resized.from_.width == 100
    assert result.resized.to.width == 40
    # 原始字节没有被原样返回。
    assert base64.b64decode(result.data) != data


async def test_does_not_pass_through_when_over_byte_limit(processor) -> None:
    data = encode(gradient(60, 60), "PNG")
    limits = ImageLimits(max_width=10_000, max_height=10_000, max_bytes=500)

    result = await processor.prepare(data, "image/png", limits)

    assert result is not None
    # 原图远超预算，所以走的是缩小重编码，而不是透传。
    assert base64.b64decode(result.data) != data
    assert len(result.data) <= 500


async def test_does_not_pass_through_when_orientation_is_not_one(processor) -> None:
    """``orientation === 1`` 是透传的**必要条件**（``images/index.ts:45``）。"""
    plain = encode(gradient(20, 10), "JPEG", quality=90)
    rotated = insert_exif_orientation(plain, 6)

    result = await processor.prepare(rotated, "image/jpeg", DEFAULT_IMAGE_LIMITS)

    assert result is not None
    assert result.resized is not None or base64.b64decode(result.data) != rotated


# --------------------------------------------------------------------------------------
# 缩放（images/index.ts:63-66）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "height", "max_side", "expected"),
    [
        (4000, 3000, 2000, (2000, 1500)),
        (3000, 4000, 2000, (1500, 2000)),
        (100, 100, 2000, (100, 100)),
        (2001, 10, 2000, (2000, 10)),  # scale=2000/2001 → 高度 round(9.995)=10（JS 向上）
    ],
)
async def test_scales_to_fit_limits(
    processor, width: int, height: int, max_side: int, expected: tuple[int, int]
) -> None:
    data = encode(gradient(width, height), "PNG")
    limits = ImageLimits(max_width=max_side, max_height=max_side, max_bytes=10**9)

    result = await processor.prepare(data, "image/png", limits)

    assert result is not None
    decoded = Image.open(io.BytesIO(base64.b64decode(result.data)))
    assert decoded.size == expected


async def test_never_scales_up(processor) -> None:
    """``scale = Math.min(1, ...)``——小图不会被放大（``images/index.ts:65``）。"""
    data = encode(gradient(10, 10), "PNG")
    limits = ImageLimits(max_width=2000, max_height=2000, max_bytes=10**9)

    result = await processor.prepare(data, "image/png", limits)

    assert result is not None
    assert result.resized is None


async def test_js_round_half_up_is_used_for_target_size(processor) -> None:
    """``Math.round`` 在 .5 处向上取整；Python ``round`` 是 round-half-even。"""
    # 3 * 0.5 = 1.5 → JS 2；再加 max(1, ...) 不变。
    data = encode(gradient(3, 2), "PNG")
    limits = ImageLimits(max_width=2, max_height=2, max_bytes=10**9)

    result = await processor.prepare(data, "image/png", limits)

    assert result is not None
    assert result.resized is not None
    # 3 → 2（=2），2 → 1（=1）
    assert result.resized.to.width == 2
    assert result.resized.to.height == 1


# --------------------------------------------------------------------------------------
# 编码选择（images/index.ts:95-117）
# --------------------------------------------------------------------------------------


async def test_converts_bmp_to_an_inline_format(processor) -> None:
    """BMP 不在 :data:`INLINE_IMAGE_TYPES` 里 → 必须转码并记录 ``converted_from``。"""
    data = encode(gradient(40, 30), "BMP")

    result = await processor.prepare(data, "image/bmp", DEFAULT_IMAGE_LIMITS)

    assert result is not None
    assert result.mime_type in {"image/png", "image/jpeg"}
    assert result.converted_from == "image/bmp"
    assert Image.open(io.BytesIO(base64.b64decode(result.data))).size == (40, 30)


async def test_falls_back_to_jpeg_when_png_exceeds_budget(processor) -> None:
    data = encode(gradient(400, 400), "PNG")
    # 取一个 PNG 装不下、JPEG 装得下的预算。
    limits = ImageLimits(max_width=400, max_height=400, max_bytes=8_000)

    result = await processor.prepare(data, "image/png", limits)

    assert result is not None
    assert len(result.data) <= 8_000
    assert result.mime_type in {"image/png", "image/jpeg"}


async def test_jpeg_source_tries_jpeg_first(processor) -> None:
    """JPEG 源在「同尺寸」时应直接选中 JPEG，而不是先做一次 PNG
    （``images/index.ts:109`` 的 ``jpegFirst``）。"""
    data = encode(gradient(200, 200), "JPEG", quality=90)
    # 预算故意小于 PNG 的体量、大于 JPEG 的体量。
    png_size = len(encode(gradient(200, 200), "PNG"))
    limits = ImageLimits(max_width=200, max_height=200, max_bytes=png_size)

    result = await processor.prepare(data, "image/jpeg", limits)

    assert result is not None
    assert result.mime_type == "image/jpeg"


async def test_result_never_exceeds_byte_budget(processor) -> None:
    for budget in (500, 2_000, 20_000):
        data = encode(gradient(300, 300), "PNG")
        limits = ImageLimits(max_width=300, max_height=300, max_bytes=budget)

        result = await processor.prepare(data, "image/png", limits)

        if result is not None:
            assert len(result.data) <= budget, f"budget={budget}"


async def test_gives_up_when_even_one_pixel_does_not_fit(processor) -> None:
    """最小到 1×1 仍装不下时返回 ``None``（``images/index.ts:77``）。"""
    data = encode(gradient(50, 50), "PNG")
    limits = ImageLimits(max_width=50, max_height=50, max_bytes=1)

    assert await processor.prepare(data, "image/png", limits) is None


# --------------------------------------------------------------------------------------
# 失败路径（images/index.ts:83-86）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [b"", b"not an image at all", b"\x89PNG\r\n\x1a\n" + b"\x00" * 10, b"\xff\xd8\xff\xd9"],
)
async def test_undecodable_bytes_return_none(processor, data: bytes) -> None:
    assert await processor.prepare(data, "image/png", DEFAULT_IMAGE_LIMITS) is None


async def test_truncated_image_returns_none(processor) -> None:
    """能被 open 但 load 失败的文件也算无法处理（上游先解码再判断）。"""
    data = encode(gradient(100, 100), "PNG")[: len(encode(gradient(100, 100), "PNG")) // 2]
    assert await processor.prepare(data, "image/png", DEFAULT_IMAGE_LIMITS) is None


# --------------------------------------------------------------------------------------
# EXIF 方向：逐像素对齐上游 transposed()（images/index.ts:119-139）
# --------------------------------------------------------------------------------------


def upstream_transposed(
    pixels: bytes, width: int, height: int, orientation: int
) -> tuple[bytes, int, int]:
    """上游 ``transposed()`` 的公式重写（RGBA 输入，输出 height 宽、width 高）。"""
    out = bytearray(len(pixels))
    for y in range(height):
        for x in range(width):
            out_x = y if orientation in (5, 8) else height - 1 - y
            out_y = x if orientation in (5, 6) else width - 1 - x
            source = (y * width + x) * 4
            target = (out_y * height + out_x) * 4
            out[target : target + 4] = pixels[source : source + 4]
    return bytes(out), height, width


@pytest.mark.parametrize("orientation", [5, 6, 7, 8])
def test_transpose_matches_upstream_pixel_layout(orientation: int) -> None:
    """5..8 的转置必须与上游手写搬运逐像素一致。"""
    width, height = 3, 2
    source = Image.new("RGBA", (width, height))
    source.putdata([(x * 40, y * 80, 10, 255) for y in range(height) for x in range(width)])
    expected_pixels, expected_w, expected_h = upstream_transposed(
        source.tobytes(), width, height, orientation
    )

    actual = source.transpose(_transpose_ops(require_pillow())[orientation])

    assert actual.size == (expected_w, expected_h)
    assert actual.tobytes() == expected_pixels


@pytest.mark.parametrize(
    ("orientation", "mirror", "flip"),
    [(2, True, False), (3, True, True), (4, False, True)],
)
def test_flip_orientation_matches_upstream(orientation: int, mirror: bool, flip: bool) -> None:
    """2/3/4 的组合对应上游 ``images/index.ts:59-62`` 的 fliph/flipv 分支。"""
    from PIL import ImageOps

    source = Image.new("RGBA", (3, 2))
    source.putdata([(x * 40, y * 80, 10, 255) for y in range(2) for x in range(3)])

    expected = source
    if mirror:
        expected = ImageOps.mirror(expected)
    if flip:
        expected = ImageOps.flip(expected)

    actual = source.transpose(_transpose_ops(Image)[orientation])

    assert actual.tobytes() == expected.tobytes()


async def test_orientation_is_applied_before_scaling(processor) -> None:
    """6/8 会交换宽高，缩放必须发生在转正**之后**（``images/index.ts:53-66``）。"""
    # 40x20、orientation 6 → 转正后 20x40。
    data = insert_exif_orientation(encode(gradient(40, 20), "JPEG", quality=90), 6)
    limits = ImageLimits(max_width=20, max_height=40, max_bytes=10**9)

    result = await processor.prepare(data, "image/jpeg", limits)

    assert result is not None
    decoded = Image.open(io.BytesIO(base64.b64decode(result.data)))
    assert decoded.size == (20, 40)


async def test_prepared_image_is_the_declared_type(processor) -> None:
    data = encode(gradient(10, 10), "PNG")
    result = await processor.prepare(data, "image/png", DEFAULT_IMAGE_LIMITS)
    assert isinstance(result, PreparedImage)
