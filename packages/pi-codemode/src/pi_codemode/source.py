"""Codemode source format (upstream ``src/source.ts``): an optional first-line options directive.

```js
// @options: {"max_output_tokens": 2000, "timeout_ms": 30000}
const source = await tools.read({ path: "package.json" });
text(JSON.parse(source).name);
```

:data:`CODEMODE_SOURCE_GRAMMAR` is the same Lark grammar upstream exports for providers
with grammar-constrained tool input (v1.3.0 removed MCP's TypeScript preamble, see
``codemode/CHANGELOG.md:22``); it is a constant, not something the parser is built on —
upstream's own :func:`parse_codemode_source` is hand-written string logic, so this port
needs no extra dependency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "CODEMODE_OPTIONS_PREFIX",
    "CODEMODE_SOURCE_GRAMMAR",
    "MAX_TIMEOUT_MS",
    "CodemodeSourceError",
    "CodemodeSourceOptions",
    "ParsedCodemodeSource",
    "parse_codemode_source",
]

CODEMODE_OPTIONS_PREFIX: Final = "// @options:"

#: Largest delay ``setTimeout`` supports, which bounds ``timeout_ms`` (upstream ``MAX_TIMEOUT_MS``).
MAX_TIMEOUT_MS: Final = 2_147_483_647

_SUPPORTED_FIELDS: Final = ("max_output_tokens", "timeout_ms")
_SUPPORTED_FIELDS_TEXT: Final = "`max_output_tokens` and `timeout_ms`"
_MAX_SAFE_INTEGER: Final = 2**53 - 1

#: Lark grammar for providers with grammar-constrained tool input. It only fixes the shape of the
#: options line; the options JSON and the code are checked by :func:`parse_codemode_source`.
#: Byte-identical to upstream v1.3.0 (184 characters); ``test_source.py`` asserts that.
CODEMODE_SOURCE_GRAMMAR: Final = r"""
start: options_source | plain_source
options_source: OPTIONS_LINE NEWLINE SOURCE
plain_source: SOURCE

OPTIONS_LINE: /[ \t]*\/\/ @options:[^\r\n]*/
NEWLINE: /\r?\n/
SOURCE: /[\s\S]+/
"""


class CodemodeSourceError(ValueError):
    """Invalid codemode source: empty input, bad options, or a misplaced options line."""


@dataclass(slots=True)
class CodemodeSourceOptions:
    """Options the caller applies; the sandbox itself does not act on them."""

    #: Token budget for the script's output.
    max_output_tokens: int | None = None
    #: Hard deadline for the whole script in milliseconds, including tool calls.
    timeout_ms: int | None = None


@dataclass(slots=True)
class ParsedCodemodeSource:
    """A script with its options line removed."""

    #: The script with the options line replaced by an empty line, so line numbers are unchanged.
    code: str
    options: CodemodeSourceOptions = field(default_factory=CodemodeSourceOptions)


def parse_codemode_source(source: str) -> ParsedCodemodeSource:
    """Split an optional first-line ``// @options: {...}`` from the script.

    Raises :class:`CodemodeSourceError` for empty input, invalid options, and an options
    line that is not the first line — which would otherwise be ignored without notice
    (upstream issue #10631).
    """
    if source.strip() == "":
        raise CodemodeSourceError(
            "Expected JavaScript source text (non-empty). Provide JS only, optionally with a "
            'first line `// @options: {"max_output_tokens": 1000}`.'
        )
    lines = source.split("\n")
    misplaced = next(
        (
            index
            for index, line in enumerate(lines)
            if index > 0 and line.lstrip().startswith(CODEMODE_OPTIONS_PREFIX)
        ),
        -1,
    )
    if misplaced != -1:
        raise CodemodeSourceError(
            f"The {CODEMODE_OPTIONS_PREFIX} line must be the first line of the input; "
            f"found one on line {misplaced + 1}"
        )
    newline = source.find("\n")
    first_line = lines[0][:-1] if lines[0].endswith("\r") else lines[0]
    trimmed = first_line.lstrip()
    if not trimmed.startswith(CODEMODE_OPTIONS_PREFIX):
        return ParsedCodemodeSource(code=source)
    code = "" if newline == -1 else source[newline:]
    if code.strip() == "":
        raise CodemodeSourceError(
            "The @options line must be followed by JavaScript source on subsequent lines"
        )
    directive = trimmed[len(CODEMODE_OPTIONS_PREFIX) :].strip()
    return ParsedCodemodeSource(code=code, options=_parse_options(directive))


def _parse_options(directive: str) -> CodemodeSourceOptions:
    if directive == "":
        raise CodemodeSourceError(
            f"@options must be a JSON object with supported fields {_SUPPORTED_FIELDS_TEXT}"
        )
    try:
        value: object = json.loads(directive)
    except json.JSONDecodeError as error:
        raise CodemodeSourceError(
            f"@options must be valid JSON with supported fields {_SUPPORTED_FIELDS_TEXT}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise CodemodeSourceError(
            f"@options must be a JSON object with supported fields {_SUPPORTED_FIELDS_TEXT}"
        )
    for key in value:
        if key not in _SUPPORTED_FIELDS:
            raise CodemodeSourceError(
                f"@options only supports {_SUPPORTED_FIELDS_TEXT}; got `{key}`"
            )
    options = CodemodeSourceOptions()
    if "max_output_tokens" in value:
        max_output_tokens = value["max_output_tokens"]
        if not _is_safe_integer(max_output_tokens):
            raise CodemodeSourceError(
                "@options field `max_output_tokens` must be a non-negative safe integer"
            )
        options.max_output_tokens = max_output_tokens
    if "timeout_ms" in value:
        timeout_ms = value["timeout_ms"]
        if not _is_safe_integer(timeout_ms) or timeout_ms == 0 or timeout_ms > MAX_TIMEOUT_MS:
            raise CodemodeSourceError(
                f"@options field `timeout_ms` must be a positive integer up to {MAX_TIMEOUT_MS}"
            )
        options.timeout_ms = timeout_ms
    return options


def _is_safe_integer(value: object) -> bool:
    """``Number.isSafeInteger(value) && value >= 0`` for JSON-decoded values.

    JSON has one number type, so a whole float (``1.0``) counts, exactly as it does in
    JavaScript; ``True`` is not a number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return 0 <= value <= _MAX_SAFE_INTEGER
