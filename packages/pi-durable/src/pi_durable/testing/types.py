"""Types of the runner-independent conformance suites — port of ``testing/types.ts``.

Two halves, matching upstream: the ``ExecutionEnv`` suite (batch A) and the ``Storage``
suite (batch B).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from pi_durable.env import ExecutionEnv
from pi_durable.types import Storage

__all__ = [
    "EnvConformanceAssertions",
    "EnvConformanceCase",
    "EnvConformanceOptions",
    "EnvConformanceProvider",
    "StorageConformanceAssertions",
    "StorageConformanceCase",
    "StorageConformanceOptions",
    "StorageConformanceProvider",
]


class EnvConformanceAssertions(Protocol):
    """The six assertion primitives the suites are written against.

    Upstream names them ``ok``, ``strictEqual``, ``deepEqual``, ``partialDeepEqual``,
    ``greaterThan`` and ``rejects``; ``assertions.ts`` adapts whichever test runner is at hand.
    """

    def ok(self, value: object, message: str | None = None) -> None:
        """Assert ``value`` is truthy."""
        ...

    def strict_equal(self, actual: object, expected: object) -> None:
        """Assert identity-like equality (upstream ``toBe``)."""
        ...

    def deep_equal(self, actual: object, expected: object) -> None:
        """Assert recursive structural equality (upstream ``toEqual``)."""
        ...

    def partial_deep_equal(self, actual: object, expected: object) -> None:
        """Assert ``expected`` matches ``actual`` recursively, allowing extra members."""
        ...

    def greater_than(self, actual: float, expected: float) -> None:
        """Assert ``actual > expected``."""
        ...

    async def rejects(self, operation: Awaitable[object], message_includes: str) -> None:
        """Assert ``operation`` raises, with ``message_includes`` in its message."""
        ...


#: Calls ``use`` once with an environment whose ``cwd`` is a fresh, empty, writable directory,
#: then cleans up.
EnvConformanceProvider = Callable[[Callable[[ExecutionEnv], Awaitable[None]]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class EnvConformanceOptions:
    """How :func:`~pi_durable.testing.env_conformance.create_env_conformance` builds its cases."""

    assertions: EnvConformanceAssertions
    with_env: EnvConformanceProvider
    #: Program and arguments that run a POSIX shell script from the next argument.
    shell: tuple[str, ...] = ("sh", "-c")
    #: Whether the shell's ``ln -s`` creates symbolic links.
    symlinks: bool = True
    #: Whether the environment implements the optional ``list_files``, which the suite then checks.
    list_files: bool = False


@dataclass(frozen=True, slots=True)
class EnvConformanceCase:
    """One executable specification case."""

    name: str
    #: Cases that wait for an environment's watch latency need longer than a runner's default.
    run: Callable[[], Awaitable[None]] = field(repr=False)
    timeout_ms: int | None = None


# --------------------------------------------------------------------------------------
# Storage 套件（上游 testing/types.ts 的 Storage 半边）
# --------------------------------------------------------------------------------------


class StorageConformanceAssertions(Protocol):
    """The six primitives the ``Storage`` suite is written against.

    Upstream names them ``ok``, ``strictEqual``, ``deepEqual``, ``partialDeepEqual``,
    ``greaterThan`` and ``rejects``; ``assertions.ts`` adapts whichever test runner is at hand.
    """

    def ok(self, value: object, message: str | None = None) -> None: ...

    def strict_equal(self, actual: object, expected: object) -> None: ...

    def deep_equal(self, actual: object, expected: object) -> None: ...

    def partial_deep_equal(self, actual: object, expected: object) -> None: ...

    def greater_than(self, actual: float, expected: float) -> None: ...

    async def rejects(self, operation: Awaitable[object], message_includes: str) -> None: ...


#: Calls ``use`` exactly once with a fresh ``Storage``, then cleans up.
StorageConformanceProvider = Callable[[Callable[[Storage], Awaitable[None]]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class StorageConformanceOptions:
    """How :func:`~pi_durable.testing.storage_conformance.create_storage_conformance` builds its cases."""

    assertions: StorageConformanceAssertions
    with_storage: StorageConformanceProvider


@dataclass(frozen=True, slots=True)
class StorageConformanceCase:
    """One executable specification case."""

    name: str
    run: Callable[[], Awaitable[None]] = field(repr=False)
