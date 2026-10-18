"""Minimal codemode execution path: prelude in, script out, ``tools.*`` in between.

Lifecycle of one :meth:`CodemodeSandbox.execute` (mirrors upstream
``src/runtime/execution.ts`` + ``src/runtime/vm.ts``, minus the worker thread):

1. a fresh :class:`~pi_codemode.host.QuickJSVm` (one wasm instance = one VM);
2. the upstream prelude is evaluated verbatim and called with the ``bridge`` host
   function plus the tool/global/store tables;
3. the script is compiled exactly like upstream — ``(async (tools, console) => {<code>})``
   — so ``return`` and top-level ``await`` work and stack lines match the input;
4. ``bridge`` messages are drained by a small pump: a pending ``tools.x()`` call is run
   by the host, answered through the prelude's ``settle()``, then the promise-job queue
   is stepped until the script finishes;
5. a watchdog thread enforces the deadline: it first raises the in-VM interrupt flag
   (``host_interrupt``), then, if the script keeps computing, advances the wasm engine
   epoch for a hard trap.

Everything crossing the boundary is JSON, like upstream (``protocol.ts:3-8``).
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from .assets import load_prelude
from .host import (
    DEFAULT_MAX_STACK_SIZE,
    QuickJSEngine,
    QuickJSError,
    QuickJSVm,
    default_engine,
)
from .identifier import to_codemode_identifier
from .parsing import parse_script_error, parse_store_writes
from .run import CodemodeRun, RunConfig, _deadline_seconds
from .types import (
    CodemodeCall,
    CodemodeError,
    CodemodeErrorKind,
    CodemodeOutputItem,
    CodemodeResult,
    CodemodeRunOptions,
    CodemodeTool,
)

__all__ = [
    "DEFAULT_MEMORY_LIMIT_BYTES",
    "DEFAULT_TIMEOUT_MS",
    "WATCHDOG_GRACE_SECONDS",
    "CodemodeSandbox",
    "to_codemode_identifier",
]

#: Upstream ``DEFAULT_TIMEOUT_MS`` (``src/runtime/execution.ts:49``).
DEFAULT_TIMEOUT_MS: Final = 300_000
#: Upstream coding-agent's ``memoryLimitBytes`` (``coding-agent/.../execute.ts:56``).
DEFAULT_MEMORY_LIMIT_BYTES: Final = 256 * 1024 * 1024
#: How long the in-VM interrupt gets to land before the wasm epoch is advanced.
WATCHDOG_GRACE_SECONDS: Final = 0.05

_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_RESERVED_GLOBALS: Final = frozenset(
    {"tools", "ALL_TOOLS", "console", "text", "image", "exit", "globalThis", "store", "load"}
)


@dataclass(slots=True)
class _PendingCall:
    """A ``tools.x()`` call the script is waiting on."""

    call_id: int
    target: str  # "tool" | "global"
    name: str
    args_json: str | None
    started_at: float
    record: CodemodeCall | None = None


@dataclass(slots=True)
class _ExecutionState:
    """Mutable state shared between the VM pump and the callbacks."""

    vm: QuickJSVm | None = None
    api: int = 0
    settle: int = 0
    run: int = 0
    stalled: int = 0
    done: tuple[bool, str | None, str | None] | None = None
    output: list[CodemodeOutputItem] = field(default_factory=list)
    calls: list[CodemodeCall] = field(default_factory=list)
    pending: dict[int, _PendingCall] = field(default_factory=dict)
    stop_requested: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    timed_out: bool = False
    budget_exhausted: bool = False
    job_error: str | None = None
    deadline_ms: float = 0.0


class CodemodeSandbox:
    """Runs model-written JavaScript where the only capability is calling tools.

    The skeleton executes scripts **synchronously on the calling thread** and tools are
    synchronous callables; an asyncio integration (async tools, ``execute_async``) is the
    next slice of work (see ``PORTING.md``).
    """

    def __init__(
        self,
        *,
        tools: Iterable[CodemodeTool] = (),
        globals: Iterable[CodemodeTool] = (),
        timeout_ms: float = DEFAULT_TIMEOUT_MS,
        memory_limit_bytes: int | None = DEFAULT_MEMORY_LIMIT_BYTES,
        max_stack_size: int | None = DEFAULT_MAX_STACK_SIZE,
        interrupt_budget: int | None = None,
        engine: QuickJSEngine | None = None,
        capture_stdout: bool = False,
    ) -> None:
        self._tools: dict[str, CodemodeTool] = {}
        self._globals: dict[str, CodemodeTool] = {}
        self._timeout_ms = timeout_ms
        self._memory_limit_bytes = memory_limit_bytes
        self._max_stack_size = max_stack_size
        self._interrupt_budget = interrupt_budget
        self._engine = engine
        self._capture_stdout = capture_stdout
        self._closed = False
        for tool in tools:
            self.register_tool(tool)
        self._check_globals(globals)
        for global_tool in globals:
            self._globals[global_tool.name] = global_tool

    # ------------------------------------------------------------------ tool table
    def register_tool(self, tool: CodemodeTool) -> None:
        """Add a tool; a duplicate name is an error (upstream ``registerTool``)."""
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

    @property
    def globals(self) -> list[CodemodeTool]:
        """Registered globals, in registration order."""
        return list(self._globals.values())

    @staticmethod
    def _check_globals(globals: Iterable[CodemodeTool]) -> None:
        """Global names must be identifiers or ``namespace.member`` (upstream rules)."""
        names: set[str] = set()
        namespaces: set[str] = set()
        for global_tool in globals:
            parts = global_tool.name.split(".")
            if len(parts) > 2 or not all(_IDENTIFIER.match(part) for part in parts):
                raise ValueError(f'Invalid global name "{global_tool.name}"')
            if parts[0] in _RESERVED_GLOBALS:
                raise ValueError(f'Invalid global name "{global_tool.name}"')
            if global_tool.name in names:
                raise ValueError(f'Global "{global_tool.name}" is already registered')
            if len(parts) == 2:
                namespaces.add(parts[0])
            names.add(global_tool.name)
        for namespace in namespaces:
            if namespace in names:
                raise ValueError(f'Global "{namespace}" conflicts with the namespace "{namespace}"')

    # ------------------------------------------------------------------ execution
    def execute(
        self,
        code: str,
        *,
        store: Mapping[str, object] | None = None,
        timeout_ms: float | None = None,
        abort_event: threading.Event | None = None,
    ) -> CodemodeResult:
        """Run ``code`` as an async function body; never raises for script failures.

        ``abort_event`` is the minimal analogue of upstream's ``AbortSignal``: setting it
        ends the execution with ``kind="aborted"``.
        """
        if self._closed:
            raise RuntimeError("Sandbox is closed")
        state = _ExecutionState()
        deadline_ms = self._timeout_ms if timeout_ms is None else timeout_ms
        state.deadline_ms = deadline_ms
        engine = self._engine or default_engine()

        def should_interrupt() -> bool:
            if state.stop_requested.is_set() or state.budget_exhausted:
                return True
            if abort_event is not None and abort_event.is_set():
                state.stop_requested.set()
                return True
            vm = state.vm
            if (
                vm is not None
                and self._interrupt_budget is not None
                and vm.interrupt_polls > self._interrupt_budget
            ):
                state.budget_exhausted = True
                return True
            return False

        try:
            vm = engine.new_vm(
                should_interrupt=should_interrupt,
                memory_limit_bytes=self._memory_limit_bytes,
                max_stack_size=self._max_stack_size,
                capture_stdout=self._capture_stdout,
            )
        except Exception as error:  # noqa: BLE001 - reported as a sandbox failure
            return CodemodeResult(ok=False, error=CodemodeError(kind="sandbox", message=str(error)))
        state.vm = vm

        try:
            self._prepare(state, vm, store)
            if state.api == 0:
                return CodemodeResult(
                    ok=False,
                    error=CodemodeError(kind="sandbox", message="prelude failed to load"),
                )
            watchdog = self._start_watchdog(state, vm, deadline_ms)
            try:
                return self._run_script(state, vm, code)
            finally:
                state.finished.set()
                if watchdog is not None:
                    watchdog.join(timeout=1.0)
        except Exception as error:  # noqa: BLE001 - traps and host bugs both land here
            return CodemodeResult(
                ok=False,
                error=self._classify_failure(state, str(error) or type(error).__name__),
                output=state.output,
                calls=state.calls,
            )
        finally:
            state.pending.clear()
            vm.dispose()

    def start(self, code: str, options: CodemodeRunOptions) -> CodemodeRun:
        """Run ``code`` deterministically, with the caller answering its calls.

        The same script, start time, seed, tools, globals and store, answered with the same
        results in the same ``seq`` order, make the same calls with the same arguments and
        produce the same output on any VM with the same fingerprint, so a host can replay a
        run after a restart.

        The VM runs in UTC, its clock reads ``started_at`` and then the ``now`` of the result
        being processed, its randomness comes from ``seed``, and ``WeakRef`` and
        ``FinalizationRegistry`` do not exist. The sandbox's own tools and globals are not
        used: the run declares its own and every call goes to ``options.on_call``. A run has
        no deadline unless ``options.timeout_ms`` is given; abort it through ``options.signal``
        to end it with an error the script cannot catch.
        """
        if self._closed:
            raise RuntimeError("Sandbox is closed")
        return CodemodeRun(
            code,
            options,
            RunConfig(
                engine=self._engine or default_engine(),
                memory_limit_bytes=self._memory_limit_bytes,
                max_stack_size=self._max_stack_size,
                interrupt_budget=self._interrupt_budget,
            ),
        )

    def close(self) -> None:
        """Reject new executions (in-flight ones are unaffected in this skeleton)."""
        self._closed = True

    # ------------------------------------------------------------------ internals
    def _prepare(
        self, state: _ExecutionState, vm: QuickJSVm, store: Mapping[str, object] | None
    ) -> None:
        """Load the upstream prelude and wire ``bridge``/``settle``/``run``/``stalled``."""
        api_fn = vm.eval_code(load_prelude(), "codemode-prelude.js")
        if vm.is_exception(api_fn):
            return
        bridge = vm.host_function("bridge", lambda args: self._bridge(state, vm, args))
        api = vm.call(
            api_fn,
            vm.undefined(),
            [
                bridge,
                vm.new_string(self._tools_json()),
                vm.new_string(self._globals_json()),
                vm.new_string(self._store_json(store)),
            ],
        )
        if vm.is_exception(api):
            return
        state.api = api
        state.settle = vm.get_prop(api, "settle")
        state.run = vm.get_prop(api, "run")
        state.stalled = vm.get_prop(api, "stalled")

    def _run_script(self, state: _ExecutionState, vm: QuickJSVm, code: str) -> CodemodeResult:
        # Compile the script the way upstream does (vm.ts:327): the wrapper shares line 1
        # with the script so reported line numbers match what the model wrote.
        script = vm.eval_code(f"(async (tools, console) => {{{code}\n}})", "codemode.js")
        if vm.is_exception(script):
            error = vm.pending_exception()
            return CodemodeResult(
                ok=False,
                error=CodemodeError(
                    kind="script", message=error.message, name=error.name, stack=error.stack
                ),
                output=state.output,
                calls=state.calls,
            )

        # All VM setup is done: arm the epoch so the watchdog can hard-stop the wasm.
        if vm.engine.epoch_interruption:
            vm.arm_epoch_deadline(1)

        started = vm.call(state.run, state.api, [script])
        if vm.is_exception(started):
            error = vm.pending_exception()
            state.job_error = error.describe()
        while state.done is None and state.job_error is None and not state.stop_requested.is_set():
            if state.pending:
                for call in list(state.pending.values()):
                    self._settle(vm, state, call)
            report = vm.drain_jobs(should_interrupt=lambda: state.stop_requested.is_set())
            if report.error is not None:
                state.job_error = report.error.describe()
                break
            if state.done is not None or state.stop_requested.is_set() or report.interrupted:
                break
            if report.jobs == 0 and not state.pending:
                # No tool pending and nothing queued: nothing can ever resume the script.
                vm.call(state.stalled, state.api, [])
                if state.done is None:
                    state.job_error = "script is waiting on a promise that can never settle"
                break
        return self._finish(state, vm)

    def _finish(self, state: _ExecutionState, vm: QuickJSVm) -> CodemodeResult:
        """Turn the bridge messages plus stop flags into a :class:`CodemodeResult`."""
        for call in state.pending.values():
            if call.record is not None:
                call.record.status = "cancelled"
                call.record.duration_ms = (time.perf_counter() - call.started_at) * 1000
        if state.done is not None:
            ok, payload, writes = state.done
            if ok:
                value: object = None
                if payload is not None:
                    try:
                        value = json.loads(payload)
                    except json.JSONDecodeError as exc:
                        return CodemodeResult(
                            ok=False,
                            error=CodemodeError(
                                kind="sandbox", message=f"invalid return value: {exc}"
                            ),
                            output=state.output,
                            calls=state.calls,
                        )
                return CodemodeResult(
                    ok=True,
                    value=value,
                    output=state.output,
                    calls=state.calls,
                    store_writes=parse_store_writes(writes),
                )
            error = parse_script_error(payload)
            if state.stop_requested.is_set() or state.timed_out or state.budget_exhausted:
                return CodemodeResult(
                    ok=False,
                    error=CodemodeError(
                        kind=self._stop_kind(state),
                        message=self._stop_message(state),
                        name=error.name,
                    ),
                    output=state.output,
                    calls=state.calls,
                )
            return CodemodeResult(
                ok=False,
                error=CodemodeError(
                    kind="script", message=error.message, name=error.name, stack=error.stack
                ),
                output=state.output,
                calls=state.calls,
            )
        message = state.job_error or "script did not finish"
        return CodemodeResult(
            ok=False,
            error=self._classify_failure(state, message),
            output=state.output,
            calls=state.calls,
        )

    def _stop_kind(self, state: _ExecutionState) -> CodemodeErrorKind:
        if state.timed_out:
            return "timeout"
        if state.budget_exhausted:
            return "timeout"
        return "aborted"

    def _stop_message(self, state: _ExecutionState) -> str:
        """Upstream-shaped reason for a stopped execution."""
        if state.timed_out:
            return f"Execution timed out after {int(state.deadline_ms)} ms"
        if state.budget_exhausted:
            return f"Execution exceeded its interrupt budget of {self._interrupt_budget}"
        return "Execution aborted"

    def _classify_failure(self, state: _ExecutionState, message: str) -> CodemodeError:
        if state.timed_out or state.budget_exhausted:
            return CodemodeError(kind="timeout", message=self._stop_message(state))
        if state.stop_requested.is_set():
            return CodemodeError(kind="aborted", message=self._stop_message(state))
        return CodemodeError(kind="sandbox", message=message)

    def _start_watchdog(
        self, state: _ExecutionState, vm: QuickJSVm, deadline_ms: float
    ) -> threading.Thread | None:
        """Enforce the deadline: flag first (catchable), epoch tick second (hard stop).

        ``None`` 与**非有限**的 deadline 都表示「无期限」（上游用 ``Number.POSITIVE_INFINITY``），
        此时**不起看门狗线程**：把 ``inf`` 交给 ``threading.Event.wait(timeout=)`` 会在
        ``waiter.acquire(True, timeout)`` 里抛 ``OverflowError: timestamp out of range`` ——
        而且抛在这个线程内部，主路径照常返回，**只有 stderr 有 traceback**（静默失效）。
        """
        limit = _deadline_seconds(deadline_ms)
        if limit is None:
            return None

        def watchdog() -> None:
            if state.finished.wait(limit):
                return
            state.timed_out = True
            state.stop_requested.set()
            if state.finished.wait(WATCHDOG_GRACE_SECONDS):
                return
            with contextlib.suppress(QuickJSError):
                vm.tick_epoch()

        thread = threading.Thread(target=watchdog, name="pi-codemode-watchdog", daemon=True)
        thread.start()
        return thread

    def _bridge(self, state: _ExecutionState, vm: QuickJSVm, args: list[int]) -> int:
        """The single host entry point the prelude talks to (``bridge(kind, a, b, c)``)."""
        kind = vm.to_str(args[0])
        if kind in ("call", "global"):
            call_id = int(vm.to_float(args[1]))
            name = vm.to_str(args[2]) or ""
            args_json = vm.to_str(args[3]) if len(args) > 3 else None
            record = CodemodeCall(name=name) if kind == "call" else None
            if record is not None:
                state.calls.append(record)
            state.pending[call_id] = _PendingCall(
                call_id=call_id,
                target="tool" if kind == "call" else "global",
                name=name,
                args_json=args_json,
                started_at=time.perf_counter(),
                record=record,
            )
        elif kind == "output":
            source = vm.to_str(args[1])
            if source == "image":
                state.output.append(
                    CodemodeOutputItem(
                        type="image", data=vm.to_str(args[2]), mime_type=vm.to_str(args[3])
                    )
                )
            else:
                state.output.append(
                    CodemodeOutputItem(
                        type="text", text=vm.to_str(args[2]), console=source == "console"
                    )
                )
        elif kind == "done":
            payload = None
            if len(args) > 2 and not vm.is_undefined(args[2]):
                payload = vm.to_str(args[2])
            state.done = (
                vm.to_bool(args[1]),
                payload,
                vm.to_str(args[3]) if len(args) > 3 else None,
            )
        return vm.undefined()

    def _settle(self, vm: QuickJSVm, state: _ExecutionState, call: _PendingCall) -> None:
        """Answer one pending call through the prelude's ``settle()``."""
        state.pending.pop(call.call_id, None)
        executor = (self._tools if call.target == "tool" else self._globals).get(call.name)
        ok = False
        payload: str | None = None
        if executor is None:
            payload = f'Unknown {call.target} "{call.name}"'
        else:
            try:
                args: object = None if call.args_json is None else json.loads(call.args_json)
                value = executor.execute(args)
                payload = None if value is None else json.dumps(value)
                ok = True
            except Exception as error:  # noqa: BLE001 - surfaced inside the script
                payload = str(error) or type(error).__name__
        if call.record is not None:
            call.record.status = "ok" if ok else "error"
            call.record.duration_ms = (time.perf_counter() - call.started_at) * 1000
        vm.call(
            state.settle,
            state.api,
            [
                vm.number(call.call_id),
                vm.true() if ok else vm.false(),
                vm.undefined() if payload is None else vm.new_string(payload),
                vm.undefined(),
            ],
        )

    def _tools_json(self) -> str:
        return json.dumps(
            [
                {"name": tool.name, "jsName": tool.js_name, "description": tool.description}
                for tool in self._tools.values()
            ]
        )

    def _globals_json(self) -> str:
        # ``spread`` 必须取自工具本身：多参全局（``searchTools(query, opts)``、
        # ``models.classify(model, context)``）靠它才能拿到**全部**实参。
        # 曾经这里硬编码 ``False``，于是多参全局**静默丢掉**第一个以外的实参
        # （``two("a", "b")`` → 宿主只收到 ``"a"``，且**不报错**）。
        return json.dumps(
            [{"name": tool.name, "spread": tool.spread} for tool in self._globals.values()]
        )

    @staticmethod
    def _store_json(store: Mapping[str, object] | None) -> str:
        serialized: dict[str, str] = {}
        for key, value in (store or {}).items():
            text = json.dumps(value)
            if text is not None:
                serialized[key] = text
        return json.dumps(serialized)
