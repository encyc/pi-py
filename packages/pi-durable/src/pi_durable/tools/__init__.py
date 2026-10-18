"""工具层 —— ``@earendil-works/pi-durable/tools`` 的移植。

上游入口 ``durable/src/tools/index.ts``（39 行）导出 ``coding-tools`` 扩展：
``create_read_tool`` / ``create_write_tool`` / ``create_edit_tool`` / ``create_bash_tool``，
以及把它们打包起来的 :func:`create_coding_tools`。

内部的纯逻辑模块：

* :mod:`pi_durable.tools.image` —— 不解码就判定图像格式与尺寸
* :mod:`pi_durable.tools.image_processor` —— 图像契约（上限、``PreparedImage``、base64）
* :mod:`pi_durable.tools.path_utils` —— 模型给的路径归一化与变体探测
* :mod:`pi_durable.tools.file_mutation_queue` —— 同文件写操作串行化
* :mod:`pi_durable.tools.edit_diff` / :mod:`pi_durable.tools.jsdiff` —— ``edit`` 的 diff 层
"""

from __future__ import annotations

from typing import Any

from pi_durable.harness import Extension, define_extension
from pi_durable.tools.bash import (
    BASH_SCHEMA,
    POWERSHELL_SCHEMA,
    SHELL_STRUCTURED_OUTPUT_SCHEMA,
    BashExecution,
    BashPrepare,
    BashToolInput,
    BashToolOptions,
    PowerShellToolInput,
    PowerShellToolOptions,
    ShellStructuredOutput,
    create_bash_tool,
    create_powershell_tool,
)
from pi_durable.tools.edit import (
    EDIT_SCHEMA,
    EditToolDetails,
    EditToolInput,
    ReplaceEditInput,
    create_edit_tool,
)
from pi_durable.tools.env import require_env
from pi_durable.tools.image import (
    BLOCK_BYTES,
    HEADER_BYTES,
    ByteSource,
    detect_supported_image_mime_type,
    detect_supported_image_mime_type_of,
    image_dimensions,
)
from pi_durable.tools.image_processor import (
    DEFAULT_IMAGE_LIMITS,
    INLINE_IMAGE_TYPES,
    ImageLimits,
    ImageProcessor,
    ImageSize,
    PreparedImage,
    Resized,
    base64_length,
    to_base64,
)
from pi_durable.tools.path_utils import (
    NARROW_NO_BREAK_SPACE,
    UNICODE_SPACES,
    normalize_tool_path,
    resolve_read_tool_path,
    resolve_tool_path,
)
from pi_durable.tools.read import ReadToolDetails, ReadToolInput, ReadToolOptions, create_read_tool
from pi_durable.tools.write import WRITE_SCHEMA, WriteToolInput, create_write_tool

__all__ = [
    "BASH_SCHEMA",
    "BLOCK_BYTES",
    "CodingTools",
    "EDIT_SCHEMA",
    "POWERSHELL_SCHEMA",
    "SHELL_STRUCTURED_OUTPUT_SCHEMA",
    "WRITE_SCHEMA",
    "BashExecution",
    "BashPrepare",
    "BashToolInput",
    "BashToolOptions",
    "EditToolDetails",
    "EditToolInput",
    "PowerShellToolInput",
    "PowerShellToolOptions",
    "ReadToolDetails",
    "ReadToolInput",
    "ReadToolOptions",
    "ReplaceEditInput",
    "ShellStructuredOutput",
    "WriteToolInput",
    "create_bash_tool",
    "create_coding_tools",
    "create_edit_tool",
    "create_powershell_tool",
    "create_read_tool",
    "create_write_tool",
    "require_env",
    "DEFAULT_IMAGE_LIMITS",
    "HEADER_BYTES",
    "INLINE_IMAGE_TYPES",
    "NARROW_NO_BREAK_SPACE",
    "UNICODE_SPACES",
    "ByteSource",
    "ImageLimits",
    "ImageProcessor",
    "ImageSize",
    "PreparedImage",
    "Resized",
    "base64_length",
    "detect_supported_image_mime_type",
    "detect_supported_image_mime_type_of",
    "image_dimensions",
    "normalize_tool_path",
    "resolve_read_tool_path",
    "resolve_tool_path",
    "to_base64",
]


def create_coding_tools(options: ReadToolOptions | None = None) -> Extension[Any]:
    """``coding-tools`` 扩展：``read`` / ``write`` / ``edit`` / ``bash``
    （``tools/index.ts:27-36``）。

    **不会**被自动安装。``create_powershell_tool()`` 另外提供 ``powershell``；
    ``images`` 负责准备 ``read`` 返回的图像（见 :class:`ReadToolOptions`）。
    """
    return define_extension(
        name="coding-tools",
        tools=[
            create_read_tool(options or ReadToolOptions()),
            create_write_tool(),
            create_edit_tool(),
            create_bash_tool(),
        ],
    )


#: ``create_coding_tools()``（``tools/index.ts:39``）。
CodingTools: Extension[Any] = create_coding_tools()
