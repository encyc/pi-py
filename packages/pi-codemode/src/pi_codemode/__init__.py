"""``pi_codemode`` — sandboxed JavaScript whose only capability is calling injected tools.

Port of the runtime half of `@earendil-works/pi-codemode`_ 1.3.0: the engine host
(``wasmtime`` + the same ``quickjs.wasm`` upstream pins), the upstream prelude loaded
verbatim from :mod:`pi_codemode.assets`, and a minimal synchronous execution path
(``tools.*`` trampoline in, ``text()``/``done()`` out).

Also here: deterministic replay (:meth:`~pi_codemode.CodemodeSandbox.start`), snapshots
(:meth:`~pi_codemode.QuickJSVm.snapshot`) and opt-in subprocess isolation
(:class:`~pi_codemode.SubprocessCodemodeSandbox`). See ``PORTING.md`` for what is left.

.. _@earendil-works/pi-codemode: https://github.com/earendil-works/pi
"""

from __future__ import annotations

from .assets import (
    PRELUDE_SHA256,
    WASM_SHA256,
    WASM_SIZE,
    WasmIntegrityError,
    load_prelude,
    load_wasm,
)
from .declarations import (
    DEFAULT_INPUT_SCHEMA_MAX_CHARS,
    ToolDeclaration,
    ToolLike,
    render_declarations,
    render_tool_output_type,
    render_tool_sample,
    render_tool_signature,
    schema_to_type,
)
from .host import (
    DEFAULT_MAX_STACK_SIZE,
    DETERMINISTIC_INTRINSICS,
    DrainReport,
    QuickJSEngine,
    QuickJSError,
    QuickJSSnapshot,
    QuickJSVm,
    ScriptError,
    default_engine,
    load_quickjs_wasm,
)
from .identifier import to_codemode_identifier
from .run import CodemodeRun
from .sandbox import (
    DEFAULT_MEMORY_LIMIT_BYTES,
    DEFAULT_TIMEOUT_MS,
    CodemodeSandbox,
)
from .source import (
    CODEMODE_OPTIONS_PREFIX,
    CODEMODE_SOURCE_GRAMMAR,
    MAX_TIMEOUT_MS,
    CodemodeSourceError,
    CodemodeSourceOptions,
    ParsedCodemodeSource,
    parse_codemode_source,
)
from .subprocess import SubprocessCodemodeSandbox
from .types import (
    CodemodeCall,
    CodemodeCallStatus,
    CodemodeDeclaredGlobal,
    CodemodeDeclaredTool,
    CodemodeDuring,
    CodemodeError,
    CodemodeErrorKind,
    CodemodeHostCall,
    CodemodeJsonSchema,
    CodemodeOutcome,
    CodemodeOutputItem,
    CodemodeResult,
    CodemodeRunOptions,
    CodemodeStoreWrites,
    CodemodeTool,
    ToolExecutor,
)

__version__ = "1.3.0"
__upstream_ref__ = "earendil-works/pi@v1.3.0"
__upstream_engine__ = "quickjs-wasi@3.6.2"

__all__ = [
    "CODEMODE_OPTIONS_PREFIX",
    "CODEMODE_SOURCE_GRAMMAR",
    "DEFAULT_INPUT_SCHEMA_MAX_CHARS",
    "DEFAULT_MAX_STACK_SIZE",
    "DEFAULT_MEMORY_LIMIT_BYTES",
    "DEFAULT_TIMEOUT_MS",
    "DETERMINISTIC_INTRINSICS",
    "PRELUDE_SHA256",
    "WASM_SHA256",
    "WASM_SIZE",
    "CodemodeCall",
    "CodemodeCallStatus",
    "CodemodeDeclaredGlobal",
    "CodemodeDeclaredTool",
    "CodemodeDuring",
    "CodemodeError",
    "CodemodeErrorKind",
    "CodemodeHostCall",
    "CodemodeJsonSchema",
    "CodemodeOutcome",
    "CodemodeOutputItem",
    "CodemodeResult",
    "CodemodeRun",
    "CodemodeRunOptions",
    "CodemodeSandbox",
    "CodemodeSourceError",
    "CodemodeSourceOptions",
    "CodemodeStoreWrites",
    "CodemodeTool",
    "DrainReport",
    "MAX_TIMEOUT_MS",
    "ParsedCodemodeSource",
    "QuickJSEngine",
    "QuickJSError",
    "QuickJSSnapshot",
    "QuickJSVm",
    "ScriptError",
    "SubprocessCodemodeSandbox",
    "ToolDeclaration",
    "ToolExecutor",
    "ToolLike",
    "WasmIntegrityError",
    "__upstream_engine__",
    "__upstream_ref__",
    "__version__",
    "default_engine",
    "load_prelude",
    "load_quickjs_wasm",
    "load_wasm",
    "parse_codemode_source",
    "render_declarations",
    "render_tool_output_type",
    "render_tool_sample",
    "render_tool_signature",
    "schema_to_type",
    "to_codemode_identifier",
]
