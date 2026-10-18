"""``to_llm_content`` 的行为规格。

逐条对齐上游 ``packages/mcp/test/content.test.ts``（v1.3.0，39 行）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pi_mcp import to_llm_content


def test_passes_text_and_images_through_and_replaces_other_blocks_with_text() -> None:
    """上游 content.test.ts:5 —— 文本/图像原样通过，其余块变文本占位。"""
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": "hello", "annotations": {"priority": 1}},
        {"type": "image", "data": "aW1n", "mimeType": "image/png", "_meta": {"x": 1}},
        {"type": "audio", "data": "YXVk", "mimeType": "audio/wav"},
        {"type": "resource_link", "uri": "file:///a.txt", "name": "a.txt"},
        {"type": "resource", "resource": {"uri": "file:///b.txt", "text": "inline"}},
        {
            "type": "resource",
            "resource": {"uri": "file:///c.png", "mimeType": "image/png", "blob": "Yw=="},
        },
        {"type": "resource", "resource": {"uri": "file:///d.bin", "blob": "ZA=="}},
    ]
    assert to_llm_content({"content": blocks}) == [
        {"type": "text", "text": "hello"},
        {"type": "image", "data": "aW1n", "mimeType": "image/png"},
        {"type": "text", "text": "[audio audio/wav omitted]"},
        {"type": "text", "text": "a.txt: file:///a.txt"},
        {"type": "text", "text": "inline"},
        {"type": "image", "data": "Yw==", "mimeType": "image/png"},
        {"type": "text", "text": "[binary resource file:///d.bin (unknown type) omitted]"},
    ]


def test_falls_back_to_the_structured_content_as_json_when_there_are_no_blocks() -> None:
    """上游 content.test.ts:26 —— 无内容块时用 ``structuredContent`` 的 JSON（#10416）。"""
    assert to_llm_content({"content": [], "structuredContent": [1, 2]}) == [
        {"type": "text", "text": "[\n  1,\n  2\n]"}
    ]
    assert to_llm_content({"content": [], "structuredContent": None}) == []
    assert to_llm_content({"content": [], "structuredContent": {"n": 1}}) == [
        {"type": "text", "text": '{\n  "n": 1\n}'}
    ]
    assert to_llm_content(
        {"content": [{"type": "text", "text": "n=1"}], "structuredContent": {"n": 1}}
    ) == [{"type": "text", "text": "n=1"}]


def test_structured_content_false_and_zero_are_still_rendered() -> None:
    """本端补充：``False`` / ``0`` / ``""`` 不是 ``None``，应当照样渲染出来。"""
    assert to_llm_content({"content": [], "structuredContent": False}) == [
        {"type": "text", "text": "false"}
    ]
    assert to_llm_content({"content": [], "structuredContent": 0}) == [
        {"type": "text", "text": "0"}
    ]
    assert to_llm_content({"content": [], "structuredContent": ""}) == [
        {"type": "text", "text": '""'}
    ]
    # null 是唯一被当作「没有结构化结果」的值（与上游 ``!== undefined`` 对齐：
    # 上游 undefined 才跳过，JSON 的 null 会渲染成 "null"）。
    assert to_llm_content({"content": [], "structuredContent": None}) == []


def test_unsupported_block_type_becomes_a_placeholder() -> None:
    """本端补充：未知块类型不炸，变成一句占位文本。"""
    assert to_llm_content({"content": [{"type": "future_thing"}]}) == [
        {"type": "text", "text": "[unsupported MCP content future_thing]"}
    ]


# ============================================================
# 跨包形状差异 E1：返回元素必须**同时**支持下标与属性访问
# ------------------------------------------------------------
# 上游 ``LlmContent`` 是 TS 对象字面量类型，消费者写 ``block.text``；Python 的 dict 没有
# 属性，机械照搬的下游代码会 AttributeError（下游实测踩过 4 条测试）。
# 见 PORTING.md〈跨包形状差异清单〉E1。
# ============================================================


def test_blocks_support_attribute_access_like_upstream_objects() -> None:
    """属性访问是**契约**，不是便利：下游按 ``.type`` / ``.text`` 读取。"""
    text, image = to_llm_content(
        {
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "data": "aW1n", "mimeType": "image/png"},
            ]
        }
    )
    assert text.type == "text"
    assert text.text == "hello"
    assert image.type == "image"
    assert image.data == "aW1n"
    assert image.mimeType == "image/png"


def test_blocks_accept_snake_case_aliases_for_camel_case_keys() -> None:
    """``mimeType`` 键也能用 ``.mime_type`` 读 —— pi-ai 的 ``ImageContent`` 就是 snake_case 字段。

    这让「从 pi-ai 那边机械搬过来的读法」也能直接用。
    """
    (image,) = to_llm_content(
        {"content": [{"type": "image", "data": "aW1n", "mimeType": "image/png"}]}
    )
    assert image.mime_type == "image/png"
    assert image.mimeType == "image/png"


def test_attribute_assignment_writes_the_key() -> None:
    """属性赋值写回键（对应上游直接改对象字段）。"""
    (text,) = to_llm_content({"content": [{"type": "text", "text": "a"}]})
    text.text = "b"
    assert text["text"] == "b"
    assert text == {"type": "text", "text": "b"}


def test_missing_attribute_raises_attribute_error() -> None:
    """缺键走属性访问要抛 ``AttributeError``（``hasattr`` / ``getattr`` 默认值依赖它）。"""
    (text,) = to_llm_content({"content": [{"type": "text", "text": "a"}]})
    assert hasattr(text, "nope") is False
    assert getattr(text, "nope", "fallback") == "fallback"
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        _ = text.nope


def test_blocks_keep_full_mapping_semantics() -> None:
    """仍然是货真价实的 ``dict``：既有消费者的写法一条都不该坏。"""
    import copy
    import json

    (text,) = to_llm_content({"content": [{"type": "text", "text": "a"}]})
    assert isinstance(text, dict)
    assert text == {"type": "text", "text": "a"}
    assert dict(text) == {"type": "text", "text": "a"}
    assert {**text} == {"type": "text", "text": "a"}
    assert json.loads(json.dumps(text)) == {"type": "text", "text": "a"}
    assert copy.deepcopy(text) == {"type": "text", "text": "a"}
    assert text.get("text") == "a"
    assert list(text.keys()) == ["type", "text"]
    # dict 自带的方法不会被属性访问劫持。
    assert callable(text.keys)
    assert callable(text.items)


def test_blocks_work_as_keyword_arguments_for_object_construction() -> None:
    """``**block`` 直接喂给目标对象构造器 —— 下游适配器可以退化成这一步。"""

    class _FakeTextContent:
        def __init__(self, *, type: str, text: str) -> None:  # noqa: A002 - 对齐 pi-ai 字段名
            self.type = type
            self.text = text

    class _FakeImageContent:
        def __init__(self, *, type: str, data: str, mimeType: str) -> None:  # noqa: A002, N803
            self.type = type
            self.data = data
            self.mime_type = mimeType

    text, image = to_llm_content(
        {
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "data": "aW1n", "mimeType": "image/png"},
            ]
        }
    )
    built_text = _FakeTextContent(**text)
    built_image = _FakeImageContent(**image)
    assert (built_text.type, built_text.text) == ("text", "hello")
    assert (built_image.type, built_image.mime_type) == ("image", "image/png")


def test_structured_content_fallback_also_returns_attribute_blocks() -> None:
    """JSON 回退出来的块同样是 ``LlmContentBlock``（形状一致，不搞例外）。"""
    from pi_mcp.protocol.content import LlmContentBlock

    (block,) = to_llm_content({"content": [], "structuredContent": {"n": 1}})
    assert isinstance(block, LlmContentBlock)
    assert block.type == "text"
    assert json.loads(block.text) == {"n": 1}
