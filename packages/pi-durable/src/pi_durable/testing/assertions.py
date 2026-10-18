"""The six assertion primitives — port of ``testing/assertions.ts``.

Upstream adapts a Vitest/Jest ``expect`` function without importing either runner.
Python has no runner-coupled ``expect``, so the port implements the primitives
directly on top of ``assert``: they raise :class:`AssertionError` with a message
that reads the same way under pytest as the upstream assertions do under Vitest.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Mapping
from typing import final

__all__ = ["PytestAssertions", "create_pytest_assertions"]


def _as_mapping(value: object) -> dict[str, object] | None:
    """A dataclass or mapping as a plain dict; ``None`` for anything else."""
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    return None


def _normalize(value: object) -> object:
    mapping = _as_mapping(value)
    if mapping is not None:
        return {key: _normalize(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def _same_value(actual: object, expected: object) -> bool:
    """Upstream ``toBe``: numbers compare by value, everything else by type and identity."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return float(actual) == float(expected)
    if type(actual) is not type(expected):
        return False
    return bool(actual == expected)


def _partial_match(actual: object, expected: object) -> bool:
    expected_mapping = _as_mapping(expected)
    if expected_mapping is not None:
        actual_mapping = _as_mapping(actual)
        if actual_mapping is None:
            return False
        return all(
            key in actual_mapping and _partial_match(actual_mapping[key], item)
            for key, item in expected_mapping.items()
        )
    if isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)) or len(actual) != len(expected):
            return False
        return all(
            _partial_match(item, wanted) for item, wanted in zip(actual, expected, strict=True)
        )
    return _same_value(actual, expected)


@final
class PytestAssertions:
    """The conformance assertion primitives, raising :class:`AssertionError`."""

    def ok(self, value: object, message: str | None = None) -> None:
        assert value, message or f"expected a truthy value, got {value!r}"

    def strict_equal(self, actual: object, expected: object) -> None:
        assert _same_value(actual, expected), f"expected {expected!r}, got {actual!r}"

    def deep_equal(self, actual: object, expected: object) -> None:
        assert _normalize(actual) == _normalize(expected), (
            f"expected {_normalize(expected)!r}, got {_normalize(actual)!r}"
        )

    def partial_deep_equal(self, actual: object, expected: object) -> None:
        assert _partial_match(actual, expected), (
            f"expected {_normalize(actual)!r} to match {_normalize(expected)!r}"
        )

    def greater_than(self, actual: float, expected: float) -> None:
        assert actual > expected, f"expected {actual!r} > {expected!r}"

    async def rejects(self, operation: Awaitable[object], message_includes: str) -> None:
        try:
            await operation
        except Exception as error:
            assert message_includes in str(error), (
                f"expected {message_includes!r} in {str(error)!r}"
            )
            return
        raise AssertionError(f"expected the operation to raise {message_includes!r}")


def create_pytest_assertions() -> PytestAssertions:
    """Assertions for pytest, the counterpart of ``createExpectAssertions(expect)``."""
    return PytestAssertions()
