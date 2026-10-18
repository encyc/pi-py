"""工具看到的图像契约 —— 移植 ``durable/src/tools/image-processor.ts``（58 行）。

本模块是**纯数据与纯函数**，不依赖任何图像库：

* :class:`ImageLimits` / :data:`DEFAULT_IMAGE_LIMITS` —— 模型能接受的图像上限
* :class:`PreparedImage` —— 准备好的图像（base64 + MIME + 可选的重置/转码说明）
* :class:`ImageProcessor` —— 具体的解码/裁剪/编码实现由外部注入（上游是 Photon WASM，
  见 ``images/index.ts``；Python 侧尚未实现，登记在 ``PORTING.md``）
* :func:`to_base64` / :func:`base64_length`

``read`` 工具在没有 processor 时，把「格式受支持且在字节上限内」的图像原样透传；
给了 processor 才做方向矫正、转码与缩放（``image-processor.ts:26-30``）。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "DEFAULT_IMAGE_LIMITS",
    "INLINE_IMAGE_TYPES",
    "ImageLimits",
    "ImageProcessor",
    "ImageSize",
    "PreparedImage",
    "Resized",
    "base64_length",
    "to_base64",
]


@dataclass(frozen=True, slots=True)
class ImageLimits:
    """模型看到的图像必须守住的上限（``image-processor.ts:1-9``）。"""

    #: 像素宽上限。上游默认 2000。
    max_width: int
    #: 像素高上限。上游默认 2000。
    max_height: int
    #: base64 字节上限。上游默认 4.5 MiB，低于 Anthropic 的 5 MB。
    max_bytes: int


#: 上游 ``DEFAULT_IMAGE_LIMITS``（``image-processor.ts:11``），取自 coding agent。
DEFAULT_IMAGE_LIMITS = ImageLimits(
    max_width=2000,
    max_height=2000,
    max_bytes=int(4.5 * 1024 * 1024),
)


@dataclass(frozen=True, slots=True)
class ImageSize:
    """一对像素尺寸（``image-processor.ts:19-20`` 的内联对象）。"""

    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Resized:
    """缩放前后的尺寸，供模型把坐标映射回去（``image-processor.ts:17-21``）。"""

    from_: ImageSize
    to: ImageSize


@dataclass(frozen=True, slots=True)
class PreparedImage:
    """模型能接受的图像：受支持的格式、在上限内、已 base64（``image-processor.ts:13-24``）。"""

    #: 标准 base64（无 data URL 前缀）。
    data: str
    mime_type: str
    #: 发生过缩放时给出前后尺寸。
    resized: Resized | None = None
    #: 原始格式与 :attr:`mime_type` 不同时给出原格式（BMP 变 PNG、过大的 PNG 变 JPEG）。
    converted_from: str | None = None


class ImageProcessor(Protocol):
    """解码、定向、缩放、重新编码图像，使模型能接受它们（``image-processor.ts:31-37``）。

    上游把它做成 ``Promise``，这样 processor 可以把工作放到事件循环之外
    （Node 侧的 Photon 就跑在 worker thread 里）。Python 侧因此是 ``async def``。
    """

    async def prepare(
        self,
        data: bytes,
        mime_type: str,
        limits: ImageLimits,
    ) -> PreparedImage | None: ...


#: 每个 provider 都能内联接受的格式；其余（如 BMP）需要转码（``image-processor.ts:40``）。
INLINE_IMAGE_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)


def to_base64(data: bytes) -> str:
    """``data`` 的标准 base64（``image-processor.ts:42-53``）。

    上游为什么按 8 KiB 分块再拼：JS 的 ``btoa`` 只吃「二进制字符串」，而
    ``String.fromCharCode.apply(null, megabytes)`` 会撑爆调用栈；1.3.0 又把它换成分块
    构建以避开 Cloudflare Durable Object 的 128 MB 内存上限
    （``CHANGELOG.md:16``，issue #10923）。

    Python 的 :func:`base64.b64encode` 直接吃 ``bytes``，没有这两个约束，
    因此不需要分块——**输出逐字节相同**。
    """
    return base64.b64encode(data).decode("ascii")


def base64_length(byte_length: int) -> int:
    """``byte_length`` 个字节的 base64 长度（``image-processor.ts:55-57``）。

    上游 ``Math.ceil(byteLength / 3) * 4``；整数向上取整在 Python 里写成 ``-(-a // b)``。
    """
    return -(-byte_length // 3) * 4
