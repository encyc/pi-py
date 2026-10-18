"""图像处理 —— ``@earendil-works/pi-durable/images`` 的**部分**移植。

上游入口 ``durable/src/images/index.ts``（140 行）导出 ``createPhotonImages(wasm)``：
一个基于 **Photon**（Rust 编译成 WebAssembly）的 :class:`~pi_durable.tools.ImageProcessor`，
负责按 EXIF 转正、缩放到上限、并以递减的 JPEG 质量重新编码。

Python 侧的对等物是 **Pillow**：

* :func:`create_pillow_images` —— 行为等价物（见 :mod:`pi_durable.images.index`）
* Pillow 是**可选 extra**：``pip install 'pi-py-durable[images]'``。
  本包在没有 Pillow 的环境里仍可 import；只有真正调用 processor 才抛
  带安装提示的 ``ImportError``。

未移植 / 已排除：

* ``images/node.ts``（130 行）+ ``images/node-worker.ts``（35 行）：把 Photon 放进
  ``worker_threads``，空闲 30 s 退出以释放不会收缩的 WASM 内存
  （``images/node.ts:15-18``、``115-123``）。Python 若需要「不阻塞事件循环」应对应
  线程池/进程池，而不是移植 worker 消息协议——**登记为未移植**。
* ``images/cloudflare.ts``（10 行）：依赖 Wrangler 的 ``CompiledWasm`` 导入规则
  （Workers 不能在运行时编译 WASM），Python 无对等物。**按任务要求明确排除**。
"""

from __future__ import annotations

from pi_durable.images.exif import exif_orientation
from pi_durable.images.index import JPEG_QUALITIES, create_pillow_images

__all__ = [
    "JPEG_QUALITIES",
    "create_pillow_images",
    "exif_orientation",
]
