"""MCP server 配置的读写。

对应上游 ``extensions/mcp/config.ts``（242 行，**完整移植**）。

server 从 agent 目录的 ``mcp.json`` 读取；项目受信时还从 ``<project>/.pi/mcp.json`` 读取。
两者都用 ``mcpServers`` 形状（与其它 MCP 客户端共用），因此已有配置可直接拷过来。
**同名时项目条目替换全局条目。**

项目条目若没有 ``command`` / ``url`` / ``type``，就只覆盖全局同名 server 的
``enabled`` / ``exposure`` / ``toolExposure``，例如在某个项目里关掉它::

    {"mcpServers": {"internal-tools": {"enabled": false}}}

全局条目的其余部分（含项目本身设不了的凭据）会保留。

顶层 ``autoEnableCodemode``（默认 true）表示 codemode 曝光度的 server 连上时是否激活
codemode 工具；项目值覆盖全局值。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from ...mcp_servers import (
    McpExposure,
    McpServerConfig,
    mcp_namespace,
    validate_mcp_server_config,
)

__all__ = [
    "CONFIG_DIR_NAME",
    "LoadedMcpConfig",
    "McpServerConfigPatch",
    "McpServerEntry",
    "add_mcp_server_config",
    "edit_mcp_servers",
    "load_mcp_config",
    "remove_mcp_server_config",
    "update_mcp_server_config",
]

#: 项目级配置目录名（上游 ``config.ts: CONFIG_DIR_NAME``）。
CONFIG_DIR_NAME = ".pi"

#: 项目条目只能覆盖这几个键。
_OVERRIDE_KEYS = ("enabled", "exposure", "toolExposure")

_INDENT_RE = re.compile(r"^([ \t]+)\S", re.M)


@dataclass
class McpServerEntry:
    """配置里的一条 server 条目。"""

    name: str
    config: McpServerConfig
    #: 定义该条目的配置文件，或注册它的扩展路径。
    source: str
    #: 全局 / 项目 ``mcp.json``，或扩展注册的 ``extension``。
    scope: str | None = None
    #: 项目 ``mcp.json`` 里覆盖了该全局 server 的 enabled/exposure/toolExposure。
    override: str | None = None


@dataclass
class LoadedMcpConfig:
    servers: list[McpServerEntry]
    #: codemode 曝光的 server 连上时是否激活 codemode。默认 true。
    auto_enable_codemode: bool | None = None
    errors: list[str] = field(default_factory=list)
    #: 项目受信时的项目 ``mcp.json`` 路径（``/mcp`` 往这里存项目覆盖）。
    project_config: str | None = None


@dataclass
class McpServerConfigPatch:
    """``/mcp`` 会改的设置；``enabled=True`` 与 ``exposure="codemode"`` 是默认值，会删除对应键。"""

    enabled: bool | None = None
    exposure: McpExposure | None = None


@dataclass
class _ConfigState:
    servers: dict[str, McpServerEntry] = field(default_factory=dict)
    auto_enable_codemode: bool | None = None
    errors: list[str] = field(default_factory=list)


def _is_override(value: dict[str, Any]) -> bool:
    """该条目是「覆盖别处的 server」而不是「定义一个 server」。"""
    return value.get("command") is None and value.get("url") is None and value.get("type") is None


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _read_config_file(path: str, scope: str, state: _ConfigState) -> None:
    if not os.path.exists(path):
        return
    try:
        parsed = json.loads(_read_text(path))
    except (OSError, ValueError) as error:
        state.errors.append(f"{path}: {error}")
        return
    if not _is_record(parsed) or (
        parsed.get("mcpServers") is not None and not _is_record(parsed["mcpServers"])
    ):
        state.errors.append(f'{path}: expected an object with an "mcpServers" object')
        return
    auto_enable = parsed.get("autoEnableCodemode")
    if isinstance(auto_enable, bool):
        state.auto_enable_codemode = auto_enable
    elif auto_enable is not None:
        state.errors.append(f"{path}: autoEnableCodemode must be a boolean")

    for name, value in (parsed.get("mcpServers") or {}).items():
        if scope == "project" and _is_record(value) and _is_override(value):
            base = state.servers.get(name)
            extra = [key for key in value if key not in _OVERRIDE_KEYS]
            if base is None:
                state.errors.append(
                    f'{path}: server "{name}" needs "command" or "url", or a global server to override'
                )
            elif extra:
                state.errors.append(
                    f'{path}: server "{name}": an override can only set {", ".join(_OVERRIDE_KEYS)}'
                )
            else:
                merged = {
                    **base.config.model_dump(by_alias=True, exclude_none=True),
                    **value,
                }
                config = validate_mcp_server_config(name, merged)
                if isinstance(config, str):
                    state.errors.append(f"{path}: {config}")
                else:
                    state.servers[name] = McpServerEntry(
                        name=base.name,
                        config=config,
                        source=base.source,
                        scope=base.scope,
                        override=path,
                    )
            continue

        config = validate_mcp_server_config(name, value)
        if isinstance(config, str):
            state.errors.append(f"{path}: {config}")
            continue
        # 只在 `-` 与 `_` 上不同的名字会共用同一个 namespace。
        clash = next(
            (
                other
                for other in state.servers
                if other != name and mcp_namespace(other) == mcp_namespace(name)
            ),
            None,
        )
        if clash is not None:
            state.errors.append(f'{path}: server "{name}" conflicts with "{clash}"')
            continue
        if (
            scope == "project"
            and getattr(config, "url", None) is not None
            and getattr(config, "auth", None)
        ):
            state.errors.append(
                f'{path}: server "{name}": auth is only allowed in the global mcp.json'
            )
            continue
        state.servers[name] = McpServerEntry(name=name, config=config, source=path, scope=scope)


def load_mcp_config(options: dict[str, Any] | None = None, **kwargs: Any) -> LoadedMcpConfig:
    """读取全局、（项目受信时）项目 MCP 配置。

    被禁用的 server 也会以 ``enabled: false`` 返回，以便重新启用。
    """
    params = {**(options or {}), **kwargs}
    agent_dir: str = params["agent_dir"]
    cwd: str = params["cwd"]
    project_trusted: bool = params["project_trusted"]

    state = _ConfigState()
    _read_config_file(os.path.join(agent_dir, "mcp.json"), "global", state)
    project_config = os.path.join(cwd, CONFIG_DIR_NAME, "mcp.json") if project_trusted else None
    if project_config is not None:
        _read_config_file(project_config, "project", state)
    return LoadedMcpConfig(
        servers=list(state.servers.values()),
        auto_enable_codemode=state.auto_enable_codemode,
        errors=state.errors,
        project_config=project_config,
    )


def update_mcp_server_config(
    path: str,
    name: str,
    patch: McpServerConfigPatch,
    options: dict[str, Any] | None = None,
) -> None:
    """改动定义（或覆盖）该 server 的那个 ``mcp.json``。

    带 ``override=True`` 时，缺失的条目会作为覆盖项补上。覆盖项保留默认值，因为它们要替换全局
    server 的值。其它内容保持不变，文件按其原有缩进重写。
    """
    use_override = bool((options or {}).get("override"))

    def edit(servers: dict[str, Any] | None, parsed: dict[str, Any]) -> bool:
        current: dict[str, Any] = servers if servers is not None else {}
        raw: Any = current.get(name)
        if raw is None and use_override:
            created: dict[str, Any] = {}
            current[name] = created
            parsed["mcpServers"] = current
            raw = created
        if not _is_record(raw):
            raise ValueError(f'{path} does not define MCP server "{name}"')
        server: dict[str, Any] = raw
        keep_defaults = _is_override(server)
        if patch.enabled is not None:
            if patch.enabled and not keep_defaults:
                server.pop("enabled", None)
            else:
                server["enabled"] = patch.enabled
        if patch.exposure is not None:
            if patch.exposure == "codemode" and not keep_defaults:
                server.pop("exposure", None)
            else:
                server["exposure"] = patch.exposure
        return True

    edit_mcp_servers(path, edit)


def add_mcp_server_config(path: str, name: str, config: McpServerConfig) -> bool:
    """往 ``mcp.json`` 加一个 server（文件不存在则创建）。同名条目会被替换。返回是否替换过。"""
    replaced = False

    def edit(servers: dict[str, Any] | None, parsed: dict[str, Any]) -> bool:
        nonlocal replaced
        target = servers if servers is not None else {}
        replaced = target.get(name) is not None
        target[name] = config.model_dump(by_alias=True, exclude_none=True)
        parsed["mcpServers"] = target
        return True

    edit_mcp_servers(path, edit)
    return replaced


def remove_mcp_server_config(path: str, name: str) -> bool:
    """从 ``mcp.json`` 移除一个 server。文件未定义它时返回 False。"""
    if not os.path.exists(path):
        return False
    removed = False

    def edit(servers: dict[str, Any] | None, parsed: dict[str, Any]) -> bool:
        nonlocal removed
        if not servers or servers.get(name) is None:
            return False
        del servers[name]
        removed = True
        return True

    edit_mcp_servers(path, edit)
    return removed


def edit_mcp_servers(
    path: str,
    edit: Any,
) -> None:
    """读取 ``mcp.json``（缺失时当作空配置），让 ``edit`` 改它的 ``mcpServers``，
    并在 ``edit`` 返回 True 时按原缩进写回。其它内容保持不变。
    """
    text = _read_text(path) if os.path.exists(path) else None
    parsed: Any = {} if text is None else json.loads(text)
    if not _is_record(parsed) or (
        parsed.get("mcpServers") is not None and not _is_record(parsed["mcpServers"])
    ):
        raise ValueError(f'{path}: expected an object with an "mcpServers" object')
    servers = parsed["mcpServers"] if _is_record(parsed.get("mcpServers")) else None
    if not edit(servers, parsed):
        return
    match = _INDENT_RE.search(text) if text else None
    indent = match.group(1) if match else "  "
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _write_text(path, json.dumps(parsed, indent=indent, ensure_ascii=False) + "\n")


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
