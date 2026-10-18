"""``Shell.exec`` 的边界：spill、回调失败、环境继承、进程清理（env-conformance 之外）。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from pi_chord.context import BACKGROUND_CONTEXT
from pi_durable.env import (
    Err,
    ExecutionError,
    Ok,
    ShellExecOptions,
    ShellOutputInfo,
    ShellSpillOptions,
    get_or_throw,
)
from pi_durable.env.node import NodeExecutionEnv, NodeExecutionEnvOptions


def _read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def _exists(path: str) -> bool:
    return Path(path).exists()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[NodeExecutionEnv]:
    yield NodeExecutionEnv(NodeExecutionEnvOptions(cwd=str(tmp_path)))


def _collect(holder: dict[str, str]):  # type: ignore[no-untyped-def]
    def on_output(text: str, _context: object, info: ShellOutputInfo) -> None:
        holder[info.stream] += text

    return on_output


async def test_spill_keeps_the_complete_output(env: NodeExecutionEnv) -> None:
    holder = {"stdout": "", "stderr": ""}
    result = get_or_throw(
        await env.exec(
            "printf 'aaa\nbbb\nccc\n'",
            ShellExecOptions(
                spill=ShellSpillOptions(after_bytes=10, after_lines=2),
                on_output=_collect(holder),
            ),
            BACKGROUND_CONTEXT,
        )
    )
    assert result.exit_code == 0
    assert result.spill_path is not None
    assert _read_bytes(result.spill_path) == b"aaa\nbbb\nccc\n"
    # The caller still receives every chunk, exactly as without a spill.
    assert holder["stdout"] == "aaa\nbbb\nccc\n"


async def test_no_spill_below_the_thresholds(env: NodeExecutionEnv) -> None:
    result = get_or_throw(
        await env.exec(
            "printf 'aaa\nbbb\nccc\n'",
            ShellExecOptions(spill=ShellSpillOptions(after_bytes=1000, after_lines=1000)),
            BACKGROUND_CONTEXT,
        )
    )
    assert result.spill_path is None


async def test_a_throwing_output_callback_fails_the_command(env: NodeExecutionEnv) -> None:
    def on_output(text: str, _context: object, info: ShellOutputInfo) -> None:
        raise RuntimeError("callback boom")

    result = await env.exec(
        "printf hello; sleep 5", ShellExecOptions(on_output=on_output), BACKGROUND_CONTEXT
    )
    assert isinstance(result, Err)
    assert result.error.code == "callback_error"
    assert "callback boom" in str(result.error)


async def test_inherit_env_can_be_turned_off(env: NodeExecutionEnv) -> None:
    holder = {"stdout": "", "stderr": ""}
    result = get_or_throw(
        await env.exec(
            'printf "%s|%s" "$FOO" "${HOME-unset}"',
            ShellExecOptions(env={"FOO": "bar"}, inherit_env=False, on_output=_collect(holder)),
            BACKGROUND_CONTEXT,
        )
    )
    assert result.exit_code == 0
    assert holder["stdout"] == "bar|unset"


async def test_cleanup_kills_running_commands(env: NodeExecutionEnv) -> None:
    running = asyncio.ensure_future(env.exec("sleep 5", None, BACKGROUND_CONTEXT))
    await asyncio.sleep(0.2)
    await env.cleanup(BACKGROUND_CONTEXT)
    result = await asyncio.wait_for(running, timeout=5)
    assert isinstance(result, Ok)
    # 128 + SIGKILL, the conventional code for a signalled process.
    assert result.value.exit_code == 137
    assert env._active_child_pids == set()


async def test_a_process_killed_by_a_signal_reports_128_plus_signal(
    env: NodeExecutionEnv,
) -> None:
    result = get_or_throw(await env.exec("kill -9 $$", None, BACKGROUND_CONTEXT))
    assert result.exit_code == 137


async def test_missing_working_directory_is_a_spawn_error(env: NodeExecutionEnv) -> None:
    result = await env.exec("true", ShellExecOptions(cwd="missing-dir"), BACKGROUND_CONTEXT)
    assert isinstance(result, Err) and result.error.code == "spawn_error"
    assert "Working directory does not exist" in str(result.error)


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
async def test_invalid_timeouts_are_rejected(env: NodeExecutionEnv, seconds: float) -> None:
    result = await env.exec("true", ShellExecOptions(timeout=seconds), BACKGROUND_CONTEXT)
    assert isinstance(result, Err) and result.error.code == "timeout"


async def test_aborting_kills_only_that_command(env: NodeExecutionEnv) -> None:
    from pi_chord.context import AbortController, with_abort_signal

    controller = AbortController()
    context = with_abort_signal(controller.signal, BACKGROUND_CONTEXT)
    running = asyncio.ensure_future(env.exec("sleep 5", None, context))
    survive = asyncio.ensure_future(env.exec("sleep 0.4; printf ok", None, BACKGROUND_CONTEXT))
    await asyncio.sleep(0.1)
    controller.abort()
    stopped = await asyncio.wait_for(running, timeout=5)
    assert isinstance(stopped, Err) and stopped.error.code == "aborted"
    survivor = await asyncio.wait_for(survive, timeout=5)
    assert isinstance(survivor, Ok) and survivor.value.exit_code == 0


async def test_execution_error_carries_a_spill_path_when_it_spilled(
    env: NodeExecutionEnv,
) -> None:
    result = await env.exec(
        "printf 'aaaaaaaaaa\nbbbbbbbbbb\n'; sleep 5",
        ShellExecOptions(timeout=0.2, spill=ShellSpillOptions(after_bytes=5, after_lines=1)),
        BACKGROUND_CONTEXT,
    )
    assert isinstance(result, Err)
    assert result.error.code == "timeout"
    assert isinstance(result.error, ExecutionError)
    assert result.error.spill_path is not None
    assert _exists(result.error.spill_path)
