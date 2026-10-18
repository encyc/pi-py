"""MCP 扩展的内核：工具注册、loadout 与资源工具同步。

对应上游 ``extensions/mcp/index.ts`` **279–536 行**（C3b-2a 切片）。

**尚未移植**（见 ``PORTING.md`` 的 Tier-B③）：
* **C3b-2b**（上游 537–783）：6 个 ``pi.on(...)`` 生命周期钩子与连接生命周期
* **C3b-3 已取消**：上游 784–1346 是 Manager（``/mcp`` in the TUI），**不移植**；
  ``create_mcp_extension`` 因此在 C3b-2b 完成前不导出。

**有意偏离**
* 上游 ``index.ts:382`` 的 ``pi.registerToolRenderer(...)`` 调用**删除** —— 需要渲染宿主，本包没有
  （见 ``PORTING.md`` 的〈TUI 相关工作整体不移植〉）。
* 上游 ``ensureDiscoveryActive`` 用 ``ctx.ui.notify`` 弹警告；本包无 UI，
  **改为返回该警告文本**（一次性语义由内部的 ``warned_unreachable`` 保留）。
  这是「可测的真实判定 + 去掉呈现」，不是桩：可达性结论本身被完整计算并返回。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...extensions.types import ExtensionAPI, ToolDefinition
from ...mcp_servers import get_mcp_tool_exposure, mcp_namespace
from ..tool_search import TOOL_SEARCH_TOOL_NAME
from .index import (
    McpServer,
    configured_exposures,
    exposure_of,
    is_enabled,
)
from .resources import create_mcp_resource_tool_definitions
from .runtime import McpServerConnection
from .tools import create_mcp_tool_definition, create_mcp_tool_name

__all__ = ["CODEMODE_TOOL_NAME", "McpToolRegistration"]

#: ``codemode`` 工具的固定名字（上游 ``@earendil-works/pi-codemode`` 导出）。
CODEMODE_TOOL_NAME = "codemode"

#: 资源工具同步时按此优先级取「最宽」的 exposure。
_EXPOSURE_PRIORITY: tuple[str, ...] = ("direct", "codemode", "deferred")


@dataclass
class McpToolRegistration:
    """把 MCP server 的工具注册进宿主，并维护 loadout。

    对应上游 ``index.ts:374-536``。所有对外部状态的读取都走回调，因此可在测试里注入假宿主。
    """

    pi: ExtensionAPI
    #: 按名字找 server（上游 ``findServer``）。
    find_server: Callable[[str], McpServer | None]
    #: 全部 server（资源工具需要看它们的状态）。
    servers: Callable[[], Sequence[McpServer]]
    #: 资源工具能触达的连接（上游 ``resourceServers``）。
    resource_servers: Callable[[], Sequence[McpServerConnection]]
    #: 是否自动启用 codemode（来自 ``mcp.json`` 的 ``autoEnableCodemode``）。
    auto_enable_codemode: Callable[[], bool] = lambda: True

    #: pi 工具名 → 分配给的 ``<server>\0<tool>``，使名字唯一且稳定。
    tool_owners: dict[str, str] = field(default_factory=dict)
    #: 每个 server 当前提供的工具名。
    server_tools: dict[str, set[str]] = field(default_factory=dict)
    #: 每个工具名最后注册的定义，用来把撤下的工具重新注册为 hidden。
    definitions: dict[str, ToolDefinition] = field(default_factory=dict)
    #: 资源工具上次注册时的 exposure；某个 server 有资源前为 None。
    resource_tools_exposure: str | None = None
    #: 是否已经就「MCP 工具不可达」告警过（一次性）。
    warned_unreachable: bool = False

    # ---- 工具注册 ----

    def register_tools(self, connection: McpServerConnection) -> None:
        """把一个已连接 server 的工具注册进宿主（并撤下它不再提供的工具）。"""
        server = connection.entry.name
        found = self.find_server(server)
        entry = found.entry if found is not None else connection.entry
        description = (getattr(entry.config, "description", None) or "").strip()
        namespace: dict[str, Any] = {
            "name": mcp_namespace(server),
            # `describeNamespace("docs")` 能找到 `mcp__docs`。
            "aliases": [server],
        }
        if description:
            namespace["description"] = description
        if connection.instructions:
            namespace["instructions"] = connection.instructions

        from ...extensions.types import ToolNamespace

        namespace_obj = ToolNamespace(
            name=namespace["name"],
            aliases=namespace.get("aliases"),
            description=namespace.get("description"),
            instructions=namespace.get("instructions"),
        )

        previous = self.server_tools.get(server, set())
        current: set[str] = set()
        # 与 Codex 一致：凡净化后同名的工具**都**加哈希后缀，因此「谁保留朴素名」不取决于列表顺序。
        plain = [
            create_mcp_tool_name(server, name)
            for name in dict.fromkeys(tool.get("name", "") for tool in connection.tools)
        ]

        def assign_name(tool: str, owner: str) -> str:
            def is_taken(candidate: str) -> bool:
                existing = self.tool_owners.get(candidate)
                return (
                    (existing is not None and existing != owner)
                    or candidate in current
                    or plain.count(candidate) > 1
                )

            name = create_mcp_tool_name(server, tool, is_taken)
            self.tool_owners[name] = owner
            current.add(name)
            return name

        for tool in connection.tools:
            tool_name = tool.get("name", "")
            definition = create_mcp_tool_definition(
                server=server,
                tool=tool,
                name=assign_name(tool_name, f"{server}\0{tool_name}"),
                exposure=get_mcp_tool_exposure(entry.config, tool_name),
                namespace=namespace_obj,
                timeout_ms=connection.timeout_ms,
                get_client=_client_resolver(self.find_server, server, tool_name),
                readable_resources=lambda: any(
                    current_server.name == server for current_server in self.resource_servers()
                ),
            )
            self.definitions[definition.name] = definition
            self.pi.register_tool(definition)

        self.server_tools[server] = current
        # 工具无法注销，所以 server 撤下的工具被**重新注册为 hidden**。等 server 再次提供它们时，
        # 上面的分支会用配置的 exposure 重新注册。
        for name in previous:
            stale = self.definitions.get(name)
            if name not in current and stale is not None:
                self.pi.register_tool(dataclasses.replace(stale, exposure="hidden"))
        self.sync_resource_tools()

    def hide_tools(self, server: str) -> None:
        """让被禁用 server 的工具不可达。"""
        for name in self.server_tools.get(server, set()):
            hidden = self.definitions.get(name)
            if hidden is not None:
                self.pi.register_tool(dataclasses.replace(hidden, exposure="hidden"))
        self.server_tools[server] = set()
        self.sync_resource_tools()

    # ---- 资源工具 ----

    def servers_with_resources(self) -> list[McpServer]:
        """启用、有资源、且 exposure 不是 ``hidden`` 的 server —— 资源工具触达它们。"""
        return [
            server
            for server in self.servers()
            if server.connection is not None
            and server.connection.has_resources
            and is_enabled(server)
            and exposure_of(server.entry) != "hidden"
        ]

    def sync_resource_tools(self) -> None:
        """用所触达 server 里**最宽**的 exposure 注册资源工具。

        有 direct 就 direct，依次类推；没有 server 有资源时它们被隐藏。
        """
        exposures = {exposure_of(server.entry) for server in self.servers_with_resources()}
        exposure = next((item for item in _EXPOSURE_PRIORITY if item in exposures), None)
        nxt = exposure or "hidden"
        if nxt == self.resource_tools_exposure or (
            self.resource_tools_exposure is None and nxt == "hidden"
        ):
            return
        was_direct = self.resource_tools_exposure == "direct"
        self.resource_tools_exposure = nxt

        # McpServerConnection 结构上满足 McpResourceServer；mypy 需要显式转一次。
        def _resource_servers() -> list[Any]:
            return list(self.resource_servers())

        definitions = create_mcp_resource_tool_definitions(exposure=nxt, servers=_resource_servers)
        for definition in definitions:
            self.pi.register_tool(definition)
        if was_direct:
            names = {definition.name for definition in definitions}
            self.pi.set_active_tools(
                [name for name in self.pi.get_active_tools() if name not in names]
            )

    # ---- 发现工具的激活 ----

    def ensure_discovery_active(self) -> str | None:
        """激活按 exposure 触达 MCP 工具所需的那个发现工具。

        未声明给模型的工具只能经 ``codemode``（脚本调用）或 ``tool_search``（声明它们）触达；
        两者都能触达全部这类工具。按配置激活所需的那一个：``codemode`` 对应 ``codemode``
        exposure（除非 ``autoEnableCodemode`` 为假），``deferred`` 对应 ``tool_search``。

        返回需要告知用户的警告文本（**上游在此时弹 UI 警告；本包无 UI，改为返回**），
        没有问题时返回 None。
        """
        # 从**配置**读，因此在 server 连上之前就能激活。
        exposures: set[str] = set()
        for server in self.servers():
            if is_enabled(server):
                exposures |= configured_exposures(server.entry)
        needs_codemode = "codemode" in exposures
        needs_tool_search = "deferred" in exposures
        if not needs_codemode and not needs_tool_search:
            return None

        # 只有声明了 ``reachesTools`` 的同名工具才能触达 MCP 工具；别的扩展注册的同名工具不算。
        tools = self.pi.get_all_tools()

        def has_reaching_tool(name: str) -> bool:
            return any(tool.name == name and tool.reaches_tools for tool in tools)

        has_codemode = has_reaching_tool(CODEMODE_TOOL_NAME)
        has_tool_search = has_reaching_tool(TOOL_SEARCH_TOOL_NAME)
        active = self.pi.get_active_tools()
        activate: list[str] = []
        if (
            needs_codemode
            and has_codemode
            and self.auto_enable_codemode()
            and CODEMODE_TOOL_NAME not in active
        ):
            activate.append(CODEMODE_TOOL_NAME)
        if needs_tool_search and has_tool_search and TOOL_SEARCH_TOOL_NAME not in active:
            activate.append(TOOL_SEARCH_TOOL_NAME)
        if activate:
            self.pi.set_active_tools([*active, *activate])

        reachable = [*active, *activate]
        if has_codemode and CODEMODE_TOOL_NAME in reachable:
            return None
        if has_tool_search and TOOL_SEARCH_TOOL_NAME in reachable:
            return None
        if self.warned_unreachable:
            return None
        self.warned_unreachable = True
        reason = (
            " (autoEnableCodemode is false)"
            if needs_codemode and has_codemode and not self.auto_enable_codemode()
            else ""
        )
        return (
            "MCP tools are only reachable from the codemode or tool_search tool, but neither is "
            f"active{reason}; they cannot be called."
        )


def _client_resolver(
    find_server: Callable[[str], McpServer | None], server: str, tool_name: str
) -> Callable[[], Any]:
    """构造「执行时再解析 client」的取用器（上游 ``registerTools`` 内的 ``getClient``）。

    已准备好的调用可能活过它那个定义所属的连接（例如就绪钩子在等禁用/重新启用）。因此执行时
    重新解析替换者。
    """

    async def resolve() -> Any:
        current = find_server(server)
        if current is None or not is_enabled(current):
            raise RuntimeError(f'MCP server "{server}" is disabled.')
        client = current.connection
        if client is None:
            raise RuntimeError(f'MCP server "{server}" is still starting.')
        if get_mcp_tool_exposure(current.entry.config, tool_name) == "hidden" or (
            client.state == "connected"
            and not any(offered.get("name") == tool_name for offered in client.tools)
        ):
            raise RuntimeError(f'MCP tool "{server}/{tool_name}" is no longer available.')
        return client

    return resolve
