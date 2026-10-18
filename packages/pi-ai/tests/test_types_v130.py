"""v1.3.0 类型层对齐测试（M-2 / M-3 / M-17 / M-21 / M-22）。

对应上游 ``test/message-types.test.ts``、``test/model-types.test.ts`` 的语义，
外加本端 v1.3.0 同步新增字段的断言。
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from pi_ai.types import (
    AssistantMessage,
    Context,
    KnownProvider,
    Message,
    Model,
    ModelCost,
    SimpleStreamOptions,
    StreamOptions,
    SystemMessage,
    TextContent,
    Tool,
    ToolCall,
    ToolReference,
    ToolResultMessage,
    TranscriptContext,
    UserMessage,
)

# ============================================================
# M-2：SystemMessage 与 Message 联合
# ============================================================


def test_system_message_defaults_and_aliases():
    msg = SystemMessage(content="base")
    assert msg.role == "system"
    assert msg.sections is None
    assert msg.tools_added is None
    assert msg.tools_removed is None
    assert msg.timestamp == 0


def test_system_message_accepts_camel_case_aliases():
    msg = SystemMessage.model_validate(
        {
            "role": "system",
            "content": "base",
            "sections": {"a": "A", "b": None},
            "toolsAdded": [{"name": "t", "description": "d", "parameters": {}}],
            "toolsRemoved": [{"name": "old"}],
            "timestamp": 5,
        }
    )
    assert msg.sections == {"a": "A", "b": None}
    assert [t.name for t in msg.tools_added or []] == ["t"]
    assert [t.name for t in msg.tools_removed or []] == ["old"]
    assert msg.timestamp == 5


def test_system_message_content_accepts_text_blocks():
    msg = SystemMessage(content=[TextContent(text="a"), TextContent(text="b")])
    assert msg.content[0].text == "a"  # type: ignore[index]


def test_message_union_discriminates_system_role():
    messages: list[Message] = []
    for raw in (
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": []},
        {"role": "toolResult", "toolCallId": "c", "toolName": "t", "content": [], "isError": False},
    ):
        messages.append(Context.model_validate({"messages": [raw]}).messages[0])
    assert [type(m).__name__ for m in messages] == [
        "SystemMessage",
        "UserMessage",
        "AssistantMessage",
        "ToolResultMessage",
    ]


def test_tool_reference_only_carries_a_name():
    ref = ToolReference(name="t")
    assert ref.name == "t"
    with pytest.raises(ValidationError):
        ToolReference.model_validate({"name": "t", "extra": 1})


# ============================================================
# M-3：ToolCall.argumentsError
# ============================================================


def test_tool_call_arguments_error_defaults_to_none():
    call = ToolCall(id="c1", name="t", arguments={})
    assert call.arguments_error is None


def test_tool_call_arguments_error_roundtrips_camel_case():
    call = ToolCall(
        id="c1", name="t", arguments={}, argumentsError="boom\n\nReceived arguments:\nnot json"
    )
    assert call.arguments_error is not None
    dumped = call.model_dump(by_alias=True)
    assert "argumentsError" in dumped
    assert dumped["argumentsError"].startswith("boom")


# ============================================================
# M-21 / M-11 字段：Model 采样参数分层、消息耗时
# ============================================================


def test_model_sampling_params_by_thinking_level_alias():
    model = Model(
        id="m",
        name="m",
        api="openai-completions",
        provider="p",
        base_url="https://x",
        samplingParams={"top_p": 0.9},
        samplingParamsByThinkingLevel={"high": {"temperature": 0.2}},
    )
    assert model.sampling_params == {"top_p": 0.9}
    assert model.sampling_params_by_thinking_level == {"high": {"temperature": 0.2}}
    dumped = model.model_dump(by_alias=True)
    assert dumped["samplingParamsByThinkingLevel"] == {"high": {"temperature": 0.2}}


def test_assistant_message_duration_and_thinking_level_roundtrip():
    msg = AssistantMessage(durationMs=1500, thinkingLevel="high")
    assert msg.duration_ms == 1500
    assert msg.thinking_level == "high"
    dumped = msg.model_dump(by_alias=True)
    assert dumped["durationMs"] == 1500
    assert dumped["thinkingLevel"] == "high"


def test_tool_result_message_duration_ms():
    msg = ToolResultMessage(toolCallId="c", toolName="t", durationMs=42)
    assert msg.duration_ms == 42
    assert msg.model_dump(by_alias=True)["durationMs"] == 42


def test_tool_result_message_keeps_legacy_added_tool_names():
    """v0.85.1 遗留字段保留（上游改为 SystemMessage.tools_added）。"""
    msg = ToolResultMessage(toolCallId="c", toolName="t", addedToolNames=["x"])
    assert msg.added_tool_names == ["x"]


# ============================================================
# M-22：onProviderStreamEvent 选项
# ============================================================


def test_stream_options_exposes_on_provider_stream_event():
    seen: list[str] = []

    def hook(data: object, model: object) -> None:
        seen.append("called")

    options = StreamOptions(onProviderStreamEvent=hook)
    assert options.on_provider_stream_event is hook
    options.on_provider_stream_event("chunk", None)
    assert seen == ["called"]
    # 函数不参与序列化
    assert "onProviderStreamEvent" not in options.model_dump(by_alias=True)


def test_simple_stream_options_inherits_the_hook():
    options = SimpleStreamOptions(onProviderStreamEvent=lambda *_: None)
    assert options.on_provider_stream_event is not None


# ============================================================
# M-17：KnownProvider / TranscriptContext
# ============================================================


def test_known_provider_includes_azure_typesafe_and_meta():
    values = set(get_args(KnownProvider))
    # 上游 v1.3.0 把 provider id 由 azure-openai-responses 改名为 azure
    assert "azure" in values
    assert "azure-openai-responses" in values  # api id 仍在 KnownApi
    assert {"typesafe", "meta"} <= values


def test_known_provider_literal_matches_get_args_helper():
    assert set(get_args(KnownProvider)) == set(KnownProvider.__args__)


def test_transcript_context_holds_messages_and_equates_structurally():
    ctx = TranscriptContext([UserMessage(content="hi")])
    assert len(ctx.messages) == 1
    assert ctx == TranscriptContext([UserMessage(content="hi")])
    assert ctx != TranscriptContext([])
    with pytest.raises(TypeError):
        hash(ctx)


def test_context_keeps_public_shape_for_callers():
    """M-1 关键不变量：公开 ``Context`` 形状不变，下游无需改动。"""
    ctx = Context(
        system_prompt="p",
        messages=[UserMessage(content="u")],
        tools=[Tool(name="t", description="d")],
    )
    assert ctx.system_prompt == "p"
    assert len(ctx.messages) == 1
    assert ctx.tools is not None and ctx.tools[0].name == "t"


def test_model_cost_tiers_still_supported():
    model = Model(
        id="m",
        name="m",
        api="a",
        provider="p",
        base_url="b",
        cost=ModelCost(input=1, tiers=[{"inputTokensAbove": 200000, "input": 2, "output": 3}]),
    )
    assert model.cost.tiers is not None
    assert model.cost.tiers[0].input_tokens_above == 200000
