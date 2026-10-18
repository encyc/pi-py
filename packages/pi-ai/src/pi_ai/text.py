"""内容块与 system message 的文本渲染。

对应上游 ``utils/text.ts``。纯函数，无副作用。
"""

from __future__ import annotations

from typing import Any

from .types import SystemMessage

__all__ = ["content_text", "get_system_message_text", "render_system_message_update"]


def content_text(content: Any, separator: str = "\n\n") -> str:
    """把内容块列表（或字符串）渲染为纯文本。对应上游 ``contentText``。

    仅拼接 ``text`` 块的 ``text`` 字段；图像/思考/工具调用块不产生文本。
    """
    if isinstance(content, str):
        return content
    if not content:
        return ""
    parts = [block.text for block in content if getattr(block, "type", None) == "text"]
    return separator.join(parts)


def get_system_message_text(message: SystemMessage) -> str:
    """把 system message 渲染为完整 prompt：``content`` + 各 ``sections``。

    对应上游 ``getSystemMessageText``。``sections`` 中值为 ``None`` 的项表示
    该 section 已被删除，不参与渲染。
    """
    parts = [content_text(message.content)]
    for text in (message.sections or {}).values():
        if text is not None:
            parts.append(text)
    return "\n\n".join(part for part in parts if part)


def render_system_message_update(message: SystemMessage) -> str:
    """渲染**后续** system message（供支持会话中途 system 消息的 API 使用）。

    对应上游 ``renderSystemMessageUpdate``。section 变更按名字加框，使模型能把
    更新与首条 prompt 里的同名 section 对应起来。此框选措辞仅存在于请求期，
    不写入会话历史，故允许随版本变化。
    """
    parts: list[str] = []
    text = content_text(message.content)
    if text:
        parts.append(text)
    for name, value in (message.sections or {}).items():
        if value is None:
            parts.append(f'Removed system prompt section "{name}".')
        else:
            parts.append(f'Updated system prompt section "{name}":\n\n{value}')
    return "\n\n".join(parts)
