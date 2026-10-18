"""TypeScript declarations for the script-visible API (upstream ``src/declarations.ts``).

Tools become members of ``declare const tools``, globals become ``declare function``
statements (``ns.member`` globals group into ``declare const ns``), descriptions become
doc comments and JSON Schemas become TypeScript types. The output is what the model sees
in the codemode tool description, so the formatting is part of the contract.

Deviations from upstream are naming only (``renderDeclarations`` →
:func:`render_declarations`); every rendered character matches ``declarations.ts`` at
v1.3.0, and ``tests/test_declarations.py`` ports the upstream expectations verbatim.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol, TypeAlias, TypeGuard, cast
from urllib.parse import unquote

from .identifier import to_codemode_identifier
from .types import CodemodeJsonSchema

__all__ = [
    "DEFAULT_INPUT_SCHEMA_MAX_CHARS",
    "ToolDeclaration",
    "ToolInput",
    "ToolLike",
    "render_declarations",
    "render_tool_output_type",
    "render_tool_sample",
    "render_tool_signature",
    "schema_to_type",
    "to_codemode_identifier",
]

_IDENTIFIER: Final = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
INDENT: Final = "  "
#: Largest rendered input type, in characters, before it becomes ``unknown``.
DEFAULT_INPUT_SCHEMA_MAX_CHARS: Final = 16_000
#: Local ``$ref`` expansions per rendered schema, so shared definitions cannot blow up the output.
_MAX_REF_EXPANSIONS: Final = 32
#: JSON Schema keywords with a number or string value shown as ``@keyword value`` property tags.
_TAG_KEYWORDS: Final = (
    "minimum",
    "exclusiveMinimum",
    "maximum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
)
#: Tag keywords whose values all hold at once, so only the largest (lower) or smallest (upper) matters.
_LOWER_BOUNDS: Final = frozenset({"minimum", "exclusiveMinimum", "minLength", "minItems"})
_UPPER_BOUNDS: Final = frozenset({"maximum", "exclusiveMaximum", "maxLength", "maxItems"})
#: Longest property tag; longer ones (large defaults, long patterns) are left out.
_MAX_TAG_CHARS: Final = 80
#: Characters that end a ``//`` comment.
_LINE_TERMINATORS: Final = re.compile(r"[\r\n\u2028\u2029]")
_MAX_SAFE_INTEGER: Final = 2**53 - 1


class ToolLike(Protocol):
    """What the renderers read from a tool or global (upstream's ``Pick<CodemodeTool, …>``)."""

    name: str
    description: str
    input_schema: CodemodeJsonSchema | None
    output_schema: CodemodeJsonSchema | None


#: Anything the renderers accept: a :class:`ToolLike`, a :class:`ToolDeclaration`, or a mapping
#: with ``name`` / ``description`` / ``input_schema`` / ``output_schema`` / ``signature`` keys —
#: the Python analogue of upstream passing an object literal.
ToolInput: TypeAlias = "ToolLike | Mapping[str, object]"


@dataclass(slots=True)
class ToolDeclaration:
    """Declaration-only tool (upstream ``Omit<CodemodeTool, "execute">``).

    ``signature`` is globals-only: it replaces the rendered parameter list, exactly like
    upstream's ``signature`` field.
    """

    name: str
    description: str = ""
    input_schema: CodemodeJsonSchema | None = None
    output_schema: CodemodeJsonSchema | None = None
    signature: str | None = None


@dataclass(slots=True)
class _SchemaContext:
    root: CodemodeJsonSchema
    resolving: set[str] = field(default_factory=set)
    expansions: int = 0
    #: Whether property comments include constraint tags.
    tags: bool = True


def render_declarations(
    *,
    tools: Iterable[ToolInput] = (),
    globals: Iterable[ToolInput] = (),
) -> str:
    """Render TypeScript declarations for the script-visible API.

    Tools become members of ``declare const tools``, globals become ``declare function``
    statements, and ``ns.member`` globals members of ``declare const ns``.
    """
    sections: list[str] = []
    tool_list = [_as_declaration(tool) for tool in tools]
    if tool_list:
        members = [
            f"{_doc_comment(tool.description, INDENT)}{INDENT}{render_tool_signature(tool)}"
            for tool in tool_list
        ]
        sections.append("declare const tools: {\n" + "\n".join(members) + "\n};")
    namespaces: dict[str, list[str]] = {}
    for item in globals:
        global_tool = _as_declaration(item)
        dot = global_tool.name.find(".")
        if dot == -1:
            sections.append(_render_global(f"declare function {global_tool.name}", global_tool, ""))
            continue
        namespace = global_tool.name[:dot]
        members = namespaces.setdefault(namespace, [])
        members.append(_render_global(global_tool.name[dot + 1 :], global_tool, INDENT))
    for namespace, members in namespaces.items():
        sections.append(f"declare const {namespace}: {{\n" + "\n".join(members) + "\n};")
    return "\n\n".join(sections)


def render_tool_signature(tool: ToolInput, *, input_max_chars: int | None = None) -> str:
    """One tool as a member of ``tools``: ``name(args: T): Promise<R>;``.

    Input types longer than ``input_max_chars`` render as ``unknown``.
    """
    tool = _as_declaration(tool)
    if tool.input_schema is None:
        input_type = "unknown"
    else:
        budget = DEFAULT_INPUT_SCHEMA_MAX_CHARS if input_max_chars is None else input_max_chars
        input_type = schema_to_type(tool.input_schema, max_chars=budget)
    output_type = render_tool_output_type(tool.output_schema)
    return f"{to_codemode_identifier(tool.name)}(args: {input_type}): Promise<{output_type}>;"


def render_tool_sample(tool: ToolInput, *, input_max_chars: int | None = None) -> str:
    """A tool's sample: the description followed by the tool's declaration."""
    tool = _as_declaration(tool)
    declaration = f"declare const tools: {{ {render_tool_signature(tool, input_max_chars=input_max_chars)} }};"
    description = tool.description.strip() if tool.description else ""
    return f"{description}\n\ncodemode tool declaration:\n```ts\n{declaration}\n```"


def render_tool_output_type(schema: CodemodeJsonSchema | None) -> str:
    """The type a tool call resolves to: the output schema's type, ``unknown`` without one."""
    return "unknown" if schema is None else schema_to_type(schema)


def schema_to_type(schema: CodemodeJsonSchema, *, max_chars: int | None = None) -> str:
    """Convert a JSON Schema to a TypeScript type expression.

    A result longer than ``max_chars`` is rendered again without constraint tags, and
    becomes ``unknown`` if it is still too long.
    """

    def render(tags: bool) -> str:
        return _to_type(schema, _SchemaContext(root=schema, tags=tags))

    rendered = render(True)
    if max_chars is None or len(rendered) <= max_chars:
        return rendered
    untagged = render(False)
    return untagged if len(untagged) <= max_chars else "unknown"


def _as_declaration(tool: ToolInput) -> ToolDeclaration:
    """Normalize attributes or a mapping into a :class:`ToolDeclaration`."""
    if isinstance(tool, Mapping):
        schema_input = tool.get("input_schema")
        schema_output = tool.get("output_schema")
        signature = tool.get("signature")
        description = tool.get("description")
        return ToolDeclaration(
            name=str(tool.get("name", "")),
            description=description if isinstance(description, str) else "",
            input_schema=cast("CodemodeJsonSchema | None", schema_input),
            output_schema=cast("CodemodeJsonSchema | None", schema_output),
            signature=signature if isinstance(signature, str) else None,
        )
    return ToolDeclaration(
        name=tool.name,
        description=tool.description,
        input_schema=tool.input_schema,
        output_schema=tool.output_schema,
        signature=cast("str | None", getattr(tool, "signature", None)),
    )


def _render_global(head: str, global_tool: ToolDeclaration, indent: str) -> str:
    signature = global_tool.signature
    if signature is not None:
        return f"{_doc_comment(global_tool.description, indent)}{indent}{head}{signature};"
    input_type = (
        "unknown"
        if global_tool.input_schema is None
        else _to_type(global_tool.input_schema, _SchemaContext(root=global_tool.input_schema))
    )
    output_type = (
        "unknown"
        if global_tool.output_schema is None
        else _to_type(global_tool.output_schema, _SchemaContext(root=global_tool.output_schema))
    )
    return f"{_doc_comment(global_tool.description, indent)}{indent}{head}(args: {input_type}): Promise<{output_type}>;"


def _doc_comment(description: str | None, indent: str) -> str:
    text = description.strip() if description else ""
    if not text:
        return ""
    lines = text.replace("*/", "*\\/").split("\n")
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    if len(lines) == 1:
        return f"{indent}/** {lines[0]} */\n"
    body = "\n".join(f"{indent} *{f' {line}' if line else ''}" for line in lines)
    return f"{indent}/**\n{body}\n{indent} */\n"


def _property_key(name: str) -> str:
    return name if _IDENTIFIER.match(name) else (_json_text_or_none(name) or '""')


def _union(types: Sequence[str]) -> str:
    unique = list(dict.fromkeys(types))
    if "unknown" in unique:
        return "unknown"
    return " | ".join(unique) if unique else "never"


def _json_text_or_none(value: object) -> str | None:
    """``JSON.stringify`` semantics: unserializable values give ``None`` (JS ``undefined``)."""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _js_number_text(value: int | float) -> str:
    """Format a number the way a JS template literal would."""
    if isinstance(value, int):
        return str(value)
    if math.isnan(value) or math.isinf(value):
        return str(value)
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    return repr(value)


def _resolve_ref(ref: str, root: CodemodeJsonSchema) -> CodemodeJsonSchema | None:
    if ref != "#" and not ref.startswith("#/"):
        return None
    current: object = root
    for segment in [part for part in ref[2:].split("/") if part]:
        key = _unescape_ref_segment(segment)
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    if isinstance(current, bool):
        return current
    if isinstance(current, Mapping):
        return current
    return None


def _unescape_ref_segment(segment: str) -> str:
    """JSON Pointer unescaping (upstream uses ``decodeURIComponent`` + ``~1``/``~0``)."""
    return unquote(segment).replace("~1", "/").replace("~0", "~")


def _to_type(schema: CodemodeJsonSchema, context: _SchemaContext) -> str:
    if schema is True:
        return "unknown"
    if schema is False:
        return "never"
    if not isinstance(schema, Mapping):
        return "unknown"
    document = dict(schema)

    ref = document.get("$ref")
    if isinstance(ref, str):
        if ref in context.resolving or context.expansions >= _MAX_REF_EXPANSIONS:
            return "unknown"
        target = _resolve_ref(ref, context.root)
        if target is None:
            return "unknown"
        context.expansions += 1
        context.resolving.add(ref)
        try:
            return _to_type(target, context)
        finally:
            context.resolving.discard(ref)

    if "const" in document:
        return _json_text_or_none(document["const"]) or "unknown"
    enum = document.get("enum")
    if isinstance(enum, list):
        return _union([_json_text_or_none(value) or "unknown" for value in enum])

    variants: list[object] | None = None
    for keyword in ("anyOf", "oneOf"):
        candidate = document.get(keyword)
        if isinstance(candidate, list):
            variants = candidate
            break
    if variants is not None:
        return _union(
            [_to_type(cast("CodemodeJsonSchema", variant), context) for variant in variants]
        )
    all_of = document.get("allOf")
    if isinstance(all_of, list):
        parts = [_to_type(cast("CodemodeJsonSchema", part), context) for part in all_of]
        parts = [part for part in parts if part != "unknown"]
        if not parts:
            return "unknown"
        return " & ".join(f"({part})" if " | " in part else part for part in parts)

    type_field = document.get("type")
    if isinstance(type_field, list):
        return _union(
            [
                _to_type(cast("CodemodeJsonSchema", {**document, "type": entry}), context)
                for entry in type_field
            ]
        )
    if type_field == "string":
        return "string"
    if type_field in ("number", "integer"):
        return "number"
    if type_field == "boolean":
        return "boolean"
    if type_field == "null":
        return "null"
    if type_field == "array":
        return _array_type(document, context)
    if type_field == "object":
        return _object_type(document, context)
    if type_field is None:
        if "properties" in document or "additionalProperties" in document or "required" in document:
            return _object_type(document, context)
        if "items" in document or "prefixItems" in document:
            return _array_type(document, context)
        return "unknown"
    return "unknown"


def _array_type(schema: Mapping[str, object], context: _SchemaContext) -> str:
    items = schema.get("items")
    if items is not None and not isinstance(items, list):
        return f"Array<{_to_type(cast('CodemodeJsonSchema', items), context)}>"
    prefix_items = schema.get("prefixItems")
    if isinstance(prefix_items, list):
        tuple_items = prefix_items
    elif isinstance(items, list):
        tuple_items = items
    else:
        tuple_items = []
    if tuple_items:
        rendered = ", ".join(
            _to_type(cast("CodemodeJsonSchema", item), context) for item in tuple_items
        )
        return f"[{rendered}]"
    return "unknown[]"


def _description_of(property_schema: object) -> str:
    if not isinstance(property_schema, Mapping):
        return ""
    description = property_schema.get("description")
    return description.strip() if isinstance(description, str) else ""


def _constraint_tags(schema: object) -> str:
    """Constraints a TypeScript type cannot express, as JSDoc-style tags."""
    if not isinstance(schema, Mapping):
        return ""
    prop = dict(schema)
    variants: object = prop.get("anyOf")
    if not isinstance(variants, list):
        variants = prop.get("oneOf")
    non_null: list[object] = []
    if isinstance(variants, list):
        non_null = [
            variant
            for variant in variants
            if not (isinstance(variant, Mapping) and variant.get("type") == "null")
        ]
    branch = non_null[0] if len(non_null) == 1 else None
    branches: list[dict[str, object]] = [prop]
    if isinstance(branch, Mapping):
        branches.append(dict(branch))
    # Draft-04 and OpenAPI 3.0 make bounds exclusive with a boolean flag.
    schemas: list[dict[str, object]] = []
    for entry in branches:
        merged = dict(entry)
        if merged.get("exclusiveMinimum") is True:
            merged["minimum"] = None
            merged["exclusiveMinimum"] = entry.get("minimum")
        if merged.get("exclusiveMaximum") is True:
            merged["maximum"] = None
            merged["exclusiveMaximum"] = entry.get("maximum")
        schemas.append(merged)

    tags: list[str] = []
    if any(_is_integer_type(entry.get("type")) for entry in schemas):
        tags.append("@integer")
    for keyword in _TAG_KEYWORDS:
        values = [entry.get(keyword) for entry in schemas]
        numbers = [value for value in values if _is_number(value)]
        if keyword in _LOWER_BOUNDS or keyword in _UPPER_BOUNDS:
            if not numbers:
                continue
            bound = max(numbers) if keyword in _LOWER_BOUNDS else min(numbers)
            tags.append(f"@{keyword} {_js_number_text(bound)}")
            continue
        for value in dict.fromkeys(values):
            if _is_number(value):
                tags.append(f"@{keyword} {_js_number_text(value)}")
            elif isinstance(value, str):
                tags.append(f"@{keyword} {value}")
    if any(entry.get("uniqueItems") is True for entry in schemas):
        tags.append("@uniqueItems")
    # Annotations, unlike constraints, come from the property itself.
    default = prop.get("default")
    if default is not None:
        # A JS template literal renders an unserializable default as `undefined`.
        tags.append(f"@default {_json_text_or_none(default) or 'undefined'}")
    if prop.get("deprecated") is True:
        tags.append("@deprecated")
    return " ".join(
        _LINE_TERMINATORS.sub(lambda match: f"\\u{ord(match.group(0)):04x}", tag)
        for tag in tags
        if len(tag) <= _MAX_TAG_CHARS
    )


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_integer_type(type_field: object) -> bool:
    if type_field == "integer":
        return True
    return isinstance(type_field, list) and "integer" in type_field


def _object_type(schema: Mapping[str, object], context: _SchemaContext) -> str:
    raw_properties = schema.get("properties")
    properties: Mapping[str, object] = raw_properties if isinstance(raw_properties, Mapping) else {}
    raw_required = schema.get("required")
    required = set(raw_required) if isinstance(raw_required, list) else set()
    names = sorted(properties, key=_js_sort_key)
    members = [
        f"{_property_key(name)}{'' if name in required else '?'}: "
        f"{_to_type(cast('CodemodeJsonSchema', properties[name]), context)};"
        for name in names
    ]
    additional = schema.get("additionalProperties")
    if additional is not None and additional is not False:
        extra_type = (
            "unknown"
            if additional is True
            else _to_type(cast("CodemodeJsonSchema", additional), context)
        )
        members.append(f"[key: string]: {extra_type};")
    elif additional is None and not names:
        members.append("[key: string]: unknown;")
    if not members:
        return "{}"
    comments = [_property_comments(name, properties[name], context) for name in names]
    if not any(comments):
        return "{ " + " ".join(members) + " }"

    lines = ["{"]
    for index, comment in enumerate(comments):
        lines.extend(f"{INDENT}// {line}" for line in comment)
        # A nested multiline type is indented as a whole (upstream replaces every newline
        # in the member with a newline plus one indent).
        lines.append(f"{INDENT}{members[index].replace(chr(10), chr(10) + INDENT)}")
    lines.extend(f"{INDENT}{member}" for member in members[len(names) :])
    lines.append("}")
    return "\n".join(lines)


def _property_comments(name: str, property_schema: object, context: _SchemaContext) -> list[str]:
    lines = [
        line.strip()
        for line in _LINE_TERMINATORS.split(_description_of(property_schema))
        if line.strip()
    ]
    tags = _constraint_tags(property_schema) if context.tags else ""
    return [*lines, tags] if tags else lines


def _js_sort_key(name: str) -> bytes:
    """``Array.prototype.sort()`` compares UTF-16 code units, not code points."""
    return name.encode("utf-16-be", "surrogatepass")
