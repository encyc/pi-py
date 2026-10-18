"""pi-chord: Python port of ``@earendil-works/chord``.

Batches are ported incrementally. The package currently mirrors:

- ``@earendil-works/chord/context`` → :mod:`pi_chord.context` (batch A)
- ``@earendil-works/chord/delta`` → :mod:`pi_chord.delta` (batch B;
  op layer **and** tracker)
- ``@earendil-works/chord/json`` → :mod:`pi_chord.json` (strict-JSON copy/validation)
- ``@earendil-works/chord/output`` (byte utilities only) → :mod:`pi_chord.output`

The upstream host half (host, contract, object, link, reload, registry, …) is
**not** ported and is not planned: it has zero consumers in upstream v1.3.0
outside ``packages/chord`` itself. See ``PORTING.md``.
"""

from __future__ import annotations

__version__ = "1.3.0"
__upstream_ref__ = "earendil-works/pi@v1.3.0"

# Upstream's root entry point re-exports `Context`/`ContextKey` as types
# (`chord/src/index.ts:3`), `type Draft` from delta (`:28`), and the JSON base —
# `copyJson` / `isJsonValue` / `JsonValue` (`:44`). Every other *value* export
# lives on a subpath, so `pi_chord` stays a thin root: the rest is reached through
# `pi_chord.context` / `pi_chord.delta` / `pi_chord.output`.
from .context import Context, ContextKey
from .delta import Draft
from .json import CopyJsonOptions, JsonRepresentation, JsonValue, copy_json, is_json_value
from .state import (
    AttachedReplicatedState,
    MutableReplicatedState,
    ReplicatedState,
    ReplicatedStateDelivery,
    ReplicatedStateSource,
    ReplicatedStateSourceAttachment,
    ReplicatedStateSourceFrame,
    ReplicatedStateSourceOptions,
    replicated_state,
)

__all__ = [
    "__version__",
    "__upstream_ref__",
    "AttachedReplicatedState",
    "Context",
    "ContextKey",
    "CopyJsonOptions",
    "Draft",
    "JsonRepresentation",
    "JsonValue",
    "MutableReplicatedState",
    "ReplicatedState",
    "ReplicatedStateDelivery",
    "ReplicatedStateSource",
    "ReplicatedStateSourceAttachment",
    "ReplicatedStateSourceFrame",
    "ReplicatedStateSourceOptions",
    "copy_json",
    "is_json_value",
    "replicated_state",
]
