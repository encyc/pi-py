"""跨模型思考处理测试。

用例对应上游 ``test/transform-messages-cross-model-thinking.test.ts``（v1.3.0）
与 ``api/transform-messages.ts`` 的 ``thinking`` / ``thoughtSignature`` 分支。

背景（上游 CHANGELOG 1.3.0 Changed, issue #10995）：v0.85.1 把**跨模型**的思考块
降级成普通文本发给新模型，新模型会把旧模型的推理当成自己回复的一部分。v1.3.0
反转为**直接丢弃**。签名/加密载荷只对产生它的模型有效，跨模型发送还会让
Anthropic 400（同类问题 issue #10324）。
"""

from __future__ import annotations

from pi_ai.transform_messages import (
    drop_cross_model_thinking,
    transform_assistant_content,
)
from pi_ai.types import AssistantMessage, Model, TextContent, ThinkingContent, ToolCall

KIMI = Model(
    id="moonshotai/Kimi-K3",
    name="Kimi K3",
    api="openai-completions",
    provider="sference",
    base_url="https://api.sference.com/v1",
    reasoning=True,
    input=["text", "image"],
    context_window=1048576,
    max_tokens=32768,
)


def assistant(model: str, content: list[object]) -> AssistantMessage:
    return AssistantMessage(
        content=content,  # type: ignore[arg-type]
        api="openai-completions",
        provider="sference",
        model=model,
        stop_reason="toolUse",
        timestamp=1,
    )


def thinking(text: str) -> ThinkingContent:
    return ThinkingContent(thinking=text, thinking_signature="reasoning_content")


def test_does_not_send_another_models_readable_thinking_as_text():
    """issue #10995：跨模型思考**不得**作为文本出现，且完全丢弃。"""
    msg = assistant(
        "zai-org/GLM-5.3",
        [
            thinking("The user asks about ECONNRESET. Candidates: proxy, IPv6, TLS."),
            TextContent(text="Let me check the TLS versions."),
            ToolCall(id="call_1", name="bash", arguments={"command": "curl -v"}),
        ],
    )

    result = transform_assistant_content(list(msg.content), msg, KIMI)

    assert [b.type for b in result] == ["text", "toolCall"]
    assert result[0].text == "Let me check the TLS versions."
    # 明确断言：没有产生任何携带思考文本的 text 块
    assert not any(
        b.type == "text" and "ECONNRESET" in b.text for b in result if isinstance(b, TextContent)
    )


def test_keeps_thinking_only_from_target_model_when_models_alternate():
    """交替模型时只有目标模型的思考被保留。"""
    kimi_1 = assistant("moonshotai/Kimi-K3", [thinking("kimi 1"), TextContent(text="a")])
    glm_1 = assistant("zai-org/GLM-5.3", [thinking("glm 1"), TextContent(text="b")])
    kimi_2 = assistant("moonshotai/Kimi-K3", [thinking("kimi 2"), TextContent(text="c")])
    glm_2 = assistant("zai-org/GLM-5.3", [thinking("glm 2"), TextContent(text="d")])

    kept = [
        [b.type for b in transform_assistant_content(list(m.content), m, KIMI)]
        for m in (kimi_1, glm_1, kimi_2, glm_2)
    ]

    assert kept == [
        ["thinking", "text"],  # 同模型：保留
        ["text"],  # 跨模型：丢弃
        ["thinking", "text"],
        ["text"],
    ]


def test_same_model_keeps_empty_thinking_when_redacted_or_signed():
    """同模型下 redacted / 带签名的空思考必须保留（OpenAI 加密推理靠它回放）。"""
    signed_empty = ThinkingContent(thinking="", thinking_signature="sig")
    redacted = ThinkingContent(thinking="", thinking_signature="enc", redacted=True)
    unsigned_empty = ThinkingContent(thinking="")

    kept = drop_cross_model_thinking(
        [signed_empty, redacted, unsigned_empty], assistant("moonshotai/Kimi-K3", []), KIMI
    )
    assert kept == [signed_empty, redacted]

    kept_cross = drop_cross_model_thinking(
        [signed_empty, redacted, unsigned_empty], assistant("other/model", []), KIMI
    )
    assert kept_cross == []


def test_cross_model_clears_tool_call_thought_signature():
    """跨模型时清除 Google 特有的 thoughtSignature（只对原模型有效）。"""
    msg = assistant(
        "google/gemini-3",
        [ToolCall(id="c1", name="t", arguments={"a": 1}, thought_signature="opaque-sig")],
    )
    result = transform_assistant_content(list(msg.content), msg, KIMI)
    assert len(result) == 1
    assert isinstance(result[0], ToolCall)
    assert result[0].thought_signature is None
    # 原对象不被就地修改
    assert msg.content[0].thought_signature == "opaque-sig"  # type: ignore[union-attr]


def test_same_model_keeps_tool_call_thought_signature():
    msg = assistant(
        "moonshotai/Kimi-K3",
        [ToolCall(id="c1", name="t", arguments={}, thought_signature="opaque-sig")],
    )
    result = transform_assistant_content(list(msg.content), msg, KIMI)
    assert isinstance(result[0], ToolCall)
    assert result[0].thought_signature == "opaque-sig"


def test_same_model_requires_provider_api_and_model_to_match():
    """三元组任一不同即视为跨模型（上游 transform-messages.ts:122）。"""
    base = dict(api="openai-completions", provider="sference", model="moonshotai/Kimi-K3")
    variants = [
        {**base, "provider": "other"},
        {**base, "api": "anthropic-messages"},
        {**base, "model": "moonshotai/Kimi-K2"},
    ]
    for variant in variants:
        msg = AssistantMessage(content=[thinking("x")], stop_reason="stop", timestamp=1, **variant)
        assert drop_cross_model_thinking(list(msg.content), msg, KIMI) == []
    same = AssistantMessage(content=[thinking("x")], stop_reason="stop", timestamp=1, **base)
    assert len(drop_cross_model_thinking(list(same.content), same, KIMI)) == 1
