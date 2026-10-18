"""Subprocess isolation: the VM in a child process, the tools in the parent.

This is **opt-in hardening**, not the default architecture. In-process is enough for
interruption — wasmtime releases the GIL while wasm runs, so a watchdog thread stops a
script in ~0.2 ms (Phase-0 S5b) — but a separate process additionally contains a wasm trap
that would take the host down, and gives the parent a hard ``SIGKILL``.

The bridge is upstream's ``protocol.ts`` translated to a pipe: the child owns the VM and
sends ``call`` / ``output`` / ``done`` messages; the parent owns the tool table, answers
each call and assembles the result. Interruption crosses the boundary through shared
memory (upstream's ``SharedArrayBuffer``): the parent writes a flag, the child's
``host_interrupt`` polls it, and the child's own watchdog advances the wasm epoch if the
script keeps computing inside a built-in that never polls.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import struct
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from multiprocessing import shared_memory
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any, Final

from .host import DEFAULT_MAX_STACK_SIZE
from .sandbox import DEFAULT_MEMORY_LIMIT_BYTES, DEFAULT_TIMEOUT_MS
from .types import (
    CodemodeCall,
    CodemodeError,
    CodemodeOutputItem,
    CodemodeResult,
    CodemodeStoreWrites,
    CodemodeTool,
)

__all__ = ["CHILD_SHUTDOWN_GRACE_SECONDS", "SubprocessCodemodeSandbox"]

#: How long the parent waits for the child to react to the stop flag before killing it.
CHILD_SHUTDOWN_GRACE_SECONDS: Final = 0.3
#: How long the child waits for an answer before checking the stop flag again.
_CHILD_POLL_SECONDS: Final = 0.05
#: Message kinds on the pipe.
_CALL: Final = "call"
_RESULT: Final = "result"
_OUTPUT: Final = "output"
_DONE: Final = "done"
_CRASH: Final = "crash"


def _shared_buffer(memory: shared_memory.SharedMemory) -> memoryview:
    """The writable view of a shared block (``buf`` is ``None`` only after ``close()``)."""
    buf = memory.buf
    if buf is None:
        raise RuntimeError("shared memory is closed")
    return buf


def _child_watchdog(
    vm_holder: dict[str, Any], stop_reader: Callable[[], bool], finished: threading.Event
) -> None:
    """Child-side hard stop: poll the shared flag, then advance the wasm epoch."""
    while not finished.is_set():
        if stop_reader():
            if finished.wait(0.05):
                return
            vm = vm_holder.get("vm")
            if vm is not None:
                with contextlib.suppress(Exception):  # the child is being torn down
                    vm.tick_epoch()
            return
        finished.wait(0.01)


def _child_main(connection: Connection, config: dict[str, Any]) -> None:
    """Entry point of the child process (importable, so ``spawn`` can pickle it)."""
    try:
        _child_run(connection, config)
    except BaseException as error:  # noqa: BLE001 - report anything to the parent
        with contextlib.suppress(Exception):  # the pipe may already be gone
            connection.send({"type": _CRASH, "message": f"{type(error).__name__}: {error}"})
    finally:
        with contextlib.suppress(Exception):
            connection.close()


def _child_run(connection: Connection, config: dict[str, Any]) -> None:
    from .assets import load_prelude
    from .host import load_quickjs_wasm

    memory = shared_memory.SharedMemory(name=config["shm_name"])

    def stop_requested() -> bool:
        (value,) = struct.unpack_from("<I", _shared_buffer(memory), 0)
        return int(value) != 0

    engine = load_quickjs_wasm(epoch_interruption=True, consume_fuel=False)
    vm = engine.new_vm(
        should_interrupt=stop_requested,
        memory_limit_bytes=config["memory_limit_bytes"],
        max_stack_size=config["max_stack_size"],
    )
    holder: dict[str, Any] = {"vm": vm}
    finished = threading.Event()
    watchdog = threading.Thread(
        target=_child_watchdog,
        args=(holder, stop_requested, finished),
        name="codemode-child-watchdog",
        daemon=True,
    )
    watchdog.start()

    replies: dict[int, tuple[bool, str | None]] = {}
    done: dict[str, Any] = {}

    def bridge(args: list[int]) -> int:
        kind = vm.to_str(args[0])
        if kind in ("call", "global"):
            call_id = int(vm.to_float(args[1]))
            name = vm.to_str(args[2]) or ""
            args_json = vm.to_str(args[3]) if len(args) > 3 else None
            connection.send(
                {
                    "type": _CALL,
                    "id": call_id,
                    "target": "tool" if kind == "call" else "global",
                    "name": name,
                    "args": args_json,
                }
            )
            reply = _await_reply(connection, stop_requested)
            if reply is None:
                return vm.undefined()
            replies[call_id] = (bool(reply["ok"]), reply.get("payload"))
        elif kind == "output":
            source = vm.to_str(args[1])
            item = (
                {"type": "image", "data": vm.to_str(args[2]), "mimeType": vm.to_str(args[3])}
                if source == "image"
                else {"type": "text", "text": vm.to_str(args[2]), "console": source == "console"}
            )
            connection.send({"type": _OUTPUT, "item": item})
        elif kind == "done":
            payload = None
            if len(args) > 2 and not vm.is_undefined(args[2]):
                payload = vm.to_str(args[2])
            done["value"] = (
                vm.to_bool(args[1]),
                payload,
                vm.to_str(args[3]) if len(args) > 3 else None,
            )
        return vm.undefined()

    try:
        api_fn = vm.eval_code(load_prelude(), "codemode-prelude.js")
        if vm.is_exception(api_fn):
            connection.send({"type": _CRASH, "message": vm.pending_exception().describe()})
            return
        api = vm.call(
            api_fn,
            vm.undefined(),
            [
                vm.host_function("bridge", bridge),
                vm.new_string(config["tools_json"]),
                vm.new_string(config["globals_json"]),
                vm.new_string(config["store_json"]),
            ],
        )
        if vm.is_exception(api):
            connection.send({"type": _CRASH, "message": vm.pending_exception().describe()})
            return
        settle = vm.get_prop(api, "settle")
        run = vm.get_prop(api, "run")
        stalled = vm.get_prop(api, "stalled")
        script = vm.eval_code(f"(async (tools, console) => {{{config['code']}\n}})", "codemode.js")
        if vm.is_exception(script):
            error = vm.pending_exception()
            connection.send(
                {
                    "type": _DONE,
                    "ok": False,
                    "error": json.dumps(
                        {"name": error.name, "message": error.message, "stack": error.stack}
                    ),
                    "writes": "[]",
                }
            )
            return
        vm.call(run, api, [script])
        report = vm.drain_jobs(should_interrupt=stop_requested)
        while done.get("value") is None and report.error is None and not stop_requested():
            if replies:
                call_id, (ok, payload) = replies.popitem()
                vm.call(
                    settle,
                    api,
                    [
                        vm.number(call_id),
                        vm.true() if ok else vm.false(),
                        vm.undefined() if payload is None else vm.new_string(payload),
                        vm.undefined(),
                    ],
                )
            report = vm.drain_jobs(should_interrupt=stop_requested)
            if done.get("value") is not None:
                break
            if not replies and report.jobs == 0:
                vm.call(stalled, api, [])
                if done.get("value") is None:
                    break
        if report.error is not None:
            connection.send({"type": _CRASH, "message": report.error.describe()})
            return
        if done.get("value") is not None:
            ok, payload, writes = done["value"]
            connection.send({"type": _DONE, "ok": ok, "value": payload, "writes": writes or "[]"})
        elif stop_requested():
            connection.send({"type": _DONE, "ok": False, "error": _INTERRUPTED, "writes": "[]"})
        else:
            connection.send({"type": _DONE, "ok": False, "error": _STALLED, "writes": "[]"})
    finally:
        finished.set()
        with contextlib.suppress(Exception):
            memory.close()


_INTERRUPTED: Final = json.dumps({"name": "InternalError", "message": "interrupted"})
_STALLED: Final = json.dumps(
    {
        "name": "Error",
        "message": "The script is waiting on a promise that can never settle: no tool call is "
        "pending, and timers do not exist here.",
    }
)


def _await_reply(
    connection: Connection, stop_requested: Callable[[], bool]
) -> dict[str, Any] | None:
    """Inside-wasm wait for the parent's answer; ``None`` means the run is being stopped."""
    while True:
        if connection.poll(_CHILD_POLL_SECONDS):
            reply: dict[str, Any] = connection.recv()
            return reply
        if stop_requested():
            return None


class SubprocessCodemodeSandbox:
    """A :class:`~pi_codemode.CodemodeSandbox` whose VM lives in a child process.

    Same tool table and same :class:`~pi_codemode.CodemodeResult`; the difference is what
    survives a crash. Spawned with ``spawn`` (never ``fork``) because the parent may hold a
    wasmtime engine with internal threads.
    """

    def __init__(
        self,
        *,
        tools: Iterable[CodemodeTool] = (),
        timeout_ms: float = DEFAULT_TIMEOUT_MS,
        memory_limit_bytes: int | None = DEFAULT_MEMORY_LIMIT_BYTES,
        max_stack_size: int | None = DEFAULT_MAX_STACK_SIZE,
        interrupt_budget: int | None = None,
        on_child_started: Callable[[int], None] | None = None,
        start_method: str = "spawn",
    ) -> None:
        self._tools: dict[str, CodemodeTool] = {}
        self._timeout_ms = timeout_ms
        self._memory_limit_bytes = memory_limit_bytes
        self._max_stack_size = max_stack_size
        self._interrupt_budget = interrupt_budget
        self._on_child_started = on_child_started
        # Any start method has this surface; `spawn` is the only safe one (see the class docstring).
        self._context: Any = multiprocessing.get_context(start_method)
        self._closed = False
        for tool in tools:
            self.register_tool(tool)

    # ------------------------------------------------------------------ tool table
    def register_tool(self, tool: CodemodeTool) -> None:
        """Add a tool; a duplicate name is an error (same rule as the in-process sandbox)."""
        if tool.name in self._tools:
            raise ValueError(f'Tool "{tool.name}" is already registered')
        self._tools[tool.name] = tool

    def unregister_tool(self, name: str) -> bool:
        """Remove a tool by name."""
        return self._tools.pop(name, None) is not None

    @property
    def tools(self) -> list[CodemodeTool]:
        """Registered tools, in registration order."""
        return list(self._tools.values())

    def close(self) -> None:
        """Reject new executions (a child that is running is not affected)."""
        self._closed = True

    # ------------------------------------------------------------------ execution
    def execute(
        self,
        code: str,
        *,
        store: Mapping[str, object] | None = None,
        timeout_ms: float | None = None,
    ) -> CodemodeResult:
        """Run ``code`` in a child process; never raises for script failures."""
        if self._closed:
            raise RuntimeError("Sandbox is closed")
        deadline_ms = self._timeout_ms if timeout_ms is None else timeout_ms
        memory = shared_memory.SharedMemory(create=True, size=8)
        struct.pack_into("<I", _shared_buffer(memory), 0, 0)
        parent_end, child_end = self._context.Pipe(duplex=True)
        config = {
            "shm_name": memory.name,
            "code": code,
            "tools_json": json.dumps(
                [
                    {"name": tool.name, "jsName": tool.js_name, "description": tool.description}
                    for tool in self._tools.values()
                ]
            ),
            "globals_json": "[]",
            "store_json": json.dumps(
                {key: json.dumps(value) for key, value in (store or {}).items()}
            ),
            "memory_limit_bytes": self._memory_limit_bytes,
            "max_stack_size": self._max_stack_size,
        }
        process: BaseProcess = self._context.Process(
            target=_child_main, args=(child_end, config), name="pi-codemode-vm", daemon=True
        )
        started = time.perf_counter()
        try:
            process.start()
            child_end.close()  # the parent keeps only its end
            if self._on_child_started is not None and process.pid is not None:
                self._on_child_started(process.pid)
            return self._pump(parent_end, process, memory, deadline_ms, started)
        finally:
            parent_end.close()
            if process.is_alive():
                process.kill()
            process.join(timeout=2.0)
            memory.close()
            memory.unlink()

    # ------------------------------------------------------------------ parent pump
    def _pump(
        self,
        connection: Connection,
        process: BaseProcess,
        memory: Any,
        deadline_ms: float,
        started: float,
    ) -> CodemodeResult:
        output: list[CodemodeOutputItem] = []
        calls: list[CodemodeCall] = []
        pending: dict[int, tuple[CodemodeCall, float]] = {}
        failure: CodemodeError | None = None
        deadline = started + deadline_ms / 1000.0
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            if not connection.poll(min(remaining, 0.05)):
                if not process.is_alive():
                    failure = CodemodeError(
                        kind="sandbox",
                        message=f"the sandbox process exited with code {process.exitcode} "
                        "before the script settled",
                    )
                    break
                continue
            try:
                message: dict[str, Any] = connection.recv()
            except (EOFError, OSError):
                failure = CodemodeError(
                    kind="sandbox",
                    message=f"the sandbox process closed the channel (exit code {process.exitcode})",
                )
                break
            kind = message.get("type")
            if kind == _CALL:
                call_started = time.perf_counter()
                record: CodemodeCall | None = None
                if message["target"] == "tool":
                    record = CodemodeCall(name=message["name"])
                    calls.append(record)
                    pending[message["id"]] = (record, call_started)
                ok, payload = self._run_tool(message)
                if record is not None:
                    record.status = "ok" if ok else "error"
                    record.duration_ms = (time.perf_counter() - call_started) * 1000
                    pending.pop(message["id"], None)
                try:
                    connection.send(
                        {"type": _RESULT, "id": message["id"], "ok": ok, "payload": payload}
                    )
                except (BrokenPipeError, OSError, EOFError):
                    # The VM process died while the tool ran: report it instead of raising.
                    failure = CodemodeError(
                        kind="sandbox",
                        message="the sandbox process closed the channel while a tool call was running",
                    )
                    break
            elif kind == _OUTPUT:
                item = message["item"]
                output.append(
                    CodemodeOutputItem(
                        type=item["type"],
                        text=item.get("text"),
                        data=item.get("data"),
                        mime_type=item.get("mimeType"),
                        console=bool(item.get("console")),
                    )
                )
            elif kind == _DONE:
                return self._finish(message, output, calls)
            elif kind == _CRASH:
                failure = CodemodeError(
                    kind="sandbox", message=str(message.get("message", "child crashed"))
                )
                break

        # The deadline (or a child failure) already decided this execution: ask the child to
        # stop, keep whatever output it still sends, then kill it if it does not obey. The
        # host's verdict wins over a `done` that arrives meanwhile, like upstream's
        # `finish()`: the first terminal event is the one that counts.
        struct.pack_into("<I", _shared_buffer(memory), 0, 1)
        stop_at = time.perf_counter() + CHILD_SHUTDOWN_GRACE_SECONDS
        while time.perf_counter() < stop_at and process.is_alive():
            if not connection.poll(0.02):
                continue
            try:
                message = connection.recv()
            except (EOFError, OSError):
                break
            kind = message.get("type")
            if kind == _OUTPUT:
                item = message["item"]
                output.append(
                    CodemodeOutputItem(
                        type=item["type"],
                        text=item.get("text"),
                        data=item.get("data"),
                        mime_type=item.get("mimeType"),
                        console=bool(item.get("console")),
                    )
                )
                continue
            if kind == _CRASH and failure is None:
                failure = CodemodeError(
                    kind="sandbox", message=str(message.get("message", "child crashed"))
                )
                break
            break
        if process.is_alive():
            process.kill()
        process.join(timeout=2.0)
        for record, started_at in pending.values():
            if record is not None:
                record.status = "cancelled"
                record.duration_ms = (time.perf_counter() - started_at) * 1000
        if failure is not None:
            return CodemodeResult(ok=False, error=failure, output=output, calls=calls)
        return CodemodeResult(
            ok=False,
            error=CodemodeError(
                kind="timeout", message=f"Execution timed out after {int(deadline_ms)} ms"
            ),
            output=output,
            calls=calls,
        )

    def _run_tool(self, message: Mapping[str, Any]) -> tuple[bool, str | None]:
        tool = self._tools.get(str(message["name"]))
        if tool is None:
            return False, f'Unknown tool "{message["name"]}"'
        try:
            raw = message.get("args")
            args: object = None if raw is None else json.loads(raw)
            value = tool.execute(args)
            return True, None if value is None else json.dumps(value)
        except Exception as error:  # noqa: BLE001 - surfaced inside the script
            return False, str(error) or type(error).__name__

    @staticmethod
    def _finish(
        message: Mapping[str, Any],
        output: list[CodemodeOutputItem],
        calls: list[CodemodeCall],
    ) -> CodemodeResult:
        if message.get("ok"):
            payload = message.get("value")
            value: object = None
            if payload is not None:
                try:
                    value = json.loads(str(payload))
                except json.JSONDecodeError as error:
                    return CodemodeResult(
                        ok=False,
                        error=CodemodeError(
                            kind="sandbox", message=f"invalid return value: {error}"
                        ),
                        output=output,
                        calls=calls,
                    )
            return CodemodeResult(
                ok=True,
                value=value,
                output=output,
                calls=calls,
                store_writes=_parse_writes(message.get("writes")),
            )
        raw_error = message.get("error")
        name: str | None = None
        text = "script failed"
        if isinstance(raw_error, str):
            try:
                parsed = json.loads(raw_error)
            except json.JSONDecodeError:
                text = raw_error
            else:
                if isinstance(parsed, dict):
                    raw_name = parsed.get("name")
                    raw_message = parsed.get("message")
                    name = raw_name if isinstance(raw_name, str) else None
                    if isinstance(raw_message, str):
                        text = raw_message
        return CodemodeResult(
            ok=False,
            error=CodemodeError(kind="script", message=text, name=name),
            output=output,
            calls=calls,
        )


def _parse_writes(payload: object) -> CodemodeStoreWrites:
    from .parsing import parse_store_writes

    return parse_store_writes(payload if isinstance(payload, str) else None)
