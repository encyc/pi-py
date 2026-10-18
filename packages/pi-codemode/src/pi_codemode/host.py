"""Engine host: drives `vercel-labs/quickjs-wasi` ``quickjs.wasm`` from Python.

This is the piece upstream keeps in TypeScript (``dist/index.js`` of ``quickjs-wasi``
plus ``packages/codemode/src/runtime/vm.ts``). It exposes exactly what codemode needs
and nothing else:

* one wasm instance per :class:`QuickJSVm` (its own linear memory, i.e. its own VM)
* ``qjs_eval`` / ``qjs_call`` / host functions (the ``bridge`` trampoline)
* explicit memory / stack / interrupt configuration
* single-step promise-job draining, so a ``while (true) await null`` loop stays
  interruptible (upstream ``vm.ts:238-250``)
* wasm-level hard stops: epoch ticks (``Engine.increment_epoch``) and fuel

Configuration traps found by the Phase-0 spike (``recon/09-codemode-phase0-results.md``)
are handled here, not by callers:

* the stack limit is **always** set explicitly — without it deep recursion is an
  uncatchable ``wasm trap: call stack exhausted`` (E1);
* with epoch interruption enabled the store starts at deadline ``0``, which traps
  during ``instantiate`` itself, so :data:`EPOCH_IDLE` is armed before instantiating
  and the live deadline is only armed just before user code runs (S8b);
* instantiating + ``qjs_init`` consumes ~1.94 M fuel, so fuel budgets are replenished
  after construction rather than passed as a "script budget" (S8a).
"""

from __future__ import annotations

import struct
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from wasmtime import Config, Engine, Func, FuncType, Linker, Memory, Module, Store, ValType

from .assets import load_wasm, wasm_sha256

__all__ = [
    "DEFAULT_MAX_STACK_SIZE",
    "QuickJSSnapshot",
    "DETERMINISTIC_INTRINSICS",
    "EPOCH_IDLE",
    "INTRINSICS_ALL",
    "INTRINSIC_WEAK_REF",
    "DrainReport",
    "QuickJSEngine",
    "QuickJSError",
    "QuickJSVm",
    "ScriptError",
    "default_engine",
    "load_quickjs_wasm",
]

I32 = ValType.i32()
I64 = ValType.i64()
F64 = ValType.f64()

_ERRNO_SUCCESS: Final = 0
_ERRNO_BADF: Final = 8
_ERRNO_NOSYS: Final = 52
_CLOCK_REALTIME: Final = 0
_CLOCK_MONOTONIC: Final = 1

#: QuickJS's own stack guard, chosen by calibration rather than copied from upstream.
#:
#: Upstream's Node worker uses ``MAX_STACK_SIZE`` = 512 KiB; on wasmtime's default
#: 512 KiB wasm stack that value is reached *after* the wasm stack is gone, so deep
#: recursion is an uncatchable ``wasm trap: call stack exhausted``.  Measured (macOS
#: arm64, wasmtime 49): 512/384/1024 KiB trap, 256/128 KiB raise a catchable
#: ``RangeError``.  Upstream uses 256 KiB for the calling-thread sandbox
#: (``INLINE_MAX_STACK_SIZE``, ``inline.ts:42``), which is the same situation we are in.
#: Raising it needs matching wasmtime config: ``max_wasm_stack`` may not exceed
#: ``async_stack_size`` and wasmtime *panics* (aborting the process) when it does, so
#: this knob is deliberately not exposed by :class:`QuickJSEngine`.
DEFAULT_MAX_STACK_SIZE: Final = 256 * 1024

#: Epoch deadline used while the VM is being built.  Must be far away: with
#: ``epoch_interruption`` enabled wasmtime's default deadline is ``0`` and every
#: epoch check — including the ones inside ``instantiate`` — would trap.
EPOCH_IDLE: Final = 1 << 31

#: ``Intrinsics.ALL`` / ``Intrinsics.WEAK_REF`` from the ``quickjs-wasi`` API.
INTRINSICS_ALL: Final = 0xFFFF_FFFF
INTRINSIC_WEAK_REF: Final = 1 << 9
#: Mask upstream uses for deterministic runs (``vm.ts:178``): no ``WeakRef`` /
#: ``FinalizationRegistry``, everything else intact.
DETERMINISTIC_INTRINSICS: Final = INTRINSICS_ALL & ~INTRINSIC_WEAK_REF

#: Promise jobs executed between two interrupt polls while draining (``vm.ts:154``).
JOBS_PER_POLL: Final = 50


@dataclass(frozen=True, slots=True)
class QuickJSSnapshot:
    """A whole-VM snapshot: linear memory plus the pointers QuickJS lives behind.

    Enough to bring a fresh wasm instance back to the same state (upstream
    ``QuickJS.snapshot()`` / ``restore()``): the memory holds the runtime, the context, all
    JS objects, the promise job queue and the string intern table, so handles taken before
    the snapshot stay valid after the restore. Host functions are keyed by name, which is
    why :attr:`callbacks` records the names that have to be registered again.
    """

    memory: bytes
    stack_pointer: int
    runtime_ptr: int
    context_ptr: int
    callbacks: tuple[str, ...] = ()


class QuickJSError(RuntimeError):
    """The VM, the transport or the wasm module failed outside the script's control."""


@dataclass(slots=True)
class ScriptError:
    """A JavaScript exception, decoded into the shape upstream reports."""

    name: str | None
    message: str
    stack: str | None = None

    def describe(self) -> str:
        """``Name: message`` followed by the QuickJS frames (upstream format)."""
        head = f"{self.name}: {self.message}" if self.name else self.message
        return f"{head}\n{self.stack}" if self.stack else head


@dataclass(slots=True)
class DrainReport:
    """Outcome of draining the promise-job queue."""

    jobs: int = 0
    #: Set when a host-side poll (every :data:`JOBS_PER_POLL` jobs) asked to stop.
    interrupted: bool = False
    #: Set when a job failed; ``error`` then carries the decoded exception.
    error: ScriptError | None = None


class QuickJSEngine:
    """Owns one wasmtime :class:`~wasmtime.Engine` and the compiled ``quickjs.wasm``.

    Compiling the 637 KiB module costs ~68 ms while creating a store + instance from it
    costs ~5 ms (Phase-0 E4), so one engine is shared by every execution: that is the
    difference between an ~11 ms and a ~75 ms per-execution overhead.
    """

    def __init__(
        self,
        wasm: bytes | None = None,
        *,
        epoch_interruption: bool = True,
        consume_fuel: bool = False,
    ) -> None:
        data = load_wasm() if wasm is None else wasm
        self._wasm_digest = wasm_sha256(data)
        config = Config()
        if epoch_interruption:
            config.epoch_interruption = True
        if consume_fuel:
            config.consume_fuel = True
        self.epoch_interruption = epoch_interruption
        self.consume_fuel = consume_fuel
        self.engine = Engine(config)
        #: Compiled once; every :class:`QuickJSVm` instantiates from this module.
        self.module = Module(self.engine, data)

    @property
    def wasm_sha256(self) -> str:
        """Digest of the wasm binary this engine compiled."""
        return self._wasm_digest

    def increment_epoch(self) -> None:
        """Advance the engine epoch; every armed VM traps at its next epoch check.

        Thread-safe by design: this is what a watchdog calls to hard-stop wasm code
        that never polls :attr:`QuickJSVm.should_interrupt`.
        """
        self.engine.increment_epoch()

    def new_vm(self, **options: Any) -> QuickJSVm:
        """Create a fresh VM (one wasm instance) from the cached module."""
        return QuickJSVm(self, **options)


_engines: dict[tuple[str, bool, bool], QuickJSEngine] = {}
_engines_lock = threading.Lock()


def load_quickjs_wasm(
    path: str | Path | None = None,
    *,
    epoch_interruption: bool = True,
    consume_fuel: bool = False,
) -> QuickJSEngine:
    """Load and compile ``quickjs.wasm`` once per path (upstream ``loadQuickJSWasm``).

    Upstream returns a compiled ``WebAssembly.Module`` that callers hand to
    ``QuickJS.create({wasm})``; here the compiled artifact is bound to its wasmtime
    ``Engine``, so the equivalent value is a :class:`QuickJSEngine` — a ready-to-instantiate
    engine whose :meth:`~QuickJSEngine.new_vm` gives one VM per execution::

        sandbox = CodemodeSandbox(engine=load_quickjs_wasm())          # bundled 3.6.2
        sandbox = CodemodeSandbox(engine=load_quickjs_wasm("/opt/other.wasm"))

    ``path`` defaults to the bundled asset (digest-checked); an explicit path is used
    as given, with its digest exposed as :attr:`QuickJSEngine.wasm_sha256`. Results are
    cached per ``(path, epoch_interruption, consume_fuel)``, so repeated calls return the
    same engine and the ~68 ms compile happens once.
    """
    if path is None:
        key = ("<bundled>", epoch_interruption, consume_fuel)
        data: bytes | None = None
    else:
        resolved = str(Path(path).expanduser().resolve())
        key = (resolved, epoch_interruption, consume_fuel)
        data = Path(resolved).read_bytes()
    with _engines_lock:
        engine = _engines.get(key)
        if engine is None:
            engine = QuickJSEngine(
                data, epoch_interruption=epoch_interruption, consume_fuel=consume_fuel
            )
            _engines[key] = engine
        return engine


def default_engine() -> QuickJSEngine:
    """Process-wide engine for the bundled wasm (alias of :func:`load_quickjs_wasm`)."""
    return load_quickjs_wasm()


class QuickJSVm:
    """One QuickJS runtime + context living in its own wasm instance."""

    def __init__(
        self,
        engine: QuickJSEngine,
        *,
        should_interrupt: Callable[[], bool] | None = None,
        memory_limit_bytes: int | None = None,
        max_stack_size: int | None = DEFAULT_MAX_STACK_SIZE,
        intrinsics: int | None = None,
        timezone_offset_secs: int = 0,
        arm_interrupt: bool = True,
        capture_stdout: bool = False,
        initial_fuel: int | None = None,
        time_ns: Callable[[int], int] | None = None,
        random_bytes: Callable[[int], bytes] | None = None,
    ) -> None:
        self.engine = engine
        self.should_interrupt: Callable[[], bool] = should_interrupt or (lambda: False)
        #: WASI clock override: ``clock_id -> nanoseconds`` (deterministic runs use a virtual clock).
        self._time_ns = time_ns
        #: WASI randomness override: ``length -> bytes`` (deterministic runs use a seeded PRNG).
        self._random_bytes = random_bytes
        #: How often the VM asked the host whether to stop (calibrates interrupt budgets).
        self.interrupt_polls = 0
        #: Everything QuickJS wrote to fd 1/2, when ``capture_stdout`` is set.
        self.stdout: list[str] = []
        self._capture_stdout = capture_stdout
        self._disposed = False

        self.store = Store(engine.engine)
        if engine.epoch_interruption:
            self.store.set_epoch_deadline(EPOCH_IDLE)
        if engine.consume_fuel and initial_fuel is not None:
            self.store.set_fuel(initial_fuel)

        linker = Linker(engine.engine)
        linker.define(
            self.store,
            "env",
            "host_call",
            Func(self.store, FuncType([I32] * 5, [I32]), self._host_call),
        )
        linker.define(
            self.store,
            "env",
            "host_interrupt",
            Func(self.store, FuncType([], [I32]), self._host_interrupt),
        )
        linker.define(
            self.store,
            "env",
            "host_get_timezone_offset",
            Func(
                self.store,
                FuncType([I32, I32], [I32]),
                lambda _hi, _lo: timezone_offset_secs,
            ),
        )
        linker.define(
            self.store,
            "env",
            "host_promise_rejection",
            Func(self.store, FuncType([I32, I32, I32], []), lambda _a, _b, _c: None),
        )
        linker.define(
            self.store,
            "env",
            "host_module_normalize",
            Func(self.store, FuncType([I32, I32], [I32]), lambda _a, _b: 0),
        )
        linker.define(
            self.store,
            "env",
            "host_module_load",
            Func(self.store, FuncType([I32, I32], [I32]), lambda _a, _b: 0),
        )
        # WASI is provided by us rather than by ``Linker.define_wasi()``: codemode only
        # needs six calls, and owning them is what makes virtual clocks possible later.
        linker.define(
            self.store,
            "wasi_snapshot_preview1",
            "clock_time_get",
            Func(self.store, FuncType([I32, I64, I32], [I32]), self._clock_time_get),
        )
        linker.define(
            self.store,
            "wasi_snapshot_preview1",
            "fd_write",
            Func(self.store, FuncType([I32, I32, I32, I32], [I32]), self._fd_write),
        )
        linker.define(
            self.store,
            "wasi_snapshot_preview1",
            "fd_close",
            Func(self.store, FuncType([I32], [I32]), lambda _fd: _ERRNO_NOSYS),
        )
        linker.define(
            self.store,
            "wasi_snapshot_preview1",
            "fd_fdstat_get",
            Func(self.store, FuncType([I32, I32], [I32]), self._fd_fdstat_get),
        )
        linker.define(
            self.store,
            "wasi_snapshot_preview1",
            "fd_seek",
            Func(
                self.store,
                FuncType([I32, I64, I32, I32], [I32]),
                lambda _f, _o, _w, _r: _ERRNO_NOSYS,
            ),
        )
        linker.define(
            self.store,
            "wasi_snapshot_preview1",
            "random_get",
            Func(self.store, FuncType([I32, I32], [I32]), self._random_get),
        )

        self.instance = linker.instantiate(self.store, engine.module)
        self.exports = self.instance.exports(self.store)
        self.memory = cast(Memory, self.exports["memory"])

        self._f("_initialize")(self.store)
        if intrinsics is not None:
            init_rc = int(self._f("qjs_init2")(self.store, intrinsics))
        else:
            init_rc = int(self._f("qjs_init")(self.store))
        if init_rc != 0:
            raise QuickJSError(f"qjs_init failed with {init_rc}")

        self.callbacks: dict[str, Callable[[list[int]], int]] = {}
        if memory_limit_bytes is not None:
            self._f("qjs_set_memory_limit")(self.store, int(memory_limit_bytes))
        if max_stack_size is not None:
            # Always set explicitly: see the module docstring / Phase-0 E1.
            self._f("qjs_set_max_stack_size")(self.store, int(max_stack_size))
        if arm_interrupt:
            self._f("qjs_set_interrupt_handler")(self.store, 1)

    # ------------------------------------------------------------------ lifecycle
    def _f(self, name: str) -> Func:
        """Typed accessor for a wasm export (``InstanceExports`` is a union at type level)."""
        export = self.exports[name]
        if not isinstance(export, Func):
            raise QuickJSError(f"wasm export {name} is not a function")
        return export

    @property
    def disposed(self) -> bool:
        """Whether :meth:`dispose` already released this VM."""
        return self._disposed

    def dispose(self) -> None:
        """Release the wasm instance. The VM must not be used afterwards."""
        self._disposed = True
        self.callbacks.clear()

    def _check_live(self) -> None:
        if self._disposed:
            raise QuickJSError("QuickJS VM has been disposed")

    def quickjs_version(self) -> str:
        """Engine version reported by the wasm build (quickjs-ng version string)."""
        return self.read_cstring(int(self._f("qjs_get_quickjs_version")(self.store)))

    # ------------------------------------------------------------------ wasm memory
    def read(self, ptr: int, length: int) -> bytes:
        """Copy ``length`` bytes out of the guest linear memory."""
        return bytes(self.memory.read(self.store, ptr, ptr + length))

    def write(self, data: bytes, ptr: int) -> None:
        """Copy ``data`` into the guest linear memory at ``ptr``."""
        self.memory.write(self.store, data, ptr)

    def read_u32(self, ptr: int) -> int:
        """Read a little-endian ``u32`` from guest memory."""
        (value,) = struct.unpack("<I", self.read(ptr, 4))
        return int(value)

    def write_u32(self, ptr: int, value: int) -> None:
        """Write a little-endian ``u32`` to guest memory."""
        self.write(struct.pack("<I", value), ptr)

    def write_u64(self, ptr: int, value: int) -> None:
        """Write a little-endian ``u64`` to guest memory."""
        self.write(struct.pack("<Q", value), ptr)

    def read_cstring(self, ptr: int) -> str:
        """Read a NUL-terminated UTF-8 string from guest memory."""
        out = bytearray()
        while True:
            chunk = self.read(ptr + len(out), 64)
            end = chunk.find(b"\0")
            if end >= 0:
                out += chunk[:end]
                break
            out += chunk
        return bytes(out).decode("utf-8", "replace")

    def alloc(self, length: int) -> int:
        """Allocate ``length`` bytes in the guest heap; the caller frees them."""
        ptr = int(self._f("wasm_malloc")(self.store, length))
        if ptr == 0:
            raise QuickJSError(f"wasm_malloc({length}) failed")
        return ptr

    def free_memory(self, ptr: int) -> None:
        """Release guest memory previously returned by :meth:`alloc`."""
        self._f("wasm_free")(self.store, ptr)

    def write_cstring(self, text: str) -> int:
        """Write a NUL-terminated WTF-8 string into guest memory (caller frees)."""
        data = text.encode("utf-8", "surrogatepass") + b"\0"
        ptr = self.alloc(len(data))
        self.write(data, ptr)
        return ptr

    def write_js_string(self, text: str) -> tuple[int, int]:
        """Write a WTF-8 payload, returning ``(ptr, byte_length)`` (caller frees)."""
        data = text.encode("utf-8", "surrogatepass")
        ptr = self.alloc(len(data) + 1)
        self.write(data + b"\0", ptr)
        return ptr, len(data)

    # ------------------------------------------------------------------ JS values
    def new_string(self, text: str) -> int:
        """Create a JS string handle (owned by the caller)."""
        ptr, length = self.write_js_string(text)
        handle = int(self._f("qjs_new_string")(self.store, ptr, length))
        self.free_memory(ptr)
        return handle

    def number(self, value: float) -> int:
        """Create a JS number handle."""
        return int(self._f("qjs_new_number")(self.store, float(value)))

    def undefined(self) -> int:
        """Handle for ``undefined`` (a cached singleton inside the VM)."""
        return int(self._f("qjs_get_undefined")(self.store))

    def true(self) -> int:
        """Handle for ``true``."""
        return int(self._f("qjs_get_true")(self.store))

    def false(self) -> int:
        """Handle for ``false``."""
        return int(self._f("qjs_get_false")(self.store))

    def global_object(self) -> int:
        """Handle for the JS global object."""
        return int(self._f("qjs_get_global")(self.store))

    def is_undefined(self, handle: int) -> bool:
        """Whether ``handle`` is ``undefined``."""
        return bool(self._f("qjs_is_undefined")(self.store, handle))

    def is_exception(self, handle: int) -> bool:
        """Whether a call returned an exception instead of a value."""
        return bool(self._f("qjs_is_exception")(self.store, handle))

    def to_str(self, handle: int) -> str | None:
        """Read a JS value as a string, or ``None`` when it is not stringifiable."""
        if self.is_undefined(handle):
            return None
        ptr = int(self._f("qjs_get_string")(self.store, handle))
        if ptr == 0:
            return None
        text = self.read_cstring(ptr)
        self._f("qjs_free_cstring")(self.store, ptr)
        return text

    def to_float(self, handle: int) -> float:
        """Read a JS value as a float."""
        return float(self._f("qjs_get_float64")(self.store, handle))

    def to_bool(self, handle: int) -> bool:
        """Read a JS value as a boolean."""
        return bool(self._f("qjs_get_bool")(self.store, handle))

    def free(self, *handles: int) -> None:
        """Release JS value handles created by this host."""
        for handle in handles:
            self._f("qjs_free_value")(self.store, handle)

    def get_prop(self, obj: int, name: str) -> int:
        """Read ``obj[name]`` (the name is a C string, not a JS handle)."""
        return int(self._f("qjs_get_prop_string")(self.store, obj, self.write_cstring(name)))

    def pending_exception(self) -> ScriptError:
        """Decode the VM's pending exception (``qjs_get_exception``).

        Note: the handle returned by ``qjs_eval``/``qjs_call`` is *not* the exception
        object — it only answers "is this an exception". The value itself lives in the
        VM's pending-exception slot, exactly as in the JS bindings' ``throwIfException``.
        """
        handle = int(self._f("qjs_get_exception")(self.store))
        name = self.to_str(self.get_prop(handle, "name"))
        message = self.to_str(self.get_prop(handle, "message"))
        stack = self.to_str(self.get_prop(handle, "stack"))
        if name is None and message is None:
            rendered = self.to_str(self._stringify(handle))
            return ScriptError(
                name=None, message=rendered or "<unprintable exception>", stack=stack
            )
        return ScriptError(name=name, message=message or "", stack=stack)

    def _stringify(self, handle: int) -> int:
        """``String(value)`` inside the VM, used to describe non-Error throws."""
        string_fn = self.get_prop(self.global_object(), "String")
        return self.call(string_fn, self.undefined(), [handle])

    # ------------------------------------------------------------------ engine calls
    def eval_code(self, code: str, filename: str = "<eval>", flags: int = 0) -> int:
        """Evaluate ``code``; returns a JS value handle (owned by the caller)."""
        self._check_live()
        code_ptr, code_len = self.write_js_string(code)
        filename_ptr = self.write_cstring(filename)
        handle = int(self._f("qjs_eval")(self.store, code_ptr, code_len, filename_ptr, flags))
        self.free_memory(code_ptr)
        self.free_memory(filename_ptr)
        return handle

    def call(self, func: int, this: int, args: list[int]) -> int:
        """Call a JS function with borrowed argument handles."""
        self._check_live()
        ptr = self.alloc(4 * max(1, len(args)))
        for index, handle in enumerate(args):
            self.write_u32(ptr + 4 * index, handle)
        result = int(self._f("qjs_call")(self.store, func, this, len(args), ptr))
        self.free_memory(ptr)
        return result

    def host_function(self, name: str, callback: Callable[[list[int]], int]) -> int:
        """Create a JS function backed by ``callback``.

        The callback receives the argument handles (owned by the C trampoline, valid
        only for the duration of the call) and must return an owned handle.
        """
        self.register_host_callback(name, callback)
        ptr, length = self.write_js_string(name)
        handle = int(self._f("qjs_new_host_function")(self.store, ptr, length, 0))
        self.free_memory(ptr)
        return handle

    def is_job_pending(self) -> bool:
        """Whether the promise job queue has work (upstream ``qjs_is_job_pending``)."""
        return bool(self._f("qjs_is_job_pending")(self.store))

    def execute_pending_job(self) -> int:
        """Run one queued promise job; negative means it threw."""
        return int(self._f("qjs_execute_pending_job")(self.store))

    def drain_jobs(
        self,
        *,
        should_interrupt: Callable[[], bool] | None = None,
        jobs_per_poll: int = JOBS_PER_POLL,
        max_jobs: int | None = None,
    ) -> DrainReport:
        """Run promise jobs one at a time, polling for interrupts.

        ``executePendingJobs()``-style bulk draining cannot be interrupted, so jobs run
        one by one and the host is asked to stop every ``jobs_per_poll`` jobs — the same
        shape as upstream ``vm.ts:238-250``.
        """
        report = DrainReport()
        poll = should_interrupt or self.should_interrupt
        while self.is_job_pending():
            if self.execute_pending_job() < 0:
                report.error = self.pending_exception()
                return report
            report.jobs += 1
            if jobs_per_poll > 0 and report.jobs % jobs_per_poll == 0 and poll():
                report.interrupted = True
                return report
            if max_jobs is not None and report.jobs >= max_jobs:
                return report
        return report

    # ------------------------------------------------------------------ snapshot
    def snapshot(self) -> QuickJSSnapshot:
        """Capture the whole VM (only while it is idle; a running VM must not be snapshotted)."""
        self._check_live()
        return QuickJSSnapshot(
            memory=self.read(0, self.memory.data_len(self.store)),
            stack_pointer=int(cast(Any, self.exports["__stack_pointer"]).value(self.store)),
            runtime_ptr=int(self._f("qjs_get_runtime_ptr")(self.store)),
            context_ptr=int(self._f("qjs_get_context_ptr")(self.store)),
            callbacks=tuple(self.callbacks),
        )

    def restore(
        self,
        snapshot: QuickJSSnapshot,
        *,
        callbacks: Mapping[str, Callable[[list[int]], int]] | None = None,
    ) -> None:
        """Restore ``snapshot`` into this (fresh) VM and re-register its host callbacks."""
        self._check_live()
        page_size = 65536
        current = self.memory.data_len(self.store)
        if len(snapshot.memory) > current:
            pages = -(-(len(snapshot.memory) - current) // page_size)
            if self.memory.grow(self.store, pages) < 0:
                raise QuickJSError("could not grow the linear memory for the snapshot")
        self.write(snapshot.memory, 0)
        cast(Any, self.exports["__stack_pointer"]).set_value(self.store, snapshot.stack_pointer)
        self._f("qjs_set_runtime_and_context")(
            self.store, snapshot.runtime_ptr, snapshot.context_ptr
        )
        if callbacks is not None:
            self.callbacks = dict(callbacks)
        elif snapshot.callbacks:
            # Callers that keep their callbacks registered by name can restore without a map.
            missing = [name for name in snapshot.callbacks if name not in self.callbacks]
            if missing:
                raise QuickJSError(
                    "the snapshot references host callbacks that are not registered: "
                    + ", ".join(missing)
                )

    def register_host_callback(self, name: str, callback: Callable[[list[int]], int]) -> None:
        """Register a callback by name, without creating a JS function (upstream ``registerHostCallback``)."""
        self.callbacks[name] = callback

    # ------------------------------------------------------------------ hard stops
    def arm_epoch_deadline(self, ticks: int = 1) -> None:
        """Arm the epoch deadline; the next :meth:`tick_epoch` traps the wasm."""
        if not self.engine.epoch_interruption:
            raise QuickJSError("engine was built without epoch_interruption")
        self.store.set_epoch_deadline(int(ticks))

    def tick_epoch(self) -> None:
        """Advance the engine epoch (thread-safe hard stop for runaway wasm)."""
        self.engine.increment_epoch()

    def fuel(self) -> int:
        """Remaining fuel (only meaningful when the engine consumes fuel)."""
        return int(self.store.get_fuel())

    def set_fuel(self, fuel: int) -> None:
        """Replenish fuel.

        Note: instantiating the VM and running ``qjs_init`` already consumed ~1.94 M
        fuel (Phase-0 S8a), so this sets the *script's* budget, not the VM's.
        """
        self.store.set_fuel(int(fuel))

    # ------------------------------------------------------------------ wasm imports
    def _host_interrupt(self) -> int:
        self.interrupt_polls += 1
        return 1 if self.should_interrupt() else 0

    def _host_call(
        self, name_ptr: int, name_len: int, this_ptr: int, argc: int, argv_ptr: int
    ) -> int:
        name = self.read(name_ptr, name_len).decode("utf-8", "replace")
        args = (
            [self.read_u32(argv_ptr + 4 * index) for index in range(argc)]
            if argc and argv_ptr
            else []
        )
        callback = self.callbacks.get(name)
        if callback is None:
            message = self.new_string(f'Host callback "{name}" is not registered')
            self._f("qjs_throw")(self.store, message)
            return 0
        # The trampoline owns the handles it passed in; the returned handle is duped.
        return int(self._f("qjs_dup_value")(self.store, callback(args)))

    def _clock_time_get(self, clock_id: int, _precision: int, result_ptr: int) -> int:
        if self._time_ns is not None:
            self.write_u64(result_ptr, self._time_ns(clock_id))
            return _ERRNO_SUCCESS
        if clock_id == _CLOCK_REALTIME:
            self.write_u64(result_ptr, time.time_ns())
        elif clock_id == _CLOCK_MONOTONIC:
            self.write_u64(result_ptr, time.monotonic_ns())
        else:
            return _ERRNO_NOSYS
        return _ERRNO_SUCCESS

    def _fd_write(self, fd: int, iovs_ptr: int, iovs_len: int, nwritten_ptr: int) -> int:
        if fd not in (1, 2):
            return _ERRNO_BADF
        written = 0
        for index in range(iovs_len):
            buf_ptr = self.read_u32(iovs_ptr + index * 8)
            buf_len = self.read_u32(iovs_ptr + index * 8 + 4)
            if self._capture_stdout and buf_len:
                self.stdout.append(self.read(buf_ptr, buf_len).decode("utf-8", "replace"))
            written += buf_len
        self.write_u32(nwritten_ptr, written)
        return _ERRNO_SUCCESS

    def _fd_fdstat_get(self, fd: int, stat_ptr: int) -> int:
        if fd not in (1, 2):
            return _ERRNO_BADF
        self.write(bytes(24), stat_ptr)
        self.write(b"\x02", stat_ptr)  # fs_filetype = CHARACTER_DEVICE
        return _ERRNO_SUCCESS

    def _random_get(self, buf_ptr: int, buf_len: int) -> int:
        if self._random_bytes is not None:
            self.write(self._random_bytes(buf_len), buf_ptr)
            return _ERRNO_SUCCESS
        import os

        self.write(os.urandom(buf_len), buf_ptr)
        return _ERRNO_SUCCESS
