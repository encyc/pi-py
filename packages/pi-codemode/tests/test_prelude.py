"""The upstream prelude must load unmodified and drive the ``tools.*`` trampoline."""

from __future__ import annotations

import base64

from pi_codemode import CodemodeResult, CodemodeSandbox, CodemodeTool

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d4944415478da63f8cfc0f01f00050001ffabce3689"
    "0000000049454e44ae426082"
)


def echo_tool(calls: list[object]) -> CodemodeTool:
    def execute(args: object) -> object:
        calls.append(args)
        if isinstance(args, dict) and "a" in args:
            return {"a": args["a"] + 41}
        return args

    return CodemodeTool(name="echo", execute=execute, description="Echo back")


def test_tool_roundtrip_through_the_prelude() -> None:
    """S4: ``tools.echo()`` reaches Python, ``settle`` resumes the script, ``text``/``done`` come back."""
    calls: list[object] = []
    sandbox = CodemodeSandbox(tools=[echo_tool(calls)])
    result = sandbox.execute(
        "const v = await tools.echo({ a: 1 }); text('got ' + v.a); return 'done1';"
    )
    assert result.ok, result.error
    assert result.value == "done1"
    assert [item.text for item in result.output] == ["got 42"]
    assert calls == [{"a": 1}]
    assert [(call.name, call.status) for call in result.calls] == [("echo", "ok")]
    assert result.calls[0].duration_ms >= 0


def test_tool_names_are_mapped_to_identifiers() -> None:
    tool = CodemodeTool(name="my-tool", execute=lambda args: "ok")
    assert tool.js_name == "my_tool"
    sandbox = CodemodeSandbox(tools=[tool])
    assert sandbox.execute("return await tools.my_tool({});").value == "ok"
    assert sandbox.execute('return await tools["my-tool"]({});').value == "ok"


def test_tool_exception_is_catchable_in_the_script() -> None:
    def boom(args: object) -> object:
        raise ValueError("nope")

    sandbox = CodemodeSandbox(tools=[CodemodeTool(name="boom", execute=boom)])
    result = sandbox.execute(
        "try { await tools.boom({}); return 'no-error'; } catch (e) { return 'caught:' + e.message; }"
    )
    assert result.ok, result.error
    assert result.value == "caught:nope"
    assert [(call.name, call.status) for call in result.calls] == [("boom", "error")]


def test_console_items_are_marked() -> None:
    sandbox = CodemodeSandbox()
    result = sandbox.execute("console.log('hello', 42); return 1;")
    assert result.ok, result.error
    assert [(item.text, item.console) for item in result.output] == [("hello 42", True)]


def test_image_output_is_validated_and_collected() -> None:
    sandbox = CodemodeSandbox()
    encoded = base64.b64encode(PNG_1X1).decode()
    result = sandbox.execute(f"image('data:image/png;base64,{encoded}'); return 1;")
    assert result.ok, result.error
    assert len(result.output) == 1
    item = result.output[0]
    assert item.type == "image"
    assert item.mime_type == "image/png"
    assert item.data == encoded


def test_remote_image_urls_are_rejected() -> None:
    sandbox = CodemodeSandbox()
    result = sandbox.execute("image('https://example.com/x.png'); return 1;")
    assert not result.ok
    assert result.error is not None
    assert "remote image URLs" in result.error.message


def test_store_writes_are_reported() -> None:
    sandbox = CodemodeSandbox()
    result = sandbox.execute("store('runs', (load('runs') ?? 0) + 1); return load('runs');")
    assert result.ok, result.error
    assert result.value == 1
    assert result.store_writes.set == {"runs": 1}
    assert result.store_writes.delete == []


def test_execute_never_raises_for_script_failures() -> None:
    sandbox = CodemodeSandbox()
    result: CodemodeResult = sandbox.execute("throw new Error('boom');")
    assert not result.ok
    assert result.error is not None
    assert result.error.kind == "script"
    assert "boom" in result.error.message
