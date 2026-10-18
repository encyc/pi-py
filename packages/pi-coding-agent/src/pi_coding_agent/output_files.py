"""把被截断的全文 / 二进制资源写到临时文件。

对应上游 ``utils/output-files.ts``(34) 的 ``writeOutputFile``。

**这是共享模块，不是某个扩展的私有工具**：上游同一个 ``writeOutputFile`` 同时被 MCP 工具与
codemode 的 ``execute.ts:341,362`` 使用。此前本仓只有 MCP 一个消费者，于是实现内联在
``extensions/mcp/tools.py``；现在 codemode 是**第二个**消费者，所以提到这里 ——
**两份实现会各自漂移**（例如上游 v1.0.3 起「输出文件仅本人可读」的权限位只改一处），
那正是本仓〈平移方言差异清单〉想避免的那类将来才暴露的不一致。

上游 v1.0.3 起输出文件「readable only by the user」→ 这里落 ``0o600``。
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

#: 临时文件名前缀。上游没有前缀概念（``writeOutputFile`` 自己起随机名）。
DEFAULT_OUTPUT_PREFIX = "pi-"


async def save_output_file(
    data: Any,
    extension: str,
    *,
    prefix: str = DEFAULT_OUTPUT_PREFIX,
) -> str:
    """把 ``data`` 写到临时文件（**仅本人可读**），返回路径。

    ``extension`` 含点，如 ``.txt``。``data`` 为 ``str`` 时按 UTF-8 编码，否则按字节序列处理。
    """
    handle, path = tempfile.mkstemp(prefix=prefix, suffix=extension)
    try:
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        os.write(handle, payload)
    finally:
        os.close(handle)
    os.chmod(path, 0o600)
    return path


__all__ = ["DEFAULT_OUTPUT_PREFIX", "save_output_file"]
