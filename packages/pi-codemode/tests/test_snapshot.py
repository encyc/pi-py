"""Snapshot / restore (upstream ``QuickJS.snapshot()`` / ``restore()``).

A snapshot is the whole linear memory plus the pointers QuickJS hides behind, so a fresh
wasm instance can be brought back to the same state: the runtime, the context, every JS
object, the promise job queue and the string intern table all live in that memory. Handles
taken before the snapshot stay valid after the restore, which is what makes it useful.

Host functions are keyed by name rather than by index, so a restored VM only needs the same
callbacks registered again (upstream's ``registerHostCallback``).
"""

from __future__ import annotations

from typing import Any

import pytest

from pi_codemode import QuickJSError, default_engine
from pi_codemode.assets import load_prelude


def test_snapshot_restore_keeps_values_and_host_callbacks() -> None:
    engine = default_engine()
    vm = engine.new_vm(arm_interrupt=False)
    vm.eval_code("globalThis.__answer = 41;")
    calls: list[float] = []

    def make_bridge(target: object) -> Any:
        """Callbacks read handles from *their* VM, so a restore needs freshly bound ones."""

        def bridge(args: list[int]) -> int:
            value = target.to_float(args[0])  # type: ignore[attr-defined]
            calls.append(value)
            return target.number(value + 1)  # type: ignore[attr-defined]

        return bridge

    bridge_handle = vm.host_function("bridge", make_bridge(vm))
    install = vm.eval_code("(cb) => { globalThis.__bump = (x) => cb(x); return 0; }")
    vm.call(install, vm.undefined(), [bridge_handle])
    assert vm.to_float(vm.eval_code("__bump(41)")) == 42

    snapshot = vm.snapshot()
    assert len(snapshot.memory) > 0
    assert snapshot.runtime_ptr > 0 and snapshot.context_ptr > 0
    assert snapshot.callbacks == ("bridge",)

    restored = engine.new_vm(arm_interrupt=False)
    restored.restore(snapshot, callbacks={"bridge": make_bridge(restored)})
    # State from before the snapshot is intact …
    assert restored.to_float(restored.eval_code("__answer + 1")) == 42
    # … and the host callback still works, because it is keyed by name.
    assert restored.to_float(restored.eval_code("__bump(10)")) == 11
    assert calls == [41.0, 10.0]


def test_restored_vm_can_finish_a_prelude_script() -> None:
    """The strongest case: snapshot mid-execution, restore elsewhere, keep going."""
    engine = default_engine()
    vm = engine.new_vm(arm_interrupt=False)
    record: dict[str, Any] = {"calls": [], "output": [], "done": []}

    def make_bridge(target: Any) -> Any:
        def bridge(args: list[int]) -> int:
            kind = target.to_str(args[0])
            if kind == "call":
                record["calls"].append(
                    (int(target.to_float(args[1])), target.to_str(args[2]), target.to_str(args[3]))
                )
            elif kind == "output":
                record["output"].append(target.to_str(args[2]))
            elif kind == "done":
                record["done"].append(target.to_bool(args[1]))
            return target.undefined()

        return bridge

    api_fn = vm.eval_code(load_prelude(), "codemode-prelude.js")
    api = vm.call(
        api_fn,
        vm.undefined(),
        [
            vm.host_function("bridge", make_bridge(vm)),
            vm.new_string('[{"name":"echo","jsName":"echo","description":""}]'),
            vm.new_string("[]"),
            vm.new_string("{}"),
        ],
    )
    run = vm.get_prop(api, "run")
    settle = vm.get_prop(api, "settle")
    script = vm.eval_code(
        "(async (tools, console) => { const v = await tools.echo({a: 1}); text('got ' + v.a); return 'ok'; })",
        "codemode.js",
    )
    vm.call(run, api, [script])
    assert len(record["calls"]) == 1  # suspended on the tool call

    snapshot = vm.snapshot()
    restored = engine.new_vm(arm_interrupt=False)
    restored.restore(snapshot, callbacks={"bridge": make_bridge(restored)})

    # The api object handle from the original VM is valid in the restored one.
    restored_settle = restored.get_prop(api, "settle")
    call_id, _, _ = record["calls"][0]
    restored.call(
        restored_settle,
        api,
        [
            restored.number(call_id),
            restored.true(),
            restored.new_string('{"a":42}'),
            restored.undefined(),
        ],
    )
    report = restored.drain_jobs()
    assert report.error is None
    assert record["output"] == ["got 42"]
    assert record["done"] == [True]
    assert settle != 0  # the same handle was used in both VMs


def test_restore_reports_missing_callbacks() -> None:
    vm = default_engine().new_vm(arm_interrupt=False)
    vm.host_function("bridge", lambda args: vm.undefined())
    snapshot = vm.snapshot()
    fresh = default_engine().new_vm(arm_interrupt=False)
    with pytest.raises(QuickJSError, match="not registered"):
        fresh.restore(snapshot)


def test_snapshot_grows_the_target_memory_when_needed() -> None:
    vm = default_engine().new_vm(arm_interrupt=False)
    vm.eval_code("globalThis.__big = 'x'.repeat(400000); 0;")
    snapshot = vm.snapshot()
    fresh = default_engine().new_vm(arm_interrupt=False)
    assert len(snapshot.memory) > fresh.memory.data_len(fresh.store)
    fresh.restore(snapshot, callbacks={})
    assert fresh.to_float(fresh.eval_code("__big.length")) == 400_000
