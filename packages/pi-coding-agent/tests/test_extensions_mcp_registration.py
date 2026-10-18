"""C3b-2a 测试：MCP 工具注册、loadout 与资源工具同步（``index.ts`` 279–536）。

用假宿主（实现 ``ExtensionAPI`` 的最小面）+ 假连接，不起真实 server。
"""

from __future__ import annotations

from typing import Any

import pytest

from pi_coding_agent.extensions import ExtensionAPI, ToolDefinition, ToolNamespace
from pi_coding_agent.extensions.mcp.config import McpServerEntry
from pi_coding_agent.extensions.mcp.index import McpServer
from pi_coding_agent.extensions.mcp.registration import (
    CODEMODE_TOOL_NAME,
    McpToolRegistration,
)
from pi_coding_agent.extensions.registry import ToolRegistry
from pi_coding_agent.mcp_servers import validate_mcp_server_config


class _Host(ToolRegistry):
    """参考宿主 + 记录 register_tool 的调用次数（供断言重复注册）。"""

    def __init__(self) -> None:
        super().__init__()
        self.registrations: list[str] = []

    def register_tool(
        self, definition: ToolDefinition, *, default_active: bool | None = None
    ) -> None:
        self.registrations.append(f"{definition.name}:{definition.exposure}")
        super().register_tool(definition, default_active=default_active)


class _Conn:
    """假连接：注册路径只需要 tools / instructions / timeout_ms / has_resources / state。"""

    def __init__(
        self,
        name: str,
        tools: list[dict[str, Any]] | None = None,
        *,
        instructions: str | None = None,
        has_resources: bool = False,
        state: str = "connected",
    ) -> None:
        self.name = name
        self.tools = tools or []
        self.instructions = instructions
        self.has_resources = has_resources
        self.state = state
        self.timeout_ms = 1000

    @property
    def entry(self) -> Any:
        return _EntryRef(self.name)


class _EntryRef:
    def __init__(self, name: str) -> None:
        self.name = name


def _entry(name: str, raw: dict) -> McpServerEntry:  # type: ignore[type-arg]
    config = validate_mcp_server_config(name, raw)
    assert not isinstance(config, str), config
    return McpServerEntry(name=name, config=config, source="test")


def _setup(
    servers: list[McpServer],
    *,
    auto_enable_codemode: bool = True,
    extra_tools: list[ToolDefinition] | None = None,
) -> tuple[_Host, McpToolRegistration]:
    host = _Host()
    for definition in extra_tools or []:
        host.register_tool(definition)
    by_name = {server.entry.name: server for server in servers}
    registration = McpToolRegistration(
        pi=host,
        find_server=lambda name: by_name.get(name),
        servers=lambda: servers,
        resource_servers=lambda: [
            server.connection for server in servers if server.connection is not None
        ],  # type: ignore[misc]
        auto_enable_codemode=lambda: auto_enable_codemode,
    )
    return host, registration


def _tool(name: str) -> dict[str, Any]:
    return {"name": name, "inputSchema": {"type": "object"}}


# ============================================================
# register_tools
# ============================================================


def test_register_tools_registers_with_configured_exposure():
    entry = _entry("docs", {"url": "https://x/mcp", "exposure": "direct"})
    server = McpServer(entry=entry, connection=_Conn("docs", [_tool("search")]))
    host, registration = _setup([server])
    assert isinstance(host, ExtensionAPI)

    registration.register_tools(server.connection)  # type: ignore[arg-type]
    assert host.registrations == ["mcp__docs__search:direct"]
    assert registration.server_tools["docs"] == {"mcp__docs__search"}
    assert registration.tool_owners["mcp__docs__search"] == "docs\0search"


def test_register_tools_uses_config_description_and_instructions_for_namespace():
    entry = _entry("docs", {"url": "https://x/mcp", "description": "Docs server"})
    server = McpServer(
        entry=entry, connection=_Conn("docs", [_tool("a")], instructions="Use carefully")
    )
    _, registration = _setup([server])
    registration.register_tools(server.connection)  # type: ignore[arg-type]
    definition = registration.definitions["mcp__docs__a"]
    assert definition.namespace is not None
    assert definition.namespace.name == "mcp__docs"
    assert definition.namespace.aliases == ["docs"]
    assert definition.namespace.description == "Docs server"
    assert definition.namespace.instructions == "Use carefully"


def test_register_tools_hides_withdrawn_tools():
    """server 撤下的工具被重新注册为 hidden（工具无法注销）。"""
    entry = _entry("docs", {"url": "https://x/mcp", "exposure": "direct"})
    connection = _Conn("docs", [_tool("a"), _tool("b")])
    server = McpServer(entry=entry, connection=connection)
    host, registration = _setup([server])

    registration.register_tools(connection)  # type: ignore[arg-type]
    assert "mcp__docs__a:direct" in host.registrations

    connection.tools = [_tool("a")]
    registration.register_tools(connection)  # type: ignore[arg-type]
    assert "mcp__docs__b:hidden" in host.registrations
    assert registration.server_tools["docs"] == {"mcp__docs__a"}


def test_sanitized_name_collisions_all_get_hash_suffix():
    """净化后同名的工具**都**加哈希后缀，因此谁保留朴素名不取决于列表顺序。"""
    entry = _entry("srv", {"url": "https://x/mcp", "exposure": "direct"})
    connection = _Conn("srv", [_tool("a-b"), _tool("a_b")])
    server = McpServer(entry=entry, connection=connection)
    _, registration = _setup([server])
    registration.register_tools(connection)  # type: ignore[arg-type]

    names = registration.server_tools["srv"]
    assert len(names) == 2
    # 两个都不是朴素名 mcp__srv__a_b
    assert "mcp__srv__a_b" not in names
    assert all(name.startswith("mcp__srv__a_b_") for name in names)


def test_same_tool_name_on_two_servers_stays_unique():
    a = McpServer(entry=_entry("a", {"url": "https://a/mcp"}), connection=_Conn("a", [_tool("t")]))
    b = McpServer(entry=_entry("b", {"url": "https://b/mcp"}), connection=_Conn("b", [_tool("t")]))
    _, registration = _setup([a, b])
    registration.register_tools(a.connection)  # type: ignore[arg-type]
    registration.register_tools(b.connection)  # type: ignore[arg-type]
    assert registration.server_tools["a"] == {"mcp__a__t"}
    assert registration.server_tools["b"] == {"mcp__b__t"}


def test_register_tools_does_not_duplicate_names_for_repeat_registration():
    entry = _entry("docs", {"url": "https://x/mcp", "exposure": "direct"})
    connection = _Conn("docs", [_tool("a")])
    server = McpServer(entry=entry, connection=connection)
    _, registration = _setup([server])
    registration.register_tools(connection)  # type: ignore[arg-type]
    registration.register_tools(connection)  # type: ignore[arg-type]
    assert registration.server_tools["docs"] == {"mcp__docs__a"}


# ============================================================
# hide_tools
# ============================================================


def test_hide_tools_makes_disabled_server_tools_unreachable():
    entry = _entry("docs", {"url": "https://x/mcp", "exposure": "direct"})
    connection = _Conn("docs", [_tool("a")])
    server = McpServer(entry=entry, connection=connection)
    host, registration = _setup([server])
    registration.register_tools(connection)  # type: ignore[arg-type]

    registration.hide_tools("docs")
    assert "mcp__docs__a:hidden" in host.registrations
    assert registration.server_tools["docs"] == set()


def test_hide_tools_is_safe_for_unknown_server():
    _, registration = _setup([])
    registration.hide_tools("nope")  # 不抛错
    assert registration.server_tools["nope"] == set()


# ============================================================
# 资源工具同步
# ============================================================


def test_sync_resource_tools_skips_until_a_server_has_resources():
    entry = _entry("docs", {"url": "https://x/mcp", "exposure": "direct"})
    server = McpServer(entry=entry, connection=_Conn("docs", [_tool("a")], has_resources=False))
    host, registration = _setup([server])
    registration.sync_resource_tools()
    assert host.registrations == []
    assert registration.resource_tools_exposure is None


def test_sync_resource_tools_uses_widest_exposure():
    direct = McpServer(
        entry=_entry("d", {"url": "https://d/mcp", "exposure": "direct"}),
        connection=_Conn("d", [], has_resources=True),
    )
    codemode = McpServer(
        entry=_entry("c", {"url": "https://c/mcp", "exposure": "codemode"}),
        connection=_Conn("c", [], has_resources=True),
    )
    host, registration = _setup([codemode, direct])
    registration.sync_resource_tools()
    assert registration.resource_tools_exposure == "direct"
    for name in ("list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"):
        assert f"{name}:direct" in host.registrations


def test_sync_resource_tools_ignores_hidden_and_disabled_servers():
    hidden = McpServer(
        entry=_entry("h", {"url": "https://h/mcp", "exposure": "hidden"}),
        connection=_Conn("h", [], has_resources=True),
    )
    disabled = McpServer(
        entry=_entry("x", {"url": "https://x/mcp", "exposure": "direct", "enabled": False}),
        connection=_Conn("x", [], has_resources=True),
    )
    _, registration = _setup([hidden, disabled])
    registration.sync_resource_tools()
    assert registration.resource_tools_exposure is None


def test_sync_resource_tools_deactivates_when_no_longer_direct():
    entry = _entry("d", {"url": "https://d/mcp", "exposure": "direct"})
    connection = _Conn("d", [], has_resources=True)
    server = McpServer(entry=entry, connection=connection)
    host, registration = _setup([server])

    registration.sync_resource_tools()
    assert registration.resource_tools_exposure == "direct"

    connection.has_resources = False
    host.set_active_tools([*host.get_active_tools(), "list_mcp_resources", "read_mcp_resource"])
    registration.sync_resource_tools()
    assert registration.resource_tools_exposure == "hidden"
    # 由 direct 变为非 direct 时，它们从活跃集合里去掉
    assert "list_mcp_resources" not in host.get_active_tools()


# ============================================================
# ensure_discovery_active
# ============================================================


def _reaching_tool(name: str, *, default_active: bool | None = None) -> ToolDefinition:
    class _Reaching(ToolDefinition):
        async def execute(self, tool_call_id, params, cancel_event=None, on_update=None, ctx=None):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    return _Reaching(
        name=name,
        description="d",
        parameters={},
        exposure="model-only",
        reaches_tools=lambda _input: "all",
        default_active=default_active,
    )


def test_ensure_discovery_active_returns_none_without_indirect_servers():
    server = McpServer(entry=_entry("d", {"url": "https://x/mcp", "exposure": "direct"}))
    _, registration = _setup([server], extra_tools=[_reaching_tool(CODEMODE_TOOL_NAME)])
    assert registration.ensure_discovery_active() is None


def test_ensure_discovery_active_activates_codemode():
    server = McpServer(entry=_entry("c", {"url": "https://x/mcp", "exposure": "codemode"}))
    host, registration = _setup([server], extra_tools=[_reaching_tool(CODEMODE_TOOL_NAME)])
    assert registration.ensure_discovery_active() is None
    assert CODEMODE_TOOL_NAME in host.get_active_tools()


def test_ensure_discovery_active_respects_auto_enable_flag():
    server = McpServer(entry=_entry("c", {"url": "https://x/mcp", "exposure": "codemode"}))
    # codemode 工具**未被激活**（default_active=False），才走得到告警分支：
    # 上游的判定是 `reachable.includes(name)`，若它本来就在活跃集合里则不告警。
    host, registration = _setup(
        [server],
        auto_enable_codemode=False,
        extra_tools=[_reaching_tool(CODEMODE_TOOL_NAME, default_active=False)],
    )
    warning = registration.ensure_discovery_active()
    assert warning is not None
    assert "autoEnableCodemode is false" in warning
    assert CODEMODE_TOOL_NAME not in host.get_active_tools()


def test_ensure_discovery_no_warning_when_already_active():
    """已经可达（例如用户用 defaultTools 启用了 codemode）→ 不告警。"""
    server = McpServer(entry=_entry("c", {"url": "https://x/mcp", "exposure": "codemode"}))
    host, registration = _setup([server], extra_tools=[_reaching_tool(CODEMODE_TOOL_NAME)])
    assert CODEMODE_TOOL_NAME in host.get_active_tools()  # 注册即激活（model-only）
    assert registration.ensure_discovery_active() is None
    assert registration.warned_unreachable is False


def test_ensure_discovery_warns_once_when_nothing_can_reach():
    server = McpServer(entry=_entry("c", {"url": "https://x/mcp", "exposure": "codemode"}))
    _, registration = _setup([server])  # 宿主里没有 codemode / tool_search
    first = registration.ensure_discovery_active()
    assert first is not None and "cannot be called" in first
    # 一次性
    assert registration.ensure_discovery_active() is None


def test_ensure_discovery_ignores_same_named_tool_without_reaches_tools():
    """只有声明了 reachesTools 的同名工具才算能触达 MCP 工具。"""
    server = McpServer(entry=_entry("c", {"url": "https://x/mcp", "exposure": "codemode"}))

    class _Plain(ToolDefinition):
        async def execute(self, tool_call_id, params, cancel_event=None, on_update=None, ctx=None):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    plain = _Plain(name=CODEMODE_TOOL_NAME, description="d", parameters={}, exposure="model-only")
    _, registration = _setup([server], extra_tools=[plain])
    assert registration.ensure_discovery_active() is not None


def test_ensure_discovery_activates_tool_search_for_deferred():
    from pi_coding_agent.extensions.tool_search import TOOL_SEARCH_TOOL_NAME

    server = McpServer(entry=_entry("d", {"url": "https://x/mcp", "exposure": "deferred"}))
    host, registration = _setup([server], extra_tools=[_reaching_tool(TOOL_SEARCH_TOOL_NAME)])
    assert registration.ensure_discovery_active() is None
    assert TOOL_SEARCH_TOOL_NAME in host.get_active_tools()


# ============================================================
# 未移植部分的防线
# ============================================================


def test_create_mcp_extension_still_not_exported():
    """C3b-2b 未完成 → 工厂名不存在（不留空壳）。"""
    import pi_coding_agent.extensions.mcp.registration as module

    assert not hasattr(module, "create_mcp_extension")
    with pytest.raises(ImportError):
        from pi_coding_agent.extensions.mcp.registration import create_mcp_extension  # noqa: F401


def test_tool_renderer_is_not_called_anywhere():
    """index.ts:382 的 registerToolRenderer 调用按决定删除：宿主不该有该方法，代码也不该提它。"""
    import inspect

    import pi_coding_agent.extensions.mcp.registration as module

    source = inspect.getsource(module)
    assert "register_tool_renderer" not in source
    assert not hasattr(ToolRegistry(), "register_tool_renderer")
    assert ToolNamespace  # 供类型面引用
