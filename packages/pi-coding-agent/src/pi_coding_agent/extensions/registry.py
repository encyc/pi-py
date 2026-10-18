"""工具注册表 + 参考扩展宿主：exposure / 活跃集合 / loadout 语义 + ``ExtensionAPI`` 面。

对应上游 ``core/agent-session.ts`` 里与工具装载相关的那组私有逻辑
（``_getToolExposure`` / ``_isDeclarable`` / ``_isActivatedOnRegistration`` /
``_applyToolLoadout`` / ``_getCallableTools`` / ``getAllTools``），
外加 ``core/extensions/runner.ts`` 的 ``registerTool`` 入口。

它同时是 :class:`~pi_coding_agent.extensions.types.ExtensionAPI` 的**参考实现**，
供扩展在测试或嵌入场景下直接使用：工具装载语义之外，还提供设置读取、会话条目追加、
事件订阅/派发、命令注册与 MCP server 注册。这些成员都是**真实实现**（存进本对象并可查询、
可派发），不是占位。

**未移植**（见 ``PORTING.md``）：``_isActivatable`` 里与 CLI ``--tools`` /
``--exclude-tools`` 白名单相关的分支 —— 那属于 CLI 装载逻辑，不在本包范围。
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..mcp_servers import McpServerRegistry, RegisteredMcpServer
from .types import (
    ExtensionAPI,
    ToolDefinition,
    ToolExposure,
    ToolInfo,
    ToolLoadout,
    ToolLoadoutChanges,
)

__all__ = ["ToolRegistry"]

#: 上游 ``_isDeclarable``：只有这两种 exposure 会被声明给模型。
_DECLARABLE: frozenset[ToolExposure] = frozenset({"direct", "model-only"})


@dataclass
class _Entry:
    definition: ToolDefinition
    source_info: object | None = None


@dataclass
class ToolRegistry:
    """已注册工具的容器 + 活跃集合。

    对齐上游语义：

    * exposure 缺省为 ``direct``（上游 ``_getToolExposure``）
    * ``direct`` / ``model-only`` 在注册时激活，其余不激活；
      显式 ``default_active=False`` 会抑制激活（上游 ``_isActivatedOnRegistration``）
    * 激活集合只保留**已注册、非 hidden** 的工具（上游 ``_applyToolLoadout``）
    * 可调用集合 = 「active 的 ``direct``」∪「全部 ``codemode``」∪「全部 ``deferred``」
      （上游 ``_getCallableTools``）
    """

    _entries: dict[str, _Entry] = field(default_factory=dict)
    _active: list[str] = field(default_factory=list)
    #: 有效设置（不透明映射，见 ``types.ExtensionAPI.get_settings``）。
    settings: dict[str, Any] = field(default_factory=dict)
    #: ``append_entry()`` 追加的会话条目。
    entries: list[tuple[str, Any]] = field(default_factory=list)
    #: 事件名 → handler 列表。
    handlers: dict[str, list[Any]] = field(default_factory=dict)
    #: 已注册命令：名 → CommandOptions。
    commands: dict[str, Any] = field(default_factory=dict)
    #: 扩展注册的 MCP server（真实注册表，非占位）。
    mcp_servers: McpServerRegistry = field(default_factory=McpServerRegistry)
    #: 本宿主的"扩展路径"标识，用于 MCP server 归属判定。
    _owner_path: str = "<host>"

    # ---- 注册 ----

    def register_tool(
        self, definition: ToolDefinition, *, default_active: bool | None = None
    ) -> None:
        """注册工具；按 exposure / ``default_active`` 决定是否同时激活。

        ``default_active`` 参数等价于上游 ``registerTool(def, { defaultActive })``
        与 ``definition.defaultActive`` 的合并 —— 显式参数优先。
        """
        self._entries[definition.name] = _Entry(definition=definition)
        effective = default_active if default_active is not None else definition.default_active
        if (
            self._activated_on_registration(definition, effective)
            and definition.name not in self._active
        ):
            self._active.append(definition.name)

    def unregister_tool(self, name: str) -> bool:
        """移除工具，并从活跃集合里去掉。返回是否确实移除过。"""
        if name not in self._entries:
            return False
        del self._entries[name]
        if name in self._active:
            self._active.remove(name)
        return True

    # ---- 查询（ExtensionAPI 面）----

    def get_all_tools(self) -> list[ToolInfo]:
        return [
            ToolInfo(
                name=entry.definition.name,
                description=entry.definition.description,
                parameters=entry.definition.parameters,
                exposure=self.exposure_of(entry.definition.name),
                prompt_guidelines=entry.definition.prompt_guidelines,
                namespace=entry.definition.namespace,
                annotations=entry.definition.annotations,
                reaches_tools=entry.definition.reaches_tools,
                source_info=entry.source_info,
            )
            for entry in self._entries.values()
        ]

    def get_active_tools(self) -> list[str]:
        return list(self._active)

    def set_active_tools(self, names: Sequence[str]) -> None:
        """设置活跃集合；未注册或 ``hidden`` 的条目被丢弃（对齐上游 ``_applyToolLoadout``）。"""
        self._active = [name for name in self._apply_loadout(names)]

    def get_tool_definition(self, name: str) -> ToolDefinition | None:
        entry = self._entries.get(name)
        return entry.definition if entry else None

    # ---- 语义 ----

    def exposure_of(self, name: str) -> ToolExposure:
        entry = self._entries.get(name)
        return entry.definition.exposure if entry else "direct"

    def is_declarable(self, name: str) -> bool:
        """激活该工具是否会把声明给模型（上游 ``_isDeclarable``）。"""
        return self.exposure_of(name) in _DECLARABLE

    def _activated_on_registration(
        self, definition: ToolDefinition, default_active: bool | None
    ) -> bool:
        if self.exposure_of(definition.name) not in _DECLARABLE:
            return False
        return default_active is not False

    def _apply_loadout(self, names: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            if name not in self._entries:
                continue
            if self.exposure_of(name) == "hidden":
                continue
            out.append(name)
        return out

    def get_callable_tool_names(self, active: set[str] | None = None) -> list[str]:
        """可经 ``ctx.executeTool()`` 调用的工具（上游 ``_getCallableTools``）。"""
        active_names = active if active is not None else set(self._active)
        out: list[str] = []
        for name in self._entries:
            exposure = self.exposure_of(name)
            if exposure in ("codemode", "deferred") or (
                exposure == "direct" and name in active_names
            ):
                out.append(name)
        return out

    def get_loadout(self) -> ToolLoadout:
        """构造 ``prepareLoadout`` 看到的 :class:`ToolLoadout`。"""
        registered = list(self._entries)
        declared = [name for name in self._active if name in self._entries]
        return ToolLoadout(
            declared=tuple(declared),
            callable=tuple(self.get_callable_tool_names(set(declared))),
            registered=tuple(registered),
            exposures={name: self.exposure_of(name) for name in registered},
            namespaces={
                name: entry.definition.namespace
                for name, entry in self._entries.items()
                if entry.definition.namespace is not None
            },
            prompt_guidelines={
                name: entry.definition.prompt_guidelines or ()
                for name, entry in self._entries.items()
            },
        )

    def run_prepare_loadout(self) -> dict[str, ToolLoadoutChanges]:
        """对每个活跃工具调用一次 ``prepareLoadout``，返回非空改动（按工具名）。"""
        loadout = self.get_loadout()
        changes: dict[str, ToolLoadoutChanges] = {}
        for name in self._active:
            entry = self._entries.get(name)
            if entry is None or entry.definition.prepare_loadout is None:
                continue
            result = entry.definition.prepare_loadout(loadout)
            if result is not None:
                changes[name] = result
        return changes

    # ---- ExtensionAPI：设置 / 会话状态 ----

    def get_settings(self) -> dict[str, Any]:
        """有效设置的副本。"""
        return dict(self.settings)

    def append_entry(self, custom_type: str, data: Any = None) -> None:
        """追加一条自定义会话条目（不发给模型）。"""
        self.entries.append((custom_type, data))

    # ---- ExtensionAPI：事件 / 命令 ----

    def on(self, event: str, handler: Any) -> Any:
        """订阅事件；返回取消订阅函数。"""
        self.handlers.setdefault(event, []).append(handler)

        def unsubscribe() -> None:
            handlers = self.handlers.get(event)
            if handlers is None:
                return
            with contextlib.suppress(ValueError):
                handlers.remove(handler)

        return unsubscribe

    async def emit(self, event: str, payload: Any = None) -> None:
        """派发给已注册的 handler（按注册顺序）。"""
        for handler in list(self.handlers.get(event, ())):
            outcome = handler(payload)
            if inspect.isawaitable(outcome):
                await outcome

    def register_command(self, name: str, options: Any) -> None:
        """注册自定义命令；同名重复注册报错（对齐上游的扩展错误语义）。"""
        if name in self.commands:
            raise ValueError(f'command "{name}" is already registered')
        self.commands[name] = options

    # ---- ExtensionAPI：MCP server ----

    def register_mcp_server(self, name: str, config: Any) -> None:
        """注册一个 MCP server（``config`` 为 ``McpServerConfig``）。"""
        self.mcp_servers.register(
            RegisteredMcpServer(name=name, config=config, extension_path=self._owner_path)
        )

    def unregister_mcp_server(self, name: str) -> None:
        """移除本宿主注册的 MCP server。"""
        self.mcp_servers.unregister(name, self._owner_path)

    def get_mcp_servers(self) -> list[RegisteredMcpServer]:
        """扩展注册的全部 MCP server。"""
        return self.mcp_servers.list()


# ToolRegistry 满足 ExtensionAPI 协议（结构化检查，运行期亦可 isinstance）。
def _assert_protocol() -> None:
    registry: ExtensionAPI = ToolRegistry()
    del registry


_assert_protocol()
