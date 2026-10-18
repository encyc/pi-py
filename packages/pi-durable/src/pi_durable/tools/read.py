"""``read`` 工具 —— 移植 ``durable/src/tools/read.ts``（312 行）。

读文本文件与图像。截断/续读/图像处理的说明都走 **diagnostics**；内容本身只是文件文本，
或一张图像（程序调用方拿到的是它的 ``ImageContent``）。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from pydantic import BaseModel, Field

from pi_ai import ImageContent, TextContent
from pi_chord.context import Context
from pi_chord.output import character_end
from pi_durable.env import (
    BinaryReader,
    FileInfo,
    LineRangeOptions,
    LineScan,
    get_or_throw,
    range_decoder,
    starts_with_bom,
)
from pi_durable.harness import (
    ToolDiagnostic,
    ToolExecutionApi,
    ToolExecutionResult,
    ToolRegistration,
    define_tool,
)
from pi_durable.tools.args import json_schema_of
from pi_durable.tools.env import require_env
from pi_durable.tools.image import detect_supported_image_mime_type_of, image_dimensions
from pi_durable.tools.image_processor import (
    DEFAULT_IMAGE_LIMITS,
    INLINE_IMAGE_TYPES,
    ImageLimits,
    ImageProcessor,
    base64_length,
    to_base64,
)
from pi_durable.tools.path_utils import resolve_read_tool_path
from pi_durable.truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    TruncationTotals,
    format_size,
    truncate_head_of,
    utf8_byte_length,
)

__all__ = [
    "READ_CHUNK",
    "READ_SCHEMA",
    "ReadToolDetails",
    "ReadToolInput",
    "ReadToolOptions",
    "create_read_tool",
]

#: 上游 ``read.ts:47``。
READ_CHUNK = 64 * 1024


class ReadToolInput(BaseModel):
    """``read.ts:34-38`` 的 ``readSchema``。"""

    path: str = Field(description="Path to the file to read (relative or absolute)")
    offset: float | None = Field(
        default=None, description="Line number to start reading from (1-indexed)"
    )
    limit: float | None = Field(default=None, description="Maximum number of lines to read")


READ_SCHEMA: dict[str, Any] = json_schema_of(ReadToolInput)


class ReadToolDetails(BaseModel):
    """``read.ts:42-45``：文本是怎么被截断的（文本本身在结果内容里）。"""

    model_config = {"populate_by_name": True}

    truncation: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ReadToolOptions:
    """``read.ts:80-83``：给了 processor 就缩放/转码图像，否则上限内的内联格式原样透传。"""

    images: ImageProcessor | None = None


def _slice_index(value: float) -> int:
    """``Array.prototype.slice`` 对下标的转换：NaN 当 0，其余向零截断（``read.ts:49-52``）。"""
    if math.isnan(value):
        return 0
    return math.trunc(value)


async def _read_head(
    reader: BinaryReader,
    start: int,
    end: int,
    skip_bom: bool,
    context: Context,
) -> str:
    """解码 ``[start, end)`` 的起始部分，**按整份文件来解码**（``read.ts:54-78``）。

    读到够 ``truncate_head_of`` 判定为止：超过 ``DEFAULT_MAX_BYTES + 1`` 字节，
    或已有 ``DEFAULT_MAX_LINES`` 个换行。
    """
    decoder = range_decoder()
    text = ""
    newlines = 0
    position = 3 if skip_bom and start == 0 else start
    while position < end:
        chunk = get_or_throw(await reader.read(position, min(READ_CHUNK, end - position), context))
        if len(chunk) == 0:
            break
        position += len(chunk)
        decoded = decoder.decode(chunk)
        text += decoded
        index = decoded.find("\n")
        while index != -1:
            newlines += 1
            index = decoded.find("\n", index + 1)
        if newlines >= DEFAULT_MAX_LINES or utf8_byte_length(text) > DEFAULT_MAX_BYTES + 1:
            return text
    return text + decoder.decode()


@dataclass(frozen=True, slots=True)
class _ImageModel:
    """``read.ts:193-194``：会话模型接受什么——是否支持图像、以及在什么上限内。"""

    vision: bool
    limits: ImageLimits


async def _image_model(api: ToolExecutionApi, context: Context) -> _ImageModel:
    """``read.ts:196-212``。

    没有可解析的模型时，按「接受图像、用默认上限」处理。上游从 pi-ai 的模型定义里读
    ``inputLimits.images.resize`` 与 ``input``；本端的 :class:`~pi_durable.harness.Models`
    是窄协议（只有 ``get_model``），所以用 ``getattr`` 宽容地取这两个可选属性。
    """
    ref = (await api.agent(context)).model
    model = None if ref is None else api.models.get_model(ref.provider, ref.model_id)

    resize = None
    input_kinds = None
    if model is not None:
        input_limits = getattr(model, "input_limits", None) or getattr(model, "inputLimits", None)
        images = getattr(input_limits, "images", None) if input_limits is not None else None
        resize = getattr(images, "resize", None) if images is not None else None
        input_kinds = getattr(model, "input", None)

    def limit(name: str, default: int) -> int:
        value = getattr(resize, name, None) if resize is not None else None
        return default if value is None else int(value)

    vision = True if input_kinds is None else "image" in input_kinds
    return _ImageModel(
        vision=vision,
        limits=ImageLimits(
            max_width=limit("max_width", DEFAULT_IMAGE_LIMITS.max_width),
            max_height=limit("max_height", DEFAULT_IMAGE_LIMITS.max_height),
            max_bytes=limit("max_bytes", DEFAULT_IMAGE_LIMITS.max_bytes),
        ),
    )


def _image_error(message: str) -> ToolExecutionResult[Any]:
    """``read.ts:214-216``。"""
    return ToolExecutionResult(
        output=[],
        is_error=True,
        diagnostics=[ToolDiagnostic(severity="error", code="unsupported_image", message=message)],
    )


async def _read_image(
    reader: BinaryReader,
    info: FileInfo,
    path: str,
    mime_type: str,
    processor: ImageProcessor | None,
    image_model: _ImageModel,
    context: Context,
) -> ToolExecutionResult[Any]:
    """一张图像块（``read.ts:138-191``）。"""
    notes: list[str] = []
    data: str
    final_mime: str

    if processor is not None:
        raw = get_or_throw(await reader.read(0, info.size, context))
        prepared = await processor.prepare(raw, mime_type, image_model.limits)
        if prepared is None:
            return _image_error(
                f"{path} is an image ({mime_type}) that cannot be prepared for the model"
            )
        data, final_mime = prepared.data, prepared.mime_type
        if prepared.converted_from is not None:
            notes.append(f"Converted from {prepared.converted_from} to {prepared.mime_type}.")
        if prepared.resized is not None:
            source, target = prepared.resized.from_, prepared.resized.to
            scale = f"{source.width / target.width:.2f}"
            notes.append(
                f"Resized from {source.width}x{source.height} to {target.width}x{target.height}. "
                f"Multiply coordinates by {scale} to map them to the original."
            )
    else:
        if mime_type not in INLINE_IMAGE_TYPES:
            return _image_error(
                f"{path} is an image ({mime_type}) that needs converting, "
                "and no image processor is configured"
            )
        unshrinkable = "and no image processor is configured to shrink it"
        if base64_length(info.size) > image_model.limits.max_bytes:
            return _image_error(
                f"{path} is an image ({mime_type}) of {format_size(info.size)}, "
                f"too large to send, {unshrinkable}"
            )
        raw = get_or_throw(await reader.read(0, info.size, context))
        size = image_dimensions(raw, mime_type)
        if size is None:
            return _image_error(f"{path} is an image ({mime_type}) whose size cannot be read")
        width, height = size
        if width > image_model.limits.max_width or height > image_model.limits.max_height:
            return _image_error(
                f"{path} is an image ({mime_type}) of {width}x{height}, larger than "
                f"{image_model.limits.max_width}x{image_model.limits.max_height}, {unshrinkable}"
            )
        data, final_mime = to_base64(raw), mime_type

    if not image_model.vision:
        notes.append("The current model does not support images; it sees a placeholder instead.")

    message = " ".join([f"Read image file [{final_mime}].", *notes])
    return ToolExecutionResult(
        output=[ImageContent(data=data, mime_type=final_mime)],
        diagnostics=[ToolDiagnostic(severity="info", code="image", message=message)],
    )


async def _read_text(
    reader: BinaryReader,
    path: str,
    offset: float | None,
    limit: float | None,
    context: Context,
) -> ToolExecutionResult[Any]:
    """打开文件的文本读取结果（``read.ts:218-312``）。

    等价于「整份文件按 ``TextDecoder`` 解码 → 按 ``\\n`` 切分 → 用 ``truncateHead`` 限定选中行」，
    但只读一次 scan 的量加一个头部。
    """
    start_line = max(0, offset - 1) if offset else 0
    start_line_display = start_line + 1
    slice_start = _slice_index(start_line)
    scan_start = slice_start if _is_safe_integer(slice_start) else 0
    requested_end = None if limit is None else max(scan_start + 1, _slice_index(start_line + limit))
    scan_end = (
        requested_end if requested_end is not None and _is_safe_integer(requested_end) else None
    )

    async def scan_of(end_line: int | None) -> LineScan:
        return get_or_throw(
            await reader.scan_lines(
                LineRangeOptions(start_line=scan_start, end_line=end_line), context
            )
        )

    scan = await scan_of(scan_end)
    total_file_lines = scan.newlines + 1
    if start_line >= total_file_lines:
        raise RuntimeError(
            f"Offset {_format_number(offset)} is beyond end of file ({total_file_lines} lines total)"
        )

    user_limited_lines: int | None = None
    selected_line_count = total_file_lines - slice_start
    if limit is not None:
        end_line = min(start_line + limit, total_file_lines)
        user_limited_lines = int(end_line - start_line)
        relative_end = _slice_index(end_line)
        slice_end = max(total_file_lines + relative_end, 0) if relative_end < 0 else relative_end
        selected_line_count = max(0, int(slice_end - slice_start))
        if selected_line_count > 0 and relative_end < 0:
            scan = await scan_of(slice_end)

    empty = selected_line_count == 0
    ends_with_newline = (
        not empty and scan.last_line_start == scan.end and scan.last_line_start > scan.start
    )
    totals = TruncationTotals(
        lines=0
        if empty or scan.selected_bytes == 0
        else selected_line_count - (1 if ends_with_newline else 0),
        bytes=0 if empty else scan.selected_bytes,
    )
    first_bytes = get_or_throw(await reader.read(0, 3, context))
    head = (
        ""
        if empty
        else await _read_head(reader, scan.start, scan.end, starts_with_bom(first_bytes), context)
    )

    truncation = truncate_head_of(head, totals)
    diagnostics: list[ToolDiagnostic] = []
    output_text = truncation.content
    details: ReadToolDetails | None = None

    if truncation.first_line_exceeds_limit:
        integral = float(start_line).is_integer()
        line_text = (truncation.content.split("\n")[0] if integral else "") or ""
        line_bytes = line_text.encode("utf-8")
        line_size = scan.first_line_bytes if integral else 0
        end = character_end(line_bytes, DEFAULT_MAX_BYTES)
        output_text = line_bytes[:end].decode("utf-8", errors="replace")
        diagnostics.append(
            ToolDiagnostic(
                severity="warn",
                code="truncated",
                message=(
                    f"Line {start_line_display} is {format_size(line_size)}, exceeds the "
                    f"{format_size(DEFAULT_MAX_BYTES)} limit; showing its first {format_size(end)}. "
                    f"Use bash: sed -n '{start_line_display}p' {path} | tail -c +{end + 1}"
                ),
            )
        )
        fields = asdict(truncation)
        fields.pop("content", None)
        fields.update({"output_bytes": end, "output_lines": 1})
        details = ReadToolDetails(truncation=fields)
    elif truncation.truncated:
        end_line_display = start_line_display + truncation.output_lines - 1
        next_offset = end_line_display + 1
        limit_text = (
            ""
            if truncation.truncated_by == "lines"
            else f" ({format_size(DEFAULT_MAX_BYTES)} limit)"
        )
        diagnostics.append(
            ToolDiagnostic(
                severity="info",
                code="truncated",
                message=(
                    f"Showing lines {start_line_display}-{end_line_display} of {total_file_lines}"
                    f"{limit_text}. Use offset={next_offset} to continue."
                ),
            )
        )
        fields = asdict(truncation)
        fields.pop("content", None)
        details = ReadToolDetails(truncation=fields)
    elif user_limited_lines is not None and start_line + user_limited_lines < total_file_lines:
        remaining = total_file_lines - (start_line + user_limited_lines)
        next_offset = start_line + user_limited_lines + 1
        diagnostics.append(
            ToolDiagnostic(
                severity="info",
                message=f"{remaining} more lines in file. Use offset={next_offset} to continue.",
            )
        )

    return ToolExecutionResult(
        output=[] if output_text == "" else [TextContent(text=output_text)],
        details=details,
        diagnostics=diagnostics,
    )


def _is_safe_integer(value: float) -> bool:
    """JS ``Number.isSafeInteger``。"""
    return math.isfinite(value) and float(value).is_integer() and abs(value) <= 2**53 - 1


def _format_number(value: float | None) -> str:
    """JS 的 ``${number}``：整数不带小数点。"""
    if value is None:
        return "undefined"
    return str(int(value)) if float(value).is_integer() else str(value)


async def _execute(
    args: ReadToolInput,
    api: ToolExecutionApi,
    context: Context,
    options: ReadToolOptions,
) -> ToolExecutionResult[Any]:
    env = require_env(api)
    absolute_path = await resolve_read_tool_path(env, args.path, context)
    reader = get_or_throw(await env.open_binary_reader(absolute_path, None, context))
    try:
        # 并发写入可能在 scan 与读取之间改动文件。追加（活跃日志）时按 scan 到的文本处理；
        # 图像必须是完整的。其余情况重读一次（``read.ts:101-125``）。
        for attempt in range(2):
            before = get_or_throw(await reader.info(context))
            mime_type = await detect_supported_image_mime_type_of(
                _ReaderSource(reader, context, before.size)
            )

            if mime_type is None:
                result = await _read_text(reader, args.path, args.offset, args.limit, context)
            else:
                result = await _read_image(
                    reader,
                    before,
                    args.path,
                    mime_type,
                    options.images,
                    await _image_model(api, context),
                    context,
                )

            after = get_or_throw(await reader.info(context))
            unchanged = after.size == before.size and after.mtime_ms == before.mtime_ms
            if unchanged or (mime_type is None and after.size > before.size):
                return result
            if attempt == 1:
                raise RuntimeError(f"{args.path} changed while it was read")
        raise RuntimeError(f"{args.path} changed while it was read")
    finally:
        await reader.close(context)


class _ReaderSource:
    """:class:`~pi_durable.tools.image.ByteSource` 的适配器（``read.ts:105-108``）。

    上游直接把 ``before.size`` 传进去；``size`` 必须**真的可用**——PNG 的 APNG 块遍历
    （``image.ts:34``）要拿它做边界，缺了会让所有 PNG 读取直接崩。
    """

    __slots__ = ("_reader", "_context", "size")

    def __init__(self, reader: BinaryReader, context: Context, size: int) -> None:
        self._reader = reader
        self._context = context
        self.size = size

    async def read(self, offset: int, length: int) -> bytes:
        return get_or_throw(await self._reader.read(offset, length, self._context))


def create_read_tool(options: ReadToolOptions | None = None) -> ToolRegistration[ReadToolInput]:
    """``createReadTool()``（``read.ts:89-131``）。

    有 image processor 时可选格式多一个 ``bmp``（能被转码），否则只有 provider 能内联接受的
    ``jpg, png, gif, webp``——描述文案随之不同（``read.ts:90``）。
    """
    resolved = options or ReadToolOptions()
    formats = "jpg, png, gif, webp" if resolved.images is None else "jpg, png, gif, webp, bmp"

    async def execute(
        args: ReadToolInput, api: ToolExecutionApi, context: Context
    ) -> ToolExecutionResult[Any]:
        return await _execute(args, api, context, resolved)

    return define_tool(
        name="read",
        description=(
            f"Read the contents of a file. Supports text files and images ({formats}). "
            "Images are sent as attachments. For text files, output is truncated to "
            f"{DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first). "
            "Use offset/limit for large files. When you need the full file, continue with offset "
            "until complete."
        ),
        parameters=READ_SCHEMA,
        execute=execute,
    )
