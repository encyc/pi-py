"""Result and tool shapes shared by the sandbox (mirrors upstream ``src/types.ts``)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from .identifier import to_codemode_identifier

__all__ = [
    "CodemodeCall",
    "CodemodeDeclaredGlobal",
    "CodemodeDeclaredTool",
    "CodemodeDuring",
    "CodemodeHostCall",
    "CodemodeOutcome",
    "CodemodeRunOptions",
    "CodemodeCallStatus",
    "CodemodeError",
    "CodemodeErrorKind",
    "CodemodeJsonSchema",
    "CodemodeOutputItem",
    "CodemodeResult",
    "CodemodeStoreWrites",
    "CodemodeTool",
    "ToolExecutor",
]

#: A JSON Schema document. Only used by the (not yet ported) declarations layer.
CodemodeJsonSchema: TypeAlias = Mapping[str, object] | bool

CodemodeCallStatus: TypeAlias = Literal["ok", "error", "cancelled"]

CodemodeErrorKind: TypeAlias = Literal["script", "timeout", "aborted", "sandbox"]

#: A tool runs synchronously in the skeleton; ``args`` is the JSON round-tripped value.
ToolExecutor: TypeAlias = Callable[[object], object]


@dataclass(slots=True)
class CodemodeOutputItem:
    """One item of the script's output, in the order the script produced it.

    ``text()`` and ``console.*`` produce text items (``console`` marks the latter);
    ``image()`` produces an image item whose ``data`` is base64.
    """

    type: Literal["text", "image"]
    text: str | None = None
    data: str | None = None
    mime_type: str | None = None
    console: bool = False


@dataclass(slots=True)
class CodemodeCall:
    """One tool call the script made, with its final status and duration."""

    name: str
    status: CodemodeCallStatus = "cancelled"
    duration_ms: float = 0.0


@dataclass(slots=True)
class CodemodeError:
    """Why an execution failed. ``kind`` mirrors upstream ``CodemodeErrorKind``."""

    kind: CodemodeErrorKind
    message: str
    name: str | None = None
    stack: str | None = None


@dataclass(slots=True)
class CodemodeStoreWrites:
    """Keys the script changed with ``store()``; only successful runs report writes."""

    set: dict[str, object] = field(default_factory=dict)
    delete: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CodemodeResult:
    """Outcome of one execution. ``execute()`` never raises for script failures."""

    ok: bool
    value: object = None
    error: CodemodeError | None = None
    output: list[CodemodeOutputItem] = field(default_factory=list)
    calls: list[CodemodeCall] = field(default_factory=list)
    store_writes: CodemodeStoreWrites = field(default_factory=CodemodeStoreWrites)
    #: Deterministic runs only: the result the VM was processing when the script ended.
    during: CodemodeDuring | None = None


@dataclass(slots=True)
class CodemodeDeclaredTool:
    """A tool as a ``start()`` run's script sees it: ``tools.<name>`` and its ``ALL_TOOLS`` entry.

    A run declares its tools; the host answers every call through
    :attr:`CodemodeRunOptions.on_call` instead of executing them.
    """

    name: str
    js_name: str | None = None
    description: str = ""


@dataclass(slots=True)
class CodemodeDeclaredGlobal:
    """A global a run's script sees; the same names ``CodemodeSandbox`` globals allow."""

    name: str
    spread: bool = False


@dataclass(slots=True)
class CodemodeHostCall:
    """One call the script made, answered with :meth:`CodemodeRun.deliver`."""

    #: The VM's own number for the call, ``1``, ``2``, … in the order the script made them.
    id: int
    target: Literal["tool", "global"]
    name: str
    #: What the script passed, after a JSON round trip; for spread globals the argument list.
    args: object = None


@dataclass(slots=True)
class CodemodeOutcome:
    """A call's answer. A failure becomes an ``Error`` with ``message`` inside the script.

    ``properties`` (JSON values) are copied onto that error, except ``name``/``message``/``stack``.
    """

    ok: bool
    value: object = None
    message: str = ""
    properties: Mapping[str, object] | None = None

    @classmethod
    def success(cls, value: object = None) -> CodemodeOutcome:
        """Answer a call successfully."""
        return cls(ok=True, value=value)

    @classmethod
    def failure(
        cls, message: str, properties: Mapping[str, object] | None = None
    ) -> CodemodeOutcome:
        """Answer a call with a rejection the script sees as an ``Error``."""
        return cls(ok=False, message=message, properties=properties)


@dataclass(slots=True)
class CodemodeDuring:
    """In a run: the result the VM was processing when the script finished (0 = its first run)."""

    seq: int
    issued: int


@dataclass(slots=True)
class CodemodeRunOptions:
    """Options of :meth:`CodemodeSandbox.start`.

    The same script, ``started_at``, ``seed``, tools, globals and store, answered with the
    same results in the same ``seq`` order, make the same calls with the same arguments and
    produce the same output on any VM with the same fingerprint.
    """

    #: The VM clock's start, in ms since the epoch: ``Date.now()`` until the first result arrives.
    started_at: int
    #: Seeds ``Math.random()`` and the VM's other randomness. A 32-bit unsigned integer.
    seed: int
    tools: Sequence[CodemodeDeclaredTool] = ()
    globals: Sequence[CodemodeDeclaredGlobal] = ()
    store: Mapping[str, object] | None = None
    #: Ends the run with an error the script cannot catch.
    signal: threading.Event | None = None
    #: Deadline of this run in ms. Default: none (not the sandbox's default).
    timeout_ms: float | None = None
    #: The VM's fingerprint, before the script runs and before any of its calls.
    on_started: Callable[[str], None] | None = None
    #: The script made a call. Answer it with :meth:`CodemodeRun.deliver`, now or later.
    on_call: Callable[[CodemodeHostCall], None] | None = None
    #: The VM processed result ``seq`` (``0``: the script's first run) and waits again,
    #: having issued ``issued`` calls in total.
    on_idle: Callable[[int, int], None] | None = None


@dataclass(slots=True)
class CodemodeTool:
    """A function the script reaches as ``tools.<js_name>(args)``.

    ``description``/``input_schema``/``output_schema`` are kept for the declarations
    layer (task follow-up); the sandbox does not validate values against them, exactly
    like upstream (``src/types.ts:11``).
    """

    name: str
    execute: ToolExecutor
    description: str = ""
    input_schema: CodemodeJsonSchema | None = None
    output_schema: CodemodeJsonSchema | None = None
    js_name: str = ""
    #: 脚本调用时**是否把全部实参**交给宿主。
    #:
    #: ``False``（默认）：只交第一个实参（``args[0]``）—— ``tools.<name>(x)`` 的形状。
    #: ``True``：交整个实参列表 —— ``searchTools(query, options)`` /
    #: ``models.classify(model, context)`` 这类**多参全局**需要它，上游的发现类全局与
    #: ``models.*`` 全部声明 ``spread: true``（``coding-agent/src/extensions/codemode/execute.ts:601`` 等）。
    #: 语义由 prelude 决定：``serialize(spread ? args : args[0])``（``prelude.js:155``）。
    spread: bool = False

    def __post_init__(self) -> None:
        if not self.js_name:
            self.js_name = to_codemode_identifier(self.name)
