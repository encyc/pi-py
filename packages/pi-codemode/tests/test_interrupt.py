"""A runaway script must stop — the hard requirement codemode is built on."""

from __future__ import annotations

import threading
import time

from pi_codemode import CodemodeSandbox

TIGHT_LOOP = "while (true) {}"
AWAIT_LOOP = "while (true) await null;"


def test_timeout_stops_tight_loop() -> None:
    """S5: the watchdog raises the in-VM interrupt flag; the deadline is reported as ``timeout``."""
    sandbox = CodemodeSandbox(timeout_ms=300)
    started = time.perf_counter()
    result = sandbox.execute(TIGHT_LOOP)
    elapsed = time.perf_counter() - started
    assert not result.ok
    assert result.error is not None
    assert result.error.kind == "timeout"
    assert "timed out after 300 ms" in result.error.message
    assert elapsed < 5.0, f"script was not stopped promptly ({elapsed:.2f}s)"


def test_timeout_stops_await_loop() -> None:
    """S6: ``while (true) await null`` is stopped while draining the promise-job queue."""
    sandbox = CodemodeSandbox(timeout_ms=300)
    started = time.perf_counter()
    result = sandbox.execute(AWAIT_LOOP)
    elapsed = time.perf_counter() - started
    assert not result.ok
    assert result.error is not None
    assert result.error.kind == "timeout"
    assert elapsed < 5.0, f"await loop was not stopped promptly ({elapsed:.2f}s)"


def test_abort_event_stops_tight_loop() -> None:
    """An abort event behaves like upstream's ``AbortSignal``."""
    abort = threading.Event()
    sandbox = CodemodeSandbox(timeout_ms=30_000)

    def trigger() -> None:
        time.sleep(0.2)
        abort.set()

    thread = threading.Thread(target=trigger, daemon=True)
    thread.start()
    started = time.perf_counter()
    result = sandbox.execute(TIGHT_LOOP, abort_event=abort)
    elapsed = time.perf_counter() - started
    thread.join(timeout=1.0)
    assert not result.ok
    assert result.error is not None
    assert result.error.kind == "aborted"
    assert elapsed < 5.0


def test_interrupt_budget_stops_tight_loop() -> None:
    """Upstream ``InlineCodemodeSandbox``'s budget: count host polls, then fail."""
    sandbox = CodemodeSandbox(timeout_ms=30_000, interrupt_budget=3_000)
    started = time.perf_counter()
    result = sandbox.execute(TIGHT_LOOP)
    elapsed = time.perf_counter() - started
    assert not result.ok
    assert result.error is not None
    assert result.error.kind == "timeout"
    assert "interrupt budget of 3000" in result.error.message
    assert elapsed < 5.0


def test_sandbox_is_reusable_after_a_timeout() -> None:
    sandbox = CodemodeSandbox(timeout_ms=300)
    assert not sandbox.execute(TIGHT_LOOP).ok
    later = sandbox.execute("return 1 + 1;", timeout_ms=5_000)
    assert later.ok, later.error
    assert later.value == 2
