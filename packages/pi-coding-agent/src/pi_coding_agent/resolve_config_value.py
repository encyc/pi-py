"""解析可能是 shell 命令、环境变量或字面量的配置值。

对应上游 ``core/resolve-config-value.ts``（287 行，**完整移植**）。
被 ``auth-storage.ts`` 与 ``model-registry.ts`` 使用。

**行为面（安全）**：以 ``!`` 开头的值是**要执行的 shell 命令**，其 stdout 被当作配置值。
这是上游既有行为，命令只来自**用户自己写在配置里**的内容（``mcp.json`` / ``models.json`` 的
``headers``、``env``、``clientSecret`` 等）。本端照上游保留，不做削弱；超时 10 秒，
stderr 丢弃，失败结果与成功结果一样进缓存。

**有意偏离**：上游在 Windows 上优先用 ``utils/shell.ts: getShellConfig()`` 解析出的 shell
（支持 Cygwin / git-bash 与 stdin 传命令）。本包未移植该模块的 shell 配置解析，因此
**Windows 一律走默认 shell**（``subprocess`` 的 ``shell=True``）；POSIX 上两者等价。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

__all__ = [
    "clear_config_value_cache",
    "get_config_value_env_var_name",
    "get_config_value_env_var_names",
    "get_missing_config_value_env_var_names",
    "is_command_config_value",
    "is_config_value_configured",
    "resolve_config_value",
    "resolve_config_value_or_throw",
    "resolve_config_value_uncached",
    "resolve_headers",
    "resolve_headers_or_throw",
]

#: shell 命令结果缓存（进程生命周期内有效）。键是**含 ``!`` 前缀的原始配置串**。
#: 用显式成员检查以区分「未缓存」与「缓存了一个失败（None）」。
_command_result_cache: dict[str, str | None] = {}

_ENV_VAR_NAME_PREFIX = r"[A-Za-z_][A-Za-z0-9_]*"

#: 命令执行的超时毫秒数（对齐上游 10000）。
_COMMAND_TIMEOUT_MS = 10_000


@dataclass
class _Literal:
    value: str


@dataclass
class _Env:
    name: str


_TemplatePart = _Literal | _Env


@dataclass
class _CommandReference:
    config: str


@dataclass
class _TemplateReference:
    parts: list[_TemplatePart]


def _append_literal(parts: list[_TemplatePart], value: str) -> None:
    if not value:
        return
    previous = parts[-1] if parts else None
    if isinstance(previous, _Literal):
        previous.value += value
        return
    parts.append(_Literal(value))


def _is_env_var_name(name: str) -> bool:
    if not name:
        return False
    if not (name[0].isascii() and (name[0].isalpha() or name[0] == "_")):
        return False
    return all(char.isascii() and (char.isalnum() or char == "_") for char in name[1:])


def _env_var_prefix(value: str) -> str | None:
    """匹配 ``$NAME`` 形式的前缀；返回变量名或 None。"""
    if not value:
        return None
    head = value[0]
    if not (head.isascii() and (head.isalpha() or head == "_")):
        return None
    end = 1
    while end < len(value):
        char = value[end]
        if char.isascii() and (char.isalnum() or char == "_"):
            end += 1
            continue
        break
    return value[:end]


def _parse_template(config: str) -> list[_TemplatePart]:
    parts: list[_TemplatePart] = []
    index = 0

    while index < len(config):
        dollar_index = config.find("$", index)
        if dollar_index < 0:
            _append_literal(parts, config[index:])
            break

        _append_literal(parts, config[index:dollar_index])
        next_char = config[dollar_index + 1] if dollar_index + 1 < len(config) else None

        if next_char in ("$", "!"):
            _append_literal(parts, str(next_char))
            index = dollar_index + 2
            continue

        if next_char == "{":
            end_index = config.find("}", dollar_index + 2)
            if end_index < 0:
                _append_literal(parts, "$")
                index = dollar_index + 1
                continue
            name = config[dollar_index + 2 : end_index]
            if _is_env_var_name(name):
                parts.append(_Env(name))
            else:
                _append_literal(parts, config[dollar_index : end_index + 1])
            index = end_index + 1
            continue

        prefix = _env_var_prefix(config[dollar_index + 1 :])
        if prefix:
            parts.append(_Env(prefix))
            index = dollar_index + 1 + len(prefix)
            continue

        _append_literal(parts, "$")
        index = dollar_index + 1

    return parts


def _parse_reference(config: str) -> _CommandReference | _TemplateReference:
    if config.startswith("!"):
        return _CommandReference(config)
    return _TemplateReference(_parse_template(config))


def _resolve_env_config_value(name: str, env: dict[str, str] | None) -> str | None:
    # 对齐上游 `env?.[name] || process.env[name] || undefined`：空串会落到进程环境。
    if env is not None:
        from_env = env.get(name)
        if from_env:
            return from_env
    return os.environ.get(name) or None


def _template_env_var_names(parts: list[_TemplatePart]) -> list[str]:
    names: list[str] = []
    for part in parts:
        if not isinstance(part, _Env) or part.name in names:
            continue
        names.append(part.name)
    return names


def _resolve_template(parts: list[_TemplatePart], env: dict[str, str] | None) -> str | None:
    resolved = ""
    for part in parts:
        if isinstance(part, _Literal):
            resolved += part.value
            continue
        env_value = _resolve_env_config_value(part.name, env)
        if env_value is None:
            return None
        resolved += env_value
    return resolved


def get_config_value_env_var_name(config: str) -> str | None:
    """模板恰好是单个环境变量引用时返回其名字，否则 None。"""
    reference = _parse_reference(config)
    if not isinstance(reference, _TemplateReference):
        return None
    if len(reference.parts) == 1 and isinstance(reference.parts[0], _Env):
        return reference.parts[0].name
    return None


def get_config_value_env_var_names(config: str) -> list[str]:
    reference = _parse_reference(config)
    return (
        _template_env_var_names(reference.parts)
        if isinstance(reference, _TemplateReference)
        else []
    )


def get_missing_config_value_env_var_names(
    config: str, env: dict[str, str] | None = None
) -> list[str]:
    return [
        name
        for name in get_config_value_env_var_names(config)
        if _resolve_env_config_value(name, env) is None
    ]


def is_command_config_value(config: str) -> bool:
    return isinstance(_parse_reference(config), _CommandReference)


def is_config_value_configured(config: str, env: dict[str, str] | None = None) -> bool:
    return len(get_missing_config_value_env_var_names(config, env)) == 0


def _execute_with_default_shell(command: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S602 - 上游行为：执行用户配置里的 shell 命令
            command,
            shell=True,
            capture_output=True,
            timeout=_COMMAND_TIMEOUT_MS / 1000,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.decode("utf-8", errors="replace").strip()
    return output or None


def _execute_command_uncached(command_config: str) -> str | None:
    command = command_config[1:]
    # 上游在 Windows 上优先用配置的 shell；本包未移植 shell 配置解析，统一走默认 shell。
    return _execute_with_default_shell(command)


def _execute_command(command_config: str) -> str | None:
    if command_config in _command_result_cache:
        return _command_result_cache[command_config]
    result = _execute_command_uncached(command_config)
    _command_result_cache[command_config] = result
    return result


def resolve_config_value(config: str, env: dict[str, str] | None = None) -> str | None:
    """把配置值解析成实际值。

    - 以 ``!`` 开头：把其余部分当 shell 命令执行，取其 stdout（**结果进缓存**）
    - ``$ENV_VAR`` / ``${ENV_VAR}``：用同名环境变量插值
    - 非命令值里 ``$$`` 转义字面 ``$``，``$!`` 转义字面 ``!``
    - 其余按字面量处理
    """
    reference = _parse_reference(config)
    if isinstance(reference, _CommandReference):
        return _execute_command(reference.config)
    return _resolve_template(reference.parts, env)


def resolve_config_value_uncached(config: str, env: dict[str, str] | None = None) -> str | None:
    """同 :func:`resolve_config_value`，但**不读也不写命令缓存**。"""
    reference = _parse_reference(config)
    if isinstance(reference, _CommandReference):
        return _execute_command_uncached(reference.config)
    return _resolve_template(reference.parts, env)


def resolve_config_value_or_throw(
    config: str, description: str, env: dict[str, str] | None = None
) -> str:
    resolved_value = resolve_config_value_uncached(config, env)
    if resolved_value is not None:
        return resolved_value

    reference = _parse_reference(config)
    if isinstance(reference, _CommandReference):
        raise RuntimeError(
            f"Failed to resolve {description} from shell command: {reference.config[1:]}"
        )

    if isinstance(reference, _TemplateReference):
        missing_env_vars = get_missing_config_value_env_var_names(config, env)
        if len(missing_env_vars) == 1:
            raise RuntimeError(
                f"Failed to resolve {description} from environment variable: {missing_env_vars[0]}"
            )
        if len(missing_env_vars) > 1:
            raise RuntimeError(
                f"Failed to resolve {description} from environment variables: "
                f"{', '.join(missing_env_vars)}"
            )

    raise RuntimeError(f"Failed to resolve {description}")


def resolve_headers(
    headers: dict[str, str] | None, env: dict[str, str] | None = None
) -> dict[str, str] | None:
    """用与 API key 相同的逻辑解析所有 header 值；解析不出的条目被丢弃。"""
    if not headers:
        return None
    resolved: dict[str, str] = {}
    for key, value in headers.items():
        resolved_value = resolve_config_value(value, env)
        if resolved_value:
            resolved[key] = resolved_value
    return resolved or None


def resolve_headers_or_throw(
    headers: dict[str, str] | None, description: str, env: dict[str, str] | None = None
) -> dict[str, str] | None:
    """同 :func:`resolve_headers`，但任一条目解析失败即抛错。"""
    if not headers:
        return None
    resolved: dict[str, str] = {}
    for key, value in headers.items():
        resolved[key] = resolve_config_value_or_throw(value, f'{description} header "{key}"', env)
    return resolved or None


def clear_config_value_cache() -> None:
    """清空命令结果缓存（导出供测试用）。"""
    _command_result_cache.clear()
