"""扩展基建测试：ExtensionAPI/Context、MCP server 配置、配置值解析、auth 加锁后端、嵌套调用。"""

from __future__ import annotations

import json
import os
import time

import pytest

from pi_coding_agent import (
    AuthStorageBackend,
    FileAuthStorageBackend,
    LockResult,
    McpServerRegistry,
    ModelRegistry,
    RegisteredMcpServer,
    create_tool_name_matcher,
    get_mcp_tool_exposure,
    is_command_config_value,
    is_mcp_tool_name,
    mcp_namespace,
    resolve_config_value,
    resolve_config_value_or_throw,
    resolve_headers,
    resolve_headers_or_throw,
    validate_mcp_server_config,
)
from pi_coding_agent.auth_storage import STALE_LOCK_SECONDS
from pi_coding_agent.extensions import (
    CommandOptions,
    ExtensionAPI,
    ExtensionContext,
    ToolRegistry,
)
from pi_coding_agent.mcp_servers import (
    LIST_MCP_RESOURCE_TEMPLATES_TOOL,
    LIST_MCP_RESOURCES_TOOL,
    READ_MCP_RESOURCE_TOOL,
)
from pi_coding_agent.nested_tool_calls import (
    NESTED_CALL_LIMITS,
    NestedCallRecorder,
    NestedToolCallRunner,
)
from pi_coding_agent.resolve_config_value import clear_config_value_cache

# ============================================================
# 1. ExtensionAPI 成员（参考宿主 ToolRegistry 实现）
# ============================================================


def test_reference_host_satisfies_extension_api():
    assert isinstance(ToolRegistry(), ExtensionAPI)


def test_reference_host_is_not_a_stub_it_stores_state():
    host = ToolRegistry()
    host.settings["codemode"] = {"mode": "only", "inlineBudget": 1234}
    assert host.get_settings() == {"codemode": {"mode": "only", "inlineBudget": 1234}}
    # get_settings 返回副本，改它不影响宿主
    host.get_settings()["x"] = 1
    assert "x" not in host.settings

    host.append_entry("codemode-store", {"set": {"a": 1}})
    assert host.entries == [("codemode-store", {"set": {"a": 1}})]

    host.register_command(
        "mcp", CommandOptions(handler=lambda _ctx: None, description="Manage MCP")
    )
    assert list(host.commands) == ["mcp"]
    with pytest.raises(ValueError, match="already registered"):
        host.register_command("mcp", CommandOptions(handler=lambda _ctx: None))


async def test_events_subscribe_and_unsubscribe():
    host = ToolRegistry()
    seen: list[object] = []

    async def handler(payload: object) -> None:
        seen.append(payload)

    unsubscribe = host.on("session_start", handler)
    await host.emit("session_start", {"n": 1})
    assert seen == [{"n": 1}]

    unsubscribe()
    await host.emit("session_start", {"n": 2})
    assert seen == [{"n": 1}]


def test_register_tool_renderer_is_not_declared():
    """TUI 渲染钩子未移植：名字必须不存在，而不是留个空实现。"""
    host = ToolRegistry()
    assert not hasattr(host, "register_tool_renderer")
    assert not hasattr(ExtensionContext, "register_tool_renderer")


def test_mcp_server_registration_on_host():
    host = ToolRegistry()
    config = validate_mcp_server_config("docs", {"url": "https://x.example/mcp"})
    assert not isinstance(config, str)
    host.register_mcp_server("docs", config)
    servers = host.get_mcp_servers()
    assert [s.name for s in servers] == ["docs"]
    host.unregister_mcp_server("docs")
    assert host.get_mcp_servers() == []


def test_mcp_server_registry_ownership():
    registry = McpServerRegistry()
    config = validate_mcp_server_config("a", {"command": "x"})
    assert not isinstance(config, str)
    registry.register(RegisteredMcpServer(name="a", config=config, extension_path="/one"))
    # 别的扩展不能注销
    registry.unregister("a", "/other")
    assert registry.get("a") is not None
    registry.unregister("a", "/one")
    assert registry.get("a") is None


def test_mcp_server_registry_list_deep_copies_and_notifies():
    registry = McpServerRegistry()
    config = validate_mcp_server_config("a", {"url": "https://x.example/mcp"})
    assert not isinstance(config, str)
    changes: list[int] = []
    registry.set_change_listener(lambda: changes.append(1))
    registry.register(RegisteredMcpServer(name="a", config=config, extension_path="/one"))
    assert changes == [1]
    snapshot = registry.list()
    assert snapshot[0].config is not config


# ============================================================
# 2. ExtensionContext
# ============================================================


def test_extension_context_defaults_reflect_no_ui_host():
    ctx = ExtensionContext(cwd="/tmp")
    assert ctx.has_ui is False
    assert ctx.ui is None
    assert ctx.mode == "print"
    assert ctx.is_project_trusted is False
    assert ctx.execute_tool is None


# ============================================================
# 5①. mcp-servers：命名 / 曝光 / 校验
# ============================================================


def test_mcp_namespace_replaces_dashes():
    assert mcp_namespace("my-server") == "mcp__my_server"
    assert mcp_namespace("plain") == "mcp__plain"


def test_resource_tool_constants_and_is_mcp_tool_name():
    assert (LIST_MCP_RESOURCES_TOOL, LIST_MCP_RESOURCE_TEMPLATES_TOOL, READ_MCP_RESOURCE_TOOL) == (
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
    )
    assert is_mcp_tool_name("mcp__x__y") is True
    assert is_mcp_tool_name(READ_MCP_RESOURCE_TOOL) is True
    assert is_mcp_tool_name("read") is False


def test_create_tool_name_matcher_exact_and_pattern():
    matcher = create_tool_name_matcher(["read", "mcp__radius__*"])
    assert matcher("read") is True
    assert matcher("mcp__radius__list") is True
    assert matcher("write") is False
    assert matcher("mcp__other__list") is False


def test_validate_mcp_server_config_accepts_both_transports():
    http = validate_mcp_server_config("docs", {"url": "https://mcp.example.com/mcp"})
    assert not isinstance(http, str)
    assert http.url == "https://mcp.example.com/mcp"
    stdio = validate_mcp_server_config("local", {"command": "npx", "args": ["-y", "srv"]})
    assert not isinstance(stdio, str)
    assert stdio.command == "npx"


def test_validate_mcp_server_config_error_messages_match_upstream():
    assert validate_mcp_server_config("bad name", {}) == (
        'invalid server name "bad name" (use letters, digits, "_" and "-")'
    )
    assert validate_mcp_server_config("a", {}) == (
        'server "a" needs either "command" (stdio) or "url" (streamable HTTP)'
    )
    assert validate_mcp_server_config("a", {"url": "notaurl"}) == (
        'server "a": url must be an http or https URL'
    )
    assert validate_mcp_server_config("a", {"type": "sse", "url": "https://x/y"}) == (
        'server "a": legacy SSE transport is not supported; use the streamable HTTP URL'
    )
    assert validate_mcp_server_config("a", {"url": "https://x/y", "exposure": "nope"}) == (
        'server "a": exposure must be one of "codemode", "deferred", "direct", "hidden"'
    )


def test_validate_mcp_server_config_resolves_exposure_alias():
    config = validate_mcp_server_config(
        "a", {"url": "https://x/y", "exposure": "codemode-deferred"}
    )
    assert not isinstance(config, str)
    assert config.exposure == "codemode"


def test_validate_mcp_server_config_preserves_unknown_keys():
    """mcp/config.ts 要回写 mcp.json，未知键不能丢。"""
    config = validate_mcp_server_config("a", {"url": "https://x/y", "futureKey": {"nested": True}})
    assert not isinstance(config, str)
    assert config.model_dump(by_alias=True, exclude_none=True)["futureKey"] == {"nested": True}


def test_get_mcp_tool_exposure_precedence():
    config = validate_mcp_server_config(
        "a",
        {
            "url": "https://x/y",
            "exposure": "codemode",
            "toolExposure": {"exact": "direct", "sec*": "hidden"},
        },
    )
    assert not isinstance(config, str)
    assert get_mcp_tool_exposure(config, "exact") == "direct"
    assert get_mcp_tool_exposure(config, "secretX") == "hidden"
    assert get_mcp_tool_exposure(config, "other") == "codemode"
    # 无 exposure 时默认 codemode
    plain = validate_mcp_server_config("b", {"url": "https://x/y"})
    assert not isinstance(plain, str)
    assert get_mcp_tool_exposure(plain, "anything") == "codemode"


# ============================================================
# 5②. resolve-config-value（含 !cmd 行为面）
# ============================================================


def test_resolve_config_value_literal_and_env(monkeypatch):
    assert resolve_config_value("sk-literal", None) == "sk-literal"
    assert resolve_config_value("${PI_TEST_VAR}", {"PI_TEST_VAR": "v"}) == "v"
    assert resolve_config_value("$PI_TEST_VAR", {"PI_TEST_VAR": "v"}) == "v"
    assert resolve_config_value("pre-${PI_TEST_VAR}-post", {"PI_TEST_VAR": "v"}) == "pre-v-post"


def test_resolve_config_value_escapes():
    assert resolve_config_value("a$$b") == "a$b"
    assert resolve_config_value("a$!b") == "a!b"


def test_resolve_config_value_keeps_malformed_references_literal():
    assert resolve_config_value("${not a name}") == "${not a name}"
    assert resolve_config_value("${ABC") == "${ABC"
    assert resolve_config_value("abc$") == "abc$"


def test_resolve_config_value_empty_env_falls_through_to_process_env(monkeypatch):
    monkeypatch.setenv("PI_TEST_FALLBACK", "from-process")
    assert resolve_config_value("${PI_TEST_FALLBACK}", {"PI_TEST_FALLBACK": ""}) == "from-process"


def test_resolve_config_value_missing_env_is_none():
    assert resolve_config_value("${PI_DEFINITELY_MISSING_XYZ}") is None


def test_command_values_execute_and_cache():
    clear_config_value_cache()
    assert is_command_config_value("!echo hi") is True
    assert is_command_config_value("echo hi") is False
    assert resolve_config_value("!echo  from-command ") == "from-command"
    # 失败也进缓存（对齐上游），因此重复调用仍返回 None
    assert resolve_config_value("!exit 7") is None
    assert resolve_config_value("!exit 7") is None
    clear_config_value_cache()


def test_resolve_config_value_or_throw_messages():
    with pytest.raises(RuntimeError, match="from environment variable: PI_MISSING_XYZ"):
        resolve_config_value_or_throw("${PI_MISSING_XYZ}", "API key")
    with pytest.raises(RuntimeError, match="from shell command: exit 1"):
        resolve_config_value_or_throw("!exit 1", "API key")
    # 字面量总能解析，不抛错（上游末尾那条 "Failed to resolve {description}" 兜底
    # 只在「模板解析为 None 且无缺失环境变量」时可达，而这两者互斥 → 实际不可达）
    assert resolve_config_value_or_throw("plain", "API key") == "plain"


def test_resolve_headers_drops_unresolved():
    assert resolve_headers({"A": "${PI_TEST_V}", "B": "${PI_MISSING_XYZ}"}, {"PI_TEST_V": "v"}) == {
        "A": "v"
    }
    assert resolve_headers(None) is None
    with pytest.raises(RuntimeError, match='header "B"'):
        resolve_headers_or_throw({"B": "${PI_MISSING_XYZ}"}, "docs")


# ============================================================
# 5③. auth-storage 窄接口 + 真实文件锁
# ============================================================


def test_file_auth_storage_backend_is_real(tmp_path):
    path = str(tmp_path / "nested" / "auth.json")
    backend = FileAuthStorageBackend(path)
    assert isinstance(backend, AuthStorageBackend)

    out = backend.with_lock(lambda current: LockResult(result="first", next='{"a":1}'))
    assert out == "first"
    assert (tmp_path / "nested" / "auth.json").read_text() == '{"a":1}'
    # 目录 0o700、文件 0o600（对齐上游）
    assert (tmp_path / "nested").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "nested" / "auth.json").stat().st_mode & 0o777 == 0o600

    # 读回上次内容；next=None 不写回
    assert backend.with_lock(lambda current: LockResult(result=json.loads(current or "{}"))) == {
        "a": 1
    }
    backend.with_lock(lambda current: LockResult(result=None, next=None))
    assert (tmp_path / "nested" / "auth.json").read_text() == '{"a":1}'
    # 锁目录已释放
    assert not os.path.exists(path + ".lock")


def test_file_auth_storage_backend_strips_bom(tmp_path):
    path = tmp_path / "auth.json"
    path.write_bytes(b'\xef\xbb\xbf{"c":3}')
    backend = FileAuthStorageBackend(str(path))
    assert backend.with_lock(lambda current: LockResult(result=current)) == '{"c":3}'


async def test_file_auth_storage_backend_with_lock_async(tmp_path):
    backend = FileAuthStorageBackend(str(tmp_path / "auth.json"))

    async def fn(current: str | None) -> LockResult[str]:
        return LockResult(result="async", next='{"b":2}')

    assert await backend.with_lock_async(fn) == "async"
    assert (tmp_path / "auth.json").read_text() == '{"b":2}'


def test_stale_lock_with_dead_pid_is_reclaimed(tmp_path):
    """崩溃残留（进程已不存在）必须能自动回收，否则需人工清理。"""
    path = tmp_path / "auth.json"
    backend = FileAuthStorageBackend(str(path))
    backend.with_lock(lambda current: LockResult(result="init"))
    lock_dir = tmp_path / "auth.json.lock"
    lock_dir.mkdir()
    (lock_dir / "owner.json").write_text(
        json.dumps({"pid": 999_999, "hostname": __import__("socket").gethostname()})
    )
    assert backend.with_lock(lambda current: LockResult(result="reclaimed")) == "reclaimed"
    assert not lock_dir.exists()


def test_stale_lock_by_mtime_is_reclaimed(tmp_path):
    path = tmp_path / "auth.json"
    backend = FileAuthStorageBackend(str(path))
    backend.with_lock(lambda current: LockResult(result="init"))
    lock_dir = tmp_path / "auth.json.lock"
    lock_dir.mkdir()
    old = time.time() - STALE_LOCK_SECONDS - 5
    os.utime(lock_dir, (old, old))
    assert backend.with_lock(lambda current: LockResult(result="reclaimed")) == "reclaimed"


def test_live_lock_is_not_stolen(tmp_path, monkeypatch):
    """本进程持有的锁不能被误判为过期 —— 否则会破坏排他性。"""
    path = tmp_path / "auth.json"
    backend = FileAuthStorageBackend(str(path))
    backend.with_lock(lambda current: LockResult(result="init"))
    lock_dir = tmp_path / "auth.json.lock"
    lock_dir.mkdir()
    (lock_dir / "owner.json").write_text(
        json.dumps({"pid": os.getpid(), "hostname": __import__("socket").gethostname()})
    )
    monkeypatch.setattr("pi_coding_agent.auth_storage._MAX_LOCK_ATTEMPTS", 2)
    with pytest.raises(TimeoutError, match="Could not acquire auth storage lock"):
        backend.with_lock(lambda current: LockResult(result="x"))
    assert lock_dir.exists()


def test_auth_storage_narrow_interface_omits_unported_names():
    import pi_coding_agent.auth_storage as module

    for name in (
        "AuthStorage",
        "ReadOnlyAuthStorage",
        "readStoredCredential",
        "InMemoryAuthStorageBackend",
    ):
        assert not hasattr(module, name), name


# ============================================================
# 4. 嵌套工具调用
# ============================================================


def test_nested_call_recorder_limits():
    recorder = NestedCallRecorder()
    assert recorder.snapshot() is None

    record = recorder.start("c1", "read", {"path": "a"})
    assert record is not None
    assert record.status == "unfinished"
    # 未完成 → complete=False
    snapshot = recorder.snapshot()
    assert snapshot is not None and snapshot.complete is False

    recorder.finish(record, False, "")
    snapshot = recorder.snapshot()
    assert snapshot is not None
    assert snapshot.complete is True
    assert snapshot.calls[0].status == "ok"
    assert snapshot.calls[0].duration_ms is not None


def test_nested_call_recorder_omits_oversized_arguments():
    recorder = NestedCallRecorder()
    big = {"x": "y" * (NESTED_CALL_LIMITS["max_argument_bytes_per_call"] + 10)}
    record = recorder.start("c1", "bash", big)
    assert record is not None
    assert record.arguments is None
    assert record.arguments_bytes is not None
    recorder.finish(record, False, "")
    snapshot = recorder.snapshot()
    assert snapshot is not None and snapshot.complete is False


def test_nested_call_recorder_truncates_error_text():
    recorder = NestedCallRecorder()
    record = recorder.start("c1", "bash", {})
    recorder.finish(record, True, "e" * (NESTED_CALL_LIMITS["max_error_chars"] + 50))
    snapshot = recorder.snapshot()
    assert snapshot is not None
    error = snapshot.calls[0].error
    assert error is not None
    assert len(error) == NESTED_CALL_LIMITS["max_error_chars"]


def test_nested_call_recorder_drops_calls_beyond_max():
    recorder = NestedCallRecorder()
    for index in range(NESTED_CALL_LIMITS["max_calls"]):
        assert recorder.start(f"c{index}", "t", {}) is not None
    assert recorder.start("overflow", "t", {}) is None
    snapshot = recorder.snapshot()
    assert snapshot is not None and snapshot.complete is False


async def test_nested_tool_call_runner_executes_and_records():
    from pi_agent_core.types import AgentToolResult
    from pi_ai import TextContent

    emitted: list[dict[str, object]] = []

    class _Outcome:
        def __init__(self) -> None:
            self.result = AgentToolResult(content=[TextContent(text="nested-ok")])
            self.is_error = False

    class _Tool:
        name = "read"
        execution_mode = None

    class _Host:
        def get_tools(self):  # type: ignore[no-untyped-def]
            return [_Tool()]

        def is_sequential(self) -> bool:
            return False

        async def run_tool_call(self, tool_call, parent_tool_call_id, signal, on_update):  # type: ignore[no-untyped-def]
            return _Outcome()

        async def emit(self, event):  # type: ignore[no-untyped-def]
            emitted.append(event)

    runner = NestedToolCallRunner(_Host())
    outcome = await runner.execute("call-1", "read", {"path": "a"})
    assert outcome.is_error is False
    assert [event["type"] for event in emitted] == ["tool_execution_start", "tool_execution_end"]
    assert emitted[0]["toolCallId"] == "call-1/1"
    assert emitted[0]["parentToolCallId"] == "call-1"

    summary = runner.take_record("call-1")
    assert summary is not None
    assert summary.calls is not None
    assert summary.calls.calls[0].name == "read"
    # 取过即移除
    assert runner.take_record("call-1") is None


# ============================================================
# 3. ModelRegistry：跨包阻塞时的"仅形状"声明
# ============================================================


def test_model_registry_is_a_protocol_not_an_implementation():
    """pi-ai 尚未提供多模型类型面，因此这里只声明契约，不提供实现。"""
    import pi_coding_agent.model_registry as module

    assert getattr(ModelRegistry, "_is_protocol", False) is True
    with pytest.raises(TypeError):
        ModelRegistry()  # type: ignore[abstract]
    assert not hasattr(module, "create_model_registry")


# ============================================================
# 占位桩防线（延续 task-25 准则）
# ============================================================


def test_no_placeholder_stubs_in_core_surface():
    """内置扩展工厂必须**可导入且是工厂**（mcp 与 codemode 均已落地）。

    两者都曾是「必须不存在」的守卫对象；落地后按 MCP 那轮的做法**加严**为正向断言，
    而不是删除断言 —— 它现在守的是「别退回占位桩」。
    """
    import pi_coding_agent

    for name in ("create_mcp_extension", "create_codemode_extension"):
        assert hasattr(pi_coding_agent, name), name
        assert name in pi_coding_agent.__all__, name
        assert callable(getattr(pi_coding_agent, name)()), name
