"""编码 agent 内置扩展的**声明面**。

对应上游 ``core/extensions/types.ts`` 中被内置扩展实际引用的那部分
（``src/extensions/{codemode,mcp,tool-search}/`` 只用到这些名字）。

**有意偏离**：上游该文件 2,305 行，其中大部分是扩展**宿主**（runner）、生命周期
boundary 事件、UI 上下文、渲染器类型。本包未移植扩展宿主（见 ``PORTING.md``），
因此这里只保留：

* :data:`ToolExposure` / :class:`ToolAnnotations` / :class:`ToolNamespace` —— 工具元数据
* :class:`ToolLoadout` / :class:`ToolLoadoutChanges` —— ``prepareLoadout`` 的输入输出
* :class:`ToolInfo` —— ``getAllTools()`` 的投影
* :class:`ToolDefinition` —— ``registerTool()`` 的形状
* :class:`ExtensionAPI` / :data:`ExtensionFactory` —— 扩展拿到的宿主接口

宿主接口按**当前已移植扩展的最小需要**声明（``getAllTools`` / ``getActiveTools`` /
``setActiveTools`` / ``registerTool``）。codemode 与 mcp 扩展还需要 ``getSettings``、
``appendEntry``、``on``、``registerCommand``、``registerMcpServer``、``registerToolRenderer``、
``getMcpServers`` 等成员——它们**尚未声明**，等宿主基建补齐后再扩（见 ``PORTING.md``）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pi_agent_core.types import AgentToolResult, ToolExecutionMode

__all__ = [
    "CommandOptions",
    "ExtensionAPI",
    "ExtensionContext",
    "ExtensionFactory",
    "ExtensionMode",
    "ToolAnnotations",
    "ToolDefinition",
    "ToolExposure",
    "ToolInfo",
    "ToolLoadout",
    "ToolLoadoutChanges",
    "ToolNamespace",
    "to_agent_tool",
]

#: 模型如何触达一个工具（对齐上游 ``ToolExposure``；``"callable"`` 指可被其它工具
#: 经 ``ctx.executeTool()`` 调用，例如 codemode 脚本）。
#:
#: - ``direct``：激活时声明给模型，且激活时可调用。
#: - ``model-only``：激活时声明给模型，永不可调用（编排类/交互类工具）。
#: - ``codemode``：注册后即可调用；除非显式激活，否则不声明给模型。
#: - ``deferred``：同 ``codemode``，但 codemode 工具不列出它；``tool_search`` 能找到它。
#: - ``hidden``：已注册但不可达；激活它没有效果。
#:
#: ``direct`` / ``model-only`` 在注册时即激活，其余不会。
#: 「活跃集合」（``getActiveTools`` / ``setActiveTools``）就是声明给模型的那批。
ToolExposure = Literal["direct", "model-only", "codemode", "deferred", "hidden"]


@dataclass
class ToolAnnotations:
    """工具行为提示，语义同 MCP tool annotations。

    由工具作者声明、**不经验证**；权限类扩展可据此决定哪些调用需要确认。
    """

    #: 工具不修改其环境。
    read_only_hint: bool | None = None
    #: 工具可能删除或覆盖数据，而不只是追加。仅在非只读时有意义。
    destructive_hint: bool | None = None
    #: 同参数重复调用不产生额外效果。仅在非只读时有意义。
    idempotent_hint: bool | None = None
    #: 工具触达开放世界（如网络），而非封闭域。
    open_world_hint: bool | None = None


@dataclass
class ToolNamespace:
    """一组相关工具，例如某个 MCP server 的全部工具。"""

    #: 例如 ``mcp__docs``。
    name: str
    #: 其它可用于按名查找该 namespace 的别名，例如 ``docs``。
    aliases: Sequence[str] | None = None
    #: 在面向模型的工具清单里跟着分组显示的一行摘要。
    description: str | None = None
    #: 更长的使用说明（如 MCP server instructions）。不属于工具清单，
    #: 由按需描述 namespace 的工具（codemode 的 ``describeNamespace()``）返回。
    instructions: str | None = None


@dataclass
class ToolLoadout:
    """``prepareLoadout`` 看到的一次会话工具全貌。"""

    #: 声明给模型的工具（活跃工具），保持顺序，描述为原始描述。
    declared: Sequence[Any] = field(default_factory=tuple)
    #: 可经 ``ctx.executeTool()`` 调用的工具。
    callable: Sequence[Any] = field(default_factory=tuple)
    #: 全部已注册工具。
    registered: Sequence[Any] = field(default_factory=tuple)
    #: 名称 → exposure。
    exposures: Mapping[str, ToolExposure] = field(default_factory=dict)
    #: 名称 → namespace。
    namespaces: Mapping[str, ToolNamespace] = field(default_factory=dict)
    #: 名称 → ``promptGuidelines``。
    prompt_guidelines: Mapping[str, Sequence[str]] = field(default_factory=dict)

    def get_exposure(self, name: str) -> ToolExposure:
        return self.exposures.get(name, "direct")

    def get_namespace(self, name: str) -> ToolNamespace | None:
        return self.namespaces.get(name)

    def get_prompt_guidelines(self, name: str) -> Sequence[str]:
        """某工具的 ``promptGuidelines``；被隐藏声明的工具，其条目也会从系统提示里去掉。"""
        return self.prompt_guidelines.get(name, ())


@dataclass
class ToolLoadoutChanges:
    """``prepareLoadout`` 对「模型看到什么」所做的改动。"""

    #: 按工具名覆盖面向模型的描述。
    descriptions: Mapping[str, str] | None = None
    #: 请求时不带上声明的工具。它们仍活跃、仍可调用，转写里也仍有声明，
    #: 因此活跃集合能在 ``/tree`` 与 resume 后存活。
    hidden_declarations: Sequence[str] | None = None


@dataclass
class ToolInfo:
    """``getAllTools()`` 的投影（对齐上游 ``ToolInfo``）。"""

    name: str
    description: str
    parameters: dict[str, Any]
    exposure: ToolExposure
    prompt_guidelines: Sequence[str] | None = None
    namespace: ToolNamespace | None = None
    annotations: ToolAnnotations | None = None
    reaches_tools: Callable[[dict[str, Any]], str | Sequence[str]] | None = None
    source_info: Any = None


@dataclass
class ToolDefinition(ABC):
    """``registerTool()`` 接受的工具定义（对齐上游 ``ToolDefinition``）。

    **抽象类**：上游 ``ToolDefinition`` 是纯接口、``execute`` 必填，因此这里用
    :class:`~abc.ABC` 而非可实例化的数据类 —— 直接构造 ``ToolDefinition(...)`` 会在
    **构造时**抛 ``TypeError``，而不是留下一个调用时才炸的桩。

    **有意偏离**：上游还有 ``renderCall`` / ``renderResult`` / ``renderShell``（TUI 渲染）
    与 ``constrainedSampling`` 的 provider 侧语义；前者不复刻（无 TUI），后者本包的工具
    直接作为 ``AgentTool`` 使用而不再包一层，故不在此处声明。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    #: 给 UI 的可读标签。
    label: str = ""
    #: 可选的一行摘要，用于默认系统提示的 "Available tools" 段。
    prompt_snippet: str | None = None
    #: 该工具活跃时追加到系统提示 Guidelines 段的条目。
    prompt_guidelines: Sequence[str] | None = None
    #: 模型触达方式，默认 ``direct``。
    exposure: ToolExposure = "direct"
    namespace: ToolNamespace | None = None
    annotations: ToolAnnotations | None = None
    #: 触达其它工具的工具（如 ``codemode`` / ``tool_search``）声明一次调用可能触达哪些工具。
    reaches_tools: Callable[[dict[str, Any]], str | Sequence[str]] | None = None
    #: 注册时是否激活。默认 ``direct`` / ``model-only`` 为 True，其余永不激活。
    default_active: bool | None = None
    #: 该工具活跃时调整 loadout 的呈现方式；活跃集合变化时调用。
    prepare_loadout: Callable[[ToolLoadout], ToolLoadoutChanges | None] | None = None
    #: 逐工具覆盖执行模式。
    execution_mode: ToolExecutionMode | None = None
    #: 期望出现在 ``AgentToolResult.structured_content`` 里的 JSON Schema。
    output_schema: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.name

    @abstractmethod
    def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        cancel_event: Any = None,
        on_update: Any = None,
        ctx: Any = None,
    ) -> Any:
        """执行工具。每个具体工具都必须实现。"""


#: 扩展宿主运行在哪种模式（对齐上游 ``ExtensionMode``）。
ExtensionMode = Literal["tui", "rpc", "json", "print"]


@dataclass
class CommandOptions:
    """``registerCommand()`` 的选项（上游是 ``Omit<RegisteredCommand, "name" | "sourceInfo">``）。"""

    handler: Callable[[Any], Awaitable[None] | None]
    description: str | None = None
    argument_hint: str | None = None


@dataclass
class ExtensionContext:
    """工具 ``execute()`` 拿到的一次调用上下文。

    **有意偏离**：上游还有 ``ui: ExtensionUIContext``（对话框、状态行、自定义渲染等 TUI 能力）
    与 ``projectTrust`` 等成员。本包无 TUI 宿主，``ui`` 恒为 ``None``、``has_ui`` 恒为
    ``False`` —— 这是**如实反映「本包没有 UI」**，不是占位实现：需要 UI 的扩展应检查
    ``ctx.has_ui`` 后走无 UI 分支（上游扩展本身就这么写）。
    """

    cwd: str
    mode: ExtensionMode = "print"
    has_ui: bool = False
    is_project_trusted: bool = False
    #: 模型目录/分类器/图像生成的运行时句柄（见 ``model_registry.py``）。
    model_registry: Any = None
    #: 本次调用的取消信号。
    signal: Any = None
    #: 嵌套工具调用入口（见 ``nested_tool_calls.py``）。
    execute_tool: Callable[..., Awaitable[Any]] | None = None
    #: 会话读取面（供扩展查历史/分支）。本包未移植 ``SessionManager``，故为不透明句柄。
    session_manager: Any = None
    #: 可被 ``execute_tool`` 调用的工具（对齐上游 ``ExtensionToolContext.tools``）。
    tools: Sequence[Any] = ()
    #: TUI 上下文。本包无 UI 宿主，恒为 None。
    ui: Any = None


@runtime_checkable
class ExtensionAPI(Protocol):
    """扩展拿到的宿主接口（当前为**最小面**，见模块 docstring）。"""

    def register_tool(
        self, definition: ToolDefinition, *, default_active: bool | None = None
    ) -> None:
        """注册工具。``direct`` / ``model-only`` 默认即激活。"""
        ...

    def get_all_tools(self) -> list[ToolInfo]:
        """全部已注册工具。"""
        ...

    def get_active_tools(self) -> list[str]:
        """当前声明给模型的工具名。"""
        ...

    def set_active_tools(self, names: Sequence[str]) -> None:
        """设置声明给模型的工具集合。"""
        ...

    # ---- 设置 / 会话状态 ----

    def get_settings(self) -> Mapping[str, Any]:
        """有效设置的副本（全局与项目设置合并后）。

        本包未移植 ``core/settings-manager.ts``，因此这里是**不透明映射**（上游返回 ``Settings``）：
        codemode 读 ``settings["codemode"]["mode"]`` / ``["inlineBudget"]`` 的路径与上游一致。
        """
        ...

    def append_entry(self, custom_type: str, data: Any = None) -> None:
        """向会话追加一条自定义条目用于状态持久化（不发给模型）。"""
        ...

    # ---- 事件 / 命令 ----

    def on(self, event: str, handler: Callable[..., Any]) -> Callable[[], None]:
        """订阅事件；返回取消订阅函数（对齐上游 ``pi.on()`` 的返回值）。

        上游按事件名重载出精确的 handler 类型；本包未移植 ``ExtensionEvent`` 联合，
        故 ``event`` 为字符串、``handler`` 为宽松可调用对象。
        """
        ...

    def register_command(self, name: str, options: CommandOptions) -> None:
        """注册自定义命令。"""
        ...

    # ---- MCP server ----

    def register_mcp_server(self, name: str, config: Any) -> None:
        """注册一个 MCP server（``config`` 为 ``McpServerConfig``）。

        同名重复注册会被报告为扩展错误（上游语义）；真正的连接由处理 ``mcp_servers_change``
        的扩展负责。
        """
        ...

    def unregister_mcp_server(self, name: str) -> None:
        """移除本扩展注册的 MCP server 并关闭其连接。"""
        ...

    def get_mcp_servers(self) -> list[Any]:
        """扩展注册的全部 MCP server（``RegisteredMcpServer`` 列表）。"""
        ...


#: 扩展工厂：宿主传入 ``ExtensionAPI``，扩展就地注册工具/命令/事件。
ExtensionFactory = Callable[[ExtensionAPI], None]


def to_agent_tool(definition: ToolDefinition) -> Any:
    """把 :class:`ToolDefinition` 适配成 pi-agent-core 的 ``AgentTool``。

    对应上游 ``core/tools/tool-definition-wrapper.ts: wrapToolDefinition`` 的无上下文形态。
    """

    class _Wrapped:
        name = definition.name
        label = definition.label
        description = definition.description
        parameters = definition.parameters
        execution_mode = definition.execution_mode
        output_schema = definition.output_schema

        async def execute(
            self,
            tool_call_id: str,
            params: dict[str, Any],
            cancel_event: Any = None,
            on_update: Any = None,
        ) -> AgentToolResult:
            result: AgentToolResult = await definition.execute(
                tool_call_id, params, cancel_event, on_update, None
            )
            return result

    return _Wrapped()
