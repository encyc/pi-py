"""内置 codemode 集成（``@earendil-works/pi-coding-agent`` 的 ``extensions/codemode/``）。

SDK 入口是 :func:`create_codemode_extension`。``renderer.ts`` **判定不做**（TUI），
``ui.ts`` / ``cli.ts`` 同理（见 ``PORTING.md`` 的偏离索引）。
"""

from __future__ import annotations

from .execute import (
    ACTIVE_CODEMODE_WORKERS,
    MAX_CONCURRENT_CODEMODE_WORKERS,
    CodemodeWorkerLimitError,
    create_discovery_globals,
    create_model_globals,
    execute_codemode,
)
from .index import (
    CodemodeExtensionOptions,
    create_codemode_extension,
    read_inline_budget,
    read_mode,
)
from .tool import (
    CODEMODE_DOCS_PATH,
    CODEMODE_SCHEMA,
    CODEMODE_STORE_ENTRY_TYPE,
    CODEMODE_TOOL_NAME,
    CodemodeDescriptionOptions,
    CodemodeNestedCall,
    CodemodeStoreEntryData,
    CodemodeToolDetails,
    CodemodeToolOptions,
    create_codemode_description,
    create_codemode_tool,
    create_codemode_tool_definition,
    get_codemode_callable_tools,
    get_script_reach,
    prepare_codemode_loadout,
    to_codemode_declaration,
)

__all__ = [
    "ACTIVE_CODEMODE_WORKERS",
    "CODEMODE_DOCS_PATH",
    "CODEMODE_SCHEMA",
    "CODEMODE_STORE_ENTRY_TYPE",
    "CODEMODE_TOOL_NAME",
    "MAX_CONCURRENT_CODEMODE_WORKERS",
    "CodemodeDescriptionOptions",
    "CodemodeExtensionOptions",
    "CodemodeNestedCall",
    "CodemodeStoreEntryData",
    "CodemodeToolDetails",
    "CodemodeToolOptions",
    "CodemodeWorkerLimitError",
    "create_codemode_description",
    "create_codemode_extension",
    "create_codemode_tool",
    "create_codemode_tool_definition",
    "create_discovery_globals",
    "create_model_globals",
    "execute_codemode",
    "get_codemode_callable_tools",
    "get_script_reach",
    "prepare_codemode_loadout",
    "read_inline_budget",
    "read_mode",
    "to_codemode_declaration",
]
