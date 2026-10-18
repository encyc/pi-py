"""Subprocess isolation: the parent keeps its tools (and its life) if the VM dies.

The in-process sandbox is the default; this is the opt-in hardening. The tests cover the
three things the parent must survive: a normal run, a runaway script, and a child that is
killed outright.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from typing import Any

from pi_codemode import CodemodeResult, CodemodeTool
from pi_codemode.subprocess import SubprocessCodemodeSandbox


def echo_tool(calls: list[object]) -> CodemodeTool:
    def execute(args: object) -> object:
        calls.append(args)
        if isinstance(args, dict) and "a" in args:
            return {"a": args["a"] + 41}
        return args

    return CodemodeTool(name="echo", execute=execute, description="Echo")


def test_tool_round_trip_across_processes() -> None:
    calls: list[object] = []
    sandbox = SubprocessCodemodeSandbox(
        tools=[echo_tool(calls), CodemodeTool(name="other", execute=lambda args: [1, 2, 3])]
    )
    try:
        result = sandbox.execute(
            "const v = await tools.echo({ a: 1 }); text('got ' + v.a); "
            "const other = await tools.other({}); return { v, other };"
        )
    finally:
        sandbox.close()
    assert result.ok, result.error
    assert result.value == {"v": {"a": 42}, "other": [1, 2, 3]}
    assert [item.text for item in result.output] == ["got 42"]
    assert calls == [{"a": 1}]
    assert [(call.name, call.status) for call in result.calls] == [("echo", "ok"), ("other", "ok")]


def test_tool_failure_is_catchable_in_the_child() -> None:
    def boom(args: object) -> object:
        raise ValueError("nope")

    sandbox = SubprocessCodemodeSandbox(tools=[CodemodeTool(name="boom", execute=boom)])
    try:
        result: CodemodeResult = sandbox.execute(
            "try { await tools.boom({}); return 'no-error'; } catch (e) { return 'caught:' + e.message; }"
        )
    finally:
        sandbox.close()
    assert result.ok, result.error
    assert result.value == "caught:nope"
    assert [(call.name, call.status) for call in result.calls] == [("boom", "error")]


def test_timeout_kills_the_child_and_the_parent_survives() -> None:
    sandbox = SubprocessCodemodeSandbox(timeout_ms=400)
    try:
        started = time.perf_counter()
        result = sandbox.execute("while (true) {}")
        elapsed = time.perf_counter() - started
        assert not result.ok
        assert result.error is not None
        assert result.error.kind == "timeout"
        assert elapsed < 6.0, f"child was not stopped promptly ({elapsed:.2f}s)"
        # The parent is healthy and can run another script.
        follow_up = sandbox.execute("return 1 + 1;", timeout_ms=10_000)
        assert follow_up.ok, follow_up.error
        assert follow_up.value == 2
    finally:
        sandbox.close()


def test_a_killed_child_is_reported_and_the_parent_survives() -> None:
    """Crash isolation: SIGKILL the VM process mid-script; the caller just sees an error."""
    pid_holder: dict[str, int] = {}
    released = threading.Event()
    entered = threading.Event()

    def block(args: object) -> object:
        entered.set()
        released.wait(10)
        return None

    sandbox = SubprocessCodemodeSandbox(
        tools=[CodemodeTool(name="block", execute=block)],
        timeout_ms=15_000,
        on_child_started=lambda pid: pid_holder.setdefault("pid", pid),
    )
    try:
        killer: dict[str, Any] = {}

        def kill_when_ready() -> None:
            entered.wait(10)
            os.kill(pid_holder["pid"], signal.SIGKILL)

        killer["thread"] = threading.Thread(target=kill_when_ready, daemon=True)
        killer["thread"].start()
        result = sandbox.execute("await tools.block({}); return 'never';")
        released.set()
        killer["thread"].join(timeout=5)
        assert not result.ok
        assert result.error is not None
        assert result.error.kind == "sandbox"
        # The parent is still alive and usable: that is the whole point of the isolation.
        follow_up = sandbox.execute("return 42;", timeout_ms=10_000)
        assert follow_up.ok, follow_up.error
        assert follow_up.value == 42
    finally:
        released.set()
        sandbox.close()
