"""真实 LLM 调用集成测试。

默认跳过，仅当设置了对应环境变量时运行：
    OPENAI_API_KEY    → OpenAI 测试
    DEEPSEEK_API_KEY  → DeepSeek 测试（OpenAI 兼容协议）

手动运行：
    uv run pytest packages/pi-ai/tests/test_integration.py -m integration -v -s

注意：这类测试会消耗真实 API 额度，请控制运行频率。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

# 加载 .env（项目根）—— 必须在 import pi_ai 前执行，使 key 进入环境
_load_path = Path(__file__).resolve().parents[3] / ".env"
if _load_path.exists():
    load_dotenv(_load_path)

from pi_ai import (  # noqa: E402
    Context,
    StreamOptions,
    Tool,
    UserMessage,
    complete,
    stream,
)
from pi_ai.events import TextDeltaEvent, ToolCallEndEvent  # noqa: E402

pytestmark = pytest.mark.integration

OPENAI_AVAILABLE = bool(os.environ.get("OPENAI_API_KEY"))
DEEPSEEK_AVAILABLE = bool(os.environ.get("DEEPSEEK_API_KEY"))


# ============================================================
# OpenAI（官方端点）
# ============================================================


@pytest.mark.skipif(not OPENAI_AVAILABLE, reason="未设置 OPENAI_API_KEY")
async def test_openai_text_streaming():
    """OpenAI 流式文本：真实请求，验证流式增量解析。"""
    from pi_ai import get_model

    model = get_model("openai", "gpt-4o-mini")
    assert model is not None, "gpt-4o-mini 模型未注册"
    ctx = Context(messages=[UserMessage(content="用一句话介绍 Python，不超过 20 字。")])

    es = stream(model, ctx, StreamOptions(max_tokens=100))
    deltas = []
    async for ev in es:
        if isinstance(ev, TextDeltaEvent):
            deltas.append(ev.delta)

    msg = await es.result()
    assert msg.stop_reason == "stop", f"stop_reason={msg.stop_reason}, err={msg.error_message}"
    full = "".join(deltas)
    assert len(full) > 0, "未收到任何文本增量"
    # final message 的 content 应与增量累加一致
    assert msg.content[0].text == full
    # usage 应有值
    assert msg.usage.total_tokens > 0, f"usage 全零: {msg.usage}"
    print(f"\n[OpenAI] 输出: {full}")
    print(f"[OpenAI] usage: input={msg.usage.input} output={msg.usage.output}")


@pytest.mark.skipif(not OPENAI_AVAILABLE, reason="未设置 OPENAI_API_KEY")
async def test_openai_tool_calling():
    """OpenAI 工具调用：真实请求，验证工具调用增量累加 + 参数解析。"""
    from pi_ai import get_model

    model = get_model("openai", "gpt-4o-mini")
    ctx = Context(
        messages=[UserMessage(content="北京今天天气怎么样？用 get_weather 工具查询。")],
        tools=[
            Tool(
                name="get_weather",
                description="查询某城市天气",
                parameters={
                    "type": "object",
                    "properties": {"city": {"type": "string", "description": "城市名"}},
                    "required": ["city"],
                },
            )
        ],
    )

    es = stream(model, ctx, StreamOptions(max_tokens=200))
    tool_calls_seen = []
    async for ev in es:
        if isinstance(ev, ToolCallEndEvent):
            tool_calls_seen.append(ev.tool_call)

    msg = await es.result()
    assert msg.stop_reason == "toolUse", f"stop_reason={msg.stop_reason}, err={msg.error_message}"
    assert len(tool_calls_seen) == 1, f"预期 1 个工具调用，实际 {len(tool_calls_seen)}"
    tc = tool_calls_seen[0]
    assert tc.name == "get_weather"
    assert "city" in tc.arguments
    assert "北京" in str(tc.arguments["city"]) or "Beijing" in str(tc.arguments["city"]).lower()
    print(f"\n[OpenAI] 工具调用: {tc.name}({tc.arguments})")


@pytest.mark.skipif(not OPENAI_AVAILABLE, reason="未设置 OPENAI_API_KEY")
async def test_openai_complete_non_streaming():
    """complete() 非流式聚合。"""
    from pi_ai import get_model

    model = get_model("openai", "gpt-4o-mini")
    ctx = Context(messages=[UserMessage(content="1+1 等于几？只回答数字。")])
    msg = await complete(model, ctx, StreamOptions(max_tokens=20))
    assert msg.stop_reason == "stop", f"err={msg.error_message}"
    assert len(msg.content[0].text) > 0
    print(f"\n[OpenAI complete] {msg.content[0].text}")


# ============================================================
# DeepSeek（OpenAI 兼容协议，验证第三方厂商兼容性）
# ============================================================


def _deepseek_model():
    """构造 DeepSeek 模型（OpenAI 兼容协议）。"""
    from pi_ai import Model, ModelCost

    return Model(
        id="deepseek-chat",
        name="DeepSeek Chat",
        api="openai-completions",
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
        reasoning=False,
        input=["text"],
        cost=ModelCost(input=0.14, output=0.28, cache_read=0.014),
        context_window=64000,
        max_tokens=8192,
    )


@pytest.mark.skipif(not DEEPSEEK_AVAILABLE, reason="未设置 DEEPSEEK_API_KEY")
async def test_deepseek_text_streaming():
    """DeepSeek 流式文本：验证 OpenAI provider 在第三方兼容厂商上的表现。"""
    model = _deepseek_model()
    api_key = os.environ["DEEPSEEK_API_KEY"]
    ctx = Context(messages=[UserMessage(content="用一句话介绍 DeepSeek，不超过 20 字。")])

    es = stream(model, ctx, StreamOptions(api_key=api_key, max_tokens=100))
    deltas = []
    async for ev in es:
        if isinstance(ev, TextDeltaEvent):
            deltas.append(ev.delta)

    msg = await es.result()
    assert msg.stop_reason == "stop", f"stop_reason={msg.stop_reason}, err={msg.error_message}"
    full = "".join(deltas)
    assert len(full) > 0
    assert msg.content[0].text == full
    print(f"\n[DeepSeek] 输出: {full}")
    print(f"[DeepSeek] usage: input={msg.usage.input} output={msg.usage.output}")


@pytest.mark.skipif(not DEEPSEEK_AVAILABLE, reason="未设置 DEEPSEEK_API_KEY")
async def test_deepseek_tool_calling():
    """DeepSeek 工具调用：验证第三方厂商的工具调用增量解析。"""
    model = _deepseek_model()
    api_key = os.environ["DEEPSEEK_API_KEY"]
    ctx = Context(
        messages=[UserMessage(content="查一下上海现在几点了，用 get_time 工具。")],
        tools=[
            Tool(
                name="get_time",
                description="查询某城市当前时间",
                parameters={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            )
        ],
    )

    es = stream(model, ctx, StreamOptions(api_key=api_key, max_tokens=200))
    tool_calls_seen = []
    async for ev in es:
        if isinstance(ev, ToolCallEndEvent):
            tool_calls_seen.append(ev.tool_call)

    msg = await es.result()
    assert msg.stop_reason == "toolUse", f"stop_reason={msg.stop_reason}, err={msg.error_message}"
    assert len(tool_calls_seen) >= 1
    tc = tool_calls_seen[0]
    assert tc.name == "get_time"
    assert "city" in tc.arguments
    print(f"\n[DeepSeek] 工具调用: {tc.name}({tc.arguments})")
