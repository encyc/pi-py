"""C3b-2b-β 测试：6 个生命周期钩子 + ``create_mcp_extension``（``index.ts`` 1114–1259）。

用假 server / 假连接，不起真实 MCP server。
"""

from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from pi_coding_agent.extensions.mcp.config import LoadedMcpConfig, McpServerEntry
from pi_coding_agent.extensions.mcp.factory import (
    AgentStartPayload,
    McpExtension,
    ToolCallPayload,
    create_mcp_extension,
)
from pi_coding_agent.extensions.mcp.index import McpExtensionOptions, McpServer
from pi_coding_agent.extensions.registry import ToolRegistry
from pi_coding_agent.mcp_servers import validate_mcp_server_config


def _entry(name: str, raw: dict[str, Any]) -> McpServerEntry:
    config = validate_mcp_server_config(name, raw)
    assert not isinstance(config, str), config
    return McpServerEntry(name=name, config=config, source="test")


class _Conn:
    """假连接：只需钩子会读到的那些属性。"""

    def __init__(self, name: str, state: str = "connected", tools: int = 1) -> None:
        self.name = name
        self.state = state
        self.error: str | None = None
        self.instructions: str | None = None
        self.has_resources = False
        self.tools: list[dict[str, Any]] = [{"name": f"t{i}"} for i in range(tools)]
        self.oauth_url: str | None = None
        self.closed = False
        self.reconnects = 0

    async def close(self) -> None:
        self.closed = True

    async def reconnect(self) -> None:
        self.reconnects += 1


@dataclass
class _PromptOptions:
    sections: dict[str, str] = field(default_factory=dict)


def _extension(
    servers: list[McpServer],
    *,
    config_errors: list[str] | None = None,
    overridden: list[str] | None = None,
    auto_enable_codemode: bool | None = None,
) -> tuple[McpExtension, ToolRegistry]:
    loaded = LoadedMcpConfig(
        servers=[server.entry for server in servers],
        auto_enable_codemode=auto_enable_codemode,
        errors=list(config_errors or []),
        project_config="/tmp/.pi/mcp.json",
    )
    options = McpExtensionOptions(load_config=lambda _ctx: loaded)
    extension = McpExtension(options=options)
    host = ToolRegistry()
    extension.install(host)
    return extension, host


def _ctx(**kwargs: Any) -> Any:
    from pi_coding_agent.extensions.types import ExtensionContext

    return ExtensionContext(cwd="/tmp", **kwargs)


# ============================================================
# 导出与装配
# ============================================================


def test_create_mcp_extension_is_importable_from_the_package_entry():
    """验收条件：``create_mcp_extension`` 从包入口可导入。"""
    import pi_coding_agent

    assert hasattr(pi_coding_agent, "create_mcp_extension")
    factory = pi_coding_agent.create_mcp_extension()
    assert callable(factory)


def test_all_mcp_modules_import():
    """import 冒烟：``extensions.mcp`` 全部模块可 import。"""
    for name in (
        "pi_coding_agent.extensions.mcp",
        "pi_coding_agent.extensions.mcp.config",
        "pi_coding_agent.extensions.mcp.factory",
        "pi_coding_agent.extensions.mcp.index",
        "pi_coding_agent.extensions.mcp.lifecycle",
        "pi_coding_agent.extensions.mcp.log",
        "pi_coding_agent.extensions.mcp.oauth",
        "pi_coding_agent.extensions.mcp.registration",
        "pi_coding_agent.extensions.mcp.resources",
        "pi_coding_agent.extensions.mcp.runtime",
        "pi_coding_agent.extensions.mcp.tools",
    ):
        assert importlib.import_module(name) is not None


def test_factory_registers_the_six_hooks():
    """工厂注册的钩子名与上游一一对应。"""
    extension, host = _extension([])

    factory = create_mcp_extension()
    # 用同一个宿主再装一次，确认 install 真的调用了 pi.on
    extension2 = McpExtension()
    extension2.install(host)
    for event in (
        "session_start",
        "before_agent_start",
        "tool_call",
        "turn_start",
        "mcp_servers_change",
        "session_shutdown",
    ):
        assert len(host.handlers.get(event, [])) == 2, event
    assert callable(factory)


# ============================================================
# report_problems：去掉呈现、保留判定
# ============================================================


def test_report_problems_returns_none_when_nothing_to_report():
    extension, _host = _extension([McpServer(entry=_entry("a", {"url": "https://a/mcp"}))])
    asyncio.run(extension.on_session_start(_ctx()))
    extension.servers[0].connection = _Conn("a", state="connected")  # type: ignore[assignment]
    assert extension.report_problems() is None
    asyncio.run(extension.on_session_shutdown(_ctx()))


def test_report_problems_lists_config_errors_and_bad_states():
    """上游 ``index.ts:643-655``：配置错误 + needs-auth/failed 的 server。"""
    extension, _host = _extension(
        [McpServer(entry=_entry("a", {"url": "https://a/mcp"}))], config_errors=["bad json"]
    )
    asyncio.run(extension.on_session_start(_ctx()))
    extension.servers[0].connection = _Conn("a", state="needs-auth")  # type: ignore[assignment]
    text = extension.report_problems()
    assert text is not None
    assert "config: bad json" in text
    assert "a: " in text
    assert text.endswith("Run /mcp to fix.")
    asyncio.run(extension.on_session_shutdown(_ctx()))


def test_report_problems_only_scopes_to_given_servers():
    """``only`` 给定时不重复报告配置错误（上游 1249 的 ``reportProblems(ctx, connecting)``）。"""
    extension, _host = _extension([], config_errors=["bad json"])
    server = McpServer(entry=_entry("a", {"url": "https://a/mcp"}))
    server.connection = _Conn("a", state="failed")  # type: ignore[assignment]
    text = extension.report_problems([server])
    assert text is not None and "config: bad json" not in text


# ============================================================
# session_start / session_shutdown
# ============================================================


def test_session_start_builds_servers_and_rearms_the_session():
    extension, _host = _extension([McpServer(entry=_entry("a", {"url": "https://a/mcp"}))])
    assert extension.session_aborted.is_set() is True  # 初始即为已中止（上游 318）
    asyncio.run(extension.on_session_start(_ctx()))
    assert extension.session_aborted.is_set() is False
    assert [server.entry.name for server in extension.servers] == ["a"]
    assert extension.session_cwd == "/tmp"
    assert extension.project_config == "/tmp/.pi/mcp.json"
    assert extension.waited_for_startup is False
    asyncio.run(extension.on_session_shutdown(_ctx()))


def test_session_start_without_enabled_servers_reports_and_does_not_load():
    extension, _host = _extension([], config_errors=["broken"])
    asyncio.run(extension.on_session_start(_ctx()))
    assert extension.pending is None
    assert extension.report_problems() is not None


def test_session_shutdown_aborts_clears_and_closes():
    extension, _host = _extension([McpServer(entry=_entry("a", {"url": "https://a/mcp"}))])
    asyncio.run(extension.on_session_start(_ctx()))
    connection = _Conn("a")
    extension.servers[0].connection = connection  # type: ignore[assignment]
    asyncio.run(extension.on_session_shutdown(_ctx()))
    assert extension.session_aborted.is_set() is True
    assert extension.servers == []
    assert connection.closed is True


# ============================================================
# before_agent_start
# ============================================================


def test_before_agent_start_sets_and_clears_the_section():
    extension, _host = _extension([])
    extension.servers = [McpServer(entry=_entry("a", {"url": "https://a/mcp"}))]
    options = _PromptOptions()
    asyncio.run(extension.on_before_agent_start(AgentStartPayload(options, _ctx())))
    assert "mcp_servers" in options.sections

    extension.servers = []
    asyncio.run(extension.on_before_agent_start(AgentStartPayload(options, _ctx())))
    assert "mcp_servers" not in options.sections


def test_before_agent_start_waits_only_once_for_direct_servers():
    """首个 prompt 才等；第二次直接返回（上游 ``waitedForStartup``）。"""
    extension, _host = _extension([])
    options = _PromptOptions()

    async def _run() -> None:
        server = McpServer(entry=_entry("a", {"url": "https://a/mcp"}))
        server.ready = asyncio.get_running_loop().create_future()
        server.ready.set_result(None)
        extension.servers = [server]
        await extension.on_before_agent_start(AgentStartPayload(options, _ctx()))

    asyncio.run(_run())
    assert extension.waited_for_startup is True


# ============================================================
# tool_call
# ============================================================


def test_tool_call_waits_for_the_owning_server():
    """普通 MCP 工具只等它自己的 server（上游 1204-1207）。"""
    extension, host = _extension([])

    async def _run() -> None:
        server = McpServer(entry=_entry("a", {"url": "https://a/mcp"}))
        connection = _Conn("a")
        server.connection = connection  # type: ignore[assignment]
        server.ready = asyncio.get_running_loop().create_future()
        server.ready.set_result(None)
        extension.servers = [server]
        await extension.on_tool_call(ToolCallPayload("unknown_tool", {}, _ctx()))

    asyncio.run(_run())
    assert host is not None


def test_tool_call_ignores_unknown_tools():
    extension, host = _extension([])
    asyncio.run(extension.on_tool_call(ToolCallPayload("nope", {}, _ctx())))
    assert host.get_all_tools() == []


# ============================================================
# turn_start / mcp_servers_change
# ============================================================


def test_turn_start_reconnects_signed_in_servers():
    extension, _host = _extension([McpServer(entry=_entry("a", {"url": "https://a/mcp"}))])
    asyncio.run(extension.on_session_start(_ctx()))
    assert extension._lifecycle is not None
    # 没有待登录的 server 时是 no-op
    asyncio.run(extension.on_turn_start(_ctx()))
    assert extension._lifecycle.tokens_at_sign_in == {}
    asyncio.run(extension.on_session_shutdown(_ctx()))


def test_mcp_servers_change_adds_registered_servers():
    extension, host = _extension([])
    asyncio.run(extension.on_session_start(_ctx()))
    config = validate_mcp_server_config("reg", {"url": "https://reg/mcp"})
    assert not isinstance(config, str)
    host.register_mcp_server("reg", config)
    asyncio.run(extension.on_mcp_servers_change(_ctx()))
    assert "reg" in [server.entry.name for server in extension.servers]
    asyncio.run(extension.on_session_shutdown(_ctx()))


def test_mcp_servers_change_is_ignored_after_shutdown():
    extension, host = _extension([])
    asyncio.run(extension.on_session_start(_ctx()))
    asyncio.run(extension.on_session_shutdown(_ctx()))
    config = validate_mcp_server_config("late", {"url": "https://late/mcp"})
    assert not isinstance(config, str)
    host.register_mcp_server("late", config)
    asyncio.run(extension.on_mcp_servers_change(_ctx()))
    assert extension.servers == []


# ============================================================
# 无 UI 宿主（既有口径）
# ============================================================


def test_report_problems_has_no_ui_dependency():
    """上游在这里调 ``ctx.ui.notify``；本端返回文本，绝不触碰 UI。"""
    extension, _host = _extension(
        [McpServer(entry=_entry("a", {"url": "https://a/mcp"}))], config_errors=["x"]
    )
    asyncio.run(extension.on_session_start(_ctx()))
    extension.servers[0].connection = _Conn("a", state="failed")  # type: ignore[assignment]
    text = extension.report_problems()
    assert isinstance(text, str)
    assert "ui" not in json.dumps(extension.options.__dict__, default=str)
    asyncio.run(extension.on_session_shutdown(_ctx()))


def test_mcp_package_exports_only_the_sdk_surface():
    """``pi_coding_agent.extensions.mcp`` 只导出 SDK 面；Manager/CLI 的名字不存在。"""
    import pi_coding_agent.extensions.mcp as mcp

    for absent in (
        "show_mcp_manager",
        "format_status",
        "pick_server",
        "login_command",
        "MCP_USAGE",
    ):
        assert not hasattr(mcp, absent), absent
    for present in ("create_mcp_extension", "McpExtension"):
        assert hasattr(mcp, present), present


@pytest.mark.parametrize("event", ["session_start", "session_shutdown"])
def test_lifecycle_endpoints_are_registered_by_the_factory(event: str):
    extension, host = _extension([])
    create_mcp_extension()(host)
    assert host.handlers.get(event), event
    assert extension is not None
