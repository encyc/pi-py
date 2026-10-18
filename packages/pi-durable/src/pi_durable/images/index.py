"""基于 Pillow 的图像处理器 —— 移植 ``durable/src/images/index.ts``（140 行）。

上游用 **Photon**（Rust 编译成 WebAssembly）实现 :class:`~pi_durable.tools.ImageProcessor`。
Python 侧的对等物是 **Pillow**，因此本模块是 ``create_photon_images(wasm)`` 的行为等价物，
而不是 WASM 加载器。

**惰性依赖**：Pillow 是可选 extra（``pip install 'pi-py-durable[images]'``）。
本模块可以在没有 Pillow 的环境里被 import；只有真正调用 processor 时才会抛出
带安装提示的 :class:`ImportError`。

行为逐条对齐上游（``images/index.ts:32-117``）：

1. **先解码**，即使图像会被原样返回——损坏的文件是错误，不是「provider 拒绝的图像」
   （``images/index.ts:42`` 的注释）。
2. 已经「正立 + 格式可内联 + 在上限内」的图像**原样透传原始字节**，不重新编码
   （``images/index.ts:44-52``）。
3. 否则按 EXIF 方向转正、缩放到 ``maxWidth``/``maxHeight``，
   然后按 **PNG → JPEG(80,70,55,40)** 的顺序试编码；JPEG 源图片反过来先试 JPEG
   （``images/index.ts:95-117``）。
4. 都不满足 base64 字节上限时，尺寸各乘 0.75（向下取整）再试一轮，直到 1×1
   （``images/index.ts:67-82``）。
5. 任何异常都归为「无法处理」返回 ``None``（``images/index.ts:83-86``）。
"""

from __future__ import annotations

import base64
import io
import math
from typing import Any

from pi_durable.images.exif import exif_orientation
from pi_durable.tools.image_processor import (
    INLINE_IMAGE_TYPES,
    ImageLimits,
    ImageProcessor,
    ImageSize,
    PreparedImage,
    Resized,
    base64_length,
    to_base64,
)

__all__ = [
    "JPEG_QUALITIES",
    "create_pillow_images",
    "require_pillow",
]

#: 依次尝试的 JPEG 质量（``images/index.ts:16``）。
JPEG_QUALITIES = (80, 70, 55, 40)

_INSTALL_HINT = (
    "pi_durable.images 需要 Pillow。请安装可选依赖：\n"
    "    pip install 'pi-py-durable[images]'\n"
    "（或在开发环境里跑 `uv sync --extra images`）"
)


def require_pillow() -> Any:
    """惰性导入 Pillow，缺依赖时给出可执行的安装提示；返回 ``PIL.Image`` 模块。

    **不要**在模块顶层 import Pillow——那会让没有 extra 的环境连
    ``import pi_durable.images`` 都失败。
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise ImportError(_INSTALL_HINT) from exc
    return Image


def _transpose_ops(image_module: Any) -> dict[int, Any]:
    """EXIF 方向 → Pillow 转置操作。

    与上游 ``transposed()``（``images/index.ts:119-139``）逐方向对齐：5/6/7/8 在上游是
    手写像素搬运，因为 Photon 的 ``rotate`` 在直角旋转时会掉像素；Pillow 的转置无损，
    用标准操作即可。映射已用**逐像素**用例与上游公式比对过。
    """
    transpose = image_module.Transpose
    return {
        2: transpose.FLIP_LEFT_RIGHT,
        3: transpose.ROTATE_180,
        4: transpose.FLIP_TOP_BOTTOM,
        5: transpose.TRANSPOSE,
        6: transpose.ROTATE_270,
        7: transpose.TRANSVERSE,
        8: transpose.ROTATE_90,
    }


def _js_round(value: float) -> int:
    """JS ``Math.round``：``.5`` 向 ``+Infinity`` 取整。Python ``round`` 是 round-half-even。"""
    return math.floor(value + 0.5)


def create_pillow_images() -> ImageProcessor:
    """一个基于 Pillow 的 :class:`~pi_durable.tools.ImageProcessor`。

    对应上游 ``createPhotonImages()``（``images/index.ts:32-35``）。上游要先
    ``initSync({module: wasm})`` 把 WASM 装进进程；Pillow 是普通扩展模块，没有这一步，
    所以本函数**不需要参数**。
    """
    return _PillowImages()


class _PillowImages:
    """:class:`~pi_durable.tools.ImageProcessor` 的 Pillow 实现。"""

    async def prepare(
        self,
        data: bytes,
        mime_type: str,
        limits: ImageLimits,
    ) -> PreparedImage | None:
        return _prepare(require_pillow(), data, mime_type, limits)


def _prepare(
    image_module: Any,
    data: bytes,
    mime_type: str,
    limits: ImageLimits,
) -> PreparedImage | None:
    """上游 ``prepare()``（``images/index.ts:37-89``）。"""
    orientation = exif_orientation(data)
    inline = mime_type in INLINE_IMAGE_TYPES
    image: Any = None
    try:
        # 即使会原样返回也要解码：损坏的文件必须报错（``images/index.ts:42``）。
        image = image_module.open(io.BytesIO(data))
        image.load()

        if (
            inline
            and orientation == 1
            and image.width <= limits.max_width
            and image.height <= limits.max_height
            and base64_length(len(data)) <= limits.max_bytes
        ):
            return PreparedImage(data=to_base64(data), mime_type=mime_type)

        if orientation != 1:
            image = image.transpose(_transpose_ops(image_module)[orientation])

        width, height = image.size
        scale = min(1.0, limits.max_width / width, limits.max_height / height)
        target = (max(1, _js_round(width * scale)), max(1, _js_round(height * scale)))

        while True:
            encoded = _encode_within(
                image_module, image, target, limits.max_bytes, mime_type == "image/jpeg"
            )
            if encoded is not None:
                encoded_data, encoded_mime = encoded
                resized = (
                    None
                    if target == (width, height)
                    else Resized(
                        from_=ImageSize(width=width, height=height),
                        to=ImageSize(width=target[0], height=target[1]),
                    )
                )
                converted = None if encoded_mime == mime_type else mime_type
                return PreparedImage(
                    data=encoded_data,
                    mime_type=encoded_mime,
                    resized=resized,
                    converted_from=converted,
                )
            if target == (1, 1):
                return None
            target = (max(1, math.floor(target[0] * 0.75)), max(1, math.floor(target[1] * 0.75)))
    except Exception:
        # 上游把解码器抛出的任何东西都当作「无法处理」（``images/index.ts:83-86``）。
        return None
    finally:
        if image is not None:
            image.close()


def _encode_within(
    image_module: Any,
    image: Any,
    size: tuple[int, int],
    max_bytes: int,
    jpeg_first: bool,
) -> tuple[str, str] | None:
    """``size`` 下第一个 base64 装得进 ``max_bytes`` 的编码（``images/index.ts:95-117``）。

    返回 ``(base64, mime_type)``；都不行时 ``None``。
    """
    same = size == image.size
    scaled = image if same else image.resize(size, image_module.Resampling.LANCZOS)
    try:
        for candidate_mime, quality in _candidates(jpeg_first):
            encoded = _encode_png(scaled) if quality is None else _encode_jpeg(scaled, quality)
            if base64_length(len(encoded)) <= max_bytes:
                return base64.b64encode(encoded).decode("ascii"), candidate_mime
        return None
    finally:
        if not same:
            scaled.close()


def _candidates(jpeg_first: bool) -> list[tuple[str, int | None]]:
    """候选编码顺序（``images/index.ts:104-109``）。

    ``quality is None`` 表示 PNG；否则是该质量的 JPEG。``jpegFirst`` 时 JPEG 在前。
    """
    png: tuple[str, int | None] = ("image/png", None)
    jpegs: list[tuple[str, int | None]] = [("image/jpeg", quality) for quality in JPEG_QUALITIES]
    return [*jpegs, png] if jpeg_first else [png, *jpegs]


def _encode_png(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _encode_jpeg(image: Any, quality: int) -> bytes:
    buffer = io.BytesIO()
    # JPEG 没有 alpha 通道；带透明的源图（PNG/GIF/WebP）必须先落到 RGB。
    target = image if image.mode in ("RGB", "L") else image.convert("RGB")
    try:
        target.save(buffer, format="JPEG", quality=quality)
    finally:
        if target is not image:
            target.close()
    return buffer.getvalue()
