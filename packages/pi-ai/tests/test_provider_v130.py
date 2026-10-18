"""provider 层 v1.3.0 行为对齐测试（M-1 / M-4 / M-13 / M-22）。

这些用例**穿过真实 provider 代码路径**（``_run_openai_stream`` /
``_run_anthropic_stream``），用假 SDK client 注入流式事件，因此能覆盖"参数确实
按新语义发出去"这类只在 provider 内部才能验证的行为。

对应上游 ``test/openai-completions-*``、``test/anthropic-*`` 的相关断言。
"""

from __future__ import annotations

from types import SimpleNamespace

from pi_ai.providers import openai_provider
from pi_ai.types import (
    AssistantMessage,
    Context,
    Model,
    ModelCost,
    SimpleStreamOptions,
    StreamOptions,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    UserMessage,
)


class _AsyncItems:
    def __init__(self, items):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _make_model(**overrides) -> Model:
    base = dict(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        cost=ModelCost(input=2.5, output=10, cache_read=1.25),
        context_window=128000,
        max_tokens=16384,
        reasoning=True,
    )
    base.update(overrides)
    return Model(**base)


def _chunk(*, finish_reason=None, delta=None, usage=None):
    delta = delta or SimpleNamespace(content=None, tool_calls=None)
    choice = SimpleNamespace(finish_reason=finish_reason, delta=delta)
    return SimpleNamespace(usage=usage, choices=[choice])


def _fake_openai_client(chunks, capture=None):
    async def create(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        return _AsyncItems(chunks)

    completions = SimpleNamespace(create=create)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


async def _run_openai(monkeypatch, chunks, *, model=None, context=None, options=None, capture=None):
    monkeypatch.setattr(
        openai_provider,
        "_create_client",
        lambda model, api_key, headers, http_client=None: _fake_openai_client(chunks, capture),
    )
    event_stream = openai_provider._run_openai_stream(
        model or _make_model(),
        context if context is not None else Context(messages=[UserMessage(content="hi")]),
        options or StreamOptions(api_key="test"),
    )
    events = [event async for event in event_stream]
    return events, await event_stream.result()


# ============================================================
# M-1：裸 Context 直接进 provider 不得丢 system_prompt / tools
# ============================================================


async def test_openai_provider_normalizes_a_raw_context(monkeypatch):
    """回归：``Context`` 与 ``TranscriptContext`` 都有 ``messages``，直接传裸
    ``Context`` 曾会静默丢掉 ``system_prompt`` 与 ``tools``。"""
    capture: dict = {}
    context = Context(
        system_prompt="be brief",
        messages=[UserMessage(content="hi")],
        tools=[Tool(name="t", description="d", parameters={"type": "object"})],
    )
    await _run_openai(monkeypatch, [_chunk(finish_reason="stop")], context=context, capture=capture)

    assert capture["messages"][0] == {"role": "system", "content": "be brief"}
    assert [t["function"]["name"] for t in capture["tools"]] == ["t"]


async def test_openai_provider_empty_prompt_and_tools_send_no_system_message(monkeypatch):
    capture: dict = {}
    await _run_openai(
        monkeypatch,
        [_chunk(finish_reason="stop")],
        context=Context(system_prompt="", messages=[UserMessage(content="hi")], tools=[]),
        capture=capture,
    )
    assert all(m["role"] != "system" for m in capture["messages"])
    assert "tools" not in capture


# ============================================================
# M-13.1：同一 delta 内 reasoning 必须先于 content
# ============================================================


async def test_openai_reasoning_block_precedes_text_when_same_delta(monkeypatch):
    """LiteLLM 等代理把 reasoning 与答案聚合进同一 delta（issue #10926）。"""
    mixed = SimpleNamespace(content="the answer", reasoning_content="the thinking", tool_calls=None)
    _, message = await _run_openai(
        monkeypatch,
        [_chunk(delta=mixed), _chunk(finish_reason="stop")],
    )
    types = [getattr(b, "type", None) for b in message.content]
    assert types == ["thinking", "text"], f"思考必须先于文本，实际 {types}"


async def test_openai_reasoning_details_also_precede_text(monkeypatch):
    detail = {"type": "reasoning.text", "text": "det"}
    mixed = SimpleNamespace(
        content="answer", reasoning_content=None, reasoning_details=[detail], tool_calls=None
    )
    _, message = await _run_openai(
        monkeypatch,
        [_chunk(delta=mixed), _chunk(finish_reason="stop")],
    )
    types = [getattr(b, "type", None) for b in message.content]
    assert types == ["thinking", "text"]


# ============================================================
# M-13.2：未知端点默认不发 strict（issue #9816）
# ============================================================


async def test_openai_unknown_endpoint_omits_strict_by_default(monkeypatch):
    capture: dict = {}
    tool = Tool(
        name="t",
        description="d",
        parameters={"type": "object", "properties": {"a": {"type": "string"}}},
        constrainedSampling={"type": "json_schema", "strict": "prefer"},
    )
    await _run_openai(
        monkeypatch,
        [_chunk(finish_reason="stop")],
        context=Context(messages=[UserMessage(content="hi")], tools=[tool]),
        capture=capture,
    )
    function = capture["tools"][0]["function"]
    # 上游 openai-completions.ts:1515 `...(supportsStrictMode !== false && { strict })`
    # —— 不支持时**整个 strict 键都不发**（部分端点会拒绝未知字段）。
    assert "strict" not in function
    # 也不得把 schema 改成 strict 形态（required 收拢 / additionalProperties: false）
    assert "additionalProperties" not in function["parameters"]
    assert "required" not in function["parameters"]


async def test_openai_declared_strict_endpoint_still_sends_strict(monkeypatch):
    capture: dict = {}
    model = _make_model()
    model.compat = {"supportsStrictMode": True}
    tool = Tool(
        name="t",
        description="d",
        parameters={"type": "object", "properties": {"a": {"type": "string"}}},
        constrainedSampling={"type": "json_schema", "strict": "require"},
    )
    await _run_openai(
        monkeypatch,
        [_chunk(finish_reason="stop")],
        model=model,
        context=Context(messages=[UserMessage(content="hi")], tools=[tool]),
        capture=capture,
    )
    assert capture["tools"][0]["function"]["strict"] is True
    params = capture["tools"][0]["function"]["parameters"]
    assert params["additionalProperties"] is False


# ============================================================
# M-13.4：采样参数三层合并
# ============================================================


async def test_openai_sampling_params_merge_model_level_thinking_level_and_request(monkeypatch):
    capture: dict = {}
    model = _make_model(
        samplingParams={"top_p": 0.9, "temperature": 0.5},
        samplingParamsByThinkingLevel={"high": {"top_p": 0.7, "top_k": 40}},
    )
    await _run_openai(
        monkeypatch,
        [_chunk(finish_reason="stop")],
        model=model,
        options=SimpleStreamOptions(api_key="test", reasoning="high", samplingParams={"top_k": 10}),
        capture=capture,
    )
    # 模型级 -> 思考级别 -> 请求级，后者按 key 覆盖
    assert capture["top_p"] == 0.7
    assert capture["temperature"] == 0.5
    assert capture["top_k"] == 10


async def test_openai_model_sampling_params_apply_without_thinking_level(monkeypatch):
    """回归 issue #9506：直连 stream() 时模型级采样参数也曾被丢弃。"""
    capture: dict = {}
    model = _make_model(samplingParams={"min_p": 0.05})
    await _run_openai(monkeypatch, [_chunk(finish_reason="stop")], model=model, capture=capture)
    assert capture["min_p"] == 0.05


# ============================================================
# M-13.5：空 text 块被过滤（issue #9797）
# ============================================================


async def test_openai_filters_empty_text_parts_from_user_content(monkeypatch):
    capture: dict = {}
    context = Context(
        messages=[
            UserMessage(
                content=[
                    TextContent(text=""),
                    # 实际构造由 Pydantic 校验，空文本块是模型/调用方可能给出的形态
                ]
            )
        ]
    )
    # 只有空文本块 -> 整条 user 消息被跳过
    await _run_openai(monkeypatch, [_chunk(finish_reason="stop")], context=context, capture=capture)
    assert capture["messages"] == []


# ============================================================
# M-22：onProviderStreamEvent
# ============================================================


async def test_openai_calls_on_provider_stream_event_for_every_chunk(monkeypatch):
    seen: list[object] = []
    chunks = [
        _chunk(delta=SimpleNamespace(content="a", tool_calls=None)),
        _chunk(finish_reason="stop"),
    ]
    await _run_openai(
        monkeypatch,
        chunks,
        options=StreamOptions(
            api_key="test", onProviderStreamEvent=lambda data, model: seen.append(data)
        ),
    )
    assert seen == chunks


async def test_openai_supports_async_on_provider_stream_event(monkeypatch):
    seen: list[int] = []

    async def hook(data, model):
        seen.append(1)

    await _run_openai(
        monkeypatch,
        [_chunk(finish_reason="stop")],
        options=StreamOptions(api_key="test", onProviderStreamEvent=hook),
    )
    assert seen == [1]


# ============================================================
# M-4：收尾参数非法 -> arguments_error
# ============================================================


async def test_openai_marks_arguments_error_for_malformed_final_arguments(monkeypatch):
    tool_delta = SimpleNamespace(
        index=0, id="call-1", function=SimpleNamespace(name="t", arguments='{"a":'), custom=None
    )
    delta = SimpleNamespace(content=None, tool_calls=[tool_delta])
    _, message = await _run_openai(
        monkeypatch,
        [_chunk(delta=delta), _chunk(finish_reason="tool_calls")],
    )
    call = next(b for b in message.content if isinstance(b, ToolCall))
    assert call.arguments == {}
    assert call.arguments_error is not None
    assert 'Received arguments:\n{"a":' in call.arguments_error


async def test_openai_leaves_valid_arguments_without_error(monkeypatch):
    tool_delta = SimpleNamespace(
        index=0, id="call-1", function=SimpleNamespace(name="t", arguments='{"a": 1}'), custom=None
    )
    delta = SimpleNamespace(content=None, tool_calls=[tool_delta])
    _, message = await _run_openai(
        monkeypatch,
        [_chunk(delta=delta), _chunk(finish_reason="tool_calls")],
    )
    call = next(b for b in message.content if isinstance(b, ToolCall))
    assert call.arguments == {"a": 1}
    assert call.arguments_error is None


# ============================================================
# M-6（经 provider）：跨模型思考不进请求
# ============================================================


async def test_openai_drops_cross_model_thinking_in_request(monkeypatch):
    capture: dict = {}
    model = _make_model()
    other = AssistantMessage(
        content=[
            ThinkingContent(thinking="secret", thinking_signature="sig"),
            TextContent(text="visible"),
        ],
        api="openai-completions",
        provider="openai",
        model="some-other-model",
        stop_reason="stop",
    )
    await _run_openai(
        monkeypatch,
        [_chunk(finish_reason="stop")],
        model=model,
        context=Context(messages=[other]),
        capture=capture,
    )
    assert capture["messages"] == [{"role": "assistant", "content": "visible"}]
    assert "reasoning_details" not in capture["messages"][0]


async def test_openai_keeps_same_model_signed_thinking(monkeypatch):
    capture: dict = {}
    model = _make_model(id="gpt-4o")
    import json

    details = [{"type": "reasoning.text", "text": "ab"}]
    same = AssistantMessage(
        content=[
            ThinkingContent(thinking="", thinking_signature=json.dumps(details)),
            TextContent(text="visible"),
        ],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
    )
    await _run_openai(
        monkeypatch,
        [_chunk(finish_reason="stop")],
        model=model,
        context=Context(messages=[same]),
        capture=capture,
    )
    assert capture["messages"][0]["reasoning_details"] == details
