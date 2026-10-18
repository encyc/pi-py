"""内置 MCP 集成。

对应上游 ``extensions/mcp/index.ts``（1,347 行）。**本文件目前只含 C3b-1 切片**
（上游 69–277 行 + 1343–1346 行）：扩展选项、``mcp_servers`` 系统提示段落渲染、以及
「连接前就能从配置判定」的状态族。

**尚未移植**（见 ``PORTING.md`` 的 Tier-B③）：
* **C3b-2**（上游 279–800）：连接管理器 —— 工具注册、资源工具 + loadout、连接生命周期、
  ``mcp_servers_change``
* **C3b-3**（上游 800–1259）：``create_mcp_extension`` 工厂本体
* **不做**：``/mcp`` 交互命令（上游 ~1260–1340，CLI/TUI）

因此**本模块暂不导出 ``create_mcp_extension``** —— 名字不存在优于留一个空壳工厂。

**有意偏离**
* 上游 ``/mcp`` 命令用的 ``MCP_USAGE`` 常量不移植（其唯一消费者是那条命令）。
* 排序用 ``sorted(key=name)``；上游用 ``localeCompare``（对 MCP server 名的实际取值等价）。
* ``truncate()`` 的长度按 Python 的**码点**计；上游按 JS 的 UTF-16 码元计。段落内容以 server 名与
  ASCII 摘要为主，两者一致；若将来出现大量非 BMP 字符，需按上游口径复核。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ...extensions.types import ExtensionContext
from ...mcp_servers import (
    LIST_MCP_RESOURCE_TEMPLATES_TOOL,
    LIST_MCP_RESOURCES_TOOL,
    READ_MCP_RESOURCE_TOOL,
    McpExposure,
    mcp_namespace,
)
from .config import (
    LoadedMcpConfig,
    McpServerConfigPatch,
    McpServerEntry,
    load_mcp_config,
)
from .oauth import McpOAuthCredentialStore, agent_dir
from .runtime import McpServerConnection, McpTransportFactory

__all__ = [
    "DEFAULT_STARTUP_WAIT_MS",
    "MAX_SERVERS_SECTION_CHARS",
    "MCP_SERVERS_SECTION",
    "McpExtensionOptions",
    "McpServer",
    "McpServerListing",
    "attention_rank",
    "configured_exposures",
    "default_load_config",
    "describe_state",
    "describe_transport",
    "exposure_of",
    "has_direct_tools",
    "has_indirect_tools",
    "is_enabled",
    "reaches_server",
    "render_servers_section",
    "server_summary",
]


@dataclass
class McpExtensionOptions:
    """``create_mcp_extension`` 的选项（E6：``@dataclass``，关键字 + snake_case）。"""

    #: 默认从 agent 目录与受信项目读 ``mcp.json``。
    load_config: Callable[[ExtensionContext], LoadedMcpConfig] | None = None
    #: 默认按 server 配置建 stdio / streamable HTTP 传输。
    create_transport: McpTransportFactory | None = None
    #: 默认 agent 目录下的 ``mcp-auth.json``。
    credentials: McpOAuthCredentialStore | None = None
    #: server 日志追加到的文件。默认 agent 目录下的 ``mcp.log``。
    log_path: str | None = None
    #: 打开 OAuth 授权 URL。默认用平台浏览器。
    open_url: Callable[[str], None] | None = None
    #: 把 ``/mcp`` 的改动存回该 server 的配置文件：有项目 ``override`` 就存它，否则存 ``source``。
    #: 默认编辑那个 ``mcp.json``。
    update_config: Callable[[McpServerEntry, McpServerConfigPatch], None] | None = None
    #: 首个 prompt 为「启动时仍在连接、且带 ``direct`` 工具」的 server 等多久（毫秒）。
    #: 它们的工具会在连上后可用；其它 server 由脚本或检索按需等待。默认 10000。
    startup_wait_ms: int | None = None


#: 首个 prompt 等待 direct 工具的默认毫秒数。
DEFAULT_STARTUP_WAIT_MS = 10_000

#: 三个资源工具的名字（其 loadout 由 C3b-2 统一处理）。
RESOURCE_TOOL_NAMES: frozenset[str] = frozenset(
    {LIST_MCP_RESOURCES_TOOL, LIST_MCP_RESOURCE_TEMPLATES_TOOL, READ_MCP_RESOURCE_TOOL}
)


@dataclass
class McpServer:
    """一个已配置的 server。被禁用的 server 没有连接。"""

    entry: McpServerEntry
    connection: McpServerConnection | None = None
    #: 对扩展注册的 server：注册时的配置，用来发现重复注册。
    registered_config: str | None = None
    #: 最后一次失败的 ``/mcp`` 动作的结果，在管理器里显示。
    message: str | None = None
    #: 标识当前连接尝试；禁用或替换它会作废更早的工作。
    attempt: object | None = None
    #: 在「为该 server 发起的连接」成功或失败时落定。
    ready: Any = None
    #: 已脱钩连接的清理；替换者会先等它，再打开传输。
    closing: Any = None


#: 各 exposure 的一句话说明（``hidden`` 除外）。
EXPOSURE_DESCRIPTIONS: dict[str, str] = {
    "codemode": "called from codemode scripts, which find them with searchTools()",
    "deferred": "not declared until tool_search loads them, then called directly; no codemode needed",
    "direct": "declared to the model like built-in tools",
}


def error_message(error: BaseException) -> str:
    return str(error) or error.__class__.__name__


def first_line(text: str) -> str:
    return text.split("\n", 1)[0]


def is_enabled(server: McpServer) -> bool:
    return getattr(server.entry.config, "enabled", None) is not False


def exposure_of(entry: McpServerEntry) -> McpExposure:
    return entry.config.exposure or "codemode"


def configured_exposures(entry: McpServerEntry) -> set[str]:
    """该 server 的工具可能有的 exposure，连接前就能从配置知道。"""
    return {exposure_of(entry), *(entry.config.tool_exposure or {}).values()}


def has_direct_tools(entry: McpServerEntry) -> bool:
    """该 server 是否有工具**声明给模型**（因此首个 prompt 要等它）。"""
    return "direct" in configured_exposures(entry)


def has_indirect_tools(entry: McpServerEntry) -> bool:
    """该 server 是否有工具经 codemode 或 tool_search 触达。"""
    exposures = configured_exposures(entry)
    return "codemode" in exposures or "deferred" in exposures


#: 列出「工具未声明给模型」的 server 的那个系统提示段落名。
MCP_SERVERS_SECTION = "mcp_servers"

#: 段落里单个 server 描述的字符上限（Codex 对 deferred namespace 的取值）。
MAX_SERVER_DESCRIPTION_CHARS = 250

#: 整个段落的字符上限。描述会压缩以放下；若仅 server 行就放不下，末尾的 server 被略去并在
#: 收尾行里计数。
MAX_SERVERS_SECTION_CHARS = 4096


def servers_section_intro(reaches: set[str]) -> str:
    """段落首行。只解释**所列 server 实际用到**的触达方式。"""
    intro = "MCP servers whose tools are not declared to you."
    if "codemode" in reaches:
        intro += " Call the tools of `codemode` servers from codemode scripts."
    if "tool_search" in reaches:
        intro += " Load the tools of `tool_search` servers with `tool_search`."
    return intro


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return "" if max_chars <= 1 else f"{text[: max_chars - 1].rstrip()}…"


@dataclass
class McpServerListing:
    """``mcp_servers`` 段落需要一个 server 提供的全部信息。"""

    entry: McpServerEntry
    connection: Any = None

    @property
    def instructions(self) -> str | None:
        return getattr(self.connection, "instructions", None)


def server_summary(server: McpServerListing) -> str:
    """配置里的描述首行，或连上之后的 server instructions 首行。"""
    configured = getattr(server.entry.config, "description", None)
    text = (configured or "").strip() or (server.instructions or "")
    return text.split("\n", 1)[0].strip()


def render_servers_section(servers: Sequence[McpServerListing]) -> str | None:
    """``mcp_servers`` 段落：每个启用且带 codemode/deferred 工具的 server，附触达方式与一行摘要。

    模型正是从这里得知这些 server 的，因为 codemode 与 tool_search 都不列它们。
    没有这样的 server 时返回 ``None``。
    """
    listed = sorted(
        (
            server
            for server in servers
            if is_enabled_server_listing(server) and has_indirect_tools(server.entry)
        ),
        key=lambda server: server.entry.name,
    )
    if not listed:
        return None
    reaches = [
        "codemode" if "codemode" in configured_exposures(server.entry) else "tool_search"
        for server in listed
    ]
    intro = servers_section_intro(set(reaches))
    heads = [
        f"- {mcp_namespace(server.entry.name)} ({reaches[index]})"
        for index, server in enumerate(listed)
    ]

    def omitted(count: int) -> list[str]:
        if count <= 0:
            return []
        plural = "" if count == 1 else "s"
        return [f"- … {count} more server{plural}; find their tools with searchTools()"]

    def size(kept: int) -> int:
        # 首行 + 前 kept 行 server（不含描述）+ 省略行的字符数
        return len("\n".join([intro, *heads[:kept], *omitted(len(listed) - kept)]))

    kept = len(listed)
    while kept > 0 and size(kept) > MAX_SERVERS_SECTION_CHARS:
        kept -= 1
    # 每条描述还要占一个 ": " 分隔符
    per_server = (
        0
        if kept == 0
        else min(
            MAX_SERVER_DESCRIPTION_CHARS,
            (MAX_SERVERS_SECTION_CHARS - size(kept)) // kept - 2,
        )
    )
    lines: list[str] = []
    for index, server in enumerate(listed[:kept]):
        summary = truncate(server_summary(server), per_server) if per_server > 0 else ""
        lines.append(f"{heads[index]}: {summary}" if summary else heads[index])
    return "\n".join([intro, *lines, *omitted(len(listed) - kept)])


def is_enabled_server_listing(server: McpServerListing) -> bool:
    return getattr(server.entry.config, "enabled", None) is not False


def reaches_server(reach: Any, server: str) -> bool:
    """一次触达 ``reach`` 的调用是否需要该 server：触达全部工具，或某个词包含它的 namespace。"""
    if reach == "all":
        return True
    namespace = mcp_namespace(server)
    return any(namespace in word for word in reach)


def describe_state(server: McpServer, with_error: bool = True) -> str:
    """列表与启动报告用的短状态。``with_error`` 会附上失败的首行。"""
    if not is_enabled(server):
        return "disabled"
    connection = server.connection
    if connection is None:
        return "starting"
    state = connection.state
    if state == "needs-auth":
        return "needs sign-in"
    if state == "failed":
        return (
            f"failed: {first_line(connection.error or 'unknown error')}" if with_error else "failed"
        )
    if state == "connected":
        count = len(connection.tools)
        plural = "" if count == 1 else "s"
        suffix = " · resources" if connection.has_resources else ""
        return f"connected · {count} tool{plural}{suffix}"
    if state == "connecting":
        return "connecting…"
    return state


def attention_rank(server: McpServer) -> int:
    """先需要用户的 server 排在前面。"""
    if not is_enabled(server):
        return 5
    state = server.connection.state if server.connection is not None else None
    if state == "needs-auth":
        return 0
    if state == "failed":
        return 1
    if state == "disconnected":
        return 2
    if state == "connected":
        return 4
    return 3


def describe_transport(entry: McpServerEntry) -> str:
    url = getattr(entry.config, "url", None)
    if url is not None:
        return str(url)
    args = getattr(entry.config, "args", None) or []
    return " ".join([str(getattr(entry.config, "command", "")), *(str(arg) for arg in args)])


def default_load_config(ctx: ExtensionContext) -> LoadedMcpConfig:
    """默认配置加载：agent 目录 + （受信时）项目 ``mcp.json``。"""
    return load_mcp_config(
        agent_dir=agent_dir(),
        cwd=ctx.cwd,
        project_trusted=ctx.is_project_trusted,
    )
