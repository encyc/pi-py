"""``schema_to_type`` / tool declarations — ported from upstream ``test/declarations.test.ts``.

Every expected string below is the upstream expectation, so a formatting drift fails CI.
"""

from __future__ import annotations

from pi_codemode import (
    DEFAULT_INPUT_SCHEMA_MAX_CHARS,
    CodemodeTool,
    ToolDeclaration,
    render_declarations,
    render_tool_output_type,
    render_tool_sample,
    render_tool_signature,
    schema_to_type,
)


def test_renders_primitives_literals_and_unions() -> None:
    assert schema_to_type({"type": "string"}) == "string"
    assert schema_to_type({"type": "integer"}) == "number"
    assert schema_to_type({"type": ["string", "null"]}) == "string | null"
    assert schema_to_type({"const": "a"}) == '"a"'
    assert schema_to_type({"enum": ["a", 1, None]}) == '"a" | 1 | null'
    assert schema_to_type({"anyOf": [{"type": "string"}, {"type": "number"}]}) == "string | number"
    assert schema_to_type({"anyOf": [{"type": "string"}, {}]}) == "unknown"
    assert (
        schema_to_type(
            {"allOf": [{"anyOf": [{"type": "string"}, {"type": "number"}]}, {"const": 1}]}
        )
        == "(string | number) & 1"
    )
    assert schema_to_type({"$ref": "#/defs/x"}) == "unknown"
    assert schema_to_type(True) == "unknown"
    assert schema_to_type(False) == "never"


def test_renders_objects_on_one_line_with_sorted_properties() -> None:
    assert (
        schema_to_type(
            {
                "type": "object",
                "properties": {"city": {"type": "string"}, "max-lines": {"type": "number"}},
                "required": ["city"],
                "additionalProperties": False,
            }
        )
        == '{ city: string; "max-lines"?: number; }'
    )
    assert (
        schema_to_type({"type": "object", "additionalProperties": {"type": "number"}})
        == "{ [key: string]: number; }"
    )
    assert schema_to_type({"type": "object"}) == "{ [key: string]: unknown; }"
    assert (
        schema_to_type({"type": "object", "properties": {}, "additionalProperties": False}) == "{}"
    )


def test_puts_property_descriptions_on_comment_lines() -> None:
    assert schema_to_type(
        {
            "type": "object",
            "properties": {
                "weather": {
                    "type": "array",
                    "description": "look up weather for a given list of locations",
                    "items": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"],
                    },
                }
            },
            "required": ["weather"],
        }
    ) == (
        "{\n"
        "  // look up weather for a given list of locations\n"
        "  weather: Array<{ location: string; }>;\n"
        "}"
    )
    assert (
        schema_to_type(
            {
                "type": "object",
                "properties": {
                    "outer": {
                        "type": "object",
                        "description": "Outer",
                        "properties": {"inner": {"type": "string", "description": "Inner"}},
                    }
                },
            }
        )
        == "{\n  // Outer\n  outer?: {\n    // Inner\n    inner?: string;\n  };\n}"
    )


def test_puts_constraints_the_type_cannot_express_on_property_comment_lines() -> None:
    assert schema_to_type(
        {
            "type": "object",
            "properties": {
                "timeoutMs": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 30000,
                    "default": 0,
                    "description": "Wait in milliseconds.",
                },
                "id": {"type": "string", "pattern": "^[a-z]+$", "maxLength": 8},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 5,
                    "uniqueItems": True,
                    "deprecated": True,
                },
                "name": {"type": "string"},
            },
            "required": ["id"],
        }
    ) == "\n".join(
        [
            "{",
            "  // @maxLength 8 @pattern ^[a-z]+$",
            "  id: string;",
            "  name?: string;",
            "  // @maxItems 5 @uniqueItems @deprecated",
            "  tags?: Array<string>;",
            "  // Wait in milliseconds.",
            "  // @integer @minimum 0 @maximum 30000 @default 0",
            "  timeoutMs?: number;",
            "}",
        ]
    )


def test_reads_constraints_of_optional_fields_from_the_non_null_branch() -> None:
    assert (
        schema_to_type(
            {
                "type": "object",
                "properties": {
                    "limit": {
                        "anyOf": [
                            {"type": "integer", "minimum": 1, "maximum": 100},
                            {"type": "null"},
                        ],
                        "default": None,
                        "title": "Limit",
                    }
                },
            }
        )
        == "{\n  // @integer @minimum 1 @maximum 100\n  limit?: number | null;\n}"
    )
    assert (
        schema_to_type(
            {
                "type": "object",
                "properties": {
                    "n": {
                        "anyOf": [
                            {"type": "integer", "minimum": 10, "maximum": 50, "format": "int32"},
                            {"type": "null"},
                        ],
                        "minimum": 0,
                        "maximum": 20,
                        "format": "port",
                    }
                },
            }
        )
        == "{\n  // @integer @minimum 10 @maximum 20 @format port @format int32\n  n?: number | null;\n}"
    )


def test_reads_draft_04_boolean_exclusive_bounds() -> None:
    assert schema_to_type(
        {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "minimum": 0,
                    "exclusiveMinimum": True,
                    "maximum": 10,
                    "exclusiveMaximum": False,
                },
                "m": {
                    "anyOf": [
                        {"type": "number", "maximum": 5, "exclusiveMaximum": True},
                        {"type": "null"},
                    ],
                    "maximum": 8,
                },
            },
        }
    ) == "\n".join(
        [
            "{",
            "  // @maximum 8 @exclusiveMaximum 5",
            "  m?: number | null;",
            "  // @integer @exclusiveMinimum 0 @maximum 10",
            "  n?: number;",
            "}",
        ]
    )


def test_leaves_out_long_tags_and_escapes_line_terminators() -> None:
    assert (
        schema_to_type(
            {
                "type": "object",
                "properties": {"config": {"type": "object", "default": {"text": "x" * 100}}},
            }
        )
        == "{ config?: { [key: string]: unknown; }; }"
    )
    assert schema_to_type(
        {
            "type": "object",
            "properties": {
                "sep": {
                    "type": "string",
                    "pattern": "^[\r\n\u2028\u2029]$",
                    "default": "\u2028",
                    "description": "a\u2028b",
                }
            },
        }
    ) == "\n".join(
        [
            "{",
            "  // a",
            "  // b",
            '  // @pattern ^[\\u000d\\u000a\\u2028\\u2029]$ @default "\\u2028"',
            "  sep?: string;",
            "}",
        ]
    )


def test_drops_constraint_tags_before_rendering_a_type_over_the_budget_as_unknown() -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}}
    assert schema_to_type(schema) == "{\n  // @integer @minimum 1\n  n?: number;\n}"
    assert schema_to_type(schema, max_chars=20) == "{ n?: number; }"
    assert schema_to_type(schema, max_chars=10) == "unknown"


def test_resolves_local_references_and_stops_at_recursive_ones() -> None:
    schema = {
        "type": "object",
        "properties": {
            "item": {"$ref": "#/$defs/Item"},
            "legacy": {"$ref": "#/definitions/Legacy"},
            "remote": {"$ref": "https://example.com/schema.json"},
        },
        "required": ["item"],
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "parent": {"$ref": "#/$defs/Item"}},
                "required": ["id"],
            }
        },
        "definitions": {"Legacy": {"enum": ["a", "b"]}},
    }
    assert (
        schema_to_type(schema)
        == '{ item: { id: string; parent?: unknown; }; legacy?: "a" | "b"; remote?: unknown; }'
    )


def test_renders_arrays_and_tuples() -> None:
    assert schema_to_type({"type": "array", "items": {"type": "string"}}) == "Array<string>"
    assert (
        schema_to_type({"type": "array", "prefixItems": [{"type": "string"}, {"type": "number"}]})
        == "[string, number]"
    )
    assert schema_to_type({"type": "array"}) == "unknown[]"


def test_renders_types_over_the_budget_as_unknown() -> None:
    properties = {f"field{index}": {"type": "string"} for index in range(50)}
    schema = {"type": "object", "properties": properties}
    assert schema_to_type(schema, max_chars=100) == "unknown"
    assert "field49?: string;" in schema_to_type(schema)


def test_renders_signatures_with_normalized_identifiers() -> None:
    assert (
        render_tool_signature(
            ToolDeclaration(
                name="hidden-dynamic-tool",
                input_schema={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            )
        )
        == "hidden_dynamic_tool(args: { city: string; }): Promise<{ ok: boolean; }>;"
    )
    assert (
        render_tool_signature(ToolDeclaration(name="free"))
        == "free(args: unknown): Promise<unknown>;"
    )


def test_renders_output_schemas_shaped_like_an_mcp_call_tool_result_as_plain_types() -> None:
    output_schema = {
        "type": "object",
        "properties": {
            "content": {"type": "array", "items": {"type": "object"}},
            "isError": {"type": "boolean"},
            "_meta": {"type": "object"},
        },
        "required": ["content"],
    }
    assert render_tool_signature(
        ToolDeclaration(
            name="plain",
            input_schema={"type": "object", "properties": {}},
            output_schema=output_schema,
        )
    ) == (
        "plain(args: { [key: string]: unknown; }): Promise<{ _meta?: { [key: string]: unknown; }; "
        "content: Array<{ [key: string]: unknown; }>; isError?: boolean; }>;"
    )


def test_renders_the_per_tool_sample() -> None:
    assert render_tool_sample(
        ToolDeclaration(name="foo", description="bar", input_schema={"type": "string"})
    ) == (
        "bar\n\ncodemode tool declaration:\n```ts\n"
        "declare const tools: { foo(args: string): Promise<unknown>; };\n```"
    )


def test_renders_tools_and_globals() -> None:
    text = render_declarations(
        tools=[
            ToolDeclaration(
                name="read",
                description="Read a file.\nSecond line.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                output_schema={"type": "string"},
            ),
            ToolDeclaration(name="remote-api"),
        ],
        globals=[
            ToolDeclaration(
                name="attach", description="Attach it.", input_schema={"type": "string"}
            )
        ],
    )
    assert text == "\n".join(
        [
            "declare const tools: {",
            "  /**",
            "   * Read a file.",
            "   * Second line.",
            "   */",
            "  read(args: { path: string; }): Promise<string>;",
            "  remote_api(args: unknown): Promise<unknown>;",
            "};",
            "",
            "/** Attach it. */",
            "declare function attach(args: string): Promise<unknown>;",
        ]
    )


def test_renders_namespaced_globals_and_explicit_signatures() -> None:
    text = render_declarations(
        globals=[
            ToolDeclaration(
                name="models.list",
                description="List models.",
                signature="(type: string): Promise<string[]>",
            ),
            ToolDeclaration(name="models.get", input_schema={"type": "string"}),
            ToolDeclaration(name="plain", signature="(): void"),
        ]
    )
    assert text == "\n".join(
        [
            "declare function plain(): void;",
            "",
            "declare const models: {",
            "  /** List models. */",
            "  list(type: string): Promise<string[]>;",
            "  get(args: string): Promise<unknown>;",
            "};",
        ]
    )


def test_escapes_comment_terminators_in_descriptions() -> None:
    text = render_declarations(tools=[ToolDeclaration(name="x", description="a */ b")])
    assert "/** a *\\/ b */" in text


def test_render_tool_output_type_uses_unknown_without_a_schema() -> None:
    assert render_tool_output_type(None) == "unknown"
    assert render_tool_output_type({"type": "string"}) == "string"


def test_accepts_codemode_tool_objects_too() -> None:
    tool = CodemodeTool(
        name="echo",
        description="Echo",
        input_schema={"type": "string"},
        output_schema={"type": "boolean"},
        execute=lambda args: args,
    )
    assert render_tool_signature(tool) == "echo(args: string): Promise<boolean>;"
    assert render_tool_sample(tool).startswith("Echo\n\ncodemode tool declaration:")
    assert render_declarations(tools=[tool]).startswith("declare const tools: {")


def test_default_input_budget_matches_upstream() -> None:
    assert DEFAULT_INPUT_SCHEMA_MAX_CHARS == 16_000


def test_accepts_plain_mappings_like_upstream_object_literals() -> None:
    """``toCodemodeDeclaration`` returns an object literal upstream; a dict works here too."""
    declaration = {
        "name": "read-file",
        "description": "Read a file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "output_schema": {"type": "string"},
    }
    assert (
        render_tool_signature(declaration) == "read_file(args: { path: string; }): Promise<string>;"
    )
    assert render_tool_sample(declaration).startswith("Read a file.\n\ncodemode tool declaration:")
    assert render_declarations(tools=[declaration]).startswith("declare const tools: {")
    assert render_declarations(globals=[{"name": "attach", "signature": "(): void"}]) == (
        "declare function attach(): void;"
    )
