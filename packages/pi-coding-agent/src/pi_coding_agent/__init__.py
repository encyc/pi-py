"""pi-coding-agent: Python port of @earendil-works/pi-coding-agent (core only).

编码 agent SDK：工具集（bash/read/edit/write/grep/find/ls）+ AgentSession 封装。

对应上游 ``packages/coding-agent/src/core/``。**不复刻 TUI/CLI**。

快速上手：
    from pi_coding_agent import CodingAgent
    agent = CodingAgent(model=my_model, api_key="...")
    await agent.prompt("列出当前目录的文件")
"""

from __future__ import annotations

__version__ = "0.85.1"
#: 声明基线仍是 v0.85.1（与根目录 ``UPSTREAM_VERSION`` 一致）。
#: 本包已**部分**对齐 v1.3.0（Tier-A：constrainedSampling 默认开启、bash 退出码语义、
#: structured_content/output_schema、truncate_middle、format_read_line_range）——
#: 详见 ``PORTING.md``。整体版本号与依赖上界待 Lead 统一决策后再动。
__upstream_ref__ = "earendil-works/pi@v0.85.1"

# ---- 工具 ----
# ---- 内置扩展（SDK 面）----
# ---- SDK 入口 ----
from .auth_storage import (
    AuthOperationOptions,
    AuthStorageBackend,
    FileAuthStorageBackend,
    LockResult,
)
from .extensions import (
    ExtensionContext,
    create_codemode_extension,
    create_mcp_extension,
    create_tool_search_extension,
)
from .mcp_servers import (
    LIST_MCP_RESOURCE_TEMPLATES_TOOL,
    LIST_MCP_RESOURCES_TOOL,
    READ_MCP_RESOURCE_TOOL,
    McpExposure,
    McpHttpServerConfig,
    McpOAuthConfig,
    McpServerConfig,
    McpServerRegistry,
    McpStdioServerConfig,
    RegisteredMcpServer,
    create_tool_name_matcher,
    get_mcp_tool_exposure,
    is_mcp_tool_name,
    mcp_namespace,
    validate_mcp_server_config,
)
from .model_registry import ModelRegistry
from .nested_tool_calls import (
    NESTED_CALL_LIMITS,
    NestedCallRecorder,
    NestedCallSummary,
    NestedToolCallOptions,
    NestedToolCallRunner,
)
from .render_utils import format_read_line_range
from .resolve_config_value import (
    clear_config_value_cache,
    is_command_config_value,
    resolve_config_value,
    resolve_config_value_or_throw,
    resolve_headers,
    resolve_headers_or_throw,
)
from .sdk import DEFAULT_SYSTEM_PROMPT, CodingAgent, create_agent_session
from .tools import (
    BASH_OUTPUT_SCHEMA,
    READ_OUTPUT_SCHEMA,
    STRUCTURED_OUTPUT_MAX_BYTES,
    BashTool,
    EditTool,
    FindTool,
    GrepTool,
    LsTool,
    ReadTool,
    WriteTool,
    create_all_tools,
    create_coding_tools,
    create_read_only_tools,
)

# ---- 截断工具 ----
from .truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    GREP_MAX_LINE_LENGTH,
    MiddleTruncationResult,
    TruncationResult,
    format_size,
    truncate_head,
    truncate_line,
    truncate_middle,
    truncate_tail,
)

__all__ = [
    "__version__",
    "__upstream_ref__",
    # 工具
    "BashTool",
    "EditTool",
    "FindTool",
    "GrepTool",
    "LsTool",
    "ReadTool",
    "WriteTool",
    "BASH_OUTPUT_SCHEMA",
    "READ_OUTPUT_SCHEMA",
    "STRUCTURED_OUTPUT_MAX_BYTES",
    "create_all_tools",
    "create_coding_tools",
    "create_read_only_tools",
    # 内置扩展（SDK 面）
    "ExtensionContext",
    "create_codemode_extension",
    "create_mcp_extension",
    "create_tool_search_extension",
    # MCP server 配置
    "LIST_MCP_RESOURCES_TOOL",
    "LIST_MCP_RESOURCE_TEMPLATES_TOOL",
    "READ_MCP_RESOURCE_TOOL",
    "McpExposure",
    "McpHttpServerConfig",
    "McpOAuthConfig",
    "McpServerConfig",
    "McpServerRegistry",
    "McpStdioServerConfig",
    "RegisteredMcpServer",
    "create_tool_name_matcher",
    "get_mcp_tool_exposure",
    "is_mcp_tool_name",
    "mcp_namespace",
    "validate_mcp_server_config",
    # 配置值解析（含 !cmd 行为面）
    "clear_config_value_cache",
    "is_command_config_value",
    "resolve_config_value",
    "resolve_config_value_or_throw",
    "resolve_headers",
    "resolve_headers_or_throw",
    # auth 文件加锁后端（窄接口）
    "AuthOperationOptions",
    "AuthStorageBackend",
    "FileAuthStorageBackend",
    "LockResult",
    # 模型目录句柄（跨包阻塞，仅形状）
    "ModelRegistry",
    # 嵌套工具调用
    "NESTED_CALL_LIMITS",
    "NestedCallRecorder",
    "NestedCallSummary",
    "NestedToolCallOptions",
    "NestedToolCallRunner",
    # SDK 入口
    "CodingAgent",
    "create_agent_session",
    "DEFAULT_SYSTEM_PROMPT",
    # 渲染辅助
    "format_read_line_range",
    # 截断
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "GREP_MAX_LINE_LENGTH",
    "MiddleTruncationResult",
    "TruncationResult",
    "format_size",
    "truncate_head",
    "truncate_line",
    "truncate_middle",
    "truncate_tail",
]
