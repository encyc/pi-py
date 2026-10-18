"""Deterministic-run primitives: virtual clock, seeded randomness, VM fingerprint.

Ported from ``runtime/vm.ts:71-125`` (FNV-1a prelude hash, mulberry32 PRNG, the virtual
WASI clock) and ``vm.ts:314-321`` (the fingerprint string). A run is deterministic
because the VM only runs when the host hands it something and has no timers or I/O; what
remains is fixed here:

* **Time** — every WASI clock read is virtual: ``started_at`` and then the ``now`` of the
  result being processed, never going backwards. While the context is created the clock
  reads a seed-derived value instead, because QuickJS seeds ``Math.random()`` from the
  first clock read and which read comes first is the engine's business.
* **Randomness** — ``random_get`` draws from a PRNG over ``seed ^ 0x9e3779b9``.
* **Timezone** — the caller uses ``timezone_offset_secs=0`` (UTC).
* **Garbage collection** — ``WeakRef``/``FinalizationRegistry`` are removed through the
  intrinsic mask (:data:`~pi_codemode.host.DETERMINISTIC_INTRINSICS`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .assets import WASM_PACKAGE_VERSION

__all__ = [
    "DETERMINISTIC_VERSION",
    "CLOCK_REALTIME",
    "CLOCK_MONOTONIC",
    "VirtualClock",
    "fingerprint",
    "fnv1a_utf16",
    "mulberry32",
]

#: Bumped whenever the VM or the prelude changes what a deterministic script observes.
DETERMINISTIC_VERSION = 1

CLOCK_REALTIME = 0
CLOCK_MONOTONIC = 1

_MASK32 = 0xFFFF_FFFF
_FNV_OFFSET = 0x811C_9DC5
_FNV_PRIME = 0x0100_0193
_MULBERRY_INCREMENT = 0x6D2B_79F5
_SEED_XOR = 0x9E37_79B9


def fnv1a_utf16(text: str) -> str:
    """FNV-1a over UTF-16 code units, as 8 hex digits: identifies the prelude in the fingerprint."""
    digest = _FNV_OFFSET
    encoded = text.encode("utf-16-be")
    for index in range(0, len(encoded), 2):
        unit = (encoded[index] << 8) | encoded[index + 1]
        digest ^= unit
        digest = (digest * _FNV_PRIME) & _MASK32
    return f"{digest:08x}"


def mulberry32(seed: int) -> Callable[[], int]:
    """Small, fast PRNG: one 32-bit draw per call (upstream ``prng``)."""
    state = seed & _MASK32

    def draw() -> int:
        nonlocal state
        state = (state + _MULBERRY_INCREMENT) & _MASK32
        first = ((state ^ (state >> 15)) * (1 | state)) & _MASK32
        second = ((first ^ (first >> 7)) * (61 | first)) & _MASK32
        value = ((first + second) & _MASK32) ^ first
        return (value ^ (value >> 14)) & _MASK32

    return draw


@dataclass
class VirtualClock:
    """WASI clock and randomness for one deterministic run."""

    started_at: int
    seed: int
    #: True while the QuickJS context is being created (see the module docstring).
    creating: bool = True
    now: int = 0
    _random: Callable[[], int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.seed &= _MASK32
        self.now = self.started_at
        self._random = mulberry32(self.seed ^ _SEED_XOR)

    def time_ns(self, clock_id: int) -> int:
        """Nanoseconds for ``clock_time_get`` (upstream ``deterministicWasi``)."""
        if self.creating:
            # QuickJS seeds Math.random() from this reading in microseconds and turns 0 into 1,
            # so seed s reads s + 1.
            return (self.seed + 1) * 1000 if clock_id == CLOCK_REALTIME else 0
        if clock_id == CLOCK_REALTIME:
            return round(self.now) * 1_000_000
        return round(self.now - self.started_at) * 1_000_000

    def random_bytes(self, length: int) -> bytes:
        """``random_get`` bytes, one 32-bit draw per four bytes."""
        out = bytearray(length)
        for index in range(0, length, 4):
            value = self._random()
            for byte in range(4):
                if index + byte < length:
                    out[index + byte] = (value >> (byte * 8)) & 0xFF
        return bytes(out)

    def advance_to(self, now: float) -> None:
        """Move the clock to ``now``, never backwards (a resumed host may be behind)."""
        self.now = max(self.now, round(now))


def fingerprint(
    *,
    quickjs_version: str,
    prelude_hash: str,
    max_stack_size: int | None,
    memory_limit_bytes: int | None,
    interrupt_budget: int | None,
) -> str:
    """What the VM is: engine versions, prelude hash, limits (upstream ``vm.ts:319``)."""

    def or_none(value: int | None) -> str:
        return "none" if value is None else str(value)

    return (
        f"quickjs-wasi {WASM_PACKAGE_VERSION}, quickjs {quickjs_version}, prelude {prelude_hash}, "
        f"deterministic {DETERMINISTIC_VERSION}, stack {or_none(max_stack_size)}, "
        f"heap {or_none(memory_limit_bytes)}, budget {or_none(interrupt_budget)}"
    )
