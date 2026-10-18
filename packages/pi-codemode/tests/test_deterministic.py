"""Deterministic runs (``start()``): same seed, same results, same everything.

The whole point of :meth:`CodemodeSandbox.start` is replay, so the central test runs the
same script twice with the same ``started_at``/``seed``/answers and compares the complete
observable trace — calls, arguments, output, value, idle sequence and fingerprint.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from pi_codemode import (
    CodemodeDeclaredGlobal,
    CodemodeDeclaredTool,
    CodemodeHostCall,
    CodemodeOutcome,
    CodemodeResult,
    CodemodeRunOptions,
    CodemodeSandbox,
)

STARTED_AT = 1_700_000_000_000
SCRIPT = """
text('start=' + Date.now() + ' offset=' + new Date().getTimezoneOffset());
const a = await tools.echo({ n: Math.random() });
globalHelper();
const b = await tools.other({ m: Math.random() });
text('after=' + Date.now() + ' r=' + Math.random());
return { a, b, r: Math.random(), t: Date.now() };
"""

TOOLS = [
    CodemodeDeclaredTool(name="echo", description="Echo"),
    CodemodeDeclaredTool(name="other", description="Other"),
]
GLOBALS = [CodemodeDeclaredGlobal(name="globalHelper")]
ANSWERS: dict[str, list[Any]] = {
    "echo": [{"doubled": 2}],
    "other": [[1, 2, 3]],
    "globalHelper": [None],
}


def drive(
    *, seed: int, code: str = SCRIPT, answers: dict[str, list[Any]] | None = None
) -> tuple[CodemodeResult, dict[str, Any]]:
    """Run one deterministic execution, answering each call immediately."""
    sandbox = CodemodeSandbox()
    pending_answers = {name: list(values) for name, values in (answers or ANSWERS).items()}
    trace: dict[str, Any] = {"calls": [], "idle": [], "started": [], "output": []}
    holder: dict[str, Any] = {}
    delivered = 0

    def on_started(fingerprint: str) -> None:
        trace["started"].append(fingerprint)

    def on_idle(seq: int, issued: int) -> None:
        trace["idle"].append((seq, issued))

    def on_call(call: CodemodeHostCall) -> None:
        nonlocal delivered
        trace["calls"].append((call.id, call.target, call.name, call.args))
        values = pending_answers.get(call.name, [])
        value = values.pop(0) if values else None
        delivered += 1
        holder["run"].deliver(
            delivered, STARTED_AT + 1000 * delivered, call.id, CodemodeOutcome.success(value)
        )

    holder["run"] = sandbox.start(
        code,
        CodemodeRunOptions(
            started_at=STARTED_AT,
            seed=seed,
            tools=TOOLS,
            globals=GLOBALS,
            on_started=on_started,
            on_call=on_call,
            on_idle=on_idle,
        ),
    )
    result = holder["run"].result.result(timeout=10)
    trace["output"] = [(item.type, item.text, item.console) for item in result.output]
    trace["value"] = result.value
    trace["calls_recorded"] = [(call.name, call.status) for call in result.calls]
    trace["during"] = None if result.during is None else (result.during.seq, result.during.issued)
    trace["finished"] = holder["run"].finished
    return result, trace


def test_same_seed_produces_an_identical_trace() -> None:
    """The acceptance test: same seed, same start time, same answers → same everything."""
    first_result, first = drive(seed=7)
    second_result, second = drive(seed=7)
    assert first_result.ok, first_result.error
    assert second_result.ok, second_result.error
    assert first == second
    # …and the trace is not trivially empty.
    assert len(first["calls"]) == 3
    assert first["idle"][0] == (0, 1)  # the script had already issued its first call
    assert first["during"] == (3, 3)
    assert first["output"][0][1].startswith("start=1700000000000 offset=0")


def test_a_different_seed_changes_only_the_randomness() -> None:
    _, seeded = drive(seed=7)
    _, other = drive(seed=8)
    assert seeded["value"]["r"] != other["value"]["r"]
    assert seeded["calls"][0][3]["n"] != other["calls"][0][3]["n"]
    # The virtual clock is not affected by the seed.
    assert seeded["value"]["t"] == other["value"]["t"]
    assert seeded["started"] == other["started"]


def test_virtual_clock_reads_started_at_and_delivered_now() -> None:
    holder: dict[str, Any] = {}

    def on_call(call: CodemodeHostCall) -> None:
        holder["run"].deliver(1, STARTED_AT + 5_000, call.id, CodemodeOutcome.success(None))

    holder["run"] = CodemodeSandbox().start(
        "text('a=' + Date.now()); await tools.tick(); text('b=' + Date.now()); return Date.now();",
        CodemodeRunOptions(
            started_at=STARTED_AT,
            seed=1,
            tools=[CodemodeDeclaredTool(name="tick")],
            on_call=on_call,
        ),
    )
    result = holder["run"].result.result(timeout=10)
    assert result.ok, result.error
    assert [item.text for item in result.output] == [f"a={STARTED_AT}", f"b={STARTED_AT + 5_000}"]
    assert result.value == STARTED_AT + 5_000


def test_clock_never_goes_backwards() -> None:
    holder: dict[str, Any] = {}

    def on_call(call: CodemodeHostCall) -> None:
        # A resumed host may be behind: deliver an *earlier* now than the previous result.
        seq = len(delivered) + 1
        delivered.append(seq)
        holder["run"].deliver(
            seq, STARTED_AT + (5_000 if seq == 1 else 1_000), call.id, CodemodeOutcome.success(None)
        )

    delivered: list[int] = []
    holder["run"] = CodemodeSandbox().start(
        "await tools.a(); const first = Date.now(); await tools.b(); return [first, Date.now()];",
        CodemodeRunOptions(
            started_at=STARTED_AT,
            seed=1,
            tools=[CodemodeDeclaredTool(name="a"), CodemodeDeclaredTool(name="b")],
            on_call=on_call,
        ),
    )
    result = holder["run"].result.result(timeout=10)
    assert result.ok, result.error
    assert result.value == [STARTED_AT + 5_000, STARTED_AT + 5_000]


def test_results_are_applied_in_seq_order_whatever_arrives_first() -> None:
    """Two calls in flight: answering seq 2 first must not reorder what the script sees."""
    holder: dict[str, Any] = {}
    pending: list[CodemodeHostCall] = []
    idle: list[int] = []

    def on_call(call: CodemodeHostCall) -> None:
        pending.append(call)
        if len(pending) == 2:
            # Answer the second call first: the VM must wait for seq 1 before applying either.
            holder["run"].deliver(
                2, STARTED_AT + 2, pending[1].id, CodemodeOutcome.success("second")
            )
            holder["run"].deliver(
                1, STARTED_AT + 1, pending[0].id, CodemodeOutcome.success("first")
            )

    holder["run"] = CodemodeSandbox().start(
        "const [a, b] = await Promise.all([tools.a(), tools.b()]); return [a, b];",
        CodemodeRunOptions(
            started_at=STARTED_AT,
            seed=1,
            tools=[CodemodeDeclaredTool(name="a"), CodemodeDeclaredTool(name="b")],
            on_call=on_call,
            on_idle=lambda seq, issued: idle.append(seq),
        ),
    )
    result = holder["run"].result.result(timeout=10)
    assert result.ok, result.error
    assert result.value == ["first", "second"]
    assert idle == [0, 1, 2]  # the VM reported each applied result in seq order


def test_deliver_rejects_bad_answers() -> None:
    holder: dict[str, Any] = {}
    started = threading.Event()

    def on_call(call: CodemodeHostCall) -> None:
        run = holder["run"]
        with pytest.raises(ValueError, match="is not a finite number"):
            run.deliver(1, float("nan"), call.id, CodemodeOutcome.success(None))
        with pytest.raises(ValueError, match="not a positive integer used once"):
            run.deliver(0, STARTED_AT, call.id, CodemodeOutcome.success(None))
        with pytest.raises(ValueError, match="is not waiting for a result"):
            run.deliver(1, STARTED_AT, call.id + 99, CodemodeOutcome.success(None))
        run.deliver(1, STARTED_AT, call.id, CodemodeOutcome.success("ok"))
        with pytest.raises(ValueError, match="not a positive integer used once"):
            run.deliver(1, STARTED_AT, call.id, CodemodeOutcome.success("ok"))
        started.set()

    holder["run"] = CodemodeSandbox().start(
        "return await tools.a();",
        CodemodeRunOptions(
            started_at=STARTED_AT,
            seed=1,
            tools=[CodemodeDeclaredTool(name="a")],
            on_call=on_call,
        ),
    )
    assert started.wait(10)
    result = holder["run"].result.result(timeout=10)
    assert result.ok, result.error
    assert result.value == "ok"


def test_failures_reach_the_script_as_errors_with_properties() -> None:
    holder: dict[str, Any] = {}

    def on_call(call: CodemodeHostCall) -> None:
        holder["run"].deliver(
            1,
            STARTED_AT,
            call.id,
            CodemodeOutcome.failure("nope", {"structuredOutput": {"code": 7}}),
        )

    holder["run"] = CodemodeSandbox().start(
        "try { await tools.a(); return 'no-error'; } "
        "catch (e) { return [e.message, e.structuredOutput.code]; }",
        CodemodeRunOptions(
            started_at=STARTED_AT,
            seed=1,
            tools=[CodemodeDeclaredTool(name="a")],
            on_call=on_call,
        ),
    )
    result = holder["run"].result.result(timeout=10)
    assert result.ok, result.error
    assert result.value == ["nope", 7]
    assert [(call.name, call.status) for call in result.calls] == [("a", "error")]


def test_run_reports_store_writes_and_during() -> None:
    holder: dict[str, Any] = {}

    def on_call(call: CodemodeHostCall) -> None:
        holder["run"].deliver(1, STARTED_AT + 10, call.id, CodemodeOutcome.success(None))

    holder["run"] = CodemodeSandbox().start(
        "store('seen', 1); await tools.a(); return load('seen');",
        CodemodeRunOptions(
            started_at=STARTED_AT,
            seed=1,
            tools=[CodemodeDeclaredTool(name="a")],
            on_call=on_call,
        ),
    )
    result = holder["run"].result.result(timeout=10)
    assert result.ok, result.error
    assert result.store_writes.set == {"seen": 1}
    assert result.during is not None
    assert (result.during.seq, result.during.issued) == (1, 1)


def test_runs_use_deterministic_intrinsics_and_utc() -> None:
    holder: dict[str, Any] = {}
    holder["run"] = CodemodeSandbox().start(
        "return [typeof WeakRef, typeof FinalizationRegistry, new Date().getTimezoneOffset(), "
        "typeof Proxy, typeof Promise];",
        CodemodeRunOptions(started_at=STARTED_AT, seed=1),
    )
    result = holder["run"].result.result(timeout=10)
    assert result.ok, result.error
    assert result.value == ["undefined", "undefined", 0, "function", "function"]


def test_fingerprint_is_stable_across_runs() -> None:
    first = drive(seed=7)[1]["started"]
    second = drive(seed=7)[1]["started"]
    assert first == second
    assert first[0].startswith("quickjs-wasi 3.6.2, quickjs 0.17.0, prelude ")
    assert "deterministic 1" in first[0]


def test_run_timeout_and_abort() -> None:
    sandbox = CodemodeSandbox()
    timed_out = sandbox.start(
        "while (true) {}",
        CodemodeRunOptions(started_at=STARTED_AT, seed=1, timeout_ms=300),
    )
    result = timed_out.result.result(timeout=10)
    assert not result.ok
    assert result.error is not None
    assert result.error.kind == "timeout"

    signal = threading.Event()
    threading.Timer(0.2, signal.set).start()
    aborted = sandbox.start(
        "while (true) {}",
        CodemodeRunOptions(started_at=STARTED_AT, seed=1, signal=signal),
    )
    aborted_result = aborted.result.result(timeout=10)
    assert not aborted_result.ok
    assert aborted_result.error is not None
    assert aborted_result.error.kind == "aborted"


def test_run_stalled_script_fails_instead_of_hanging() -> None:
    run = CodemodeSandbox().start(
        "await new Promise(() => {});",
        CodemodeRunOptions(started_at=STARTED_AT, seed=1),
    )
    result = run.result.result(timeout=10)
    assert not result.ok
    assert result.error is not None
    assert "never settle" in result.error.message


def test_run_option_validation() -> None:
    sandbox = CodemodeSandbox()
    with pytest.raises(ValueError, match="startedAt"):
        sandbox.start("return 1;", CodemodeRunOptions(started_at=-1, seed=1))
    with pytest.raises(ValueError, match="32-bit unsigned"):
        sandbox.start("return 1;", CodemodeRunOptions(started_at=STARTED_AT, seed=2**32))
    with pytest.raises(ValueError, match="Tool names must be unique"):
        sandbox.start(
            "return 1;",
            CodemodeRunOptions(
                started_at=STARTED_AT,
                seed=1,
                tools=[CodemodeDeclaredTool(name="a"), CodemodeDeclaredTool(name="a")],
            ),
        )
    with pytest.raises(ValueError, match="Invalid global name"):
        sandbox.start(
            "return 1;",
            CodemodeRunOptions(
                started_at=STARTED_AT, seed=1, globals=[CodemodeDeclaredGlobal(name="text")]
            ),
        )


def test_run_is_rejected_when_the_sandbox_is_closed() -> None:
    sandbox = CodemodeSandbox()
    sandbox.close()
    with pytest.raises(RuntimeError, match="closed"):
        sandbox.start("return 1;", CodemodeRunOptions(started_at=STARTED_AT, seed=1))


def test_virtual_clock_and_prng_primitives() -> None:
    from pi_codemode.deterministic import VirtualClock, fnv1a_utf16, mulberry32

    # Upstream's FNV-1a over UTF-16 code units, as 8 hex digits.
    assert fnv1a_utf16("") == "811c9dc5"
    assert len(fnv1a_utf16("abc")) == 8
    draws = [mulberry32(7)() for _ in range(1)]
    assert draws == [mulberry32(7)()]
    clock = VirtualClock(started_at=STARTED_AT, seed=7)
    assert clock.creating is True
    assert clock.time_ns(0) == (7 + 1) * 1000  # QuickJS seeds Math.random() from this read
    assert clock.time_ns(1) == 0
    clock.creating = False
    assert clock.time_ns(0) == STARTED_AT * 1_000_000
    clock.advance_to(STARTED_AT + 10)
    assert clock.time_ns(1) == 10 * 1_000_000
    clock.advance_to(STARTED_AT + 5)  # never backwards
    assert clock.time_ns(0) == (STARTED_AT + 10) * 1_000_000
    assert len(clock.random_bytes(7)) == 7


def test_start_is_wired_to_the_sandbox_engine() -> None:
    """A run reuses the sandbox's engine (one compiled module per configuration)."""
    from pi_codemode import load_quickjs_wasm

    sandbox = CodemodeSandbox(engine=load_quickjs_wasm())
    run = sandbox.start("return 1;", CodemodeRunOptions(started_at=STARTED_AT, seed=1))
    result = run.result.result(timeout=10)
    assert result.ok, result.error
    assert result.value == 1
