"""Anthropic provider 层 v1.3.0 行为对齐测试（M-1 / M-4 / M-6 / M-22）。

穿过真实 ``_run_anthropic_stream`` 路径，用假 SDK client 注入类型化流事件。
"""

from __future__ import annotations

from types import SimpleNamespace

from pi_ai.providers import anthropic_provider
from pi_ai.types import (
    AssistantMessage,
    Context,
    Model,
    SimpleStreamOptions,
    StreamOptions,
    TextContent,
    ThinkingContent,
    Tool,
    UserMessage,
)


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


def _model(**overrides) -> Model:
    base = dict(
        id="claude",
        name="claude",
        api="anthropic-messages",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        reasoning=True,
    )
    base.update(overrides)
    return Model(**base)


def _client(events, capture=None):
    async def create(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        return _AnthropicAsyncItems(events)

    return SimpleNamespace(messages=SimpleNamespace(create=create))


async def _run(monkeypatch, events, *, model=None, context=None, options=None, capture=None):
    monkeypatch.setattr(
        anthropic_provider,
        "_create_client",
        lambda model, api_key, headers, http_client=None: _client(events, capture),
    )
    event_stream = anthropic_provider._run_anthropic_stream(
        model or _model(),
        context if context is not None else Context(messages=[UserMessage(content="hi")]),
        options or StreamOptions(api_key="test"),
    )
    seen = [event async for event in event_stream]
    return seen, await event_stream.result()


def _start(model_id: str = "claude") -> SimpleNamespace:
    return SimpleNamespace(
        type="message_start",
        message=SimpleNamespace(id="m1", model=model_id, usage=None),
    )


# ============================================================
# M-1：裸 Context 直接进 provider 不得丢 system_prompt / tools
# ============================================================


async def test_anthropic_provider_normalizes_a_raw_context(monkeypatch):
    capture: dict = {}
    context = Context(
        system_prompt="be brief",
        messages=[UserMessage(content="hi")],
        tools=[Tool(name="t", description="d", parameters={"type": "object"})],
    )
    await _run(
        monkeypatch,
        [_start(), SimpleNamespace(type="message_stop")],
        context=context,
        capture=capture,
    )
    assert capture["system"] == [{"type": "text", "text": "be brief"}]
    assert [t["name"] for t in capture["tools"]] == ["t"]


# ============================================================
# M-22：onProviderStreamEvent
# ============================================================


async def test_anthropic_calls_on_provider_stream_event_for_every_event(monkeypatch):
    events = [_start(), SimpleNamespace(type="message_stop")]
    seen: list[object] = []
    await _run(
        monkeypatch,
        events,
        options=StreamOptions(
            api_key="test", onProviderStreamEvent=lambda data, model: seen.append(data)
        ),
    )
    assert seen == events


async def test_anthropic_supports_async_on_provider_stream_event(monkeypatch):
    calls: list[int] = []

    async def hook(data, model):
        calls.append(1)

    await _run(
        monkeypatch,
        [_start(), SimpleNamespace(type="message_stop")],
        options=StreamOptions(api_key="test", onProviderStreamEvent=hook),
    )
    assert calls == [1, 1]


# ============================================================
# M-4：收尾参数非法 -> arguments_error
# ============================================================


def _tool_use_events(partial_json: str) -> list[SimpleNamespace]:
    return [
        _start(),
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="tool_use", id="tc1", name="t", input={}),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="input_json_delta", partial_json=partial_json),
        ),
        SimpleNamespace(type="content_block_stop", index=0),
        SimpleNamespace(type="message_stop"),
    ]


async def test_anthropic_marks_arguments_error_for_malformed_final_arguments(monkeypatch):
    _, message = await _run(monkeypatch, _tool_use_events('{"a":'))
    call = message.content[0]
    assert call.type == "toolCall"
    assert call.arguments == {}
    assert call.arguments_error is not None
    assert 'Received arguments:\n{"a":' in call.arguments_error


async def test_anthropic_leaves_valid_arguments_without_error(monkeypatch):
    _, message = await _run(monkeypatch, _tool_use_events('{"a": 1}'))
    call = message.content[0]
    assert call.arguments == {"a": 1}
    assert call.arguments_error is None


# ============================================================
# M-6：跨模型思考不进请求
# ============================================================


async def test_anthropic_drops_cross_model_thinking_in_request(monkeypatch):
    """回归：v0.85.1 本端"有签名就回放"，跨模型切换会发送无效签名，
    Anthropic 会以 "Invalid `signature` in `thinking` block" 400（上游 #10324 同类）。"""
    capture: dict = {}
    other = AssistantMessage(
        content=[
            ThinkingContent(thinking="secret", thinking_signature="sig-from-other-model"),
            TextContent(text="visible"),
        ],
        api="anthropic-messages",
        provider="anthropic",
        model="claude-3-5-sonnet",
        stop_reason="stop",
    )
    await _run(
        monkeypatch,
        [_start(), SimpleNamespace(type="message_stop")],
        model=_model(id="claude"),
        context=Context(messages=[other]),
        capture=capture,
    )
    blocks = capture["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["text"]
    assert blocks[0]["text"] == "visible"


async def test_anthropic_keeps_same_model_signed_thinking(monkeypatch):
    capture: dict = {}
    same = AssistantMessage(
        content=[
            ThinkingContent(thinking="reasoning", thinking_signature="sig-1"),
            TextContent(text="visible"),
        ],
        api="anthropic-messages",
        provider="anthropic",
        model="claude",
        stop_reason="stop",
    )
    await _run(
        monkeypatch,
        [_start(), SimpleNamespace(type="message_stop")],
        model=_model(id="claude"),
        context=Context(messages=[same]),
        capture=capture,
    )
    blocks = capture["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["thinking", "text"]
    assert blocks[0]["signature"] == "sig-1"


async def test_anthropic_keeps_redacted_thinking_of_same_model(monkeypatch):
    capture: dict = {}
    same = AssistantMessage(
        content=[ThinkingContent(thinking="", thinking_signature="enc", redacted=True)],
        api="anthropic-messages",
        provider="anthropic",
        model="claude",
        stop_reason="stop",
    )
    await _run(
        monkeypatch,
        [_start(), SimpleNamespace(type="message_stop")],
        model=_model(id="claude"),
        context=Context(messages=[same]),
        capture=capture,
    )
    blocks = capture["messages"][0]["content"]
    assert blocks[0]["type"] == "redacted_thinking"
    assert blocks[0]["data"] == "enc"


# ============================================================
# M-11：流计时（经 provider 落盘）
# ============================================================


async def test_anthropic_stream_stamps_duration_ms(monkeypatch):
    _, message = await _run(monkeypatch, [_start(), SimpleNamespace(type="message_stop")])
    assert message.duration_ms is not None


async def test_openai_and_anthropic_use_assistant_message_event_stream(monkeypatch):
    """provider 必须返回会计时的流类型（M-11）。"""
    from pi_ai.event_stream import AssistantMessageEventStream
    from pi_ai.providers import openai_provider as openai_mod
    from pi_ai.types import ModelCost

    monkeypatch.setattr(openai_mod, "_create_client", lambda *a, **k: None)

    class _Chunks:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def create(**kwargs):
        return _Chunks()

    monkeypatch.setattr(
        openai_mod,
        "_create_client",
        lambda *a, **k: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    model = Model(
        id="m",
        name="m",
        api="openai-completions",
        provider="openai",
        base_url="https://x",
        cost=ModelCost(),
        max_tokens=16,
    )
    stream = openai_mod._run_openai_stream(
        model, Context(messages=[UserMessage(content="hi")]), StreamOptions(api_key="k")
    )
    assert isinstance(stream, AssistantMessageEventStream)


async def test_anthropic_simple_options_still_work(monkeypatch):
    capture: dict = {}
    await _run(
        monkeypatch,
        [_start(), SimpleNamespace(type="message_stop")],
        options=SimpleStreamOptions(api_key="test", reasoning="high"),
        capture=capture,
    )
    assert capture["messages"] == [{"role": "user", "content": "hi"}]
