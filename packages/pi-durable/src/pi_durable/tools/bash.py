"""``bash`` 与 ``powershell`` 工具 —— 移植 ``durable/src/tools/bash.ts``（184 行）。"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from pi_chord.context import Context
from pi_durable.env import ExecutionError, ShellExecOptions, ShellSpillOptions
from pi_durable.harness import (
    ToolDiagnostic,
    ToolExecutionApi,
    ToolExecutionResult,
    ToolOutputLimits,
    ToolRegistration,
    define_tool,
)
from pi_durable.tools.args import json_schema_of
from pi_durable.tools.env import require_env
from pi_durable.truncate import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES

__all__ = [
    "BASH_SCHEMA",
    "POWERSHELL_SCHEMA",
    "SHELL_STRUCTURED_OUTPUT_SCHEMA",
    "BashExecution",
    "BashPrepare",
    "BashToolInput",
    "BashToolOptions",
    "PowerShellToolInput",
    "PowerShellToolOptions",
    "ShellStructuredOutput",
    "create_bash_tool",
    "create_powershell_tool",
]

#: ``bash.ts:8``：JS 定时器上限（2^31-1 毫秒）换算成秒。
MAX_TIMEOUT_SECONDS = 2_147_483_647 / 1000


class BashToolInput(BaseModel):
    """``bash.ts:10-13`` 的 ``bashSchema``。"""

    command: str = Field(description="Bash command to execute")
    timeout: float | None = Field(
        default=None, description="Timeout in seconds (optional, no default timeout)"
    )


class PowerShellToolInput(BaseModel):
    """``bash.ts:15-18`` 的 ``powershellSchema``。"""

    command: str = Field(description="PowerShell command to execute")
    timeout: float | None = Field(
        default=None, description="Timeout in seconds (optional, no default timeout)"
    )


BASH_SCHEMA: dict[str, Any] = json_schema_of(BashToolInput)
POWERSHELL_SCHEMA: dict[str, Any] = json_schema_of(PowerShellToolInput)

#: ``bash.ts:21-26`` 的 ``shellStructuredOutputSchema``。
SHELL_STRUCTURED_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "output": {
            "type": "string",
            "description": "Combined stdout and stderr: the retained tail, as the model sees it",
        },
        "truncated": {"type": "boolean", "description": "Whether earlier output was dropped"},
        "fullOutputPath": {
            "type": "string",
            "description": "File with the complete output, when it was spilled",
        },
        "exitCode": {"type": "number"},
    },
    "required": ["output", "truncated", "exitCode"],
}


@dataclass
class BashExecution:
    """即将运行的命令；``prepare`` 可以改它（``bash.ts:32-38``）。"""

    command: str
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    inherit_env: bool = True


#: ``bash.ts:40``：``prepare`` 回调，可在运行前改写命令、工作目录与环境。
BashPrepare = Callable[[BashExecution, ToolExecutionApi, Context], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class BashToolOptions:
    """``bash.ts:42-45``。"""

    command_prefix: str | None = None
    prepare: BashPrepare | None = None


@dataclass(frozen=True, slots=True)
class PowerShellToolOptions:
    """``bash.ts:47-53``。"""

    command_prefix: str | None = None
    prepare: BashPrepare | None = None
    programs: Sequence[str] | None = None


@dataclass(frozen=True, slots=True)
class ShellStructuredOutput:
    """``bash.ts:20-30`` 的 ``ShellStructuredOutput``。"""

    output: str
    truncated: bool
    exit_code: int
    full_output_path: str | None = None

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "output": self.output,
            "truncated": self.truncated,
            "exitCode": self.exit_code,
        }
        if self.full_output_path is not None:
            result["fullOutputPath"] = self.full_output_path
        return result


def _validate_timeout(timeout: float | None) -> None:
    """``bash.ts:55-63``。消息逐字保留。"""
    if timeout is None:
        return
    if not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeError("Invalid timeout: must be a finite number of seconds")
    if timeout > MAX_TIMEOUT_SECONDS:
        raise RuntimeError(f"Invalid timeout: maximum is {MAX_TIMEOUT_SECONDS} seconds")


async def _prepare_execution(
    command: str,
    options: BashToolOptions | PowerShellToolOptions | None,
    api: ToolExecutionApi,
    context: Context,
) -> BashExecution:
    """``bash.ts:65-80``。前缀拼在命令**上面一行**。"""
    prefix = options.command_prefix if options is not None else None
    execution = BashExecution(
        command=f"{prefix}\n{command}" if prefix else command,
        cwd=require_env(api).cwd,
        env={},
        inherit_env=True,
    )
    prepare = options.prepare if options is not None else None
    if prepare is not None:
        outcome = prepare(execution, api, context)
        if outcome is not None:
            await outcome
    return execution


async def _run_command(
    commands: Sequence[str | Sequence[str]],
    execution: BashExecution,
    timeout_seconds: float | None,
    api: ToolExecutionApi,
    context: Context,
) -> ToolExecutionResult[Any]:
    """依次尝试每种命令形式，直到有一个能启动（``bash.ts:82-136``）。"""
    env = require_env(api)
    result = None
    for command in commands:
        options = ShellExecOptions(
            cwd=execution.cwd,
            env=dict(execution.env),
            inherit_env=execution.inherit_env,
            timeout=timeout_seconds,
            on_output=lambda text, _context, info: api.output(text, info.skipped),
            spill=ShellSpillOptions(after_bytes=DEFAULT_MAX_BYTES, after_lines=DEFAULT_MAX_LINES),
            window=api.output_window,
        )
        result = await env.exec(command, options, context)
        # 起不来的程序没有输出，换下一种形式试试（``bash.ts:111-112``）。
        if result.ok or result.error.code != "spawn_error":
            break
    if result is None:
        raise RuntimeError("No command to run")

    spill_path = result.value.spill_path if result.ok else result.error.spill_path
    if spill_path is not None:
        api.diagnostic(
            ToolDiagnostic(
                severity="info", code="full_output", message=f"Full output: {spill_path}"
            )
        )

    if not result.ok:
        error: ExecutionError = result.error
        if (
            error.code == "aborted"
            and context.abort_signal is not None
            and context.abort_signal.aborted
        ):
            raise error
        if error.code == "timeout":
            raise RuntimeError(f"Command timed out after {timeout_seconds} seconds")
        if error.code == "aborted":
            raise RuntimeError("Command aborted")
        raise error

    exit_code = result.value.exit_code
    retained = api.retained_output()
    structured = ShellStructuredOutput(
        output=retained.text,
        truncated=retained.truncated,
        exit_code=exit_code,
        full_output_path=spill_path,
    )
    if exit_code == 0:
        return ToolExecutionResult(structured_output=structured.to_json())

    return ToolExecutionResult(
        structured_output=structured.to_json(),
        is_error=True,
        diagnostics=[
            ToolDiagnostic(
                severity="error", code="exit_code", message=f"Command exited with code {exit_code}"
            )
        ],
    )


def _shell_description(noun: str) -> str:
    return (
        f"Execute a {noun} command in the current working directory. Returns combined stdout and "
        f"stderr. Output is truncated to last {DEFAULT_MAX_LINES} lines or "
        f"{DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first). If truncated, full output is "
        "saved to a temp file. Optionally provide a timeout in seconds."
    )


def create_bash_tool(options: BashToolOptions | None = None) -> ToolRegistration[BashToolInput]:
    """``createBashTool()``（``bash.ts:145-158``）。

    输出保留**尾部**（``retain: "tail"``）——错误与结果在末尾。
    """

    async def _execute(
        args: BashToolInput, api: ToolExecutionApi, context: Context
    ) -> ToolExecutionResult[Any]:
        _validate_timeout(args.timeout)
        execution = await _prepare_execution(args.command, options, api, context)
        return await _run_command([execution.command], execution, args.timeout, api, context)

    return define_tool(
        name="bash",
        description=_shell_description("bash"),
        parameters=BASH_SCHEMA,
        structured_output_schema=SHELL_STRUCTURED_OUTPUT_SCHEMA,
        output_limits=ToolOutputLimits(retain="tail"),
        execute=_execute,
    )


#: ``bash.ts:161-162``。
UTF8_OUTPUT = "try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}"
POWERSHELL_ARGS = ("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command")


def create_powershell_tool(
    options: PowerShellToolOptions | None = None,
) -> ToolRegistration[PowerShellToolInput]:
    """``createPowerShellTool()``（``bash.ts:168-183``）。

    直接以 argv 形式起 ``pwsh``（PowerShell 7），否则退回 Windows PowerShell——
    命令作为参数传入，**不经过其它 shell 解析**。
    """
    programs = (
        options.programs if options is not None and options.programs else ("pwsh", "powershell")
    )

    async def _execute(
        args: PowerShellToolInput, api: ToolExecutionApi, context: Context
    ) -> ToolExecutionResult[Any]:
        _validate_timeout(args.timeout)
        execution = await _prepare_execution(args.command, options, api, context)
        script = f"{UTF8_OUTPUT}\n{execution.command}"
        commands: list[list[str]] = [[program, *POWERSHELL_ARGS, script] for program in programs]
        return await _run_command(commands, execution, args.timeout, api, context)

    return define_tool(
        name="powershell",
        description=_shell_description("PowerShell"),
        parameters=POWERSHELL_SCHEMA,
        structured_output_schema=SHELL_STRUCTURED_OUTPUT_SCHEMA,
        output_limits=ToolOutputLimits(retain="tail"),
        execute=_execute,
    )
