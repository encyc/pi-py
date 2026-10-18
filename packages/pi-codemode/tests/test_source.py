"""``parse_codemode_source`` — ported case by case from upstream ``test/source.test.ts``."""

from __future__ import annotations

import pytest

from pi_codemode import (
    CODEMODE_OPTIONS_PREFIX,
    CODEMODE_SOURCE_GRAMMAR,
    MAX_TIMEOUT_MS,
    CodemodeSourceError,
    CodemodeSourceOptions,
    ParsedCodemodeSource,
    parse_codemode_source,
)


def test_grammar_is_the_upstream_lark_grammar_verbatim() -> None:
    assert len(CODEMODE_SOURCE_GRAMMAR) == 184
    assert CODEMODE_SOURCE_GRAMMAR.count("\n") == 8
    assert CODEMODE_SOURCE_GRAMMAR.split("\n")[0] == ""
    assert CODEMODE_SOURCE_GRAMMAR == (
        "\n"
        "start: options_source | plain_source\n"
        "options_source: OPTIONS_LINE NEWLINE SOURCE\n"
        "plain_source: SOURCE\n"
        "\n"
        "OPTIONS_LINE: /[ \\t]*\\/\\/ @options:[^\\r\\n]*/\n"
        "NEWLINE: /\\r?\\n/\n"
        "SOURCE: /[\\s\\S]+/\n"
    )
    assert CODEMODE_OPTIONS_PREFIX == "// @options:"


def test_returns_plain_code_unchanged() -> None:
    assert parse_codemode_source("text('hi')") == ParsedCodemodeSource(code="text('hi')")
    assert parse_codemode_source("// just a comment\nreturn 1") == ParsedCodemodeSource(
        code="// just a comment\nreturn 1"
    )


def test_parses_the_options_line_and_keeps_line_numbers() -> None:
    parsed = parse_codemode_source('// @options: {"timeout_ms": 10}\nconst a = 1;\ntext(a)')
    assert parsed == ParsedCodemodeSource(
        code="\nconst a = 1;\ntext(a)", options=CodemodeSourceOptions(timeout_ms=10)
    )
    indented = parse_codemode_source(
        '  // @options:{"max_output_tokens":0,"timeout_ms":1500}\r\ntext(1)'
    )
    assert indented.options == CodemodeSourceOptions(max_output_tokens=0, timeout_ms=1500)
    assert indented.code == "\ntext(1)"
    assert parse_codemode_source("// @options: {}\ntext(1)") == ParsedCodemodeSource(
        code="\ntext(1)"
    )


def test_only_treats_the_first_line_as_an_options_line() -> None:
    assert parse_codemode_source("// @optionsx {}\ntext(1)").options == CodemodeSourceOptions()
    assert parse_codemode_source('text("// @options: {}")').options == CodemodeSourceOptions()


@pytest.mark.parametrize(
    ("source", "line"),
    [
        ('// count things\n// @options: {"timeout_ms": 1000}\nwhile (true) {}', 2),
        ('\n  // @options: {"timeout_ms": 1000}\nwhile (true) {}', 2),
        ('text(1)\r\n// @options: {"timeout_ms": 1}', 2),
        ('// @options: {"timeout_ms": 1}\ntext(1)\n// @options: {"timeout_ms": 2}', 3),
    ],
)
def test_rejects_an_options_line_that_is_not_the_first_line(source: str, line: int) -> None:
    with pytest.raises(CodemodeSourceError) as error:
        parse_codemode_source(source)
    assert str(error.value) == (
        f"The {CODEMODE_OPTIONS_PREFIX} line must be the first line of the input; "
        f"found one on line {line}"
    )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "Expected JavaScript source text (non-empty)"),
        ("  \n", "Expected JavaScript source text (non-empty)"),
        ("// @options:\ntext(1)", "@options must be a JSON object with supported fields"),
        (
            "// @options: {timeout_ms: 1}\ntext(1)",
            "@options must be valid JSON with supported fields",
        ),
        ("// @options: [1]\ntext(1)", "@options must be a JSON object with supported fields"),
        (
            '// @options: {"yield": 1}\ntext(1)',
            "@options only supports `max_output_tokens` and `timeout_ms`; got `yield`",
        ),
        (
            '// @options: {"max_output_tokens": 1.5}\ntext(1)',
            "@options field `max_output_tokens` must be a non-negative safe integer",
        ),
        (
            '// @options: {"timeout_ms": 0}\ntext(1)',
            "@options field `timeout_ms` must be a positive integer",
        ),
        (
            '// @options: {"timeout_ms": 1}',
            "The @options line must be followed by JavaScript source on subsequent lines",
        ),
        (
            '// @options: {"timeout_ms": 1}\n  \n',
            "The @options line must be followed by JavaScript source on subsequent lines",
        ),
    ],
)
def test_rejects_empty_input_and_invalid_options(source: str, message: str) -> None:
    with pytest.raises(CodemodeSourceError) as error:
        parse_codemode_source(source)
    assert message in str(error.value)


@pytest.mark.parametrize(
    "source",
    [
        '// @options: {"timeout_ms": true}\ntext(1)',
        '// @options: {"max_output_tokens": null}\ntext(1)',
        '// @options: {"max_output_tokens": -1}\ntext(1)',
        f'// @options: {{"timeout_ms": {MAX_TIMEOUT_MS + 1}}}\ntext(1)',
        '// @options: {"max_output_tokens": 9007199254740992}\ntext(1)',
    ],
)
def test_rejects_values_outside_the_supported_ranges(source: str) -> None:
    with pytest.raises(CodemodeSourceError):
        parse_codemode_source(source)


def test_accepts_whole_floats_like_json_numbers() -> None:
    # JSON has one number type: `1.0` is the number 1 for `Number.isSafeInteger`.
    parsed = parse_codemode_source('// @options: {"timeout_ms": 1500.0}\ntext(1)')
    assert parsed.options.timeout_ms == 1500


def test_error_type_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        parse_codemode_source("")
