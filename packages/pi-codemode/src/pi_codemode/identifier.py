"""Tool-name → script identifier mapping (upstream ``src/identifier.ts``)."""

from __future__ import annotations

__all__ = ["to_codemode_identifier"]


def to_codemode_identifier(name: str) -> str:
    """Map a tool name to the identifier scripts use.

    ``my-tool`` becomes ``my_tool``; ``tools["my-tool"]`` keeps working either way.
    Names that would not start an identifier are prefixed with ``_``.
    """
    mapped = "".join(char if (char.isalnum() or char in "_$") else "_" for char in name)
    if not mapped or mapped[0].isdigit():
        mapped = f"_{mapped}"
    return mapped
