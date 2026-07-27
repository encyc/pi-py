"""OpenAI provider 纯逻辑测试（不需要 API key）：usage 解析、流式 JSON 解析、stop reason 映射。

实际网络调用需集成测试（test_integration.py，需要 OPENAI_API_KEY，默认跳过）。
"""

from __future__ import annotations

import pytest

from pi_ai import AssistantMessage, Context, Model, ModelCost, Tool, ToolCall
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
