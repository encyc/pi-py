"""Typed, invocation-scoped values — port of ``packages/chord/src/context/index.ts``.

Mirrors the upstream subpath ``@earendil-works/chord/context``: a context is an
immutable, explicitly-passed carrier of values (most importantly a cancellation
signal). Deriving a value never mutates the parent context.

Two deliberate differences from the TypeScript source, both recorded in
``PORTING.md``:

* Python has no ``AbortSignal``/``AbortController``. This module ships a minimal
  equivalent (:class:`AbortSignal`, :class:`AbortController`) with the same
  semantics that the port actually relies on: ``aborted``, ``reason``,
  ``add_listener``/``remove_listener`` and :meth:`AbortSignal.any`.
* ``awaitWithContext`` becomes :func:`await_with_context`, an ``async`` function
  (Python has no way to return a suspended awaitable from a plain function).
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast, final

__all__ = [
    "BACKGROUND_CONTEXT",
    "TODO_CONTEXT",
    "AbortController",
    "AbortError",
    "AbortSignal",
    "BaseContext",
    "CancelScope",
    "Context",
    "ContextKey",
    "await_with_context",
    "create_context_key",
    "with_abort_signal",
    "with_cancel",
    "with_context_value",
    "without_abort_signal",
]

T = TypeVar("T")
Value = TypeVar("Value")


@final
class AbortError(Exception):
    """Stand-in for the ``DOMException`` named ``AbortError`` used by upstream."""


@final
class AbortSignal:
    """Minimal ``AbortSignal``.

    Listeners are called in registration order when the signal aborts; a
    listener registered after the abort is *not* called (matching the DOM).
    """

    __slots__ = ("_aborted", "_listeners", "_reason")

    def __init__(self) -> None:
        self._aborted = False
        self._reason: object = None
        self._listeners: list[Callable[[], None]] = []

    @property
    def aborted(self) -> bool:
        """Whether the signal has aborted."""
        return self._aborted

    @property
    def reason(self) -> object:
        """Why the signal aborted; an :class:`AbortError` when no reason was given."""
        return self._reason

    def add_listener(self, listener: Callable[[], None]) -> None:
        """Register ``listener`` for the abort. Does nothing if already aborted."""
        if self._aborted:
            return
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[], None]) -> None:
        """Unregister ``listener``; unknown listeners are ignored."""
        with suppress(ValueError):
            self._listeners.remove(listener)

    def throw_if_aborted(self) -> None:
        """Raise the abort reason when the signal has aborted."""
        if self._aborted:
            raise abort_reason(self)

    def _abort(self, reason: object) -> None:
        if self._aborted:
            return
        self._aborted = True
        self._reason = reason if reason is not None else AbortError("The operation was aborted")
        listeners, self._listeners = self._listeners, []
        for listener in listeners:
            listener()

    @staticmethod
    def any(signals: Sequence[AbortSignal]) -> AbortSignal:
        """A signal aborted by the first of ``signals`` to abort, with its reason."""
        combined = AbortSignal()
        if not signals:
            return combined
        detachers: list[Callable[[], None]] = []

        def detach_all() -> None:
            while detachers:
                detachers.pop()()

        def forward(source: AbortSignal) -> Callable[[], None]:
            def on_abort() -> None:
                detach_all()
                combined._abort(source.reason)

            return on_abort

        def detacher(source: AbortSignal, listener: Callable[[], None]) -> Callable[[], None]:
            def detach() -> None:
                source.remove_listener(listener)

            return detach

        for source in signals:
            if source.aborted:
                detach_all()
                combined._abort(source.reason)
                return combined
            listener = forward(source)
            detachers.append(detacher(source, listener))
            source.add_listener(listener)
        return combined


@final
class AbortController:
    """Owner of one :class:`AbortSignal`; :meth:`abort` fires it once."""

    __slots__ = ("_signal",)

    def __init__(self) -> None:
        self._signal = AbortSignal()

    @property
    def signal(self) -> AbortSignal:
        """The signal this controller aborts."""
        return self._signal

    def abort(self, reason: object = None) -> None:
        """Abort the signal; later calls are ignored."""
        self._signal._abort(reason)


def abort_reason(signal: AbortSignal) -> BaseException:
    """The exception an aborted operation should raise for ``signal``."""
    reason = signal.reason
    if isinstance(reason, BaseException):
        return reason
    return AbortError("The operation was aborted")


@final
class ContextKey(Generic[T]):
    """Typed identity for one value carried by a :class:`Context`.

    The type parameter is the Python counterpart of upstream's type-only
    ``valueType`` marker: it makes keys of different value types
    non-interchangeable for a type checker. Runtime identity is :attr:`token`.
    """

    __slots__ = ("_description", "_token")

    def __init__(self, description: str) -> None:
        self._token = object()
        self._description = description

    @property
    def token(self) -> object:
        """Identity token; only the very same object identifies the same key."""
        return self._token

    @property
    def description(self) -> str:
        """Human-readable name, used by ``str(context)``."""
        return self._description


class Context(ABC):
    """Immutable invocation-scoped values passed explicitly through operations."""

    @property
    @abstractmethod
    def abort_signal(self) -> AbortSignal | None:
        """Cancellation of the invocation, when one was installed."""

    @abstractmethod
    def value(self, key: ContextKey[Value]) -> Value | None:
        """The value carried for ``key``, or ``None``."""

    @abstractmethod
    def __str__(self) -> str:
        """A description of how this context was derived."""


class BaseContext(Context):
    """Base implementation: the abort signal is itself carried as a context value."""

    @property
    def abort_signal(self) -> AbortSignal | None:
        return self.value(_ABORT_SIGNAL_CONTEXT_KEY)


_ABORT_SIGNAL_CONTEXT_KEY = ContextKey[AbortSignal | None]("chord.abortSignal")


@final
class _EmptyContext(BaseContext):
    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def value(self, key: ContextKey[Value]) -> Value | None:
        return None

    def __str__(self) -> str:
        return self._name


@final
class _ContextValue(BaseContext, Generic[T]):
    __slots__ = ("_key", "_parent", "_value")

    def __init__(self, parent: Context, key: ContextKey[T], value: T) -> None:
        self._parent = parent
        self._key = key
        self._value = value

    def value(self, key: ContextKey[Value]) -> Value | None:
        if key.token is self._key.token:
            return cast("Value", self._value)
        return self._parent.value(key)

    def __str__(self) -> str:
        return f"{self._parent}.WithValue({self._key.description or 'anonymous'})"


BACKGROUND_CONTEXT: Context = _EmptyContext("[Context BACKGROUND_CONTEXT]")
TODO_CONTEXT: Context = _EmptyContext("[Context TODO_CONTEXT]")


def create_context_key(description: str) -> ContextKey[T]:
    """Return a fresh key; the value type is inferred at the call site."""
    return ContextKey(description)


def with_context_value(key: ContextKey[T], value: T, parent: Context) -> Context:
    """Derive a context containing one additional or replaced value."""
    return _ContextValue(parent, key, value)


def with_abort_signal(signal: AbortSignal, context: Context) -> Context:
    """Derive a context cancelled by either the parent signal or ``signal``.

    The parent context remains unchanged.
    """
    parent_signal = context.abort_signal
    combined = signal if parent_signal is None else AbortSignal.any([parent_signal, signal])
    return with_context_value(_ABORT_SIGNAL_CONTEXT_KEY, combined, context)


def without_abort_signal(context: Context) -> Context:
    """Derive a context retaining all values except caller cancellation.

    Intended for mandatory cleanup only.
    """
    return with_context_value(_ABORT_SIGNAL_CONTEXT_KEY, None, context)


@final
@dataclass(frozen=True, slots=True)
class CancelScope:
    """An independently cancellable child context and the function that cancels it."""

    context: Context
    cancel: Callable[[object], None]


def with_cancel(context: Context) -> CancelScope:
    """Derive an independently cancellable child context."""
    controller = AbortController()
    return CancelScope(
        context=with_abort_signal(controller.signal, context), cancel=controller.abort
    )


async def await_with_context(awaitable: Awaitable[T], context: Context) -> T:
    """Observe ``awaitable`` until it settles or the invocation is cancelled.

    Cancellation raises in this waiter only; it does not cancel the underlying
    awaitable, which keeps running to completion. Like upstream — where calling an
    async function starts its promise before the waiter ever sees it — the
    awaitable is started here even when the context is already cancelled.
    """
    signal = context.abort_signal
    if signal is None:
        return await awaitable

    task = asyncio.ensure_future(awaitable)
    if signal.aborted:
        task.add_done_callback(_consume_outcome)
        raise abort_reason(signal)

    aborted = asyncio.Event()
    waiter = asyncio.ensure_future(aborted.wait())

    def on_abort() -> None:
        aborted.set()

    signal.add_listener(on_abort)
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            return task.result()
        task.add_done_callback(_consume_outcome)
        raise abort_reason(signal)
    finally:
        signal.remove_listener(on_abort)
        if not waiter.done():
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter


def _consume_outcome(task: asyncio.Future[Any]) -> None:
    """Retrieve the outcome of a detached task so its failure is not reported as unhandled."""
    if not task.cancelled():
        task.exception()
