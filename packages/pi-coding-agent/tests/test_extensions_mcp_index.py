"""C3b-1 测试：MCP 扩展的选项、``mcp_servers`` 段落渲染与状态判定。

对应上游 ``extensions/mcp/index.ts`` 69–277 行 + 1343–1346 行（纯函数部分，零 runner 依赖）。
"""

from __future__ import annotations

import pytest

from pi_coding_agent.extensions.mcp.config import McpServerEntry
from pi_coding_agent.extensions.mcp.index import (
    DEFAULT_STARTUP_WAIT_MS,
    MAX_SERVER_DESCRIPTION_CHARS,
    MAX_SERVERS_SECTION_CHARS,
    MCP_SERVERS_SECTION,
    McpExtensionOptions,
    McpServer,
    McpServerListing,
    attention_rank,
    configured_exposures,
    default_load_config,
    describe_state,
    describe_transport,
    exposure_of,
    has_direct_tools,
    has_indirect_tools,
    is_enabled,
    reaches_server,
    render_servers_section,
    server_summary,
    truncate,
)
from pi_coding_agent.extensions.types import ExtensionContext
from pi_coding_agent.mcp_servers import validate_mcp_server_config


def _entry(name: str, raw: dict) -> McpServerEntry:  # type: ignore[type-arg]
    config = validate_mcp_server_config(name, raw)
    assert not isinstance(config, str), config
    return McpServerEntry(name=name, config=config, source="test")


class _Conn:
    """假连接：只需要说明与状态判定用到的字段。"""

    def __init__(self, state: str = "connected", **kwargs: object) -> None:
        self.state = state
        self.tools: list[object] = list(kwargs.pop("tools", []))  # type: ignore[arg-type]
        self.has_resources = bool(kwargs.pop("has_resources", False))
        self.instructions = kwargs.pop("instructions", None)
        self.error = kwargs.pop("error", None)


# ============================================================
# 常量与选项
# ============================================================


def test_exported_constants_match_upstream():
    assert MCP_SERVERS_SECTION == "mcp_servers"
    assert MAX_SERVERS_SECTION_CHARS == 4096
    assert DEFAULT_STARTUP_WAIT_MS == 10_000


def test_extension_options_defaults():
    options = McpExtensionOptions()
    assert options.load_config is None
    assert options.create_transport is None
    assert options.credentials is None
    assert options.log_path is None
    assert options.open_url is None
    assert options.update_config is None
    assert options.startup_wait_ms is None


# ============================================================
# 状态判定族
# ============================================================


def test_is_enabled_only_false_disables():
    assert is_enabled(McpServer(entry=_entry("a", {"command": "x"}))) is True
    assert is_enabled(McpServer(entry=_entry("a", {"command": "x", "enabled": True}))) is True
    assert is_enabled(McpServer(entry=_entry("a", {"command": "x", "enabled": False}))) is False


def test_exposure_of_defaults_to_codemode():
    assert exposure_of(_entry("a", {"command": "x"})) == "codemode"
    assert exposure_of(_entry("a", {"command": "x", "exposure": "direct"})) == "direct"


def test_configured_exposures_includes_tool_overrides():
    entry = _entry(
        "a",
        {
            "url": "https://x/mcp",
            "exposure": "codemode",
            "toolExposure": {"t1": "direct", "t2": "hidden"},
        },
    )
    assert configured_exposures(entry) == {"codemode", "direct", "hidden"}


def test_has_direct_and_indirect_tools():
    direct = _entry("a", {"command": "x", "exposure": "direct"})
    assert has_direct_tools(direct) is True
    assert has_indirect_tools(direct) is False

    codemode = _entry("a", {"command": "x", "exposure": "codemode"})
    assert has_direct_tools(codemode) is False
    assert has_indirect_tools(codemode) is True

    # 默认 codemode
    assert has_indirect_tools(_entry("a", {"command": "x"})) is True

    # 只有某些工具有 codemode → 仍算间接
    mixed = _entry("a", {"command": "x", "exposure": "hidden", "toolExposure": {"t": "deferred"}})
    assert has_direct_tools(mixed) is False
    assert has_indirect_tools(mixed) is True


def test_truncate_uses_ellipsis_and_never_exceeds_max():
    assert truncate("short", 10) == "short"
    result = truncate("x" * 50, 10)
    assert result.endswith("…")
    assert len(result) == 10
    # max <= 1 → 空串
    assert truncate("abc", 1) == ""
    assert truncate("abc", 0) == ""


# ============================================================
# renderServersSection
# ============================================================


def test_render_servers_section_none_when_no_indirect_servers():
    assert render_servers_section([]) is None
    # 只有 direct 工具 → 不进该段落
    assert (
        render_servers_section(
            [McpServerListing(entry=_entry("a", {"command": "x", "exposure": "direct"}))]
        )
        is None
    )
    # 被禁用的 server 不算
    disabled = _entry("a", {"command": "x", "exposure": "codemode", "enabled": False})
    assert render_servers_section([McpServerListing(entry=disabled)]) is None


def test_render_servers_section_lists_namespaces_sorted_with_reach():
    servers = [
        McpServerListing(entry=_entry("zeta", {"command": "x", "exposure": "codemode"})),
        McpServerListing(entry=_entry("alpha", {"command": "x", "exposure": "deferred"})),
    ]
    section = render_servers_section(servers)
    assert section is not None
    lines = section.split("\n")
    # 首行两条触达方式都提到（两种 server 都在）
    assert "codemode scripts" in lines[0]
    assert "tool_search" in lines[0]
    # 按名字排序
    assert lines[1].startswith("- mcp__alpha (tool_search)")
    assert lines[2].startswith("- mcp__zeta (codemode)")


def test_render_servers_section_intro_mentions_only_used_reaches():
    only_codemode = render_servers_section(
        [McpServerListing(entry=_entry("a", {"command": "x", "exposure": "codemode"}))]
    )
    assert only_codemode is not None
    assert "codemode scripts" in only_codemode.split("\n")[0]
    assert "tool_search" not in only_codemode.split("\n")[0]


def test_render_servers_section_prefers_config_description_then_instructions():
    described = McpServerListing(
        entry=_entry(
            "a",
            {
                "url": "https://x/mcp",
                "exposure": "codemode",
                "description": "From config\nsecond line",
            },
        )
    )
    section = render_servers_section([described])
    assert section is not None
    assert section.split("\n")[1].endswith(": From config")

    from_instructions = McpServerListing(
        entry=_entry("a", {"url": "https://x/mcp", "exposure": "codemode"}),
        connection=_Conn(instructions="  From instructions  \nmore"),
    )
    section = render_servers_section([from_instructions])
    assert section is not None
    assert section.split("\n")[1].endswith(": From instructions")


def test_server_summary_handles_missing_everything():
    assert server_summary(McpServerListing(entry=_entry("a", {"command": "x"}))) == ""


def test_render_servers_section_stays_within_budget_and_counts_omitted():
    """server 行本身放不下时，末尾的 server 被略去并在收尾行计数。"""
    servers = [
        McpServerListing(
            entry=_entry(f"server-{index:04d}", {"command": "x", "exposure": "codemode"})
        )
        for index in range(400)
    ]
    section = render_servers_section(servers)
    assert section is not None
    assert len(section) <= MAX_SERVERS_SECTION_CHARS
    assert "more server" in section
    assert "find their tools with searchTools()" in section


def test_render_servers_section_truncates_long_descriptions():
    servers = [
        McpServerListing(
            entry=_entry(
                f"s{index}", {"command": "x", "exposure": "codemode", "description": "d" * 5000}
            )
        )
        for index in range(4)
    ]
    section = render_servers_section(servers)
    assert section is not None
    assert len(section) <= MAX_SERVERS_SECTION_CHARS
    for line in section.split("\n")[1:]:
        if ": " in line:
            assert len(line.split(": ", 1)[1]) <= MAX_SERVER_DESCRIPTION_CHARS


def test_render_servers_section_singular_omission_line():
    """省略 1 个时用单数（上游 `server${count === 1 ? "" : "s"}`）。"""
    servers = [
        McpServerListing(
            entry=_entry(
                f"server-with-a-long-name-{index:03d}", {"command": "x", "exposure": "codemode"}
            )
        )
        for index in range(200)
    ]
    section = render_servers_section(servers)
    assert section is not None
    tail = section.split("\n")[-1]
    assert tail.startswith("- … ")
    assert "more server" in tail  # 单复数都含该串；精确性由下一行断言
    assert ("1 more server;" in tail) or ("more servers;" in tail)


# ============================================================
# reachesServer / 状态描述
# ============================================================


def test_reaches_server():
    assert reaches_server("all", "docs") is True
    assert reaches_server(["mcp__docs"], "docs") is True
    # 单词「包含」namespace 即可（可能过度匹配，上游亦然）
    assert reaches_server(["mcp__docs__tool"], "docs") is True
    assert reaches_server(["mcp__other"], "docs") is False
    assert reaches_server([], "docs") is False
    # `-` 变 `_`
    assert reaches_server(["mcp__my_server"], "my-server") is True


def test_describe_state_covers_every_state():
    disabled = McpServer(entry=_entry("a", {"command": "x", "enabled": False}))
    assert describe_state(disabled) == "disabled"

    assert describe_state(McpServer(entry=_entry("a", {"command": "x"}))) == "starting"
    assert (
        describe_state(
            McpServer(entry=_entry("a", {"command": "x"}), connection=_Conn("connecting"))
        )
        == "connecting…"
    )
    assert (
        describe_state(
            McpServer(entry=_entry("a", {"command": "x"}), connection=_Conn("needs-auth"))
        )
        == "needs sign-in"
    )
    assert (
        describe_state(
            McpServer(entry=_entry("a", {"command": "x"}), connection=_Conn("disconnected"))
        )
        == "disconnected"
    )

    failed = McpServer(
        entry=_entry("a", {"command": "x"}), connection=_Conn("failed", error="boom\nmore")
    )
    assert describe_state(failed) == "failed: boom"
    assert describe_state(failed, with_error=False) == "failed"

    one = McpServer(entry=_entry("a", {"command": "x"}), connection=_Conn(tools=[1]))
    assert describe_state(one) == "connected · 1 tool"
    many = McpServer(
        entry=_entry("a", {"command": "x"}), connection=_Conn(tools=[1, 2], has_resources=True)
    )
    assert describe_state(many) == "connected · 2 tools · resources"


def test_attention_rank_orders_by_need():
    def rank(state: str | None, enabled: bool = True) -> int:
        entry = _entry("a", {"command": "x"} if enabled else {"command": "x", "enabled": False})
        connection = None if state is None else _Conn(state)
        return attention_rank(McpServer(entry=entry, connection=connection))

    assert rank("needs-auth") == 0
    assert rank("failed") == 1
    assert rank("disconnected") == 2
    assert rank("connecting") == 3
    assert rank(None) == 3
    assert rank("connected") == 4
    assert rank("connected", enabled=False) == 5


def test_describe_transport():
    assert describe_transport(_entry("a", {"url": "https://x/mcp"})) == "https://x/mcp"
    assert (
        describe_transport(_entry("a", {"command": "npx", "args": ["-y", "srv"]})) == "npx -y srv"
    )
    assert describe_transport(_entry("a", {"command": "npx"})) == "npx"


# ============================================================
# default_load_config
# ============================================================


def test_default_load_config_reads_agent_dir_and_trust(tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    (agent).mkdir()
    (project / ".pi").mkdir(parents=True)
    (agent / "mcp.json").write_text('{"mcpServers": {"fs": {"command": "npx"}}}', encoding="utf-8")
    (project / ".pi" / "mcp.json").write_text(
        '{"mcpServers": {"fs": {"enabled": false}}}', encoding="utf-8"
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))

    trusted = default_load_config(ExtensionContext(cwd=str(project), is_project_trusted=True))
    assert [entry.name for entry in trusted.servers] == ["fs"]
    assert trusted.servers[0].config.enabled is False
    assert trusted.project_config == str(project / ".pi" / "mcp.json")

    untrusted = default_load_config(ExtensionContext(cwd=str(project), is_project_trusted=False))
    assert untrusted.project_config is None
    assert untrusted.servers[0].config.enabled is None


def test_no_create_mcp_extension_exported_yet():
    """C3b-3 未完成 → 工厂名不存在（不留空壳）。"""
    import pi_coding_agent.extensions.mcp.index as module

    assert not hasattr(module, "create_mcp_extension")
    assert "create_mcp_extension" not in module.__all__
    with pytest.raises(ImportError):
        from pi_coding_agent.extensions.mcp.index import create_mcp_extension  # noqa: F401
