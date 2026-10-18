"""A deterministic run: the host answers every call, so a run can be replayed.

Port of ``CodemodeSandboxBase.start()`` (``execution.ts:530-566``) plus the deterministic
half of ``vm.ts`` (virtual clock, seeded randomness, ``seq``-ordered delivery, ``idle`` /
``during`` reporting). The VM only runs when the host hands it something, so the same
script, ``started_at``, ``seed``, tools, globals and store — answered with the same
results in the same ``seq`` order — make the same calls with the same arguments and
produce the same output on any VM with the same fingerprint.

The pump runs on its own thread so :meth:`CodemodeRun.deliver` can be called from the
host at any later point (after a restart, for instance), exactly like upstream's
``Promise``-based API.
"""

from __future__ import annotations

import contextlib
import json
import math
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Final

from .assets import load_prelude
from .deterministic import VirtualClock, fingerprint, fnv1a_utf16
from .host import DETERMINISTIC_INTRINSICS, QuickJSEngine, QuickJSVm
from .identifier import to_codemode_identifier
from .parsing import parse_script_error, parse_store_writes
from .types import (
    CodemodeCall,
    CodemodeDeclaredGlobal,
    CodemodeDeclaredTool,
    CodemodeDuring,
    CodemodeError,
    CodemodeErrorKind,
    CodemodeHostCall,
    CodemodeOutcome,
    CodemodeOutputItem,
    CodemodeResult,
    CodemodeRunOptions,
)

__all__ = ["CodemodeRun", "RunConfig"]

#: How long the in-VM interrupt gets before the wasm epoch is advanced (see ``sandbox``).
WATCHDOG_GRACE_SECONDS: Final = 0.05
#: Granularity of the pump's wait, so aborts and deadlines are noticed without a delivery.
_WAIT_SLICE_SECONDS: Final = 0.05


def _deadline_seconds(timeout_ms: float | None) -> float | None:
    """``timeout_ms`` → 秒；``None`` 与**非有限值**都表示「无 deadline」。

    上游用 ``Number.POSITIVE_INFINITY`` 表示「不设期限」（``execute.ts:510`` 的
    ``sourceOptions.timeoutMs ?? Number.POSITIVE_INFINITY``）。本端必须把 ``inf`` 与 ``None``
    一视同仁，**绝不能把 ``inf`` 交给 ``threading.Condition.wait(timeout=)``**：
    那会在 ``waiter.acquire(True, timeout)`` 里抛
    ``OverflowError: timestamp out of range for platform time_t`` ——
    而且它抛在**看门狗线程**里，主路径照常返回结果，**只有 stderr 有 traceback**，
    于是「超时保护失效」这件事是**静默**的（本仓 PORTING 的〈失效类偏离〉正抓这一类）。
    """
    if timeout_ms is None:
        return None
    seconds = timeout_ms / 1000.0
    return seconds if math.isfinite(seconds) else None


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Where a run gets its engine and limits from (the owning sandbox's settings)."""

    engine: QuickJSEngine
    memory_limit_bytes: int | None
    max_stack_size: int | None
    interrupt_budget: int | None


@dataclass(slots=True)
class _PendingCall:
    call_id: int
    target: str
    name: str
    args_json: str | None
    record: CodemodeCall | None
    started_at: float
    announced: bool = False


@dataclass(slots=True)
class _Delivery:
    seq: int
    now: float
    call_id: int
    ok: bool
    payload: str | None
    properties: str | None


class CodemodeRun:
    """One running :meth:`~pi_codemode.CodemodeSandbox.start` execution."""

    def __init__(self, code: str, options: CodemodeRunOptions, config: RunConfig) -> None:
        if not math.isfinite(options.started_at) or options.started_at < 0:
            raise ValueError(
                "startedAt must be a non-negative number of milliseconds since the epoch"
            )
        if (
            not isinstance(options.seed, int)
            or isinstance(options.seed, bool)
            or not (0 <= options.seed <= 0xFFFF_FFFF)
        ):
            raise ValueError("seed must be a 32-bit unsigned integer")
        _validate_declared_tools(options.tools)
        _validate_declared_globals(options.globals)

        self.result: Future[CodemodeResult] = Future()
        self._code = code
        self._options = options
        self._config = config
        self._clock = VirtualClock(started_at=int(options.started_at), seed=int(options.seed))

        self._condition = threading.Condition()
        self._pending: dict[int, _PendingCall] = {}
        self._buffer: dict[int, _Delivery] = {}
        self._delivered: set[int] = set()
        self._next_seq = 1
        self._issued = 0
        self._current_seq = 0
        self._output: list[CodemodeOutputItem] = []
        self._calls: list[CodemodeCall] = []
        self._done: tuple[bool, str | None, str | None] | None = None
        self._job_error: str | None = None

        self._finished = False
        self._decided = False
        self._timed_out = False
        self._aborted = False
        self._stop = threading.Event()
        self._finished_event = threading.Event()
        self._vm: QuickJSVm | None = None
        self._api = 0
        self._settle = 0
        self._stalled = 0
        self._fingerprint = ""

        self._thread = threading.Thread(target=self._pump, name="pi-codemode-run", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ public surface
    @property
    def finished(self) -> bool:
        """Whether the run has its outcome (the script ended, timed out or was aborted).

        Set when that is decided, before :attr:`result` settles; calls can no longer be
        answered afterwards. It does not mean the VM has exited.
        """
        return self._finished

    @property
    def fingerprint(self) -> str:
        """What the VM is, as reported to ``on_started`` (empty until the VM exists)."""
        return self._fingerprint

    def deliver(self, seq: int, now: float, id: int, outcome: CodemodeOutcome) -> None:  # noqa: A002
        """Answer call ``id`` as result number ``seq``.

        Results are numbered ``1, 2, …`` without gaps and the VM applies them strictly in
        that order, whatever order they arrive in: after a gap it waits for the missing
        number. While the VM processes this one, its clock reads ``now`` (ms since the
        epoch), or the previous value if ``now`` is earlier. Raises for a call that is not
        waiting for an answer, a ``seq`` used before, or a ``now`` that is not finite.
        """
        if not math.isfinite(now):
            raise ValueError(f"The clock value of result {seq} is not a finite number")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise ValueError(f"Result number {seq} is not a positive integer used once")
        if (
            not outcome.ok
            and outcome.properties is not None
            and not isinstance(outcome.properties, Mapping)
        ):
            raise ValueError(f"The properties of result {seq} are not an object")
        with self._condition:
            if self._finished:
                return
            if seq in self._delivered:
                raise ValueError(f"Result number {seq} is not a positive integer used once")
            call = self._pending.get(id)
            if call is None:
                raise ValueError(f"Call {id} is not waiting for a result")
            # Serialized before anything is recorded, so a value JSON cannot hold leaves the
            # call waiting for another answer. A rejection's payload is its message.
            if outcome.ok:
                payload = None if outcome.value is None else json.dumps(outcome.value)
            else:
                payload = outcome.message
            properties = (
                None if outcome.properties is None else json.dumps(dict(outcome.properties))
            )
            self._delivered.add(seq)
            del self._pending[id]
            if call.record is not None:
                call.record.status = "ok" if outcome.ok else "error"
                call.record.duration_ms = (time.perf_counter() - call.started_at) * 1000
            self._buffer[seq] = _Delivery(seq, now, id, outcome.ok, payload, properties)
            self._condition.notify_all()

    # ------------------------------------------------------------------ pump
    def _pump(self) -> None:
        try:
            self._run()
        except Exception as error:  # noqa: BLE001 - reported as a sandbox failure
            self._decide(
                CodemodeResult(
                    ok=False,
                    error=CodemodeError(kind="sandbox", message=str(error) or type(error).__name__),
                    output=self._output,
                    calls=self._calls,
                )
            )

    def _run(self) -> None:
        vm = self._config.engine.new_vm(
            should_interrupt=self._should_interrupt,
            memory_limit_bytes=self._config.memory_limit_bytes,
            max_stack_size=self._config.max_stack_size,
            intrinsics=DETERMINISTIC_INTRINSICS,
            timezone_offset_secs=0,
            time_ns=self._clock.time_ns,
            random_bytes=self._clock.random_bytes,
        )
        self._vm = vm
        self._clock.creating = False

        self._fingerprint = fingerprint(
            quickjs_version=vm.quickjs_version(),
            prelude_hash=fnv1a_utf16(load_prelude()),
            max_stack_size=self._config.max_stack_size,
            memory_limit_bytes=self._config.memory_limit_bytes,
            interrupt_budget=self._config.interrupt_budget,
        )
        watchdog = self._start_watchdog()
        try:
            self._run_script(vm)
        finally:
            self._finished_event.set()
            if watchdog is not None:
                watchdog.join(timeout=1.0)

    def _run_script(self, vm: QuickJSVm) -> None:
        options = self._options
        api_fn = vm.eval_code(load_prelude(), "codemode-prelude.js")
        if vm.is_exception(api_fn):
            error = vm.pending_exception()
            self._decide(
                CodemodeResult(
                    ok=False,
                    error=CodemodeError(kind="sandbox", message=error.describe()),
                    output=self._output,
                    calls=self._calls,
                )
            )
            return
        bridge = vm.host_function("bridge", lambda args: self._bridge(vm, args))
        api = vm.call(
            api_fn,
            vm.undefined(),
            [
                bridge,
                vm.new_string(_tools_json(options.tools)),
                vm.new_string(_globals_json(options.globals)),
                vm.new_string(_store_json(options.store)),
            ],
        )
        if vm.is_exception(api):
            error = vm.pending_exception()
            self._decide(
                CodemodeResult(
                    ok=False,
                    error=CodemodeError(kind="sandbox", message=error.describe()),
                    output=self._output,
                    calls=self._calls,
                )
            )
            return
        self._api = api
        self._settle = vm.get_prop(api, "settle")
        run = vm.get_prop(api, "run")
        self._stalled = vm.get_prop(api, "stalled")

        if options.on_started is not None:
            options.on_started(self._fingerprint)

        script = vm.eval_code(f"(async (tools, console) => {{{self._code}\n}})", "codemode.js")
        if vm.is_exception(script):
            error = vm.pending_exception()
            self._decide(
                CodemodeResult(
                    ok=False,
                    error=CodemodeError(
                        kind="script", message=error.message, name=error.name, stack=error.stack
                    ),
                    output=self._output,
                    calls=self._calls,
                )
            )
            return

        if vm.engine.epoch_interruption:
            vm.arm_epoch_deadline(1)

        started = vm.call(run, api, [script])
        if vm.is_exception(started):
            self._job_error = vm.pending_exception().describe()
        # Upstream (vm.ts:333-336): run, drain the promise jobs, then report idle(0).
        report = vm.drain_jobs(should_interrupt=self._should_interrupt)
        if report.error is not None:
            self._job_error = report.error.describe()
        self._announce_calls()
        self._notify_idle(0)
        self._pump_loop(vm)

    def _pump_loop(self, vm: QuickJSVm) -> None:
        while True:
            if self._done is not None or self._job_error is not None:
                break
            if self._timed_out or self._aborted or self._stop.is_set():
                break
            # 1) Apply the next delivered result, if the host answered the waiting call.
            if self._next_seq in self._buffer:
                self._apply(vm, self._buffer.pop(self._next_seq))
                self._next_seq += 1
                self._announce_calls()
                self._notify_idle(self._next_seq - 1)
                continue
            # 2) Run queued promise jobs: the script's continuations live there, and they are
            #    what turns `return 1;` into a `done` message.
            report = vm.drain_jobs(should_interrupt=self._should_interrupt)
            if report.error is not None:
                self._job_error = report.error.describe()
                break
            if self._done is not None:
                break
            self._announce_calls()
            if report.jobs:
                continue
            # 3) No queued work at all.
            if not self._pending:
                vm.call(self._stalled, self._api, [])
                if self._done is not None:
                    break
                if not self._pending:
                    self._job_error = (
                        "The script is waiting on a promise that can never settle: no tool call "
                        "is pending, and timers do not exist here."
                    )
                    break
                continue
            # 4) A call is waiting for the host.
            if not self._wait_for_delivery():
                break
        self._decide(self._finish())

    def _wait_for_delivery(self) -> bool:
        """Wait until a result arrives, the deadline expires or the run is aborted."""
        limit = _deadline_seconds(self._options.timeout_ms)
        deadline = None if limit is None else time.perf_counter() + limit
        with self._condition:
            while (
                self._next_seq not in self._buffer and not self._decided and not self._stop.is_set()
            ):
                if self._options.signal is not None and self._options.signal.is_set():
                    self._aborted = True
                    self._stop.set()
                    return False
                if deadline is not None and time.perf_counter() >= deadline:
                    self._timed_out = True
                    self._stop.set()
                    return False
                slice_seconds = _WAIT_SLICE_SECONDS
                if deadline is not None:
                    slice_seconds = max(0.001, min(slice_seconds, deadline - time.perf_counter()))
                self._condition.wait(slice_seconds)
        return not self._stop.is_set()

    def _apply(self, vm: QuickJSVm, delivery: _Delivery) -> None:
        """Apply one delivered result: advance the clock, settle the call, drain the jobs."""
        self._clock.advance_to(delivery.now)
        self._current_seq = delivery.seq
        payload_handle = (
            vm.undefined() if delivery.payload is None else vm.new_string(delivery.payload)
        )
        properties_handle = (
            vm.undefined() if delivery.properties is None else vm.new_string(delivery.properties)
        )
        vm.call(
            self._settle,
            self._api,
            [
                vm.number(delivery.call_id),
                vm.true() if delivery.ok else vm.false(),
                payload_handle,
                properties_handle,
            ],
        )
        report = vm.drain_jobs(should_interrupt=self._should_interrupt)
        if report.error is not None:
            self._job_error = report.error.describe()

    # ------------------------------------------------------------------ bridge
    def _bridge(self, vm: QuickJSVm, args: list[int]) -> int:
        kind = vm.to_str(args[0])
        if kind in ("call", "global"):
            call_id = int(vm.to_float(args[1]))
            name = vm.to_str(args[2]) or ""
            args_json = vm.to_str(args[3]) if len(args) > 3 else None
            record = CodemodeCall(name=name) if kind == "call" else None
            if record is not None:
                self._calls.append(record)
            self._issued += 1
            with self._condition:
                self._pending[call_id] = _PendingCall(
                    call_id=call_id,
                    target="tool" if kind == "call" else "global",
                    name=name,
                    args_json=args_json,
                    record=record,
                    started_at=time.perf_counter(),
                )
        elif kind == "output":
            source = vm.to_str(args[1])
            if source == "image":
                self._output.append(
                    CodemodeOutputItem(
                        type="image", data=vm.to_str(args[2]), mime_type=vm.to_str(args[3])
                    )
                )
            else:
                self._output.append(
                    CodemodeOutputItem(
                        type="text", text=vm.to_str(args[2]), console=source == "console"
                    )
                )
        elif kind == "done":
            payload = None
            if len(args) > 2 and not vm.is_undefined(args[2]):
                payload = vm.to_str(args[2])
            self._done = (
                vm.to_bool(args[1]),
                payload,
                vm.to_str(args[3]) if len(args) > 3 else None,
            )
        return vm.undefined()

    def _announce_calls(self) -> None:
        on_call = self._options.on_call
        if on_call is None:
            return
        with self._condition:
            fresh = [call for call in self._pending.values() if not call.announced]
            for call in fresh:
                call.announced = True
        for call in fresh:
            args: object = None if call.args_json is None else json.loads(call.args_json)
            on_call(
                CodemodeHostCall(
                    id=call.call_id,
                    target="tool" if call.target == "tool" else "global",
                    name=call.name,
                    args=args,
                )
            )

    def _notify_idle(self, seq: int) -> None:
        if self._options.on_idle is not None:
            self._options.on_idle(seq, self._issued)

    # ------------------------------------------------------------------ completion
    def _should_interrupt(self) -> bool:
        signal = self._options.signal
        if signal is not None and signal.is_set():
            self._aborted = True
            self._stop.set()
        return self._stop.is_set()

    def _start_watchdog(self) -> threading.Thread | None:
        limit = _deadline_seconds(self._options.timeout_ms)
        if limit is None:
            return None

        def watchdog() -> None:
            if self._finished_event.wait(limit):
                return
            self._timed_out = True
            self._stop.set()
            with self._condition:
                self._condition.notify_all()
            if self._finished_event.wait(WATCHDOG_GRACE_SECONDS):
                return
            vm = self._vm
            if vm is not None:
                with contextlib.suppress(Exception):  # the run is already ending
                    vm.tick_epoch()

        thread = threading.Thread(target=watchdog, name="pi-codemode-run-watchdog", daemon=True)
        thread.start()
        return thread

    def _finish(self) -> CodemodeResult:
        """Turn bridge messages plus stop flags into a result (upstream ``_finish``)."""
        during = (
            None
            if self._done is None
            else CodemodeDuring(seq=self._current_seq, issued=self._issued)
        )
        if self._done is not None:
            ok, payload, writes = self._done
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
                            output=self._output,
                            calls=self._calls,
                            during=during,
                        )
                return CodemodeResult(
                    ok=True,
                    value=value,
                    output=self._output,
                    calls=self._calls,
                    store_writes=parse_store_writes(writes),
                    during=during,
                )
            error = parse_script_error(payload)
            return CodemodeResult(
                ok=False,
                error=CodemodeError(
                    kind=self._stop_kind(),
                    message=self._stop_message() or error.message,
                    name=error.name,
                    stack=error.stack,
                ),
                output=self._output,
                calls=self._calls,
                during=during,
            )
        return CodemodeResult(
            ok=False,
            error=CodemodeError(
                kind=self._stop_kind(),
                message=self._stop_message() or self._job_error or "run did not finish",
            ),
            output=self._output,
            calls=self._calls,
            during=during,
        )

    def _stop_kind(self) -> CodemodeErrorKind:
        if self._timed_out or self._aborted or self._stop.is_set():
            return "timeout" if self._timed_out else "aborted"
        return "script"

    def _stop_message(self) -> str:
        if self._timed_out:
            return f"Execution timed out after {int(self._options.timeout_ms or 0)} ms"
        if self._aborted:
            return "Execution aborted"
        return ""

    def _decide(self, result: CodemodeResult) -> None:
        with self._condition:
            if self._decided:
                return
            self._decided = True
            self._finished = True
            self._stop.set()
            self._condition.notify_all()
        self._finished_event.set()
        self.result.set_result(result)


# ---------------------------------------------------------------- helpers
def _validate_declared_tools(tools: Sequence[CodemodeDeclaredTool]) -> None:
    names = [tool.name for tool in tools]
    if len(set(names)) != len(names):
        raise ValueError("Tool names must be unique")
    script_names = [tool.js_name or to_codemode_identifier(tool.name) for tool in tools]
    if len(set(script_names)) != len(script_names):
        raise ValueError("Tool script names must be unique")


def _validate_declared_globals(globals: Sequence[CodemodeDeclaredGlobal]) -> None:
    import re

    identifier = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
    reserved = {
        "tools",
        "ALL_TOOLS",
        "console",
        "text",
        "image",
        "exit",
        "globalThis",
        "store",
        "load",
    }
    names: set[str] = set()
    namespaces: set[str] = set()
    for global_tool in globals:
        parts = global_tool.name.split(".")
        if (
            len(parts) > 2
            or not all(identifier.match(part) for part in parts)
            or parts[0] in reserved
        ):
            raise ValueError(f'Invalid global name "{global_tool.name}"')
        if global_tool.name in names:
            raise ValueError(f'Global "{global_tool.name}" is already registered')
        if len(parts) == 2:
            namespaces.add(parts[0])
        names.add(global_tool.name)
    for namespace in namespaces:
        if namespace in names:
            raise ValueError(f'Global "{namespace}" conflicts with the namespace "{namespace}"')


def _tools_json(tools: Sequence[CodemodeDeclaredTool]) -> str:
    return json.dumps(
        [
            {
                "name": tool.name,
                "jsName": tool.js_name or to_codemode_identifier(tool.name),
                "description": tool.description,
            }
            for tool in tools
        ]
    )


def _globals_json(globals: Sequence[CodemodeDeclaredGlobal]) -> str:
    return json.dumps([{"name": tool.name, "spread": tool.spread} for tool in globals])


def _store_json(store: Mapping[str, object] | None) -> str:
    serialized: dict[str, str] = {}
    for key, value in (store or {}).items():
        text = json.dumps(value)
        if text is not None:
            serialized[key] = text
    return json.dumps(serialized)
