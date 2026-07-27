from __future__ import annotations

import pytest

from pi_ai import Tool
from pi_ai.constrained_sampling import (
    GrammarToolInputJsonBuffer,
    append_grammar_tool_input_json_delta,
    create_grammar_tool_input_properties,
    resolve_grammar_constrained_sampling,
    resolve_json_schema_strict_sampling,
)


def test_tool_accepts_camel_case_constrained_sampling():
    tool = Tool.model_validate(
        {
            "name": "answer",
            "description": "Return an answer",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "constrainedSampling": {"type": "json_schema", "strict": "prefer"},
        }
    )

    assert tool.constrained_sampling is not None
    assert tool.model_dump(by_alias=True)["constrainedSampling"]["strict"] == "prefer"


def test_preferred_strict_sampling_falls_back_when_unsupported():
    tool = Tool(
        name="answer",
        description="Return an answer",
        parameters={"type": "object", "properties": {}, "required": []},
        constrained_sampling={"type": "json_schema", "strict": "prefer"},
    )

    assert resolve_json_schema_strict_sampling(tool, supports_strict_mode=False) is None


def test_grammar_requires_exactly_one_required_string_property():
    tool = Tool(
        name="bad",
        description="Bad grammar tool",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a", "b"],
        },
        constrained_sampling={
            "type": "grammar",
            "variants": {"openai_regex": ".+"},
        },
    )

    with pytest.raises(ValueError, match="exactly one required string property"):
        resolve_grammar_constrained_sampling(tool, supports_openai_grammar_tools=True)


def test_create_grammar_tool_input_properties_ignores_unsupported_provider():
    tool = Tool(
        name="sql",
        description="Generate SQL",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        constrained_sampling={
            "type": "grammar",
            "variants": {"openai_regex": "SELECT .*"},
        },
    )

    assert create_grammar_tool_input_properties([tool], False) == {}


def test_grammar_tool_input_delta_forms_valid_incremental_json():
    buffer = GrammarToolInputJsonBuffer()

    first = append_grammar_tool_input_json_delta(buffer, "query", 'SELECT "a', close=False)
    second = append_grammar_tool_input_json_delta(buffer, "query", 'SELECT "a"', close=True)

    assert first == '{"query":"SELECT \\"a'
    assert second == '\\""}'
