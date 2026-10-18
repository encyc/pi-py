"""MCP 资源，经由 Codex 与 opencode 用的那三个工具暴露。

对应上游 ``extensions/mcp/resources.ts``（339 行，**完整移植**）。

``list_mcp_resources`` / ``list_mcp_resource_templates`` / ``read_mcp_resource`` 都接受 ``server``
参数，并覆盖**每个**提供已连接 server 的资源，因此按这些工具训练过的模型可以原样使用。

清单是 JSON（与 Codex 一致）：``{server?, resources: [{server, ...resource}], nextCursor?}``。
给了 ``server`` 就列一页、``cursor`` 续页；没给就列每个 server 的每一页。
MCP App 资源（``ui://`` URI 与 ``profile=mcp-app`` 的 HTML）被排除——它们是给能渲染的宿主用的
用户界面，图标同理。读到的资源对模型变成文本与图像；二进制资源落到临时文件。脚本拿到 JSON 载荷。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from pi_agent_core.types import AgentToolResult
from pi_ai import TextContent

from ...extensions.types import ToolAnnotations, ToolDefinition
from ...mcp_servers import (
    LIST_MCP_RESOURCE_TEMPLATES_TOOL,
    LIST_MCP_RESOURCES_TOOL,
    READ_MCP_RESOURCE_TOOL,
)
from .tools import McpToolDetails, limit_mcp_content, to_model_content, to_tool_exposure

__all__ = [
    "LIST_MCP_RESOURCE_TEMPLATES_TOOL",
    "LIST_MCP_RESOURCES_TOOL",
    "READ_MCP_RESOURCE_TOOL",
    "McpResourceServer",
    "create_mcp_resource_tool_definitions",
    "is_mcp_app_resource",
    "listed",
    "string_argument",
]

#: MCP App 用户界面：只有能渲染它们的宿主用得上。
_MCP_APP_PROFILE = re.compile(r";\s*profile\s*=\s*\"?mcp-app\"?", re.IGNORECASE)


class McpResourceServer(Protocol):
    """一个提供资源的已连接 server。"""

    name: str
    timeout_ms: int

    async def resources_page(self, cursor: str | None, options: Any) -> dict[str, Any]: ...

    async def resource_templates_page(self, cursor: str | None, options: Any) -> dict[str, Any]: ...

    async def all_resources(self, options: Any) -> list[dict[str, Any]]: ...

    async def all_resource_templates(self, options: Any) -> list[dict[str, Any]]: ...

    async def read_resource(self, uri: str, options: Any) -> dict[str, Any]: ...


def is_mcp_app_resource(item: dict[str, Any]) -> bool:
    """是否是 MCP App 资源（只有渲染它们的宿主能用）。"""
    uri = item.get("uri") or item.get("uriTemplate") or ""
    return uri.startswith("ui://") or bool(_MCP_APP_PROFILE.search(item.get("mimeType") or ""))


def listed(server: str, item: dict[str, Any]) -> dict[str, Any]:
    """列出的资源或模板：去掉 ``_meta`` 与图标，并打上 server 标记。"""
    return {"server": server, **{k: v for k, v in item.items() if k not in ("_meta", "icons")}}


def _string_property(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


_SERVER_FILTER = _string_property("MCP server name. Omit to list every server with resources.")
_CURSOR = _string_property(
    "Opaque cursor from a previous call with the same server; omit for the first page."
)

_LIST_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"server": _SERVER_FILTER, "cursor": _CURSOR},
    "additionalProperties": False,
}

_READ_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": _string_property(
            "MCP server name exactly as configured. Must match the 'server' field returned "
            "by list_mcp_resources."
        ),
        "uri": _string_property(
            "Resource URI to read. Must be one of the URIs returned by list_mcp_resources."
        ),
    },
    "required": ["server", "uri"],
    "additionalProperties": False,
}

_OPTIONAL_STRING: dict[str, Any] = {"type": "string"}

_LISTING_ERRORS: dict[str, Any] = {
    "type": "array",
    "description": "Servers that could not be listed",
    "items": {
        "type": "object",
        "properties": {"server": {"type": "string"}, "error": {"type": "string"}},
        "required": ["server", "error"],
    },
}

_LIST_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": _OPTIONAL_STRING,
        "resources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "uri": {"type": "string"},
                    "name": {"type": "string"},
                    "title": _OPTIONAL_STRING,
                    "description": _OPTIONAL_STRING,
                    "mimeType": _OPTIONAL_STRING,
                    "size": {"type": "number"},
                },
                "required": ["server", "uri", "name"],
            },
        },
        "nextCursor": _OPTIONAL_STRING,
        "errors": _LISTING_ERRORS,
    },
    "required": ["resources"],
}

_LIST_TEMPLATES_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": _OPTIONAL_STRING,
        "resourceTemplates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "uriTemplate": {"type": "string", "description": "RFC 6570 URI template"},
                    "name": {"type": "string"},
                    "title": _OPTIONAL_STRING,
                    "description": _OPTIONAL_STRING,
                    "mimeType": _OPTIONAL_STRING,
                },
                "required": ["server", "uriTemplate", "name"],
            },
        },
        "nextCursor": _OPTIONAL_STRING,
        "errors": _LISTING_ERRORS,
    },
    "required": ["resourceTemplates"],
}

_READ_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": {"type": "string"},
        "uri": {"type": "string"},
        "contents": {
            "type": "array",
            "items": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "uri": {"type": "string"},
                            "mimeType": _OPTIONAL_STRING,
                            "text": {"type": "string"},
                        },
                        "required": ["uri", "text"],
                    },
                    {
                        "type": "object",
                        "properties": {
                            "uri": {"type": "string"},
                            "mimeType": _OPTIONAL_STRING,
                            "blob": {"type": "string", "description": "base64"},
                        },
                        "required": ["uri", "blob"],
                    },
                ],
            },
        },
    },
    "required": ["server", "uri", "contents"],
}


def string_argument(params: Any, key: str) -> str | None:
    """取一个字符串参数；非字符串抛错，空白视为未提供。"""
    value = (params or {}).get(key) if isinstance(params, dict) else None
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value.strip() or None


def _error_message(error: BaseException) -> str:
    return str(error) or error.__class__.__name__


async def _json_result(tool: str, server: str | None, payload: dict[str, Any]) -> AgentToolResult:
    content, full_output_path = await limit_mcp_content(
        [TextContent(text=json.dumps(payload, ensure_ascii=False))]
    )
    return AgentToolResult(
        content=content,
        details=McpToolDetails(server=server or "", tool=tool, full_output_path=full_output_path),
        structured_content=payload,
    )


def create_mcp_resource_tool_definitions(
    *,
    exposure: str,
    servers: Callable[[], Sequence[McpResourceServer]],
) -> list[ToolDefinition]:
    """三个资源工具。``servers`` 在**调用时**返回它们能触达的 server。"""
    read_only = ToolAnnotations(read_only_hint=True)

    def find_server(name: str) -> McpResourceServer:
        available = list(servers())
        for candidate in available:
            if candidate.name == name:
                return candidate
        names = ", ".join(candidate.name for candidate in available)
        raise ValueError(
            f'MCP server "{name}" has no resources'
            + (f". Servers with resources: {names}" if names else "")
        )

    async def run_list(
        params: Any,
        signal: Any,
        key: str,
        page: Callable[[McpResourceServer, str | None, Any], Awaitable[dict[str, Any]]],
        all_items: Callable[[McpResourceServer, Any], Awaitable[list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        """一个 server 的一页，或每个 server 的每一页。"""
        server_name = string_argument(params, "server")
        cursor = string_argument(params, "cursor")
        if server_name:
            server = find_server(server_name)
            result = await page(server, cursor, {"signal": signal, "timeoutMs": server.timeout_ms})
            items = [
                listed(server.name, item)
                for item in result.get("items", [])
                if not is_mcp_app_resource(item)
            ]
            payload: dict[str, Any] = {"server": server.name, key: items}
            if result.get("nextCursor") is not None:
                payload["nextCursor"] = result["nextCursor"]
            return payload

        if cursor:
            raise ValueError("cursor can only be used when a server is specified")
        # 上游用 localeCompare；Python 端按名字排序（对 MCP server 名的实际取值等价）。
        ordered = sorted(servers(), key=lambda candidate: candidate.name)
        outcomes = await asyncio.gather(
            *(
                all_items(server, {"signal": signal, "timeoutMs": server.timeout_ms})
                for server in ordered
            ),
            return_exceptions=True,
        )
        items_out: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for server, outcome in zip(ordered, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                errors.append({"server": server.name, "error": _error_message(outcome)})
            else:
                items_out.extend(
                    listed(server.name, item) for item in outcome if not is_mcp_app_resource(item)
                )
        payload = {key: items_out}
        if errors:
            payload["errors"] = errors
        return payload

    class _ListResources(ToolDefinition):
        async def execute(
            self,
            tool_call_id: str,
            params: dict[str, Any],
            cancel_event: Any = None,
            on_update: Any = None,
            ctx: Any = None,
        ) -> AgentToolResult:
            async def page(
                server: McpResourceServer, cursor: str | None, options: Any
            ) -> dict[str, Any]:
                result = await server.resources_page(cursor, options)
                return {
                    "items": result.get("resources") or [],
                    "nextCursor": result.get("nextCursor"),
                }

            payload = await run_list(
                params,
                cancel_event,
                "resources",
                page,
                lambda server, options: server.all_resources(options),
            )
            return await _json_result(
                LIST_MCP_RESOURCES_TOOL, string_argument(params, "server"), payload
            )

    class _ListTemplates(ToolDefinition):
        async def execute(
            self,
            tool_call_id: str,
            params: dict[str, Any],
            cancel_event: Any = None,
            on_update: Any = None,
            ctx: Any = None,
        ) -> AgentToolResult:
            async def page(
                server: McpResourceServer, cursor: str | None, options: Any
            ) -> dict[str, Any]:
                result = await server.resource_templates_page(cursor, options)
                return {
                    "items": result.get("resourceTemplates") or [],
                    "nextCursor": result.get("nextCursor"),
                }

            payload = await run_list(
                params,
                cancel_event,
                "resourceTemplates",
                page,
                lambda server, options: server.all_resource_templates(options),
            )
            return await _json_result(
                LIST_MCP_RESOURCE_TEMPLATES_TOOL, string_argument(params, "server"), payload
            )

    class _ReadResource(ToolDefinition):
        async def execute(
            self,
            tool_call_id: str,
            params: dict[str, Any],
            cancel_event: Any = None,
            on_update: Any = None,
            ctx: Any = None,
        ) -> AgentToolResult:
            server_name = string_argument(params, "server")
            uri = string_argument(params, "uri")
            if not server_name:
                raise ValueError("server must be provided")
            if not uri:
                raise ValueError("uri must be provided")
            server = find_server(server_name)
            result = await server.read_resource(
                uri, {"signal": cancel_event, "timeoutMs": server.timeout_ms}
            )
            contents = result.get("contents") or []
            # 多个内容（例如一个目录）要按 URI 标注。
            blocks: list[dict[str, Any]] = []
            for entry in contents:
                if len(contents) > 1:
                    blocks.append({"type": "text", "text": f"{entry.get('uri')}:"})
                blocks.append({"type": "resource", "resource": entry})
            converted = await to_model_content(server.name, blocks)
            content, full_output_path = await limit_mcp_content(
                converted or [TextContent(text=f"Resource {uri} is empty.")]
            )
            return AgentToolResult(
                content=content,
                details=McpToolDetails(
                    server=server.name,
                    tool=READ_MCP_RESOURCE_TOOL,
                    full_output_path=full_output_path,
                ),
                structured_content={
                    "server": server.name,
                    "uri": uri,
                    "contents": [
                        {k: v for k, v in entry.items() if k != "_meta"} for entry in contents
                    ],
                },
            )

    return [
        _ListResources(
            name=LIST_MCP_RESOURCES_TOOL,
            label=LIST_MCP_RESOURCES_TOOL,
            description=(
                "Lists resources provided by MCP servers. Resources allow servers to share data "
                "that provides context to language models, such as files, database schemas, or "
                "application-specific information. Prefer resources over web search when possible."
            ),
            parameters=_LIST_PARAMETERS,
            output_schema=_LIST_OUTPUT_SCHEMA,
            exposure=to_tool_exposure(exposure),
            annotations=read_only,
        ),
        _ListTemplates(
            name=LIST_MCP_RESOURCE_TEMPLATES_TOOL,
            label=LIST_MCP_RESOURCE_TEMPLATES_TOOL,
            description=(
                "Lists resource templates provided by MCP servers. Parameterized resource "
                "templates allow servers to share data that takes parameters and provides context "
                "to language models, such as files, database schemas, or application-specific "
                "information. Prefer resource templates over web search when possible."
            ),
            parameters=_LIST_PARAMETERS,
            output_schema=_LIST_TEMPLATES_OUTPUT_SCHEMA,
            exposure=to_tool_exposure(exposure),
            annotations=read_only,
        ),
        _ReadResource(
            name=READ_MCP_RESOURCE_TOOL,
            label=READ_MCP_RESOURCE_TOOL,
            description="Read a specific resource from an MCP server given the server name and resource URI.",
            parameters=_READ_PARAMETERS,
            output_schema=_READ_OUTPUT_SCHEMA,
            exposure=to_tool_exposure(exposure),
            annotations=read_only,
        ),
    ]
