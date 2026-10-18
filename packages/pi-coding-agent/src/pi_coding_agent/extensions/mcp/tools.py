"""把 MCP 工具适配成 pi 的工具定义。

对应上游 ``extensions/mcp/tools.ts``（391 行）。

结果映射成 pi 面向模型的内容（文本与图像）。超过 :data:`MCP_OUTPUT_MAX_BYTES`（20KB）的文本
保留首尾、中间挖掉（Codex 风格），全文存到临时文件供模型读取。图像以外的二进制资源同样落临时
文件；资源链接会点名 ``read_mcp_resource`` 工具。codemode 脚本拿到的是**完整** ``CallToolResult``
（去掉 ``_meta``：``content`` 原样、``structuredContent``、``isError``），**从不截断** —— 它就是该工具的
``structuredContent``，而每个 MCP 工具都声明了 ``CallToolResult`` 输出 schema。
MCP 错误（``isError``）对模型是错误结果，但脚本仍解析到该结果。

**有意偏离（TUI）**：上游本文件还导出 ``createMcpToolRenderers()``（``renderCall`` /
``renderResult``，依赖 ``pi-tui`` 的 ``Text`` / ``Container`` / ``VisualLinePreview`` / ``keyHint``）。
本包无渲染宿主，故**不移植、不导出**（见 ``PORTING.md`` 的「越界判断是双向的」）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from pi_agent_core.types import AgentToolResult
from pi_ai import ImageContent, TextContent
from pi_mcp import to_llm_content

from ...extensions.types import ToolAnnotations, ToolDefinition, ToolExposure, ToolNamespace
from ...mcp_servers import READ_MCP_RESOURCE_TOOL
from ...output_files import save_output_file
from ...truncate import format_size, truncate_middle

__all__ = [
    "MCP_OUTPUT_MAX_BYTES",
    "MAX_TOOL_NAME_LENGTH",
    "McpToolCaller",
    "McpToolDetails",
    "READ_MCP_RESOURCE_TOOL",
    "convert_mcp_result",
    "create_mcp_result_schema",
    "create_mcp_tool_definition",
    "create_mcp_tool_name",
    "limit_mcp_content",
    "save_to_temp_file",
    "to_model_content",
    "to_tool_exposure",
]

#: provider 的工具名上限：64 个 ``[A-Za-z0-9_-]``。
MAX_TOOL_NAME_LENGTH = 64

#: 超过该字节数的面向模型文本会被中间挖空。
MCP_OUTPUT_MAX_BYTES = 20 * 1024

#: 值属于数据、其 ``$ref`` 键不是引用的关键字。
_SCHEMA_DATA_KEYWORDS = frozenset({"const", "enum", "default", "examples"})

#: 值是「名字 → schema」映射、其键是名字而非关键字的关键字。
_SCHEMA_MAP_KEYWORDS = frozenset(
    {
        "properties",
        "patternProperties",
        "dependentSchemas",
        "dependencies",
        "$defs",
        "definitions",
    }
)

#: 注解里作为提示透传的布尔字段。
_ANNOTATION_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

_URI_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def to_tool_exposure(exposure: str) -> ToolExposure:
    """MCP exposure → pi exposure。

    ``codemode`` 与 ``deferred`` 都把工具排除在 codemode 描述之外；区别只在 MCP 扩展为触达它们
    而激活哪个工具。
    """
    return "deferred" if exposure == "codemode" else exposure  # type: ignore[return-value]


@dataclass
class McpToolDetails:
    server: str
    tool: str
    #: 面向模型文本被截断时，存有完整文本的临时文件。
    full_output_path: str | None = None


#: 保存被截断结果的全文或二进制资源，返回文件路径。``extension`` 含点，如 ``.txt``。
McpOutputSaver = Callable[[Any, str], Awaitable[str]]


def save_to_temp_file(data: Any, extension: str) -> Awaitable[str]:
    """把 ``data`` 写到临时文件（**仅本人可读**），返回路径。

    上游走 ``utils/output-files.ts: writeOutputFile``。实现已提到共享模块
    :mod:`pi_coding_agent.output_files` —— codemode 的 ``execute.ts`` 用的是**同一个**
    上游函数，两份实现会各自漂移（例如上游 v1.0.3 的 ``0o600`` 只会改到一处）。
    这里保留 MCP 自己的文件名前缀，行为与之前完全一致。
    """
    return save_output_file(data, extension, prefix="pi-mcp-")


class McpToolCaller(Protocol):
    """调用一个 MCP 工具的客户端（``pi_mcp.McpClient`` 满足此形状）。"""

    async def call_tool(self, name: str, args: dict[str, Any], options: Any) -> dict[str, Any]: ...


def create_mcp_tool_name(
    server: str,
    tool: str,
    is_taken: Callable[[str], bool] | None = None,
) -> str:
    """``mcp__<server>__<tool>``，净化并在过长时加哈希后缀。

    与 Codex 一致：除 ``[A-Za-z0-9_]`` 外的字符都变成 ``_``，因此该名字同时也是 codemode 脚本
    调用的标识符。``is_taken`` 报告已被**另一个** MCP 工具占用的名字：净化会把两个工具映射到同一
    名字（``a-b`` 与 ``a_b``），此时加哈希后缀。
    """
    taken = is_taken or (lambda _name: False)
    name = re.sub(r"[^A-Za-z0-9_]", "_", f"mcp__{server}__{tool}")
    if len(name) <= MAX_TOOL_NAME_LENGTH and not taken(name):
        return name
    digest = hashlib.sha256(f"{server}\0{tool}".encode()).hexdigest()[:8]
    return f"{name[: MAX_TOOL_NAME_LENGTH - len(digest) - 1]}_{digest}"


def _text_of(content: Sequence[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts)


#: MCP 内容块的紧凑类型；脚本最常读 text 与 image 块的字段。
_CONTENT_BLOCK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"enum": ["text", "image", "audio", "resource_link", "resource"]},
        "text": {"type": "string"},
        "data": {"type": "string"},
        "mimeType": {"type": "string"},
    },
    "required": ["type"],
    "additionalProperties": True,
}


def _relocate_schema(schema: Any, pointer: str) -> Any:
    """把 ``schema`` 当作另一份 schema 在 JSON pointer ``pointer`` 处的子 schema。

    其局部引用（``#``、``#/$defs/Item``）被改写到 ``pointer`` 之下，从而仍解析到原来的子 schema。
    """
    if isinstance(schema, list):
        return [_relocate_schema(item, pointer) for item in schema]
    if not isinstance(schema, dict):
        return schema
    relocated: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "$ref" and isinstance(value, str) and (value == "#" or value.startswith("#/")):
            relocated[key] = f"{pointer}{value[1:]}"
        elif key in _SCHEMA_DATA_KEYWORDS:
            relocated[key] = value
        elif key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            relocated[key] = {name: _relocate_schema(sub, pointer) for name, sub in value.items()}
        else:
            relocated[key] = _relocate_schema(value, pointer)
    return relocated


def create_mcp_result_schema(structured_content_schema: dict[str, Any] | None) -> dict[str, Any]:
    """每个 MCP 工具的输出 schema：脚本拿到的 ``CallToolResult``（去掉 ``_meta``）。

    工具自己的输出 schema 作为 ``structuredContent`` 嵌进去。
    """
    return {
        "type": "object",
        "properties": {
            "content": {"type": "array", "items": _CONTENT_BLOCK_SCHEMA},
            # server 可以只发结构化内容而不声明 schema，且它可以是任意 JSON 值。
            "structuredContent": (
                _relocate_schema(structured_content_schema, "#/properties/structuredContent")
                if structured_content_schema
                else {}
            ),
            "isError": {"type": "boolean"},
        },
        "required": ["content"],
        # 更新版协议会加的字段。
        "additionalProperties": True,
    }


async def limit_mcp_content(
    content: list[Any],
    save_output: McpOutputSaver | None = None,
) -> tuple[list[Any], str | None]:
    """把面向模型的文本限制在 :data:`MCP_OUTPUT_MAX_BYTES` 内。

    更长的文本变成 Codex 截断格式的单个文本块，后面跟全文文件路径；图像排在其后。
    返回 ``(content, full_output_path)``。
    """
    saver = save_output or save_to_temp_file
    combined = _text_of(content)
    truncation = truncate_middle(combined, MCP_OUTPUT_MAX_BYTES)
    if not truncation.truncated:
        return content, None

    full_output_path: str | None = None
    try:
        full_output_path = await saver(combined, ".txt")
        where = f"[Full output: {full_output_path} (read it with offset/limit)]"
    except Exception as error:  # noqa: BLE001 - 保存失败不能吞掉结果本身
        where = f"[Could not save the full output: {error}]"

    tokens = -(-truncation.total_bytes // 4)  # ceil
    text = (
        f"Warning: truncated output (original token count: {tokens})\n"
        f"Total output lines: {truncation.total_lines}\n\n"
        f"{truncation.content}\n\n{where}"
    )
    images = [block for block in content if getattr(block, "type", None) == "image"]
    return [TextContent(text=text), *images], full_output_path


@dataclass
class ConvertMcpResultOptions:
    """``convert_mcp_result`` 的选项。"""

    #: 保存被截断的文本与二进制资源。默认临时文件。
    save_output: McpOutputSaver | None = None
    #: server 的资源能否用 ``read_mcp_resource`` 读取；资源链接会据此点名该工具。
    readable_resources: bool = False


def _extension_of(uri: str) -> str:
    """被保存的二进制资源的扩展名：URI 结尾的那个，否则 ``.bin``。"""
    path = urlsplit(uri).path if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", uri) else uri
    match = _URI_EXTENSION.search(path or "")
    return match.group(0) if match else ".bin"


def _is_text_mime_type(mime_type: str | None) -> bool:
    """这些类型的 blob 当文本展示。"""
    if not mime_type:
        return False
    kind = mime_type.split(";", 1)[0].strip().lower()
    return (
        kind.startswith("text/")
        or kind == "application/json"
        or kind.endswith("+json")
        or kind.endswith("+xml")
    )


def _to_pi_content(blocks: Sequence[Any]) -> list[Any]:
    """把 ``pi_mcp.to_llm_content`` 的 dict 形状（``LlmTextContent`` / ``LlmImageContent``）
    规范成 pi-ai 的 ``TextContent`` / ``ImageContent`` 对象。

    pi-mcp 的返回与 pi-ai 的**字段同形但是 dict**；``AgentToolResult.content`` 与下游
    （agent 循环、``_text_of``）按属性访问 ``.type`` / ``.text``，因此这里必须转换。
    """
    out: list[Any] = []
    for block in blocks:
        if isinstance(block, dict):
            if block.get("type") == "image":
                out.append(
                    ImageContent(
                        data=str(block.get("data", "")), mime_type=str(block.get("mimeType", ""))
                    )
                )
            else:
                out.append(TextContent(text=str(block.get("text", ""))))
        else:
            out.append(block)
    return out


async def _block_to_content(
    server: str, block: dict[str, Any], options: ConvertMcpResultOptions
) -> list[Any]:
    """``server`` 结果中单个块的面向模型内容。"""
    block_type = block.get("type")
    if block_type == "resource_link":
        details = [
            block.get("mimeType"),
            None if block.get("size") is None else format_size(block["size"]),
        ]
        detail_text = ", ".join(str(item) for item in details if item)
        read = (
            f'. Read it with {READ_MCP_RESOURCE_TOOL} (server "{server}")'
            if options.readable_resources
            else ""
        )
        description = f": {block['description']}" if block.get("description") else ""
        title = block.get("title") or block.get("name")
        suffix = f" ({detail_text})" if detail_text else ""
        return [
            TextContent(text=f'[Resource {block.get("uri")} "{title}"{suffix}{description}{read}]')
        ]

    resource = block.get("resource") if block_type == "resource" else None
    if isinstance(resource, dict) and "blob" in resource:
        mime_type = resource.get("mimeType")
        if not (mime_type or "").startswith("image/"):
            uri = resource.get("uri", "")
            data = base64.b64decode(resource["blob"])
            if _is_text_mime_type(mime_type):
                return [TextContent(text=data.decode("utf-8", errors="replace"))]
            kind = f"{mime_type or 'unknown type'}, {format_size(len(data))}"
            saver = options.save_output or save_to_temp_file
            try:
                path = await saver(data, _extension_of(uri))
                return [TextContent(text=f"[Binary resource {uri} ({kind}) saved to {path}]")]
            except Exception as error:  # noqa: BLE001
                return [
                    TextContent(
                        text=f"[Binary resource {uri} ({kind}) could not be saved: {error}]"
                    )
                ]

    return _to_pi_content(to_llm_content({"content": [block]}))


async def to_model_content(
    server: str,
    blocks: Sequence[dict[str, Any]],
    options: ConvertMcpResultOptions | None = None,
) -> list[Any]:
    """``server`` 内容块的面向模型内容（在输出限额之前）。"""
    opts = options or ConvertMcpResultOptions()
    out: list[Any] = []
    for block in blocks:
        out.extend(await _block_to_content(server, block, opts))
    return out


async def convert_mcp_result(
    server: str,
    tool: str,
    result: dict[str, Any],
    options: ConvertMcpResultOptions | None = None,
) -> AgentToolResult:
    """转换一个 MCP 结果。``isError`` 结果会变成错误结果，但保留结构化结果。"""
    opts = options or ConvertMcpResultOptions()
    raw_content = result.get("content") or []
    # 没有内容块时，toLlmContent 退回用结构化内容当 JSON。
    converted: list[Any] = (
        await to_model_content(server, raw_content, opts)
        if raw_content
        else list(to_llm_content(result))
    )
    if result.get("isError") and _text_of(converted) == "":
        converted.append(TextContent(text=f"MCP tool {server}/{tool} returned an error"))
    content, full_output_path = await limit_mcp_content(converted, opts.save_output)
    script_result = {key: value for key, value in result.items() if key != "_meta"}
    details = McpToolDetails(server=server, tool=tool, full_output_path=full_output_path)
    return AgentToolResult(
        content=content,
        details=details,
        structured_content=json.loads(json.dumps(script_result, default=str)),
        is_error=bool(result.get("isError")),
    )


def _to_parameters(schema: dict[str, Any]) -> dict[str, Any]:
    """工具输入 schema 必须是对象。MCP server 可能省略 ``type``，而某些 provider 拒绝没有
    ``properties`` 的对象 schema。
    """
    return {
        **schema,
        "type": schema.get("type") or "object",
        **({} if schema.get("properties") is not None else {"properties": {}}),
    }


def to_tool_annotations(tool: dict[str, Any]) -> ToolAnnotations | None:
    """MCP 工具注解里的布尔提示；没有则返回 None。"""
    raw = tool.get("annotations") or {}
    hints: dict[str, bool] = {}
    for hint in _ANNOTATION_HINTS:
        value = raw.get(hint)
        if isinstance(value, bool):
            hints[hint] = value
    if not hints:
        return None
    return ToolAnnotations(
        read_only_hint=hints.get("readOnlyHint"),
        destructive_hint=hints.get("destructiveHint"),
        idempotent_hint=hints.get("idempotentHint"),
        open_world_hint=hints.get("openWorldHint"),
    )


def _make_progress(on_update: Any, server: str, tool_name: str) -> Any:
    """构造 MCP 进度通知 → pi 部分结果的回调（对齐上游 onProgress）。"""

    def report(progress: dict[str, Any]) -> None:
        total = progress.get("total")
        suffix = "" if total is None else f"/{total}"
        text = progress.get("message") or f"Progress {progress.get('progress')}{suffix}"
        on_update(
            AgentToolResult(
                content=[TextContent(text=text)],
                details=McpToolDetails(server=server, tool=tool_name),
            )
        )

    return report


def create_mcp_tool_definition(
    *,
    server: str,
    tool: dict[str, Any],
    name: str,
    exposure: str,
    namespace: ToolNamespace,
    timeout_ms: int,
    get_client: Callable[[], Awaitable[McpToolCaller]],
    readable_resources: Callable[[], bool] | None = None,
) -> ToolDefinition:
    """构造一个 MCP 工具的 :class:`ToolDefinition`。"""
    tool_name = tool.get("name", "")
    title = tool.get("title") or (tool.get("annotations") or {}).get("title")
    annotations = to_tool_annotations(tool)
    label = f"{server}/{tool_name}"
    description = (
        (tool.get("description") or "").strip()
        or title
        or f"MCP tool {tool_name} from server {server}"
    )

    class _McpTool(ToolDefinition):
        async def execute(
            self,
            tool_call_id: str,
            params: dict[str, Any],
            cancel_event: Any = None,
            on_update: Any = None,
            ctx: Any = None,
        ) -> AgentToolResult:
            client = await get_client()
            result = await client.call_tool(
                tool_name,
                params or {},
                {
                    "signal": cancel_event,
                    "timeoutMs": timeout_ms,
                    "onProgress": None
                    if on_update is None
                    else _make_progress(on_update, server, tool_name),
                },
            )
            return await convert_mcp_result(
                server,
                tool_name,
                result,
                ConvertMcpResultOptions(
                    readable_resources=readable_resources() if readable_resources else False
                ),
            )

    return _McpTool(
        name=name,
        label=label,
        description=description,
        parameters=_to_parameters(tool.get("inputSchema") or {}),
        output_schema=create_mcp_result_schema(tool.get("outputSchema")),
        exposure=to_tool_exposure(exposure),
        namespace=namespace,
        annotations=annotations,
    )
