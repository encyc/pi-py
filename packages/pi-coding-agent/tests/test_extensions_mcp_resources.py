"""C2a 测试：MCP 资源工具（``extensions/mcp/resources.ts``）。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pi_coding_agent.extensions.mcp.resources import (
    LIST_MCP_RESOURCE_TEMPLATES_TOOL,
    LIST_MCP_RESOURCES_TOOL,
    READ_MCP_RESOURCE_TOOL,
    create_mcp_resource_tool_definitions,
    is_mcp_app_resource,
    listed,
    string_argument,
)

REQ = {"signal": None, "timeoutMs": 100}


class _Server:
    """最小的 McpResourceServer 实现。"""

    def __init__(
        self, name: str, *, pages: list[dict[str, Any]] | None = None, fail: bool = False
    ) -> None:
        self.name = name
        self.timeout_ms = 100
        self._pages = pages or [{"resources": []}]
        self._fail = fail
        self.read_uris: list[str] = []

    async def resources_page(self, cursor: str | None, options: Any) -> dict[str, Any]:
        if self._fail:
            raise RuntimeError(f"{self.name} down")
        index = int(cursor) if cursor else 0
        return self._pages[index]

    async def resource_templates_page(self, cursor: str | None, options: Any) -> dict[str, Any]:
        if self._fail:
            raise RuntimeError(f"{self.name} down")
        return {"resourceTemplates": [{"uriTemplate": "file:///{p}", "name": "t"}]}

    async def all_resources(self, options: Any) -> list[dict[str, Any]]:
        if self._fail:
            raise RuntimeError(f"{self.name} down")
        return [{"uri": f"file:///{self.name}", "name": self.name}]

    async def all_resource_templates(self, options: Any) -> list[dict[str, Any]]:
        if self._fail:
            raise RuntimeError(f"{self.name} down")
        return [{"uriTemplate": "file:///{p}", "name": "t"}]

    async def read_resource(self, uri: str, options: Any) -> dict[str, Any]:
        self.read_uris.append(uri)
        return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": f"body of {uri}"}]}


def _tools(servers: list[Any], exposure: str = "direct") -> dict[str, Any]:
    return {
        tool.name: tool
        for tool in create_mcp_resource_tool_definitions(exposure=exposure, servers=lambda: servers)
    }


# ============================================================
# 辅助函数
# ============================================================


def test_is_mcp_app_resource():
    assert is_mcp_app_resource({"uri": "ui://x"}) is True
    assert is_mcp_app_resource({"uriTemplate": "ui://x/{id}"}) is True
    assert (
        is_mcp_app_resource({"uri": "file:///a", "mimeType": "text/html;profile=mcp-app"}) is True
    )
    assert (
        is_mcp_app_resource({"uri": "file:///a", "mimeType": 'text/html; profile="mcp-app"'})
        is True
    )
    assert is_mcp_app_resource({"uri": "file:///a", "mimeType": "text/html"}) is False
    assert is_mcp_app_resource({}) is False


def test_listed_strips_meta_and_icons():
    item = {"uri": "file:///a", "name": "a", "_meta": {"x": 1}, "icons": [1]}
    assert listed("srv", item) == {"server": "srv", "uri": "file:///a", "name": "a"}


def test_string_argument_trims_and_rejects_non_strings():
    assert string_argument({"a": " x "}, "a") == "x"
    assert string_argument({"a": "   "}, "a") is None
    assert string_argument({"a": None}, "a") is None
    assert string_argument({}, "a") is None
    with pytest.raises(ValueError, match="a must be a string"):
        string_argument({"a": 5}, "a")


# ============================================================
# 工具定义形状
# ============================================================


def test_three_tools_with_read_only_annotations_and_exposure():
    tools = _tools([], exposure="codemode")
    assert set(tools) == {
        LIST_MCP_RESOURCES_TOOL,
        LIST_MCP_RESOURCE_TEMPLATES_TOOL,
        READ_MCP_RESOURCE_TOOL,
    }
    for tool in tools.values():
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        # codemode → deferred
        assert tool.exposure == "deferred"
        assert tool.output_schema is not None
    assert tools[READ_MCP_RESOURCE_TOOL].parameters["required"] == ["server", "uri"]
    assert tools[LIST_MCP_RESOURCES_TOOL].parameters["additionalProperties"] is False


# ============================================================
# list：指定 server（单页）
# ============================================================


async def test_list_one_server_page_and_cursor():
    page1 = {
        "resources": [{"uri": "file:///a", "name": "a", "_meta": {}}],
        "nextCursor": "1",
    }
    page2 = {"resources": [{"uri": "file:///b", "name": "b"}], "nextCursor": "2"}
    srv = _Server("docs", pages=[page1, page2])
    tools = _tools([srv])

    first = await tools[LIST_MCP_RESOURCES_TOOL].execute("id", {"server": "docs"})
    payload = first.structured_content
    assert payload["server"] == "docs"
    assert payload["nextCursor"] == "1"
    assert payload["resources"] == [{"server": "docs", "uri": "file:///a", "name": "a"}]

    second = await tools[LIST_MCP_RESOURCES_TOOL].execute("id", {"server": "docs", "cursor": "1"})
    assert second.structured_content["resources"] == [
        {"server": "docs", "uri": "file:///b", "name": "b"}
    ]


async def test_list_one_server_excludes_mcp_app_resources():
    srv = _Server(
        "docs",
        pages=[
            {
                "resources": [
                    {"uri": "file:///a", "name": "a"},
                    {"uri": "ui://app", "name": "app"},
                ]
            }
        ],
    )
    tools = _tools([srv])
    payload = (
        await tools[LIST_MCP_RESOURCES_TOOL].execute("id", {"server": "docs"})
    ).structured_content
    assert [item["name"] for item in payload["resources"]] == ["a"]


async def test_list_content_is_json_text():
    srv = _Server("docs", pages=[{"resources": [{"uri": "file:///a", "name": "a"}]}])
    tools = _tools([srv])
    result = await tools[LIST_MCP_RESOURCES_TOOL].execute("id", {"server": "docs"})
    assert json.loads(result.content[0].text)["resources"][0]["uri"] == "file:///a"
    assert result.details.server == "docs"
    assert result.details.tool == LIST_MCP_RESOURCES_TOOL


# ============================================================
# list：不指定 server（全部）
# ============================================================


async def test_list_all_servers_sorted_and_namespaced():
    alpha, beta = _Server("beta"), _Server("alpha")
    tools = _tools([alpha, beta])
    payload = (await tools[LIST_MCP_RESOURCES_TOOL].execute("id", {})).structured_content
    # 按名字排序：alpha 在前
    assert [item["server"] for item in payload["resources"]] == ["alpha", "beta"]
    assert "server" not in payload  # 未指定 server 时不带该字段
    assert "errors" not in payload


async def test_list_all_servers_reports_per_server_errors():
    good, bad = _Server("good"), _Server("bad", fail=True)
    tools = _tools([good, bad])
    payload = (await tools[LIST_MCP_RESOURCES_TOOL].execute("id", {})).structured_content
    assert [item["server"] for item in payload["resources"]] == ["good"]
    assert payload["errors"] == [{"server": "bad", "error": "bad down"}]


async def test_cursor_without_server_is_an_error():
    tools = _tools([_Server("a")])
    with pytest.raises(ValueError, match="cursor can only be used when a server is specified"):
        await tools[LIST_MCP_RESOURCES_TOOL].execute("id", {"cursor": "x"})


async def test_unknown_server_lists_available_ones():
    tools = _tools([_Server("a"), _Server("b")])
    with pytest.raises(ValueError, match="Servers with resources: a, b"):
        await tools[LIST_MCP_RESOURCES_TOOL].execute("id", {"server": "nope"})


async def test_unknown_server_without_any_lists_no_suffix():
    tools = _tools([])
    with pytest.raises(ValueError, match=r"has no resources$"):
        await tools[LIST_MCP_RESOURCES_TOOL].execute("id", {"server": "nope"})


# ============================================================
# templates
# ============================================================


async def test_list_templates_key_and_shape():
    tools = _tools([_Server("docs")])
    payload = (
        await tools[LIST_MCP_RESOURCE_TEMPLATES_TOOL].execute("id", {"server": "docs"})
    ).structured_content
    assert payload["server"] == "docs"
    assert payload["resourceTemplates"][0]["server"] == "docs"


# ============================================================
# read
# ============================================================


async def test_read_resource_single_content():
    srv = _Server("docs")
    tools = _tools([srv])
    result = await tools[READ_MCP_RESOURCE_TOOL].execute(
        "id", {"server": "docs", "uri": "file:///a.txt"}
    )
    assert srv.read_uris == ["file:///a.txt"]
    assert "body of file:///a.txt" in result.content[0].text
    assert result.structured_content == {
        "server": "docs",
        "uri": "file:///a.txt",
        "contents": [
            {"uri": "file:///a.txt", "mimeType": "text/plain", "text": "body of file:///a.txt"}
        ],
    }
    assert result.details.tool == READ_MCP_RESOURCE_TOOL


async def test_read_resource_multiple_contents_are_labeled_by_uri():
    class _Multi(_Server):
        async def read_resource(self, uri: str, options: Any) -> dict[str, Any]:
            return {
                "contents": [
                    {"uri": "file:///a", "mimeType": "text/plain", "text": "A"},
                    {"uri": "file:///b", "mimeType": "text/plain", "text": "B"},
                ]
            }

    tools = _tools([_Multi("docs")])
    result = await tools[READ_MCP_RESOURCE_TOOL].execute(
        "id", {"server": "docs", "uri": "file:///dir"}
    )
    # 多个内容 → 多个内容块（URI 标签 + 正文各一块）；拼起来看
    text = "\n".join(block.text for block in result.content)
    assert "file:///a:" in text
    assert "file:///b:" in text
    assert "A" in text and "B" in text


async def test_read_resource_strips_meta_in_structured_content():
    class _Meta(_Server):
        async def read_resource(self, uri: str, options: Any) -> dict[str, Any]:
            return {"contents": [{"uri": uri, "text": "x", "_meta": {"hidden": 1}}]}

    tools = _tools([_Meta("docs")])
    result = await tools[READ_MCP_RESOURCE_TOOL].execute(
        "id", {"server": "docs", "uri": "file:///a"}
    )
    assert "_meta" not in result.structured_content["contents"][0]


async def test_read_resource_requires_server_and_uri():
    tools = _tools([_Server("docs")])
    with pytest.raises(ValueError, match="server must be provided"):
        await tools[READ_MCP_RESOURCE_TOOL].execute("id", {"uri": "file:///a"})
    with pytest.raises(ValueError, match="uri must be provided"):
        await tools[READ_MCP_RESOURCE_TOOL].execute("id", {"server": "docs"})


async def test_read_resource_empty_content_gets_placeholder():
    class _Empty(_Server):
        async def read_resource(self, uri: str, options: Any) -> dict[str, Any]:
            return {"contents": []}

    tools = _tools([_Empty("docs")])
    result = await tools[READ_MCP_RESOURCE_TOOL].execute(
        "id", {"server": "docs", "uri": "file:///a"}
    )
    assert "Resource file:///a is empty." in result.content[0].text
