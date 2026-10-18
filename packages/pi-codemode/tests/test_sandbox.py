"""End-to-end sandbox behaviour: values, globals, exit, and the failure kinds."""

from __future__ import annotations

import pytest

from pi_codemode import DEFAULT_MEMORY_LIMIT_BYTES, CodemodeSandbox, CodemodeTool


def test_returns_json_values() -> None:
    sandbox = CodemodeSandbox()
    assert sandbox.execute("return {a: [1, 2, 3], b: 'x'};").value == {"a": [1, 2, 3], "b": "x"}
    assert sandbox.execute("return 42;").value == 42
    assert sandbox.execute("return;").value is None


def test_globals_are_callable_and_not_recorded_as_calls() -> None:
    helper = CodemodeTool(name="helper", execute=lambda args: {"n": 7})
    sandbox = CodemodeSandbox(globals=[helper])
    result = sandbox.execute("const v = await helper(); return v.n;")
    assert result.ok, result.error
    assert result.value == 7
    assert result.calls == []


def test_exit_stops_the_script_and_keeps_output() -> None:
    sandbox = CodemodeSandbox()
    result = sandbox.execute("text('before'); exit(); text('after');")
    assert result.ok, result.error
    assert result.value is None
    assert [item.text for item in result.output] == ["before"]


def test_syntax_error_is_reported_as_a_script_error() -> None:
    sandbox = CodemodeSandbox()
    result = sandbox.execute("const = ;")
    assert not result.ok
    assert result.error is not None
    assert result.error.kind == "script"
    assert "codemode.js" in (result.error.stack or "")


def test_unknown_tool_member_throws_inside_the_script() -> None:
    sandbox = CodemodeSandbox(tools=[CodemodeTool(name="echo", execute=lambda args: args)])
    result = sandbox.execute(
        "try { tools.nope; return 'no-error'; } catch (e) { return e.message; }"
    )
    assert result.ok, result.error
    assert isinstance(result.value, str)
    assert "tools.nope does not exist" in result.value


def test_stalled_script_fails_instead_of_hanging() -> None:
    sandbox = CodemodeSandbox(timeout_ms=5_000)
    result = sandbox.execute("await new Promise(() => {});")
    assert not result.ok
    assert result.error is not None
    assert result.error.kind == "script"
    assert "never settle" in result.error.message


def test_unknown_tool_call_rejects_inside_the_script() -> None:
    sandbox = CodemodeSandbox()
    result = sandbox.execute(
        "try { await tools['ghost']({}); return 'no-error'; } catch (e) { return 'caught'; }"
    )
    assert result.ok, result.error
    assert result.value == "caught"


def test_memory_limit_default_matches_upstream_coding_agent() -> None:
    assert DEFAULT_MEMORY_LIMIT_BYTES == 256 * 1024 * 1024


def test_closed_sandbox_rejects_new_executions() -> None:
    sandbox = CodemodeSandbox()
    sandbox.close()
    with pytest.raises(RuntimeError, match="closed"):
        sandbox.execute("return 1;")


def test_duplicate_tool_registration_is_rejected() -> None:
    sandbox = CodemodeSandbox(tools=[CodemodeTool(name="echo", execute=lambda args: args)])
    with pytest.raises(ValueError, match="already registered"):
        sandbox.register_tool(CodemodeTool(name="echo", execute=lambda args: args))


def test_reserved_global_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid global name"):
        CodemodeSandbox(globals=[CodemodeTool(name="text", execute=lambda args: None)])
