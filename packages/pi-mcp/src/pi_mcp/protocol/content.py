"""MCP 内容块与「转成 LLM 可接受形状」的转换。

对应上游 ``packages/mcp/src/protocol/content.ts``（v1.3.0，118 行）。

上游的 ``interface`` 在 Python 侧用 ``TypedDict``：这些是**线上 JSON 的形状描述**，
服务端返回的对象原样保留未知键，因此只声明已知键、不做转换。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, TypedDict


class ContentAnnotations(TypedDict, total=False):
    """内容注解。"""

    audience: list[Literal["user", "assistant"]]
    priority: int
    lastModified: str


class _TextContentRequired(TypedDict):
    type: Literal["text"]
    text: str


class TextContent(_TextContentRequired, total=False):
    """文本块。"""

    annotations: ContentAnnotations
    _meta: dict[str, Any]


class _ImageContentRequired(TypedDict):
    type: Literal["image"]
    data: str
    mimeType: str


class ImageContent(_ImageContentRequired, total=False):
    """图像块（``data`` 为 base64）。"""

    annotations: ContentAnnotations
    _meta: dict[str, Any]


class _AudioContentRequired(TypedDict):
    type: Literal["audio"]
    data: str
    mimeType: str


class AudioContent(_AudioContentRequired, total=False):
    """音频块（``data`` 为 base64）。"""

    annotations: ContentAnnotations
    _meta: dict[str, Any]


class _ResourceLinkContentRequired(TypedDict):
    type: Literal["resource_link"]
    uri: str
    name: str


class ResourceLinkContent(_ResourceLinkContentRequired, total=False):
    """资源链接块。"""

    title: str
    description: str
    mimeType: str
    size: int
    annotations: ContentAnnotations
    _meta: dict[str, Any]


class _TextResourceContentsRequired(TypedDict):
    uri: str
    text: str


class TextResourceContents(_TextResourceContentsRequired, total=False):
    """内嵌文本资源。"""

    mimeType: str
    _meta: dict[str, Any]


class _BlobResourceContentsRequired(TypedDict):
    uri: str
    blob: str


class BlobResourceContents(_BlobResourceContentsRequired, total=False):
    """内嵌二进制资源（``blob`` 为 base64）。"""

    mimeType: str
    _meta: dict[str, Any]


class _EmbeddedResourceContentRequired(TypedDict):
    type: Literal["resource"]
    resource: TextResourceContents | BlobResourceContents


class EmbeddedResourceContent(_EmbeddedResourceContentRequired, total=False):
    """内嵌资源块。"""

    annotations: ContentAnnotations
    _meta: dict[str, Any]


#: 任意内容块。
ContentBlock = (
    TextContent | ImageContent | AudioContent | ResourceLinkContent | EmbeddedResourceContent
)


class CallToolResult(TypedDict, total=False):
    """``tools/call`` 的结果。

    ``content`` 按规范必填，但只返回 ``structuredContent`` 的服务端会省略它
    （官方 SDK 也默认补空列表）。``structuredContent`` 可以是任意 JSON 值
    （MCP 2026-07-28 起；更早的修订只允许对象，见上游 #10416）。
    """

    content: list[ContentBlock]
    structuredContent: Any
    isError: bool
    _meta: dict[str, Any]


class _LlmTextContentRequired(TypedDict):
    type: Literal["text"]
    text: str


class LlmTextContent(_LlmTextContentRequired, total=False):
    """LLM API 接受的文本块（与 pi-ai 的 ``TextContent`` 同形）。"""


class _LlmImageContentRequired(TypedDict):
    type: Literal["image"]
    data: str
    mimeType: str


class LlmImageContent(_LlmImageContentRequired, total=False):
    """LLM API 接受的图像块（与 pi-ai 的 ``ImageContent`` 同形）。"""


def _camel_case(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class LlmContentBlock(dict[str, Any]):
    """``to_llm_content`` 的返回元素：既是 ``dict``，也能**按属性访问**。

    形状与上游 ``LlmContent`` 一致（``{"type": "text", "text": …}`` /
    ``{"type": "image", "data": …, "mimeType": …}``），额外的属性访问是为了跨过
    **TS 结构类型 → Python 的接缝**：上游那是对象字面量类型，消费者写 ``block.text``；
    Python 的 ``dict`` 没有属性，机械照搬过来的下游代码会 ``AttributeError``
    （见 ``PORTING.md``〈跨包形状差异清单〉E1）。

    属性名接受三种拼写，都能命中同一个键：

    - 原键（``block.mimeType``）
    - 其 ``snake_case``（``block.mime_type``）
    - 其 ``camelCase``（键为 ``mime_type`` 时也可写 ``block.mimeType``）

    这样从 pi-ai 的 ``ImageContent``（字段 ``mime_type``、alias ``mimeType``）那边
    机械搬过来的读法也能直接用。

    仍然是货真价实的 ``dict``：``isinstance(x, dict)``、``==`` 比较、``json.dumps``、
    ``**`` 展开、``copy.deepcopy``、``dict(x)`` 全都照旧。
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        # ``__getattr__`` 只在常规查找失败时被调用，因此 ``keys``/``items`` 等仍是 dict 的方法。
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        for candidate in (name, _camel_case(name), _snake_case(name)):
            if candidate in self:
                return self[candidate]
        # 必须是 AttributeError：``hasattr`` / ``getattr(x, name, default)`` 依赖它。
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        """属性赋值写回键（对应上游直接改对象字段）。"""
        self[name] = value


#: LLM API 接受的内容块：文本与 base64 图像。
#:
#: 运行时类型是 :class:`LlmContentBlock`（``dict`` 子类 + 属性访问）。上游 ``LlmContent``
#: 是对象字面量类型的联合，这里把名字指向**运行时载体**，让 ``LlmContent`` 既能当注解也能
#: 反映真实返回类型；键的形状仍由 :class:`LlmTextContent` / :class:`LlmImageContent` 记录。
LlmContent = LlmContentBlock


def _as_text(text: str) -> LlmContentBlock:
    return LlmContentBlock(type="text", text=text)


def _as_image(data: str, mime_type: str) -> LlmContentBlock:
    return LlmContentBlock(type="image", data=data, mimeType=mime_type)


def _block_to_llm_content(block: dict[str, Any]) -> LlmContentBlock:
    """单个内容块 → LLM 内容块。"""
    kind = block.get("type")
    if kind == "text":
        return _as_text(str(block.get("text", "")))
    if kind == "image":
        return _as_image(str(block.get("data", "")), str(block.get("mimeType", "")))
    if kind == "audio":
        return _as_text(f"[audio {block.get('mimeType')} omitted]")
    if kind == "resource_link":
        return _as_text(f"{block.get('name')}: {block.get('uri')}")
    if kind == "resource":
        resource = block.get("resource")
        if isinstance(resource, dict):
            if "text" in resource:
                return _as_text(str(resource.get("text", "")))
            mime_type = resource.get("mimeType")
            if isinstance(mime_type, str) and mime_type.startswith("image/"):
                return _as_image(str(resource.get("blob", "")), mime_type)
            return _as_text(
                f"[binary resource {resource.get('uri')} ({mime_type or 'unknown type'}) omitted]"
            )
    return _as_text(f"[unsupported MCP content {kind}]")


def to_llm_content(result: dict[str, Any]) -> list[LlmContentBlock]:
    """把工具结果转成给模型的文本/图像内容。

    文本与图像原样通过；内嵌文本资源变文本、内嵌图像资源变图像；其余块（音频、资源链接、
    二进制资源）变成一句短文本占位。没有内容块但有 ``structuredContent`` 时，结果是它的 JSON
    —— 服务端**应当**把结构化结果同时镜像成文本，但并非总是如此。

    入参形如 ``{"content": [...], "structuredContent": ...}``（对应上游
    ``Pick<CallToolResult, "content" | "structuredContent">``）。

    返回元素是 :class:`LlmContentBlock`：既能 ``block["text"]`` 也能 ``block.text``
    （上游是 TS 对象字面量，消费者按属性访问；详见 ``PORTING.md``〈跨包形状差异清单〉E1）。
    """
    raw = result.get("content")
    blocks = raw if isinstance(raw, list) else []
    content = [_block_to_llm_content(block) for block in blocks if isinstance(block, dict)]
    if not content and result.get("structuredContent") is not None:
        content.append(
            _as_text(json.dumps(result["structuredContent"], indent=2, ensure_ascii=False))
        )
    return content


__all__ = [
    "AudioContent",
    "BlobResourceContents",
    "CallToolResult",
    "ContentAnnotations",
    "ContentBlock",
    "EmbeddedResourceContent",
    "ImageContent",
    "LlmContent",
    "LlmContentBlock",
    "LlmImageContent",
    "LlmTextContent",
    "ResourceLinkContent",
    "TextContent",
    "TextResourceContents",
    "to_llm_content",
]
