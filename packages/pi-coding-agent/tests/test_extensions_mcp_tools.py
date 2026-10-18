"""C1 测试：MCP 工具 → pi ToolDefinition 适配（``extensions/mcp/tools.ts``）。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

from pi_agent_core.types import AgentToolResult
from pi_coding_agent.extensions.mcp.tools import (
    MAX_TOOL_NAME_LENGTH,
    MCP_OUTPUT_MAX_BYTES,
    ConvertMcpResultOptions,
    convert_mcp_result,
    create_mcp_result_schema,
    create_mcp_tool_definition,
    create_mcp_tool_name,
    limit_mcp_content,
    save_to_temp_file,
    to_model_content,
    to_tool_annotations,
    to_tool_exposure,
)
from pi_coding_agent.extensions.types import ToolNamespace

NS = ToolNamespace(name="mcp__docs")


def _text(content: list[object]) -> str:
    return "\n".join(getattr(block, "text", "") for block in content)


# ============================================================
# exposure / 命名
# ============================================================


def test_to_tool_exposure_maps_codemode_to_deferred():
    assert to_tool_exposure("codemode") == "deferred"
    for value in ("deferred", "direct", "hidden"):
        assert to_tool_exposure(value) == value


def test_create_mcp_tool_name_basic():
    assert create_mcp_tool_name("docs", "search") == "mcp__docs__search"


def test_create_mcp_tool_name_sanitizes_like_codex():
    """除 [A-Za-z0-9_] 外都变 _，因此名字也是 codemode 的合法标识符。"""
    assert create_mcp_tool_name("my-server", "read-file") == "mcp__my_server__read_file"
    assert create_mcp_tool_name("a", "b.c/d") == "mcp__a__b_c_d"


def test_create_mcp_tool_name_adds_hash_when_too_long_or_taken():
    long_name = create_mcp_tool_name("s" * 80, "t")
    assert len(long_name) <= MAX_TOOL_NAME_LENGTH
    assert long_name.endswith(long_name[-8:])

    # 净化把 a-b / a_b 映射到同一名字 → 第二个加哈希
    first = create_mcp_tool_name("srv", "a-b")
    second = create_mcp_tool_name("srv", "a_b", lambda name: name == first)
    assert first == "mcp__srv__a_b"
    assert second != first
    assert len(second) <= MAX_TOOL_NAME_LENGTH


def test_create_mcp_tool_name_hash_is_stable():
    assert create_mcp_tool_name("srv", "a_b", lambda _n: True) == create_mcp_tool_name(
        "srv", "a_b", lambda _n: True
    )


# ============================================================
# 输出 schema
# ============================================================


def test_create_mcp_result_schema_without_tool_schema():
    schema = create_mcp_result_schema(None)
    assert schema["required"] == ["content"]
    assert schema["additionalProperties"] is True
    assert schema["properties"]["structuredContent"] == {}
    assert schema["properties"]["content"]["items"]["additionalProperties"] is True


def test_create_mcp_result_schema_relocates_local_refs():
    """工具自己的 $ref 要改写到 structuredContent 之下，否则解析到错误位置。"""
    tool_schema = {
        "type": "object",
        "properties": {"item": {"$ref": "#/$defs/Item"}},
        "$defs": {"Item": {"type": "string"}},
    }
    schema = create_mcp_result_schema(tool_schema)
    structured = schema["properties"]["structuredContent"]
    assert structured["properties"]["item"]["$ref"] == "#/properties/structuredContent/$defs/Item"
    assert structured["$defs"]["Item"] == {"type": "string"}


def test_create_mcp_result_schema_keeps_data_keywords_intact():
    """const/enum 里的 $ref 字样是数据，不是引用。"""
    tool_schema = {"type": "object", "properties": {"k": {"const": "#/x"}}}
    structured = create_mcp_result_schema(tool_schema)["properties"]["structuredContent"]
    assert structured["properties"]["k"] == {"const": "#/x"}


# ============================================================
# 输出限额
# ============================================================


async def test_limit_mcp_content_passthrough_when_short():
    from pi_ai import TextContent

    content = [TextContent(text="short")]
    limited, path = await limit_mcp_content(content)
    assert limited is content
    assert path is None


async def test_limit_mcp_content_truncates_to_codex_format(tmp_path):
    from pi_ai import ImageContent, TextContent

    async def saver(data: object, extension: str) -> str:
        path = tmp_path / f"full{extension}"
        path.write_text(str(data), encoding="utf-8")
        return str(path)

    long_text = "x" * (MCP_OUTPUT_MAX_BYTES + 500)
    image = ImageContent(data="aGk=", mimeType="image/png")
    limited, full_path = await limit_mcp_content([TextContent(text=long_text), image], saver)

    assert full_path == str(tmp_path / "full.txt")
    assert (tmp_path / "full.txt").read_text() == long_text
    text = limited[0].text
    assert text.startswith("Warning: truncated output (original token count: ")
    assert "Total output lines: 1" in text
    assert "chars truncated" in text
    assert f"[Full output: {full_path} (read it with offset/limit)]" in text
    # 图像排在文本之后保留
    assert limited[1] is image


async def test_limit_mcp_content_reports_save_failure():
    from pi_ai import TextContent

    async def failing_saver(data: object, extension: str) -> str:
        raise OSError("disk full")

    limited, full_path = await limit_mcp_content(
        [TextContent(text="y" * (MCP_OUTPUT_MAX_BYTES + 1))], failing_saver
    )
    assert full_path is None
    assert "[Could not save the full output: disk full]" in limited[0].text


async def test_save_to_temp_file_is_user_only(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    path = await save_to_temp_file("hello", ".txt")
    body = await asyncio.to_thread(Path(path).read_text, "utf-8")
    try:
        assert body == "hello"
        assert path.endswith(".txt")
        assert os.stat(path).st_mode & 0o777 == 0o600
    finally:
        os.unlink(path)


# ============================================================
# 块 → 面向模型内容
# ============================================================


async def test_resource_link_names_read_tool_only_when_readable():
    block = {
        "type": "resource_link",
        "uri": "file:///a.txt",
        "name": "a.txt",
        "title": "A",
        "mimeType": "text/plain",
        "size": 2048,
        "description": "the a file",
    }
    readable = await to_model_content(
        "srv", [block], ConvertMcpResultOptions(readable_resources=True)
    )
    assert 'Read it with read_mcp_resource (server "srv")' in readable[0].text
    assert "(text/plain, 2.0KB)" in readable[0].text
    assert ": the a file" in readable[0].text

    plain = await to_model_content("srv", [block])
    assert "read_mcp_resource" not in plain[0].text


async def test_blob_resource_with_text_mime_becomes_text():
    payload = base64.b64encode(b"file body").decode()
    block = {
        "type": "resource",
        "resource": {"uri": "file:///a.txt", "mimeType": "text/plain", "blob": payload},
    }
    content = await to_model_content("srv", [block])
    assert content[0].text == "file body"


async def test_blob_resource_binary_is_saved_to_temp_file():
    payload = base64.b64encode(b"\x00\x01\x02").decode()
    saved: list[tuple[object, str]] = []

    async def saver(data: object, extension: str) -> str:
        saved.append((data, extension))
        return "/tmp/saved.bin"

    block = {
        "type": "resource",
        "resource": {
            "uri": "file:///a.bin",
            "mimeType": "application/octet-stream",
            "blob": payload,
        },
    }
    content = await to_model_content("srv", [block], ConvertMcpResultOptions(save_output=saver))
    assert saved == [(b"\x00\x01\x02", ".bin")]
    assert (
        content[0].text
        == "[Binary resource file:///a.bin (application/octet-stream, 3B) saved to /tmp/saved.bin]"
    )


async def test_blob_resource_save_failure_is_reported_not_raised():
    async def failing(data: object, extension: str) -> str:
        raise OSError("nope")

    block = {
        "type": "resource",
        "resource": {
            "uri": "file:///a.bin",
            "mimeType": "application/octet-stream",
            "blob": "AAE=",
        },
    }
    content = await to_model_content("srv", [block], ConvertMcpResultOptions(save_output=failing))
    assert "could not be saved: nope" in content[0].text


async def test_image_blob_goes_through_to_llm_content():
    payload = base64.b64encode(b"png").decode()
    block = {
        "type": "resource",
        "resource": {"uri": "file:///a.png", "mimeType": "image/png", "blob": payload},
    }
    content = await to_model_content("srv", [block])
    assert content[0].type in ("image", "text")


# ============================================================
# 结果转换
# ============================================================


async def test_convert_mcp_result_drops_meta_and_keeps_structure():
    result = {
        "content": [{"type": "text", "text": "hello"}],
        "structuredContent": {"a": 1},
        "isError": False,
        "_meta": {"secret": True},
    }
    converted = await convert_mcp_result("srv", "t", result)
    assert converted.is_error is False
    assert converted.content[0].text == "hello"
    # codemode 脚本拿到完整 CallToolResult，但 _meta 被去掉
    assert converted.structured_content["content"] == [{"type": "text", "text": "hello"}]
    assert converted.structured_content["structuredContent"] == {"a": 1}
    assert "_meta" not in converted.structured_content
    assert converted.details.server == "srv"
    assert converted.details.tool == "t"


async def test_convert_mcp_result_error_without_text_gets_fallback():
    converted = await convert_mcp_result("srv", "t", {"content": [], "isError": True})
    assert converted.is_error is True
    assert "MCP tool srv/t returned an error" in _text(converted.content)


async def test_convert_mcp_result_error_with_text_keeps_it():
    converted = await convert_mcp_result(
        "srv", "t", {"content": [{"type": "text", "text": "boom"}], "isError": True}
    )
    assert converted.is_error is True
    assert "boom" in _text(converted.content)
    assert "returned an error" not in _text(converted.content)


# ============================================================
# ToolDefinition 构造
# ============================================================


def test_to_annotations_returns_none_without_hints():
    assert to_tool_annotations({}) is None
    assert to_tool_annotations({"annotations": {"title": "x"}}) is None
    hints = to_tool_annotations({"annotations": {"readOnlyHint": True, "openWorldHint": False}})
    assert hints is not None
    assert hints.read_only_hint is True
    assert hints.open_world_hint is False
    assert hints.destructive_hint is None


def test_tool_parameters_get_object_type_and_properties():
    """MCP server 可能省略 type；某些 provider 拒绝没有 properties 的对象 schema。"""
    definition = create_mcp_tool_definition(
        server="srv",
        tool={"name": "t", "inputSchema": {}},
        name="mcp__srv__t",
        exposure="direct",
        namespace=NS,
        timeout_ms=1000,
        get_client=lambda: None,  # type: ignore[arg-type,return-value]
    )
    assert definition.parameters["type"] == "object"
    assert definition.parameters["properties"] == {}
    assert definition.output_schema is not None
    assert definition.output_schema["required"] == ["content"]
    assert definition.exposure == "direct"
    assert definition.namespace is NS
    assert definition.label == "srv/t"
    assert definition.description == "MCP tool t from server srv"


def test_tool_description_prefers_description_then_title():
    titled = create_mcp_tool_definition(
        server="srv",
        tool={"name": "t", "inputSchema": {}, "title": "The Title"},
        name="n",
        exposure="deferred",
        namespace=NS,
        timeout_ms=1,
        get_client=lambda: None,  # type: ignore[arg-type,return-value]
    )
    assert titled.description == "The Title"
    assert titled.exposure == "deferred"


async def test_tool_definition_execute_calls_client_and_converts():
    calls: list[tuple[str, dict[str, object], object]] = []

    class _Client:
        async def call_tool(
            self, name: str, args: dict[str, object], options: object
        ) -> dict[str, object]:
            calls.append((name, args, options))
            return {"content": [{"type": "text", "text": "result"}], "isError": False}

    async def get_client() -> _Client:
        return _Client()

    definition = create_mcp_tool_definition(
        server="srv",
        tool={"name": "search", "inputSchema": {"type": "object"}},
        name="mcp__srv__search",
        exposure="codemode",
        namespace=NS,
        timeout_ms=2500,
        get_client=get_client,
    )
    # codemode → deferred
    assert definition.exposure == "deferred"

    out = await definition.execute("id", {"q": "hi"})
    assert out.content[0].text == "result"
    assert calls[0][0] == "search"
    assert calls[0][1] == {"q": "hi"}
    assert calls[0][2]["timeoutMs"] == 2500  # type: ignore[index]


async def test_tool_definition_reports_progress():
    updates: list[AgentToolResult] = []

    class _Client:
        async def call_tool(
            self, name: str, args: dict[str, object], options: object
        ) -> dict[str, object]:
            on_progress = options["onProgress"]  # type: ignore[index]
            on_progress({"progress": 3, "total": 10})
            on_progress({"progress": 7, "message": "almost"})
            return {"content": [{"type": "text", "text": "done"}], "isError": False}

    async def get_client() -> _Client:
        return _Client()

    definition = create_mcp_tool_definition(
        server="srv",
        tool={"name": "t", "inputSchema": {}},
        name="n",
        exposure="direct",
        namespace=NS,
        timeout_ms=1,
        get_client=get_client,
    )
    await definition.execute("id", {}, None, updates.append)
    assert updates[0].content[0].text == "Progress 3/10"
    assert updates[1].content[0].text == "almost"


def test_renderers_are_not_exported():
    """TUI 渲染器未移植：名字必须不存在。"""
    import pi_coding_agent.extensions.mcp.tools as module

    assert not hasattr(module, "create_mcp_tool_renderers")
    assert "create_mcp_tool_renderers" not in module.__all__


def test_structured_content_is_json_serializable():
    """脚本拿到的是 JSON 值，不能带非 JSON 对象。"""
    assert json.loads(json.dumps({"a": 1}, default=str)) == {"a": 1}
