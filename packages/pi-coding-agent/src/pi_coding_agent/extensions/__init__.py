"""编码 agent 的内置扩展（SDK 面）。

对应上游 ``src/extensions/``。上游该目录含四个内置扩展：

| 上游 | 本包状态 |
|---|---|
| ``tool-search/``（262 行） | ✅ 已移植（``create_tool_search_extension``） |
| ``codemode/``（1,396 行） | ❌ 未移植 —— 见 ``PORTING.md`` |
| ``mcp/``（4,257 行） | 🟡 SDK 面已移植（``create_mcp_extension``）—— `/mcp` Manager 与 CLI 命令不做，见 ``PORTING.md`` |
| ``llama/``（1,577 行） | ❌ 未移植（不在本批次范围） |

**只移植 SDK 面**：被上游包入口 ``src/index.ts`` re-export 的 ``create*Extension``
工厂及其依赖。CLI 装载逻辑（上游 ``src/cli/`` 与各扩展的 ``cli.ts``）不做。

本模块同时提供扩展的**声明面**（:mod:`.types`）与一个参考宿主实现
（:class:`~.registry.ToolRegistry`），后者是扩展在测试/嵌入场景下可直接使用的
``ExtensionAPI`` 实现：除工具装载语义外，还提供设置读取、会话条目追加、事件订阅/派发、
命令注册与 MCP server 注册。

**未移植**：上游 ``registerToolRenderer()`` 是 TUI 渲染钩子（``ToolRendererResolver`` /
``ToolRenderers`` 都依赖 ``pi-tui``），本包无渲染宿主，因此**不声明该方法**（见 ``PORTING.md``）。
"""

from __future__ import annotations

from .codemode.index import create_codemode_extension
from .mcp.factory import create_mcp_extension
from .registry import ToolRegistry
from .tool_search import create_tool_search_extension
from .types import (
    CommandOptions,
    ExtensionAPI,
    ExtensionContext,
    ExtensionFactory,
    ExtensionMode,
    ToolAnnotations,
    ToolDefinition,
    ToolExposure,
    ToolInfo,
    ToolLoadout,
    ToolLoadoutChanges,
    ToolNamespace,
    to_agent_tool,
)

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
    "ToolRegistry",
    "create_codemode_extension",
    "create_mcp_extension",
    "create_tool_search_extension",
    "to_agent_tool",
]
