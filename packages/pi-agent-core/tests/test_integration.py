"""pi-agent-core 真实 LLM 集成测试。

用 DeepSeek（OpenAI 兼容协议）验证 agent 循环 + 工具执行。
默认跳过，设置 DEEPSEEK_API_KEY 后 -m integration 运行。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

_load_path = Path(__file__).resolve().parents[3] / ".env"
if _load_path.exists():
    load_dotenv(_load_path)

from pi_agent_core import (  # noqa: E402
    Agent,
    AgentContext,
    AgentLoopConfig,
    AgentOptions,
    AgentToolResult,
    agent_loop,
)
from pi_ai import Model, ModelCost, TextContent  # noqa: E402

pytestmark = pytest.mark.integration

DEEPSEEK_AVAILABLE = bool(os.environ.get("DEEPSEEK_API_KEY"))


def _deepseek_model() -> Model:
    return Model(
        id="deepseek-chat",
        name="DeepSeek Chat",
        api="openai-completions",
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
        reasoning=False,
        input=["text"],
        cost=ModelCost(input=0.14, output=0.28),
        context_window=64000,
        max_tokens=8192,
    )


class _CalculatorTool:
    """测试用计算器工具。"""

    name = "calculate"
    description = "执行简单的数学计算，输入表达式返回结果"
    parameters = {
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "数学表达式，如 2+3"},
        },
        "required": ["expression"],
    }
    label = "Calculator"
    execution_mode = None

    async def execute(self, tool_call_id, params, cancel_event=None, on_update=None):
        expr = params.get("expression", "")
        try:
            # 仅允许数字和基本运算符（安全）
            allowed = set("0123456789+-*/(). ")
            if not all(c in allowed for c in expr):
                return AgentToolResult(
                    content=[TextContent(text=f"不支持的表达式: {expr}")],
                    details={"error": "invalid chars"},
                )
            result = eval(expr)  # noqa: S307 — 已校验字符集
            return AgentToolResult(
                content=[TextContent(text=f"{expr} = {result}")],
                details={"expression": expr, "result": result},
            )
        except Exception as e:  # noqa: BLE001
            return AgentToolResult(
                content=[TextContent(text=f"计算错误: {e}")],
                details={"error": str(e)},
            )


@pytest.mark.skipif(not DEEPSEEK_AVAILABLE, reason="未设置 DEEPSEEK_API_KEY")
async def test_agent_loop_with_real_tool_call():
    """真实调用：agent 收到数学问题 → 调用 calculate 工具 → 返回结果。"""
    from pi_ai import UserMessage

    api_key = os.environ["DEEPSEEK_API_KEY"]
    model = _deepseek_model()
    ctx = AgentContext(
        system_prompt="你是计算助手。需要计算时用 calculate 工具。",
        messages=[],
        tools=[_CalculatorTool()],
    )
    config = AgentLoopConfig(model=model, api_key=api_key, max_tokens=200)

    es = agent_loop([UserMessage(content="计算 17 * 23 等于多少")], ctx, config)

    events = []
    async for ev in es:
        events.append(ev)

    messages = await es.result()

    # 应有 tool_execution 事件
    types = [e.type for e in events]
    assert "tool_execution_start" in types, f"未触发工具执行，事件: {types}"
    assert "tool_execution_end" in types

    # 应有 ToolResultMessage
    from pi_ai import AssistantMessage, ToolResultMessage

    tool_results = [m for m in messages if isinstance(m, ToolResultMessage)]
    assert len(tool_results) >= 1, f"未收到工具结果，消息: {[type(m).__name__ for m in messages]}"
    tr = tool_results[0]
    assert tr.is_error is False
    assert "391" in tr.content[0].text  # 17*23=391
    print(f"\n[agent-loop] 工具结果: {tr.content[0].text}")

    # 最终 assistant 应看到结果
    final_assistants = [m for m in messages if isinstance(m, AssistantMessage)]
    last = final_assistants[-1]
    assert last.stop_reason == "stop"
    print(f"[agent-loop] 最终回复: {last.content[0].text if last.content else '(空)'}")


@pytest.mark.skipif(not DEEPSEEK_AVAILABLE, reason="未设置 DEEPSEEK_API_KEY")
async def test_stateful_agent_with_tool():
    """有状态 Agent：prompt → 工具调用 → 回复。"""
    api_key = os.environ["DEEPSEEK_API_KEY"]
    model = _deepseek_model()
    agent = Agent(
        AgentOptions(
            initial_state={
                "system_prompt": "你是计算助手。需要计算时用 calculate 工具，不要自己算。",
                "model": model,
                "tools": [_CalculatorTool()],
            },
            get_api_key=lambda provider: api_key,
            tool_execution="parallel",
        )
    )

    # 订阅事件
    seen_types = []

    def listener(event, signal):
        seen_types.append(event.type)

    unsub = agent.subscribe(listener)

    await agent.prompt("计算 100 / 4")

    assert "tool_execution_start" in seen_types
    assert "tool_execution_end" in seen_types
    assert "agent_end" in seen_types

    # state.messages 应含完整对话
    msgs = agent.state.messages
    print(f"\n[Agent] 消息序列: {[type(m).__name__ for m in msgs]}")
    from pi_ai import ToolResultMessage

    tool_results = [m for m in msgs if isinstance(m, ToolResultMessage)]
    assert len(tool_results) >= 1
    assert "25" in tool_results[0].content[0].text  # 100/4=25
    print(f"[Agent] 工具结果: {tool_results[0].content[0].text}")

    unsub()
