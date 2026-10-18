"""Parsing of the JSON the prelude sends across the bridge (upstream ``execution.ts:104-143``).

The prelude runs in the same VM as the script, so a script that patches built-ins can make
it send malformed payloads; every payload is validated here and a failure becomes a
``sandbox`` error instead of a corrupt result.
"""

from __future__ import annotations

import json

from .host import ScriptError
from .types import CodemodeStoreWrites

__all__ = ["BridgeError", "parse_bridge_json", "parse_script_error", "parse_store_writes"]


class BridgeError(ValueError):
    """The payload the VM sent does not decode into the shape the host requires."""


def parse_bridge_json(payload: str, what: str) -> object:
    """Decode one bridge payload, naming the payload in the error."""
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise BridgeError(f"{what} is not valid JSON") from error


def parse_script_error(payload: str | None) -> ScriptError:
    """Decode the prelude's ``{name?, message, stack?}`` error JSON."""
    if payload is None:
        return ScriptError(name=None, message="script failed")
    parsed = parse_bridge_json(payload, "script error")
    if not isinstance(parsed, dict):
        raise BridgeError("script error is not an object")
    name = parsed.get("name")
    message = parsed.get("message")
    stack = parsed.get("stack")
    if not isinstance(message, str):
        raise BridgeError("script error is malformed")
    if name is not None and not isinstance(name, str):
        raise BridgeError("script error is malformed")
    if stack is not None and not isinstance(stack, str):
        raise BridgeError("script error is malformed")
    return ScriptError(name=name, message=message, stack=stack)


def parse_store_writes(payload: str | None) -> CodemodeStoreWrites:
    """Decode the prelude's ``[[key, json], [key]]`` write list."""
    writes = CodemodeStoreWrites()
    if payload is None:
        return writes
    entries = parse_bridge_json(payload, "store writes")
    if not isinstance(entries, list):
        raise BridgeError("store writes are not an array")
    for entry in entries:
        if not isinstance(entry, list) or not entry or not isinstance(entry[0], str):
            raise BridgeError("store writes contain a malformed entry")
        key = entry[0]
        if len(entry) == 1:
            writes.delete.append(key)
            continue
        if len(entry) != 2 or not isinstance(entry[1], str):
            raise BridgeError("store writes contain a malformed entry")
        writes.set[key] = parse_bridge_json(entry[1], f"store value for {key!r}")
    return writes
