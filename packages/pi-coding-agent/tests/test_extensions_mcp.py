"""MCP 扩展测试：配置读写（``extensions/mcp/config.ts``）与日志（``log.ts``）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pi_coding_agent import validate_mcp_server_config
from pi_coding_agent.extensions.mcp.config import (
    CONFIG_DIR_NAME,
    McpServerConfigPatch,
    add_mcp_server_config,
    load_mcp_config,
    remove_mcp_server_config,
    update_mcp_server_config,
)
from pi_coding_agent.extensions.mcp.log import (
    McpServerLog,
    format_mcp_log_message,
)


def _write(path, payload, indent: int = 2):  # type: ignore[no-untyped-def]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=indent), encoding="utf-8")


def _dirs(tmp_path):  # type: ignore[no-untyped-def]
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    agent.mkdir()
    (project / CONFIG_DIR_NAME).mkdir(parents=True)
    return agent, project


# ============================================================
# config：加载
# ============================================================


def test_load_global_only(tmp_path):
    agent, project = _dirs(tmp_path)
    _write(
        agent / "mcp.json",
        {"mcpServers": {"fs": {"command": "npx"}, "docs": {"url": "https://x/mcp"}}},
    )
    loaded = load_mcp_config(agent_dir=str(agent), cwd=str(project), project_trusted=False)
    assert sorted(entry.name for entry in loaded.servers) == ["docs", "fs"]
    assert all(entry.scope == "global" for entry in loaded.servers)
    assert loaded.errors == []
    assert loaded.project_config is None


def test_load_includes_project_config_only_when_trusted(tmp_path):
    agent, project = _dirs(tmp_path)
    _write(agent / "mcp.json", {"mcpServers": {"fs": {"command": "npx"}}})
    _write(project / CONFIG_DIR_NAME / "mcp.json", {"mcpServers": {"other": {"command": "y"}}})

    trusted = load_mcp_config(agent_dir=str(agent), cwd=str(project), project_trusted=True)
    assert sorted(entry.name for entry in trusted.servers) == ["fs", "other"]
    assert trusted.project_config == str(project / CONFIG_DIR_NAME / "mcp.json")

    untrusted = load_mcp_config(agent_dir=str(agent), cwd=str(project), project_trusted=False)
    assert [entry.name for entry in untrusted.servers] == ["fs"]


def test_project_override_keeps_global_rest(tmp_path):
    agent, project = _dirs(tmp_path)
    _write(agent / "mcp.json", {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "srv"]}}})
    _write(project / CONFIG_DIR_NAME / "mcp.json", {"mcpServers": {"fs": {"enabled": False}}})

    loaded = load_mcp_config(agent_dir=str(agent), cwd=str(project), project_trusted=True)
    assert loaded.errors == []
    entry = loaded.servers[0]
    assert entry.config.enabled is False
    # 全局条目的其余部分（含凭据）保留
    assert entry.config.command == "npx"
    assert entry.config.args == ["-y", "srv"]
    assert entry.override == str(project / CONFIG_DIR_NAME / "mcp.json")


def test_project_override_with_extra_keys_is_an_error(tmp_path):
    agent, project = _dirs(tmp_path)
    _write(agent / "mcp.json", {"mcpServers": {"fs": {"command": "npx"}}})
    _write(
        project / CONFIG_DIR_NAME / "mcp.json",
        {"mcpServers": {"fs": {"enabled": False, "timeout": 5}}},
    )

    loaded = load_mcp_config(agent_dir=str(agent), cwd=str(project), project_trusted=True)
    assert len(loaded.errors) == 1
    assert "an override can only set enabled, exposure, toolExposure" in loaded.errors[0]


def test_project_override_without_global_base_is_an_error(tmp_path):
    agent, project = _dirs(tmp_path)
    _write(agent / "mcp.json", {"mcpServers": {}})
    _write(project / CONFIG_DIR_NAME / "mcp.json", {"mcpServers": {"ghost": {"enabled": False}}})

    loaded = load_mcp_config(agent_dir=str(agent), cwd=str(project), project_trusted=True)
    assert len(loaded.errors) == 1
    assert (
        'server "ghost" needs "command" or "url", or a global server to override'
        in loaded.errors[0]
    )


def test_project_entry_with_command_is_a_definition_not_an_override(tmp_path):
    """带 command/url/type 的条目是定义，会替换同名全局条目。"""
    agent, project = _dirs(tmp_path)
    _write(agent / "mcp.json", {"mcpServers": {"fs": {"command": "npx"}}})
    _write(project / CONFIG_DIR_NAME / "mcp.json", {"mcpServers": {"fs": {"command": "other"}}})

    loaded = load_mcp_config(agent_dir=str(agent), cwd=str(project), project_trusted=True)
    assert loaded.errors == []
    assert loaded.servers[0].config.command == "other"
    assert loaded.servers[0].override is None
    assert loaded.servers[0].scope == "project"


def test_project_config_cannot_use_provider_auth(tmp_path):
    """项目文件不能决定凭据去哪，否则仓库可以劫持凭据。"""
    agent, project = _dirs(tmp_path)
    _write(agent / "mcp.json", {"mcpServers": {}})
    _write(
        project / CONFIG_DIR_NAME / "mcp.json",
        {"mcpServers": {"h": {"url": "https://x/mcp", "auth": {"provider": "p"}}}},
    )

    loaded = load_mcp_config(agent_dir=str(agent), cwd=str(project), project_trusted=True)
    assert len(loaded.errors) == 1
    assert "auth is only allowed in the global mcp.json" in loaded.errors[0]


def test_namespace_clash_is_an_error(tmp_path):
    """只在 - 与 _ 上不同的名字会共用 namespace。"""
    agent, project = _dirs(tmp_path)
    _write(agent / "mcp.json", {"mcpServers": {"a-b": {"command": "x"}, "a_b": {"command": "y"}}})

    loaded = load_mcp_config(agent_dir=str(agent), cwd=str(project), project_trusted=False)
    assert len(loaded.errors) == 1
    assert 'server "a_b" conflicts with "a-b"' in loaded.errors[0]


def test_auto_enable_codemode_parsing(tmp_path):
    agent, project = _dirs(tmp_path)
    _write(agent / "mcp.json", {"mcpServers": {}, "autoEnableCodemode": False})
    assert (
        load_mcp_config(
            agent_dir=str(agent), cwd=str(project), project_trusted=False
        ).auto_enable_codemode
        is False
    )
    _write(agent / "mcp.json", {"mcpServers": {}, "autoEnableCodemode": "yes"})
    loaded = load_mcp_config(agent_dir=str(agent), cwd=str(project), project_trusted=False)
    assert loaded.auto_enable_codemode is None
    assert "autoEnableCodemode must be a boolean" in loaded.errors[0]


def test_malformed_config_reports_error(tmp_path):
    agent, project = _dirs(tmp_path)
    (agent / "mcp.json").write_text("{not json", encoding="utf-8")
    loaded = load_mcp_config(agent_dir=str(agent), cwd=str(project), project_trusted=False)
    assert len(loaded.errors) == 1
    assert loaded.servers == []

    _write(agent / "mcp.json", {"mcpServers": 5})
    loaded = load_mcp_config(agent_dir=str(agent), cwd=str(project), project_trusted=False)
    assert 'expected an object with an "mcpServers" object' in loaded.errors[0]


# ============================================================
# config：写回
# ============================================================


def test_add_update_remove_preserve_other_content_and_indent(tmp_path):
    agent, project = _dirs(tmp_path)
    path = agent / "mcp.json"
    _write(path, {"mcpServers": {}, "other": 1}, indent=4)

    config = validate_mcp_server_config("new", {"command": "x"})
    assert not isinstance(config, str)
    assert add_mcp_server_config(str(path), "new", config) is False
    text = path.read_text()
    assert '"other": 1' in text
    assert text.startswith('{\n    "mcpServers"')  # 保留了 4 空格缩进

    # 同名再加 → replaced=True
    assert add_mcp_server_config(str(path), "new", config) is True

    update_mcp_server_config(str(path), "new", McpServerConfigPatch(enabled=False))
    assert json.loads(path.read_text())["mcpServers"]["new"]["enabled"] is False
    # enabled=True 是默认值 → 删键
    update_mcp_server_config(str(path), "new", McpServerConfigPatch(enabled=True))
    assert "enabled" not in json.loads(path.read_text())["mcpServers"]["new"]

    assert remove_mcp_server_config(str(path), "new") is True
    assert remove_mcp_server_config(str(path), "new") is False
    assert json.loads(path.read_text())["mcpServers"] == {}


def test_update_creates_override_entry_and_keeps_defaults(tmp_path):
    agent, project = _dirs(tmp_path)
    path = agent / "mcp.json"
    _write(path, {"mcpServers": {"fs": {"command": "npx"}}})

    update_mcp_server_config(
        str(path), "fs", McpServerConfigPatch(enabled=False), {"override": True}
    )
    parsed = json.loads(path.read_text())
    assert parsed["mcpServers"]["fs"]["enabled"] is False
    assert parsed["mcpServers"]["fs"]["command"] == "npx"


def test_update_missing_server_without_override_raises(tmp_path):
    agent, project = _dirs(tmp_path)
    path = agent / "mcp.json"
    _write(path, {"mcpServers": {}})
    import pytest

    with pytest.raises(ValueError, match='does not define MCP server "nope"'):
        update_mcp_server_config(str(path), "nope", McpServerConfigPatch(enabled=False))


def test_remove_missing_file_returns_false(tmp_path):
    assert remove_mcp_server_config(str(tmp_path / "nope.json"), "x") is False


# ============================================================
# log
# ============================================================


def test_format_mcp_log_message_shape():
    line = format_mcp_log_message(
        "docs",
        {"level": "warning", "logger": "srv", "data": "hi"},
        datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert line == "2026-01-02T00:00:00Z [docs] warning srv: hi\n"


def test_format_mcp_log_message_defaults_and_indent():
    line = format_mcp_log_message("s", {"data": "a\nb"}, datetime(2026, 1, 2, tzinfo=UTC))
    assert line == "2026-01-02T00:00:00Z [s] info a\n    b\n"
    # 非 record 的 params 当作 data
    assert "info x" in format_mcp_log_message("s", "x", datetime(2026, 1, 2, tzinfo=UTC))
    # 非字符串 data 走 JSON
    assert '{"a": 1}' in format_mcp_log_message(
        "s", {"data": {"a": 1}}, datetime(2026, 1, 2, tzinfo=UTC)
    )


def test_mcp_server_log_appends(tmp_path):
    log = McpServerLog(str(tmp_path / "nested" / "mcp.log"))
    log.write("a", {"data": "one"})
    log.write("b", {"data": "two"})
    lines = (tmp_path / "nested" / "mcp.log").read_text().splitlines()
    assert len(lines) == 2
    assert "[a] info one" in lines[0]
    assert "[b] info two" in lines[1]


def test_mcp_server_log_rotates_when_over_limit(tmp_path, monkeypatch):
    """对齐上游：用**缓存**的大小判定，因此必须经对象写才能触发轮转。"""
    import pi_coding_agent.extensions.mcp.log as log_module

    monkeypatch.setattr(log_module, "MAX_LOG_BYTES", 64)
    path = tmp_path / "mcp.log"
    log = McpServerLog(str(path))

    # 第一次写：_size 初始化为 0 → 不轮转；写完后 _size 超过 64
    log.write("a", {"data": "x" * 100})
    assert not (tmp_path / "mcp.log.1").exists()

    # 第二次写：_size > 64 且文件实际大小 > 64 → 先轮转再写
    log.write("b", {"data": "second"})
    assert (tmp_path / "mcp.log.1").exists()
    assert "[a] info" in (tmp_path / "mcp.log.1").read_text()
    assert "[b] info second" in path.read_text()
    assert "[a] info" not in path.read_text()


def test_mcp_server_log_ignores_write_errors(tmp_path):
    """日志是尽力而为：写不了也不能抛。"""
    log = McpServerLog(str(tmp_path / "a-directory"))
    (tmp_path / "a-directory").mkdir()
    log.write("s", {"data": "x"})  # 目标是目录 → OSError，被吞掉
