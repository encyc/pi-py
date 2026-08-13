"""OpenAI provider 纯逻辑测试（不需要 API key）：usage 解析、流式 JSON 解析、stop reason 映射。

实际网络调用需集成测试（test_integration.py，需要 OPENAI_API_KEY，默认跳过）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pi_ai import (
    AssistantMessage,
    Context,
    Model,
    ModelCost,
    StreamOptions,
    Tool,
    ToolCall,
    UserMessage,
)
from pi_ai.events import ErrorEvent, StartEvent, ToolCallDeltaEvent
from pi_ai.providers import openai_provider
from pi_ai.providers.openai_provider import (
    _STOP_REASON_MAP,
    _convert_messages,
    _convert_tools,
    _parse_chunk_usage,
    _parse_streaming_json,
)


def _make_model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        cost=ModelCost(input=2.5, output=10, cache_read=1.25),
        context_window=128000,
        max_tokens=16384,
    )


class _FakeUsage:
    """模拟 openai chunk.usage 对象。"""

    def __init__(
        self,
        prompt_tokens=0,
        completion_tokens=0,
        cached_tokens=None,
        cache_write_tokens=0,
        reasoning_tokens=0,
        completion_tokens_details="auto",
    ):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.prompt_tokens_details = type(
            "Det",
            (),
            {"cached_tokens": cached_tokens, "cache_write_tokens": cache_write_tokens},
        )()
        # "auto"=构造对象；None=模拟 DeepSeek（completion_tokens_details 为 None）
        if completion_tokens_details == "auto":
            self.completion_tokens_details = type(
                "Det", (), {"reasoning_tokens": reasoning_tokens}
            )()
        else:
            self.completion_tokens_details = completion_tokens_details


def test_usage_basic():
    """基础 usage：无缓存。"""
    model = _make_model()
    raw = _FakeUsage(prompt_tokens=100, completion_tokens=50)
    usage = _parse_chunk_usage(raw, model)
    assert usage.input == 100
    assert usage.output == 50
    assert usage.cache_read == 0
    assert usage.cache_write == 0
    assert usage.total_tokens == 150


def test_usage_with_cache_read():
    """缓存读取：input 扣除 cached_tokens（恒等式成立）。"""
    model = _make_model()
    raw = _FakeUsage(prompt_tokens=100, completion_tokens=50, cached_tokens=30)
    usage = _parse_chunk_usage(raw, model)
    assert usage.input == 70  # 100 - 30
    assert usage.cache_read == 30
    assert usage.total_tokens == 150  # 70 + 30 + 0 + 50


def test_usage_cost_calculation():
    """费用计算：每百万 token 费率。"""
    model = _make_model()
    raw = _FakeUsage(prompt_tokens=1_000_000, completion_tokens=500_000)
    usage = _parse_chunk_usage(raw, model)
    # input 2.5/M * 1M = 2.5；output 10/M * 0.5M = 5.0
    assert pytest.approx(usage.cost.input, rel=1e-6) == 2.5
    assert pytest.approx(usage.cost.output, rel=1e-6) == 5.0
    assert pytest.approx(usage.cost.total, rel=1e-6) == 7.5


def test_usage_deepseek_none_fields():
    """回归：DeepSeek 返回 completion_tokens_details=None 且 cache_write_tokens=None。

    SDK 对象属性存在但值为 None 时，getattr 返回 None（非默认值），
    直接做减法会 ``int - None`` 崩溃。所有字段须 ``or 0`` 兜底。
    """
    model = _make_model()
    raw = _FakeUsage(
        prompt_tokens=8,
        completion_tokens=5,
        cached_tokens=0,
        cache_write_tokens=None,
        completion_tokens_details=None,
    )
    usage = _parse_chunk_usage(raw, model)
    assert usage.input == 8
    assert usage.output == 5
    assert usage.cache_read == 0
    assert usage.cache_write == 0
    assert usage.total_tokens == 13
    assert usage.reasoning is None


def test_streaming_json_complete():
    """完整 JSON 正常解析。"""
    assert _parse_streaming_json('{"city": "SF"}') == {"city": "SF"}


def test_streaming_json_partial():
    """不完整 JSON 容错解析（工具调用增量场景）。"""
    # 缺右括号 —— 应尽力解析出已有字段
    result = _parse_streaming_json('{"city": "SF"')
    assert result.get("city") == "SF"


def test_streaming_json_empty():
    assert _parse_streaming_json("") == {}


def test_streaming_json_incremental_accumulation():
    """模拟工具参数逐块到达：每步都应能解析出已到达的字段。"""
    chunks = ['{"ci', 'ty": ', '"SF"', ', "u', 'nit": "', "F}"]
    acc = ""
    last = {}
    for ch in chunks:
        acc += ch
        last = _parse_streaming_json(acc)
    assert last.get("city") == "SF"
    assert last.get("unit") == "F"


def test_stop_reason_mapping():
    """finish_reason 映射覆盖关键值。"""
    assert _STOP_REASON_MAP["stop"] == "stop"
    assert _STOP_REASON_MAP["length"] == "length"
    assert _STOP_REASON_MAP["tool_calls"] == "toolUse"
    assert _STOP_REASON_MAP["function_call"] == "toolUse"
    assert _STOP_REASON_MAP["content_filter"] == "error"


def test_convert_tools_enables_required_strict_json_schema():
    tool = Tool(
        name="answer",
        description="Return an answer",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        constrained_sampling={"type": "json_schema", "strict": "require"},
    )

    converted = _convert_tools([tool], supports_strict_mode=True)

    assert converted[0]["function"]["strict"] is True
    assert converted[0]["function"]["parameters"]["additionalProperties"] is False


def test_convert_tools_rejects_required_strict_when_unsupported():
    tool = Tool(
        name="answer",
        description="Return an answer",
        parameters={"type": "object", "properties": {}, "required": []},
        constrained_sampling={"type": "json_schema", "strict": "require"},
    )

    with pytest.raises(ValueError, match="requires JSON-schema constrained sampling"):
        _convert_tools([tool], supports_strict_mode=False)


def test_convert_tools_uses_openai_lark_grammar():
    tool = Tool(
        name="sql",
        description="Generate SQL",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        constrained_sampling={
            "type": "grammar",
            "variants": {
                "openai_lark": 'start: "SELECT 1"',
                "openai_regex": "SELECT .*",
            },
        },
    )

    converted = _convert_tools([tool], supports_openai_grammar_tools=True)

    assert converted == [
        {
            "type": "custom",
            "custom": {
                "name": "sql",
                "description": "Generate SQL",
                "format": {
                    "type": "grammar",
                    "grammar": {"syntax": "lark", "definition": 'start: "SELECT 1"'},
                },
            },
        }
    ]


def test_convert_messages_replays_grammar_tool_as_custom_call():
    context = Context(
        messages=[
            AssistantMessage(
                content=[ToolCall(id="call-1", name="sql", arguments={"query": "SELECT 1"})],
                api="openai-completions",
                provider="openai",
                model="gpt",
            )
        ]
    )

    messages, _ = _convert_messages(context, {"sql": "query"})

    assert messages[0]["tool_calls"] == [
        {
            "id": "call-1",
            "type": "custom",
            "custom": {"name": "sql", "input": "SELECT 1"},
        }
    ]


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


def _chunk(*, finish_reason=None, delta=None):
    delta = delta or SimpleNamespace(content=None, tool_calls=None)
    choice = SimpleNamespace(finish_reason=finish_reason, delta=delta)
    return SimpleNamespace(usage=None, choices=[choice])


def _fake_openai_client(chunks):
    async def create(**kwargs):
        return _AsyncItems(chunks)

    completions = SimpleNamespace(create=create)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


async def _collect_openai(monkeypatch, chunks, *, model=None, context=None, options=None):
    monkeypatch.setattr(
        openai_provider,
        "_create_client",
        lambda model, api_key, headers, http_client=None: _fake_openai_client(chunks),
    )
    event_stream = openai_provider._run_openai_stream(
        model or _make_model(),
        context or Context(messages=[UserMessage(content="hi")]),
        options or StreamOptions(api_key="test"),
    )
    start_reasons = []
    events = []
    async for event in event_stream:
        events.append(event)
        if isinstance(event, StartEvent):
            start_reasons.append(event.partial.stop_reason)
    return events, start_reasons, await event_stream.result()


async def test_openai_stream_starts_pending_and_preserves_raw_reason(monkeypatch):
    events, start_reasons, message = await _collect_openai(
        monkeypatch, [_chunk(finish_reason="stop")]
    )

    assert start_reasons == ["pending"]
    assert message.stop_reason == "stop"
    assert message.raw_stop_reason == "stop"
    assert not any(isinstance(event, ErrorEvent) for event in events)


async def test_openai_stream_rejects_missing_finish_reason(monkeypatch):
    events, _, message = await _collect_openai(monkeypatch, [_chunk()])

    assert isinstance(events[-1], ErrorEvent)
    assert message.stop_reason == "error"
    assert "without finish_reason" in (message.error_message or "")


async def test_openai_stream_preserves_function_when_custom_is_empty(monkeypatch):
    function = SimpleNamespace(name="get_weather", arguments='{"city":"Paris"}')
    custom = SimpleNamespace(name=None, input=None)
    tool_delta = SimpleNamespace(
        index=0,
        id="call-1",
        function=function,
        custom=custom,
    )
    delta = SimpleNamespace(content=None, tool_calls=[tool_delta])
    context = Context(
        messages=[UserMessage(content="weather")],
        tools=[
            Tool(
                name="get_weather",
                description="weather",
                parameters={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
                constrained_sampling={
                    "type": "grammar",
                    "variants": {"openai_regex": ".*"},
                },
            )
        ],
    )
    model = _make_model()
    model.compat = {"supportsOpenAIGrammarTools": True}

    events, _, message = await _collect_openai(
        monkeypatch,
        [_chunk(delta=delta), _chunk(finish_reason="tool_calls")],
        model=model,
        context=context,
    )

    assert message.content[0].arguments == {"city": "Paris"}
    argument_deltas = [event.delta for event in events if isinstance(event, ToolCallDeltaEvent)]
    assert argument_deltas == ['{"city":"Paris"}']


def test_openai_client_receives_injected_http_client(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_async_openai(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openai_provider, "AsyncOpenAI", fake_async_openai)
    openai_provider._create_client(_make_model(), "key", None, sentinel)

    assert captured["http_client"] is sentinel


# ============================================================
# v0.84.1: supportsFinishReason / samplingParams / thinking_token_budget
# ============================================================


def _capturing_openai_client(chunks, capture):
    """假 OpenAI client，捕获 create() 的请求参数。"""

    async def create(**kwargs):
        capture.update(kwargs)
        return _AsyncItems(chunks)

    completions = SimpleNamespace(create=create)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


async def test_openai_stream_infers_stop_when_finish_reason_unsupported(monkeypatch):
    """compat.supportsFinishReason=False 的端点不发 finish_reason：流结束时推断 stop。"""
    model = _make_model()
    model.compat = {"supportsFinishReason": False}

    events, _, message = await _collect_openai(
        monkeypatch, [_chunk(delta=SimpleNamespace(content="hi", tool_calls=None))], model=model
    )

    assert message.stop_reason == "stop"
    assert not any(isinstance(event, ErrorEvent) for event in events)


async def test_openai_stream_infers_tooluse_when_finish_reason_unsupported(monkeypatch):
    """supportsFinishReason=False 且含工具调用：推断 toolUse 而非报错。"""
    function = SimpleNamespace(name="echo", arguments='{"a":1}')
    tool_delta = SimpleNamespace(index=0, id="c1", function=function, custom=None)
    model = _make_model()
    model.compat = {"supportsFinishReason": False}

    events, _, message = await _collect_openai(
        monkeypatch,
        [_chunk(delta=SimpleNamespace(content=None, tool_calls=[tool_delta]))],
        model=model,
    )

    assert message.stop_reason == "toolUse"
    assert not any(isinstance(event, ErrorEvent) for event in events)


async def test_openai_sampling_params_merged_and_override(monkeypatch):
    """模型级与请求级 sampling_params 合并；请求级按 key 覆盖模型级；
    合并结果在具名字段之后写入，因此覆盖具名字段（如 temperature）。"""
    capture: dict = {}
    monkeypatch.setattr(
        openai_provider,
        "_create_client",
        lambda model, api_key, headers, http_client=None: _capturing_openai_client(
            [_chunk(finish_reason="stop")], capture
        ),
    )
    model = _make_model()
    model.sampling_params = {"top_k": 40, "min_p": 0.05}

    event_stream = openai_provider._run_openai_stream(
        model,
        Context(messages=[UserMessage(content="hi")]),
        StreamOptions(
            api_key="test",
            temperature=0.5,
            sampling_params={"top_p": 0.9, "top_k": 50, "temperature": 0.2},
        ),
    )
    async for _ in event_stream:
        pass
    await event_stream.result()

    assert capture["top_k"] == 50  # 请求级覆盖模型级
    assert capture["min_p"] == 0.05  # 模型级独有键保留
    assert capture["top_p"] == 0.9  # 请求级新增
    assert capture["temperature"] == 0.2  # 合并的 sampling_params 覆盖具名字段 0.5


async def test_openai_thinking_token_budget_injected(monkeypatch):
    """supportsThinkingTokenBudget + reasoning：注入 thinking_token_budget。"""
    capture: dict = {}
    monkeypatch.setattr(
        openai_provider,
        "_create_client",
        lambda model, api_key, headers, http_client=None: _capturing_openai_client(
            [_chunk(finish_reason="stop")], capture
        ),
    )
    model = _make_model()
    model.reasoning = True
    model.max_tokens = 32768
    model.compat = {"supportsThinkingTokenBudget": True}

    from pi_ai import SimpleStreamOptions

    event_stream = openai_provider._run_openai_stream(
        model,
        Context(messages=[UserMessage(content="hi")]),
        SimpleStreamOptions(api_key="test", reasoning="medium"),
    )
    async for _ in event_stream:
        pass
    await event_stream.result()

    # medium 默认预算 8192，上限 max_tokens(32768) - 1024 远大于 8192
    assert capture["thinking_token_budget"] == 8192


async def test_openai_thinking_token_budget_capped_by_max_tokens(monkeypatch):
    """thinking_token_budget 不超过 max_tokens - MIN_ANSWER_TOKENS，保证答案空间。"""
    capture: dict = {}
    monkeypatch.setattr(
        openai_provider,
        "_create_client",
        lambda model, api_key, headers, http_client=None: _capturing_openai_client(
            [_chunk(finish_reason="stop")], capture
        ),
    )
    model = _make_model()
    model.reasoning = True
    model.max_tokens = 4096
    model.compat = {"supportsThinkingTokenBudget": True}

    from pi_ai import SimpleStreamOptions

    event_stream = openai_provider._run_openai_stream(
        model,
        Context(messages=[UserMessage(content="hi")]),
        SimpleStreamOptions(api_key="test", reasoning="high", max_tokens=4096),
    )
    async for _ in event_stream:
        pass
    await event_stream.result()

    # high 默认 16384，但上限 = 4096 - 1024 = 3072
    assert capture["thinking_token_budget"] == 3072


async def test_openai_thinking_token_budget_skipped_without_compat(monkeypatch):
    """未声明 supportsThinkingTokenBudget 时不注入预算。"""
    capture: dict = {}
    monkeypatch.setattr(
        openai_provider,
        "_create_client",
        lambda model, api_key, headers, http_client=None: _capturing_openai_client(
            [_chunk(finish_reason="stop")], capture
        ),
    )
    model = _make_model()
    model.reasoning = True

    from pi_ai import SimpleStreamOptions

    event_stream = openai_provider._run_openai_stream(
        model,
        Context(messages=[UserMessage(content="hi")]),
        SimpleStreamOptions(api_key="test", reasoning="high"),
    )
    async for _ in event_stream:
        pass
    await event_stream.result()

    assert "thinking_token_budget" not in capture
