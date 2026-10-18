"""按目标模型变换历史消息。

对应上游 ``api/transform-messages.ts``。本模块目前只移植 **v1.3.0 跨模型思考丢弃**
这一项（M-6）；上游同文件里的图像降级（``downgradeUnsupportedImages``）与
孤立工具调用补合成结果（``insertSyntheticToolResults``）本端尚未移植，
在 ``PORTING.md``「未完成」中登记。

**为什么必须移植**：思考块及其签名/加密载荷**只对产生它的模型有效**。
- v0.85.1 的做法是把跨模型的思考降级成普通文本；
- v1.3.0 反转为**直接丢弃**（上游 CHANGELOG 1.3.0 Changed, issue #10995）：
  新模型会把旧模型的推理当成自己回复的一部分，进而开始把自身推理写进可见答案；
  更严重的是签名不匹配会让 Anthropic 直接 400（上游 issue #10324 同类问题）。

本端 v0.85.1 移植时既没有"降级为文本"也没有跨模型判断——**带签名就回放**，
因此跨模型切换时会发送无效签名。本次对齐的是"判断 + 丢弃"。
"""

from __future__ import annotations

from typing import Any

from .transcript import assistant_is_same_model
from .types import AssistantMessage, Model

__all__ = ["drop_cross_model_thinking", "transform_assistant_content"]


def drop_cross_model_thinking(
    content: list[Any], message: AssistantMessage, model: Model
) -> list[Any]:
    """同模型时原样返回；跨模型时丢弃思考块。

    对应上游 ``transform-messages.ts:127-146`` 的 ``thinking`` 分支：

    - 跨模型 → 丢弃（**不再**降级为文本）；
    - 同模型 + ``redacted`` 或带 ``thinking_signature`` → 保留（即便思考文本为空，
      OpenAI 加密推理靠它回放）；
    - 同模型 + 思考文本为空 → 丢弃；
    - 其余 → 保留。
    """
    if assistant_is_same_model(message, model):
        kept = []
        for block in content:
            if getattr(block, "type", None) == "thinking":
                text = getattr(block, "thinking", "") or ""
                replayable = getattr(block, "redacted", False) or getattr(
                    block, "thinking_signature", None
                )
                if not replayable and not text.strip():
                    continue
            kept.append(block)
        return kept
    return [block for block in content if getattr(block, "type", None) != "thinking"]


def transform_assistant_content(
    content: list[Any], message: AssistantMessage, model: Model
) -> list[Any]:
    """变换一条 assistant 消息的内容块，使其可安全发给 ``model``。

    对应上游 ``transformAssistantContent``：
    - 跨模型丢弃思考块（:func:`drop_cross_model_thinking`）；
    - 跨模型清除工具调用的 ``thought_signature``（Google 的签名只对该模型有效，
      上游 ``transform-messages.ts:152-155``）。

    上游在此处还会跨 provider 归一化工具调用 ID；本端的 ID 归一化按 provider
    就地实现（Anthropic 侧截断/替换非法字符），不在此重复。
    """
    result = drop_cross_model_thinking(content, message, model)
    if assistant_is_same_model(message, model):
        return result
    cleaned: list[Any] = []
    for block in result:
        if getattr(block, "type", None) == "toolCall" and getattr(block, "thought_signature", None):
            cloned = block.model_copy(deep=True)
            cloned.thought_signature = None
            cleaned.append(cloned)
        else:
            cleaned.append(block)
    return cleaned
