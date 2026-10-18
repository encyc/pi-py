"""Engine-level CI regressions — the Phase-0 assertions S1/S2/S3/S5b/S7/S8b/E1.

These pin the wasm ABI and the configuration traps found by the spike in
``recon/09-codemode-phase0-results.md``, so an engine upgrade cannot change them
silently.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from wasmtime import Trap

from pi_codemode import (
    DEFAULT_MAX_STACK_SIZE,
    WASM_SHA256,
    QuickJSEngine,
    default_engine,
    load_quickjs_wasm,
    load_wasm,
)
from pi_codemode.host import JOBS_PER_POLL


def test_instantiate_and_init() -> None:
    """S1: custom WASI + env imports, ``_initialize`` + ``qjs_init`` succeed."""
    engine = default_engine()
    vm = engine.new_vm(arm_interrupt=False)
    assert vm.quickjs_version() == "0.17.0"
    handle = vm.eval_code("1+2")
    assert not vm.is_exception(handle)
    assert vm.to_float(handle) == 3.0


def test_eval_roundtrip() -> None:
    """S2: numbers and strings survive the trip into and out of the VM."""
    vm = default_engine().new_vm(arm_interrupt=False)
    assert vm.to_str(vm.eval_code("'codemode:' + (6*7)")) == "codemode:42"
    assert vm.to_float(vm.eval_code("40 + 2")) == 42.0


def test_host_call_trampoline() -> None:
    """S3: JS calls a Python function and receives its return value."""
    vm = default_engine().new_vm(arm_interrupt=False)
    seen: list[tuple[str, float]] = []

    def pybridge(args: list[int]) -> int:
        seen.append((vm.to_str(args[0]) or "", vm.to_float(args[1])))
        return vm.number(42)

    bridge = vm.host_function("pybridge", pybridge)
    fn = vm.eval_code("(cb) => cb('hello', 7)")
    result = vm.call(fn, vm.undefined(), [bridge])
    assert vm.to_float(result) == 42.0
    assert seen == [("hello", 7.0)]


def test_memory_limit_is_catchable() -> None:
    """S7: the script can catch ``InternalError: out of memory`` and the VM survives."""
    vm = default_engine().new_vm(arm_interrupt=False, memory_limit_bytes=8 * 1024 * 1024)
    caught = vm.eval_code(
        "(() => { try { 'x'.repeat(64*1024*1024); return 'no-error'; } "
        "catch (e) { return 'caught:' + e.name + ':' + e.message; } })()"
    )
    assert vm.to_str(caught) == "caught:InternalError:out of memory"
    assert vm.to_float(vm.eval_code("2+2")) == 4.0


def test_stack_guard_gives_catchable_range_error() -> None:
    """E1: the default stack limit must produce a catchable ``RangeError``."""
    assert DEFAULT_MAX_STACK_SIZE == 256 * 1024
    vm = default_engine().new_vm(arm_interrupt=False)
    code = (
        "(() => { try { (function f(){ return f() + 1; })(); return 'no-error'; } "
        "catch (e) { return 'caught:' + e.name; } })()"
    )
    assert vm.to_str(vm.eval_code(code)) == "caught:RangeError"


def test_upstream_worker_stack_limit_would_trap() -> None:
    """E1: upstream's Node-worker value (512 KiB) exceeds wasmtime's 512 KiB wasm stack.

    Documented deviation: we keep 256 KiB (upstream's own ``INLINE_MAX_STACK_SIZE``).
    Raising the limit needs matching wasmtime config; without it the failure mode is an
    uncatchable trap, which is exactly what this test records.
    """
    vm = default_engine().new_vm(arm_interrupt=False, max_stack_size=512 * 1024)
    code = "(() => { (function f(){ return f() + 1; })(); })()"
    with pytest.raises(Trap):
        vm.eval_code(code)


def test_epoch_tick_hard_stops_wasm() -> None:
    """S8b: an epoch tick traps wasm code that never polls the host."""
    vm = default_engine().new_vm(arm_interrupt=False)
    vm.arm_epoch_deadline(1)
    vm.tick_epoch()
    with pytest.raises(Trap):
        vm.eval_code("while (true) {}")


def test_thread_watchdog_interrupts_tight_loop() -> None:
    """S5b: a Python thread flag stops ``while(true){}`` (GIL is released in wasm)."""
    stop = threading.Event()
    vm = default_engine().new_vm(should_interrupt=stop.is_set)

    def flip() -> None:
        time.sleep(0.2)
        stop.set()

    thread = threading.Thread(target=flip, daemon=True)
    thread.start()
    started = time.perf_counter()
    handle = vm.eval_code("while (true) {}", "<thread-interrupt>")
    elapsed = time.perf_counter() - started
    thread.join(timeout=1.0)
    assert vm.is_exception(handle)
    error = vm.pending_exception()
    assert error.message == "interrupted"
    assert elapsed < 3.0
    assert vm.interrupt_polls > 0
    # Upstream semantics: the VM stays usable after an interrupt.
    assert vm.to_float(vm.eval_code("1+2")) == 3.0


def test_job_drain_polls_between_jobs() -> None:
    """S6: ``while(true) await null`` is stoppable while single-stepping the job queue."""
    stop = threading.Event()
    vm = default_engine().new_vm(should_interrupt=stop.is_set)
    vm.eval_code("(async () => { while (true) await null; })()")

    def flip() -> None:
        time.sleep(0.2)
        stop.set()

    thread = threading.Thread(target=flip, daemon=True)
    thread.start()
    report = vm.drain_jobs(jobs_per_poll=JOBS_PER_POLL)
    thread.join(timeout=1.0)
    assert report.interrupted
    assert report.jobs > 0


def test_engine_reuses_one_compiled_module() -> None:
    """E4: compiling costs ~68 ms, so every VM of an engine shares the module."""
    engine = QuickJSEngine()
    first = engine.new_vm(arm_interrupt=False)
    second = engine.new_vm(arm_interrupt=False)
    assert first.engine.module is second.engine.module is engine.module
    assert engine.wasm_sha256.startswith("d4c9375f")


def test_load_quickjs_wasm_compiles_once_per_path() -> None:
    """``loadQuickJSWasm``: the compiled engine is cached per path (upstream caches by path)."""
    first = load_quickjs_wasm()
    second = load_quickjs_wasm()
    assert first is second
    assert first is default_engine()
    assert first.wasm_sha256 == WASM_SHA256
    assert first.new_vm(arm_interrupt=False).quickjs_version() == "0.17.0"


def test_load_quickjs_wasm_accepts_an_explicit_path(tmp_path: Path) -> None:
    copy = tmp_path / "quickjs.wasm"
    copy.write_bytes(load_wasm())
    engine = load_quickjs_wasm(copy)
    try:
        assert engine.wasm_sha256 == WASM_SHA256
        assert engine is not load_quickjs_wasm()
        assert engine is load_quickjs_wasm(copy)
    finally:
        # Keep the process-wide cache small; this engine is only used here.
        assert engine.new_vm(arm_interrupt=False).quickjs_version() == "0.17.0"
