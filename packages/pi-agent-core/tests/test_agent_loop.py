"""agent_loop 端到端测试：用 faux provider 验证双层循环、工具执行、事件序列。"""

from __future__ import annotations

from pi_agent_core import AgentContext, AgentLoopConfig, agent_loop
from pi_agent_core.types import AgentToolResult
from pi_ai import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    get_model,
)
from pi_ai.providers.faux import FauxScript, push_script


class _EchoTool:
    """测试用工具：回显参数。"""

    name = "echo"
    description = "回显输入"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    label = "Echo"
    execution_mode = None

    async def execute(self, tool_call_id, params, cancel_event=None, on_update=None):
        return AgentToolResult(
            content=[TextContent(text=f"echo: {params.get('text', '')}")],
            details={"raw": params},
        )


def _faux_model():
    return get_model("faux", "faux")


def _make_config(tools=None):
    cfg = AgentLoopConfig(model=_faux_model(), tool_execution="parallel")
    if tools:
        cfg.tools = None  # tools 走 context
    return cfg


async def _collect(es):
    events = []
    async for ev in es:
        events.append(ev)
    return events


# ============================================================
# 基础：纯文本对话，无工具
# ============================================================


async def test_text_only_no_tools():
    """纯文本对话：LLM 回复后无工具调用，循环结束。"""
    push_script(FauxScript(text="你好！"))
    ctx = AgentContext(system_prompt="你是助手", messages=[], tools=None)
    config = _make_config()

    es = agent_loop([UserMessage(content="hi")], ctx, config)
    events = await _collect(es)
    messages = await es.result()

    types = [e.type for e in events]
    assert types[0] == "agent_start"
    assert types[1] == "turn_start"
    # prompt 的 message_start/end
    assert "message_start" in types
    assert "message_end" in types
    assert types[-1] == "agent_end"

    # 最终消息列表应含 user prompt + assistant 回复
    assert len(messages) == 2
    assert isinstance(messages[1], AssistantMessage)
    assert messages[1].content[0].text == "你好！"
    assert messages[1].stop_reason == "stop"


# ============================================================
# 工具调用
# ============================================================


async def test_tool_call_single():
    """单工具调用：LLM 调用工具 → 执行 → 结果回传 → LLM 最终回复。"""
    # 第一轮：LLM 调用工具
    push_script(
        FauxScript(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "world"})])
    )
    # 第二轮：LLM 看到工具结果后回复
    push_script(FauxScript(text="done"))

    ctx = AgentContext(
        system_prompt="你是助手",
        messages=[],
        tools=[_EchoTool()],
    )
    config = _make_config(tools=[_EchoTool()])

    es = agent_loop([UserMessage(content="echo world")], ctx, config)
    events = await _collect(es)
    messages = await es.result()

    # 事件序列应含 tool_execution_start/end
    types = [e.type for e in events]
    assert "tool_execution_start" in types
    assert "tool_execution_end" in types

    # 消息序列：user prompt → assistant(tool_call) → tool_result → assistant(text)
    assert len(messages) == 4
    assert isinstance(messages[1], AssistantMessage)
    assert isinstance(messages[2], ToolResultMessage)
    assert isinstance(messages[3], AssistantMessage)

    # 工具结果内容
    tr = messages[2]
    assert tr.is_error is False
    assert tr.content[0].text == "echo: world"


async def test_tool_not_found():
    """工具不存在：返回 error result，循环继续。"""
    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="nonexistent", arguments={})]))
    push_script(FauxScript(text="ok"))

    ctx = AgentContext(system_prompt="", messages=[], tools=[_EchoTool()])
    config = _make_config()
    es = agent_loop([UserMessage(content="x")], ctx, config)
    await _collect(es)
    messages = await es.result()

    # 找到 toolResult
    tool_results = [m for m in messages if isinstance(m, ToolResultMessage)]
    assert len(tool_results) == 1
    assert tool_results[0].is_error is True
    assert "not found" in tool_results[0].content[0].text


# ============================================================
# 错误路径
# ============================================================


async def test_llm_error_terminates():
    """LLM 返回 error：循环立即终止。"""
    push_script(FauxScript(error="boom"))
    ctx = AgentContext(system_prompt="", messages=[], tools=None)
    config = _make_config()
    es = agent_loop([UserMessage(content="x")], ctx, config)
    events = await _collect(es)
    messages = await es.result()

    types = [e.type for e in events]
    assert "agent_end" in types
    # 最后一条 assistant 消息应为 error
    assistants = [m for m in messages if isinstance(m, AssistantMessage)]
    assert any(a.stop_reason == "error" for a in assistants)


# ============================================================
# 事件类型覆盖
# ============================================================


async def test_event_types_complete():
    """工具调用场景的事件类型覆盖。"""
    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "a"})]))
    push_script(FauxScript(text="ok"))

    ctx = AgentContext(system_prompt="", messages=[], tools=[_EchoTool()])
    config = _make_config()
    es = agent_loop([UserMessage(content="x")], ctx, config)
    events = await _collect(es)
    types = {e.type for e in events}

    expected = {
        "agent_start",
        "agent_end",
        "turn_start",
        "turn_end",
        "message_start",
        "message_end",
        "tool_execution_start",
        "tool_execution_end",
    }
    assert expected.issubset(types), f"缺失事件: {expected - types}"
