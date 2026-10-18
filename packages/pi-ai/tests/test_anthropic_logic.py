"""Anthropic provider 纯逻辑测试：消息转换、工具转换、thinking 配置、usage 提取。

实际网络调用需集成测试（需要 ANTHROPIC_API_KEY，默认跳过）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pi_ai import (
    AssistantMessage,
    Context,
    ImageContent,
    Model,
    StreamOptions,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    normalize_context,
)
from pi_ai.events import ErrorEvent, StartEvent
from pi_ai.providers import anthropic_provider
from pi_ai.providers.anthropic_provider import (
    _STOP_REASON_MAP,
    _build_thinking_config,
    _convert_messages,
    _convert_tools,
)

#: 消息转换测试用的"同模型"句柄。必须与测试里 AssistantMessage 的
#: provider/api/model 三元组一致（provider="anthropic"、api="anthropic-messages"、
#: model="claude"），否则 v1.3.0 的跨模型思考丢弃（M-6）会按预期丢掉 thinking 块。
_CONV_MODEL = Model(
    id="claude",
    name="claude",
    api="anthropic-messages",
    provider="anthropic",
    base_url="https://api.anthropic.com",
    reasoning=True,
)


def _adaptive_model() -> Model:
    return Model(
        id="claude-sonnet-4-5",
        name="Sonnet",
        api="anthropic-messages",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        reasoning=True,
        thinking_level_map={"off": "disabled"},
        compat={"forceAdaptiveThinking": True},
    )


def _budget_model() -> Model:
    return Model(
        id="claude-3-7-sonnet",
        name="3.7 Sonnet",
        api="anthropic-messages",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        reasoning=True,
    )


# ============================================================
# 消息转换
# ============================================================


def test_system_prompt_separate():
    """system prompt 作为独立参数（非 message）。"""
    ctx = Context(system_prompt="You are helpful.", messages=[UserMessage(content="hi")])
    messages, system, _ = _convert_messages(normalize_context(ctx), _CONV_MODEL)
    assert system == [{"type": "text", "text": "You are helpful."}]
    assert messages == [{"role": "user", "content": "hi"}]


def test_no_system():
    ctx = Context(messages=[UserMessage(content="hi")])
    _, system, _ = _convert_messages(normalize_context(ctx), _CONV_MODEL)
    assert system is None


def test_user_message_blocks():
    """用户消息多模态：text + image 块。"""
    ctx = Context(
        messages=[
            UserMessage(
                content=[
                    TextContent(text="look"),
                    ImageContent(data="abc", mime_type="image/png"),
                ]
            )
        ]
    )
    messages, _, _ = _convert_messages(normalize_context(ctx), _CONV_MODEL)
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/png"


def test_assistant_thinking_with_signature():
    """助手思考块带 signature 回放（必须带回 signature）。"""
    ctx = Context(
        messages=[
            AssistantMessage(
                content=[
                    ThinkingContent(thinking="hmm", thinking_signature="sig123"),
                    TextContent(text="answer"),
                ],
                api="anthropic-messages",
                provider="anthropic",
                model="claude",
            )
        ]
    )
    messages, _, _ = _convert_messages(normalize_context(ctx), _CONV_MODEL)
    content = messages[0]["content"]
    assert content[0] == {"type": "thinking", "thinking": "hmm", "signature": "sig123"}
    assert content[1] == {"type": "text", "text": "answer"}


def test_assistant_tool_use():
    """助手 tool_call -> tool_use 块。"""
    ctx = Context(
        messages=[
            AssistantMessage(
                content=[
                    TextContent(text="calling"),
                    ToolCall(id="tc1", name="weather", arguments={"city": "SF"}),
                ],
                api="anthropic-messages",
                provider="anthropic",
                model="claude",
            )
        ]
    )
    messages, _, _ = _convert_messages(normalize_context(ctx), _CONV_MODEL)
    content = messages[0]["content"]
    assert content[1] == {
        "type": "tool_use",
        "id": "tc1",
        "name": "weather",
        "input": {"city": "SF"},
    }


def test_consecutive_tool_results_merged():
    """连续 toolResult 合并进一个 user 轮次。"""
    ctx = Context(
        messages=[
            AssistantMessage(
                content=[ToolCall(id="tc1", name="f1", arguments={})],
                api="anthropic-messages",
                provider="anthropic",
                model="claude",
            ),
            ToolResultMessage(
                tool_call_id="tc1",
                tool_name="f1",
                content=[TextContent(text="r1")],
            ),
            ToolResultMessage(
                tool_call_id="tc2",
                tool_name="f2",
                content=[TextContent(text="r2")],
                is_error=True,
            ),
        ]
    )
    messages, _, _ = _convert_messages(normalize_context(ctx), _CONV_MODEL)
    # assistant 消息 + 一个 user 消息（含两个 tool_result）
    assert len(messages) == 2
    assert messages[1]["role"] == "user"
    results = messages[1]["content"]
    assert len(results) == 2
    assert results[0]["tool_use_id"] == "tc1"
    assert results[1]["tool_use_id"] == "tc2"
    assert results[1]["is_error"] is True


# ============================================================
# 工具转换
# ============================================================


def test_convert_tools():
    tools = [
        Tool(
            name="weather",
            description="Get weather",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        )
    ]
    result = _convert_tools(tools)
    assert result[0]["name"] == "weather"
    assert result[0]["input_schema"]["type"] == "object"
    assert "city" in result[0]["input_schema"]["properties"]
    assert result[0]["input_schema"]["required"] == ["city"]


def test_convert_tools_defaults():
    """缺失 properties/required 时给默认值。"""
    tool = Tool(name="f", description="d", parameters={})
    result = _convert_tools([tool])
    assert result[0]["input_schema"] == {"type": "object", "properties": {}, "required": []}


def test_convert_tools_enables_strict_schema_for_capable_models():
    tool = Tool(
        name="f",
        description="d",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        constrained_sampling={"type": "json_schema", "strict": "require"},
    )

    result = _convert_tools([tool], supports_strict_tools=True)

    assert result[0]["strict"] is True
    assert result[0]["input_schema"]["additionalProperties"] is False


# ============================================================
# thinking 配置
# ============================================================


def test_thinking_adaptive_enabled():
    """自适应模型启用思考。"""
    model = _adaptive_model()
    from pi_ai import StreamOptions

    opts = StreamOptions.model_construct(thinking_enabled=True, effort="high")
    cfg = _build_thinking_config(model, opts)
    assert cfg["type"] == "adaptive"
    assert cfg["effort"] == "high"


def test_thinking_budget_enabled():
    """旧模型启用思考（budget 模式）。"""
    model = _budget_model()
    from pi_ai import StreamOptions

    opts = StreamOptions.model_construct(thinking_enabled=True, thinking_budget_tokens=4096)
    cfg = _build_thinking_config(model, opts)
    assert cfg["type"] == "enabled"
    assert cfg["budget_tokens"] == 4096


def test_thinking_disabled():
    """显式禁用思考。"""
    model = _adaptive_model()
    from pi_ai import StreamOptions

    opts = StreamOptions.model_construct(thinking_enabled=False)
    cfg = _build_thinking_config(model, opts)
    assert cfg == {"type": "disabled"}


def test_thinking_none_when_unset():
    """未设置 thinking_enabled 返回 None。"""
    model = _budget_model()
    cfg = _build_thinking_config(model, None)
    assert cfg is None


# ============================================================
# stop_reason 映射
# ============================================================


def test_stop_reason_mapping():
    assert _STOP_REASON_MAP["end_turn"] == "stop"
    assert _STOP_REASON_MAP["tool_use"] == "toolUse"
    assert _STOP_REASON_MAP["max_tokens"] == "length"
    assert _STOP_REASON_MAP["refusal"] == "error"


class _AnthropicAsyncItems:
    def __init__(self, items):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _fake_anthropic_client(events):
    async def create(**kwargs):
        return _AnthropicAsyncItems(events)

    return SimpleNamespace(messages=SimpleNamespace(create=create))


async def _collect_anthropic(monkeypatch, events):
    monkeypatch.setattr(
        anthropic_provider,
        "_create_client",
        lambda model, api_key, headers, http_client=None: _fake_anthropic_client(events),
    )
    event_stream = anthropic_provider._run_anthropic_stream(
        _budget_model(),
        Context(messages=[UserMessage(content="hi")]),
        StreamOptions(api_key="test"),
    )
    seen = []
    start_reasons = []
    async for event in event_stream:
        seen.append(event)
        if isinstance(event, StartEvent):
            start_reasons.append(event.partial.stop_reason)
    return seen, start_reasons, await event_stream.result()


async def test_anthropic_stream_starts_pending_and_preserves_raw_reason(monkeypatch):
    events = [
        SimpleNamespace(type="message_start", message=SimpleNamespace(id="m1", usage=None)),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=None,
        ),
        SimpleNamespace(type="message_stop"),
    ]

    seen, start_reasons, message = await _collect_anthropic(monkeypatch, events)

    assert start_reasons == ["pending"]
    assert message.stop_reason == "stop"
    assert message.raw_stop_reason == "end_turn"
    assert not any(isinstance(event, ErrorEvent) for event in seen)


async def test_anthropic_stream_rejects_missing_stop_reason(monkeypatch):
    events = [
        SimpleNamespace(type="message_start", message=SimpleNamespace(id="m1", usage=None)),
        SimpleNamespace(type="message_stop"),
    ]

    seen, _, message = await _collect_anthropic(monkeypatch, events)

    assert isinstance(seen[-1], ErrorEvent)
    assert message.stop_reason == "error"
    assert "without a stop reason" in (message.error_message or "")


def test_anthropic_client_receives_injected_http_client(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_async_anthropic(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(anthropic_provider, "AsyncAnthropic", fake_async_anthropic)
    anthropic_provider._create_client(_budget_model(), "key", None, sentinel)

    assert captured["http_client"] is sentinel


# ============================================================
# v0.84.1: 保留 content_block_start 携带的初始内容
# ============================================================


async def test_anthropic_preserves_initial_text_block_content(monkeypatch):
    """content_block_start 携带的初始文本被保留，后续 delta 在其上累加。"""
    events = [
        SimpleNamespace(type="message_start", message=SimpleNamespace(id="m1", usage=None)),
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="text", text="Hello"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text=" world"),
        ),
        SimpleNamespace(type="content_block_stop", index=0),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=None,
        ),
        SimpleNamespace(type="message_stop"),
    ]

    seen, _, message = await _collect_anthropic(monkeypatch, events)

    assert not any(isinstance(event, ErrorEvent) for event in seen)
    assert message.content[0].text == "Hello world"


async def test_anthropic_preserves_initial_thinking_block_content(monkeypatch):
    """content_block_start 携带的初始 thinking 文本与 signature 被保留。"""
    events = [
        SimpleNamespace(type="message_start", message=SimpleNamespace(id="m1", usage=None)),
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="thinking", thinking="hmm", signature="sig0"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="thinking_delta", thinking=" more"),
        ),
        SimpleNamespace(type="content_block_stop", index=0),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=None,
        ),
        SimpleNamespace(type="message_stop"),
    ]

    seen, _, message = await _collect_anthropic(monkeypatch, events)

    assert not any(isinstance(event, ErrorEvent) for event in seen)
    thinking = message.content[0]
    assert thinking.thinking == "hmm more"
    assert thinking.thinking_signature == "sig0"


# ============================================================
# v0.85.1: mid-conversation effort / server-side fallback / toolChoice
# ============================================================


def _capturing_anthropic_client(events, capture):
    """假 Anthropic client，捕获 create() 的请求参数（含 extra_body）。"""

    async def create(**kwargs):
        capture.update(kwargs)
        return _AnthropicAsyncItems(events)

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def _mid_convo_model() -> Model:
    model = _adaptive_model()
    model.compat = {"supportsMidConvoEffort": True}
    return model


async def test_anthropic_mid_convo_effort_messages_and_params(monkeypatch):
    """supportsMidConvoEffort：effort system 消息按轮插入，thinking 走
    adaptive + block_binding，betas 经 extra_body 发送。"""
    capture: dict = {}
    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(id="m1", model="claude-sonnet-4-5", usage=None),
        ),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=None,
        ),
        SimpleNamespace(type="message_stop"),
    ]
    monkeypatch.setattr(
        anthropic_provider,
        "_create_client",
        lambda model, api_key, headers, http_client=None: _capturing_anthropic_client(
            events, capture
        ),
    )

    from pi_ai import AssistantMessage, TextContent

    context = Context(
        messages=[
            UserMessage(content="hi"),
            AssistantMessage(
                api="anthropic-messages",
                provider="anthropic",
                model="claude-sonnet-4-5",
                stop_reason="stop",
                provider_thinking_level="low",
                content=[TextContent(text="hello")],
            ),
            UserMessage(content="again"),
        ]
    )
    event_stream = anthropic_provider._run_anthropic_stream(
        _mid_convo_model(),
        context,
        StreamOptions(api_key="test", temperature=0.7),
    )
    async for _ in event_stream:
        pass
    message = await event_stream.result()

    messages = capture["messages"]
    # 历史 assistant 前插 system effort=low，末尾追加当前 effort（默认 high）
    assert messages[1] == {"role": "system", "content": [], "output_config": {"effort": "low"}}
    assert messages[2]["role"] == "assistant"
    assert messages[-1] == {"role": "system", "content": [], "output_config": {"effort": "high"}}

    assert capture["output_config"] == {"effort": "high"}
    assert "temperature" not in capture  # mid-convo effort 模型不支持 temperature

    extra_body = capture["extra_body"]
    assert "mid-conversation-output-config-2026-07-01" in extra_body["betas"]
    assert "thinking-binding-controls-2026-08-01" in extra_body["betas"]
    assert extra_body["thinking"]["type"] == "adaptive"
    assert extra_body["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}

    # providerThinkingLevel 记录在响应上
    assert message.provider_thinking_level == "high"


async def test_anthropic_fallback_usage_and_blocks(monkeypatch):
    """server-side fallback：fallbacks/betas 经 extra_body 发送；响应模型切换时
    计费按 fallback 费率；fallback 声明块被跳过。"""
    capture: dict = {}

    def _usage(**kw):
        base = {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        base.update(kw)
        return SimpleNamespace(**base)

    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="m1",
                model="claude-backup",
                usage=_usage(input_tokens=10, output_tokens=5),
            ),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="fallback"),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(type="text", text=""),
        ),
        SimpleNamespace(type="content_block_stop", index=1),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=_usage(output_tokens=6),
        ),
        SimpleNamespace(type="message_stop"),
    ]
    monkeypatch.setattr(
        anthropic_provider,
        "_create_client",
        lambda model, api_key, headers, http_client=None: _capturing_anthropic_client(
            events, capture
        ),
    )
    model = _adaptive_model()
    model.compat = {
        "allowedFallbackModels": [
            {
                "provider": "anthropic",
                "model": "claude-backup",
                "cost": {"input": 1, "output": 2, "cacheRead": 0.5, "cacheWrite": 1},
            }
        ]
    }

    event_stream = anthropic_provider._run_anthropic_stream(
        model, Context(messages=[UserMessage(content="hi")]), StreamOptions(api_key="test")
    )
    async for _ in event_stream:
        pass
    message = await event_stream.result()

    extra_body = capture["extra_body"]
    assert extra_body["fallbacks"] == [{"model": "claude-backup"}]
    assert "server-side-fallback-2026-07-01" in extra_body["betas"]

    # 响应模型切换为 fallback 模型，计费按 fallback 费率
    assert message.model == "claude-backup"
    assert message.usage.input == 10
    assert message.usage.output == 6
    assert message.usage.cost.input == pytest.approx(10 * 1 / 1_000_000)
    assert message.usage.cost.output == pytest.approx(6 * 2 / 1_000_000)
    # fallback 声明块不进 content
    assert len(message.content) == 1
    assert message.content[0].type == "text"


async def test_anthropic_mid_output_fallback_rejected(monkeypatch):
    """fallback 声明块出现在输出中间：报错。"""
    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(id="m1", model="claude-sonnet-4-5", usage=None),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="text", text="hi"),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(type="fallback"),
        ),
    ]
    monkeypatch.setattr(
        anthropic_provider,
        "_create_client",
        lambda model, api_key, headers, http_client=None: _capturing_anthropic_client(events, {}),
    )
    model = _adaptive_model()
    model.compat = {"allowedFallbackModels": [{"model": "x", "provider": "anthropic", "cost": {}}]}

    event_stream = anthropic_provider._run_anthropic_stream(
        model, Context(messages=[UserMessage(content="hi")]), StreamOptions(api_key="test")
    )
    async for _ in event_stream:
        pass
    message = await event_stream.result()

    assert message.stop_reason == "error"
    assert "mid-output model fallback" in (message.error_message or "")


async def test_anthropic_tool_choice_forwarded(monkeypatch):
    """SimpleStreamOptions.tool_choice 映射为 anthropic tool_choice 对象。"""
    capture: dict = {}
    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(id="m1", model="m", usage=None),
        ),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=None,
        ),
        SimpleNamespace(type="message_stop"),
    ]
    monkeypatch.setattr(
        anthropic_provider,
        "_create_client",
        lambda model, api_key, headers, http_client=None: _capturing_anthropic_client(
            events, capture
        ),
    )

    from pi_ai import SimpleStreamOptions

    event_stream = anthropic_provider._run_anthropic_stream(
        _budget_model(),
        Context(messages=[UserMessage(content="hi")]),
        SimpleStreamOptions(api_key="test", tool_choice="none"),
    )
    async for _ in event_stream:
        pass
    await event_stream.result()

    assert capture["tool_choice"] == {"type": "none"}
