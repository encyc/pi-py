"""agent_loop 端到端测试：用 faux provider 验证双层循环、工具执行、事件序列。"""

from __future__ import annotations

from typing import Any

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
from pi_ai.providers.faux import FauxScript, clear_scripts, push_script


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


# ============================================================
# v0.84.1: blocked tool terminate / reset guard
# v1.3.0: should_stop_after_turn → finish_turn
# ============================================================


async def test_before_tool_call_block_with_terminate_ends_loop():
    """before_tool_call 返回 block+terminate：循环在该工具后终止，不再发起下一轮。"""
    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})]))

    async def before(ctx_, cancel_event=None):
        return {"block": True, "terminate": True, "reason": "not allowed"}

    ctx = AgentContext(system_prompt="", messages=[], tools=[_EchoTool()])
    config = AgentLoopConfig(model=_faux_model(), tool_execution="parallel")
    config.before_tool_call = before

    es = agent_loop([UserMessage(content="go")], ctx, config)
    events = await _collect(es)
    messages = await es.result()

    assert events[-1].type == "agent_end"
    # 仅有一轮 assistant（工具调用），无后续回复
    assistants = [m for m in messages if isinstance(m, AssistantMessage)]
    assert len(assistants) == 1
    # 工具结果存在且为 error
    trs = [m for m in messages if isinstance(m, ToolResultMessage)]
    assert len(trs) == 1 and trs[0].is_error


async def test_before_tool_call_block_without_terminate_continues():
    """before_tool_call 返回 block（无 terminate）：循环继续到下一轮。"""
    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})]))
    push_script(FauxScript(text="done"))

    async def before(ctx_, cancel_event=None):
        return {"block": True, "reason": "blocked"}

    ctx = AgentContext(system_prompt="", messages=[], tools=[_EchoTool()])
    config = AgentLoopConfig(model=_faux_model(), tool_execution="parallel")
    config.before_tool_call = before

    es = agent_loop([UserMessage(content="go")], ctx, config)
    await _collect(es)
    messages = await es.result()

    # 循环继续：出现第二轮 assistant 文本回复
    assistants = [m for m in messages if isinstance(m, AssistantMessage)]
    assert len(assistants) == 2
    assert assistants[1].content[0].text == "done"


async def test_finish_turn_end_ends_loop():
    """finish_turn 返回 {"action": "end"}：工具轮结束后立即终止，不发起下一轮。"""
    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})]))

    async def finish(turn, cancel):
        return {"action": "end"}

    ctx = AgentContext(system_prompt="", messages=[], tools=[_EchoTool()])
    config = AgentLoopConfig(model=_faux_model(), tool_execution="parallel")
    config.finish_turn = finish

    es = agent_loop([UserMessage(content="go")], ctx, config)
    events = await _collect(es)
    messages = await es.result()

    assert events[-1].type == "agent_end"
    assistants = [m for m in messages if isinstance(m, AssistantMessage)]
    assert len(assistants) == 1  # 仅工具调用轮，无后续


async def test_finish_turn_end_runs_before_turn_end():
    """finish_turn 在 turn_end 之前运行，决策在 turn_end 之后生效。"""
    order: list[str] = []
    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})]))

    def finish(turn, cancel):
        order.append("finish_turn")
        return {"action": "end"}

    config = AgentLoopConfig(model=_faux_model(), tool_execution="parallel", finish_turn=finish)
    ctx = AgentContext(system_prompt="", messages=[], tools=[_EchoTool()])

    es = agent_loop([UserMessage(content="go")], ctx, config)
    async for ev in es:
        if ev.type == "turn_end":
            order.append("turn_end")
        elif ev.type == "agent_end":
            order.append("agent_end")

    assert order == ["finish_turn", "turn_end", "agent_end"]


async def test_finish_turn_continue_forces_extra_request():
    """finish_turn 返回 {"action": "continue"}：即使没有工具调用/steering，也再发一次请求。"""
    push_script(FauxScript(text="first"))
    push_script(FauxScript(text="second"))

    calls: list[int] = []

    def finish(turn, cancel):
        calls.append(1)
        return {"action": "continue"} if len(calls) == 1 else None

    config = AgentLoopConfig(model=_faux_model(), tool_execution="parallel", finish_turn=finish)
    ctx = AgentContext(system_prompt="", messages=[], tools=None)

    es = agent_loop([UserMessage(content="go")], ctx, config)
    messages = await _collect_result(es)

    assistants = [m for m in messages if isinstance(m, AssistantMessage)]
    assert len(assistants) == 2
    assert assistants[1].content[0].text == "second"


async def test_finish_turn_none_keeps_normal_scheduling():
    """finish_turn 返回 None：循环正常继续（工具结果自然选中下一轮）。"""
    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})]))
    push_script(FauxScript(text="after"))

    async def finish(turn, cancel):
        return None

    ctx = AgentContext(system_prompt="", messages=[], tools=[_EchoTool()])
    config = AgentLoopConfig(model=_faux_model(), tool_execution="parallel", finish_turn=finish)

    es = agent_loop([UserMessage(content="go")], ctx, config)
    await _collect(es)
    messages = await es.result()

    assistants = [m for m in messages if isinstance(m, AssistantMessage)]
    assert len(assistants) == 2


async def test_finish_turn_bool_migration_form():
    """finish_turn 兼容 bool（便于从已移除的 should_stop_after_turn 迁移）。"""
    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})]))

    ctx = AgentContext(system_prompt="", messages=[], tools=[_EchoTool()])
    config = AgentLoopConfig(
        model=_faux_model(),
        tool_execution="parallel",
        finish_turn=lambda turn, cancel: True,
    )

    es = agent_loop([UserMessage(content="go")], ctx, config)
    await _collect(es)
    messages = await es.result()

    assert len([m for m in messages if isinstance(m, AssistantMessage)]) == 1


async def test_finish_turn_sees_error_response_but_decision_ignored():
    """error 响应：finish_turn 仍被调用（看到 stop_reason=error），但其决策被忽略。"""
    push_script(FauxScript(error="boom"))

    seen: list[tuple[str, bool]] = []

    async def finish(turn, cancel):
        seen.append((turn.message.stop_reason, bool(turn.tool_results)))
        return {"action": "continue"}  # 硬退出下必须被忽略

    config = AgentLoopConfig(model=_faux_model(), tool_execution="parallel", finish_turn=finish)
    ctx = AgentContext(system_prompt="", messages=[], tools=None)

    es = agent_loop([UserMessage(content="go")], ctx, config)
    events = await _collect(es)
    messages = await es.result()

    assert seen == [("error", False)]
    # 只有一次 assistant（error），没有被 continue 拖出第二轮
    assert len([m for m in messages if isinstance(m, AssistantMessage)]) == 1
    assert events[-1].type == "agent_end"


async def test_finish_turn_sees_aborted_response_but_decision_ignored():
    """aborted 响应：finish_turn 仍被调用，其决策被忽略。"""
    from pi_ai import EventStream
    from pi_ai.events import ErrorEvent

    def aborted_stream_fn(model, context, options):
        """产出以 stop_reason="aborted" 终止的流。

        不走 faux 的 ``FauxScript(stop_reason="aborted")``：pi-ai 的 ``DoneEvent.reason``
        字面量只允许 stop/length/toolUse，faux 会在 push DoneEvent 时抛 ValidationError
        并让流永不结束。真实 provider 用 ``ErrorEvent(reason="aborted", ...)`` 终止，
        这里照此构造。
        """
        es: EventStream = EventStream()
        msg = AssistantMessage(
            content=[TextContent(text="ignored")],
            stop_reason="aborted",
            api=model.api,
            provider=model.provider,
            model=model.id,
        )
        es.push(ErrorEvent(reason="aborted", error=msg))
        es.end(msg)
        return es

    seen: list[str] = []

    async def finish(turn, cancel):
        seen.append(turn.message.stop_reason)
        return {"action": "end"}

    config = AgentLoopConfig(model=_faux_model(), tool_execution="parallel", finish_turn=finish)
    ctx = AgentContext(system_prompt="", messages=[], tools=None)

    es = agent_loop([UserMessage(content="go")], ctx, config, stream_fn=aborted_stream_fn)
    events = await _collect(es)
    messages = await es.result()

    assert seen == ["aborted"]
    assert len([m for m in messages if isinstance(m, AssistantMessage)]) == 1
    assert events[-1].type == "agent_end"


async def test_finish_turn_receives_turn_context():
    """finish_turn 的入参是 AgentTurnContext：属性访问与下标访问都可用。"""
    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})]))
    push_script(FauxScript(text="after"))

    captured: list[Any] = []

    def finish(turn, cancel):
        captured.append(turn)
        return None

    config = AgentLoopConfig(model=_faux_model(), tool_execution="parallel", finish_turn=finish)
    ctx = AgentContext(system_prompt="", messages=[], tools=[_EchoTool()])

    es = agent_loop([UserMessage(content="go")], ctx, config)
    await _collect(es)

    first = captured[0]
    assert first.message.stop_reason == "toolUse"
    assert len(first.tool_results) == 1
    assert first.context is not None
    # 下标访问（prepare_next_turn 历史沿用）与驼峰别名
    assert first["message"] is first.message
    assert first["toolResults"] is first.tool_results
    assert first["newMessages"] is first.new_messages


# ============================================================
# v1.3.0: prepare_request
# ============================================================


async def test_prepare_request_runs_on_first_request():
    """prepare_request 在第一次请求前就运行（含首次）。"""
    push_script(FauxScript(text="hi"))

    seen: list[str] = []

    def prepare_request(request, cancel):
        seen.append(request.model.provider)
        return None

    config = AgentLoopConfig(
        model=_faux_model(), tool_execution="parallel", prepare_request=prepare_request
    )
    ctx = AgentContext(system_prompt="", messages=[], tools=None)

    es = agent_loop([UserMessage(content="go")], ctx, config)
    await _collect(es)

    assert seen == ["faux"]


async def test_prepare_request_can_replace_context():
    """prepare_request 可替换本次请求的 context（例如装入规范化上下文）。"""
    push_script(FauxScript(text="hi"))

    def prepare_request(request, cancel):
        return {
            "context": AgentContext(
                system_prompt="replaced",
                messages=[UserMessage(content="canonical")],
                tools=None,
            )
        }

    seen: list[Any] = []
    from pi_ai.providers.faux import faux_api_provider

    inner_stream = faux_api_provider.stream_simple

    def capturing_stream_fn(model, context, options):
        seen.append(context)
        return inner_stream(model, context, options)

    config = AgentLoopConfig(
        model=_faux_model(), tool_execution="parallel", prepare_request=prepare_request
    )
    ctx = AgentContext(system_prompt="orig", messages=[], tools=None)

    es = agent_loop([UserMessage(content="go")], ctx, config, stream_fn=capturing_stream_fn)
    await _collect(es)

    assert seen[0].system_prompt == "replaced"
    assert [m.content for m in seen[0].messages] == ["canonical"]


async def test_prepare_request_off_thinking_level():
    """prepare_request 返回 thinkingLevel="off" 清除 reasoning。"""
    push_script(FauxScript(text="hi"))

    from pi_ai.providers.faux import faux_api_provider

    seen_reasoning: list = []
    inner_stream = faux_api_provider.stream_simple

    def capturing_stream_fn(model, context, options):
        seen_reasoning.append(getattr(options, "reasoning", None) if options else None)
        return inner_stream(model, context, options)

    config = AgentLoopConfig(
        model=_faux_model(),
        tool_execution="parallel",
        reasoning="high",
        prepare_request=lambda request, cancel: {"thinkingLevel": "off"},
    )
    ctx = AgentContext(system_prompt="", messages=[], tools=None)

    es = agent_loop([UserMessage(content="hi")], ctx, config, stream_fn=capturing_stream_fn)
    await _collect(es)

    assert seen_reasoning == [None]


async def test_prepare_request_sees_pending_messages_already_appended():
    """prepare_request 运行时，待处理（steering）消息已追加到 context。"""
    push_script(FauxScript(text="hi"))

    seen: list[int] = []
    steering: list = [UserMessage(content="steered")]

    def get_steering():
        # 只投递一次，否则每轮都会重新排入导致循环不终止
        return [steering.pop()] if steering else []

    def prepare_request(request, cancel):
        seen.append(len(request.context.messages))
        return None

    config = AgentLoopConfig(
        model=_faux_model(),
        tool_execution="parallel",
        prepare_request=prepare_request,
        get_steering_messages=get_steering,
    )
    ctx = AgentContext(system_prompt="", messages=[], tools=None)

    es = agent_loop([UserMessage(content="go")], ctx, config)
    await _collect(es)

    # prompt(1) + steering(1) 都已追加
    assert seen == [2]


# ============================================================
# v1.3.0: AgentToolResult.structured_content / is_error / output_schema
# ============================================================


class _SchemaTool:
    """声明 output_schema 并返回 structured_content 的工具。"""

    name = "schema_tool"
    description = "返回结构化结果"
    parameters = {"type": "object", "properties": {}}
    label = "Schema"
    execution_mode = None
    output_schema = {
        "type": "object",
        "properties": {"lines": {"type": "number"}},
        "required": ["lines"],
    }

    async def execute(self, tool_call_id, params, cancel_event=None, on_update=None):
        return AgentToolResult(
            content=[TextContent(text="3 lines")],
            details={"path": "x"},
            structured_content={"lines": 3},
            usage=None,
        )


async def test_tool_structured_content_reaches_event_and_not_transcript():
    """output_schema/structured_content：程序调用方经事件拿到；transcript 不存。"""
    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="schema_tool", arguments={})]))
    push_script(FauxScript(text="done"))

    config = AgentLoopConfig(model=_faux_model(), tool_execution="parallel")
    ctx = AgentContext(system_prompt="", messages=[], tools=[_SchemaTool()])

    es = agent_loop([UserMessage(content="go")], ctx, config)
    events = await _collect(es)
    messages = await es.result()

    ends = [e for e in events if e.type == "tool_execution_end"]
    assert len(ends) == 1
    assert ends[0].result.structured_content == {"lines": 3}
    assert ends[0].is_error is False

    tr = [m for m in messages if isinstance(m, ToolResultMessage)][0]
    assert tr.is_error is False
    assert tr.content[0].text == "3 lines"
    # structured_content 不写入 transcript
    assert not hasattr(tr, "structured_content")


async def test_tool_is_error_flag_without_exception():
    """工具用 is_error=True 报告失败（不抛异常）：模型看到错误结果，details 保留。"""
    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="failing", arguments={})]))
    push_script(FauxScript(text="done"))

    class _FailingTool(_EchoTool):
        name = "failing"

        async def execute(self, tool_call_id, params, cancel_event=None, on_update=None):
            return AgentToolResult(
                content=[TextContent(text="could not do it")],
                details={"reason": "nope"},
                structured_content={"ok": False},
                is_error=True,
            )

    config = AgentLoopConfig(model=_faux_model(), tool_execution="parallel")
    ctx = AgentContext(system_prompt="", messages=[], tools=[_FailingTool()])

    es = agent_loop([UserMessage(content="go")], ctx, config)
    events = await _collect(es)
    messages = await es.result()

    ends = [e for e in events if e.type == "tool_execution_end"]
    assert ends[0].is_error is True

    tr = [m for m in messages if isinstance(m, ToolResultMessage)][0]
    assert tr.is_error is True
    assert tr.content[0].text == "could not do it"
    assert tr.details == {"reason": "nope"}


async def test_tool_execution_end_carries_usage_and_added_tool_names():
    """结果里的 usage / added_tool_names 透传到 ToolResultMessage。"""

    class _UsageTool(_EchoTool):
        name = "usage_tool"

        async def execute(self, tool_call_id, params, cancel_event=None, on_update=None):
            from pi_ai import Usage

            return AgentToolResult(
                content=[TextContent(text="ok")],
                usage=Usage(input=3, output=4, total_tokens=7),
                added_tool_names=["extra"],
            )

    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="usage_tool", arguments={})]))
    push_script(FauxScript(text="done"))

    config = AgentLoopConfig(model=_faux_model(), tool_execution="parallel")
    ctx = AgentContext(system_prompt="", messages=[], tools=[_UsageTool()])

    es = agent_loop([UserMessage(content="go")], ctx, config)
    await _collect(es)
    messages = await es.result()

    tr = [m for m in messages if isinstance(m, ToolResultMessage)][0]
    assert tr.usage is not None and tr.usage.total_tokens == 7
    assert tr.added_tool_names == ["extra"]


# ============================================================
# v1.3.0: Agent 级 finish_turn / prepare_request / system_prompt 只读
# ============================================================


async def test_agent_finish_turn_wired_and_ends_run():
    """AgentOptions.finish_turn 透传到 loop：返回 {"action": "end"} 结束本次 run。"""
    from pi_agent_core import Agent, AgentOptions

    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})]))

    calls: list[str] = []

    def finish(turn, cancel):
        calls.append(turn.message.stop_reason)
        return {"action": "end"}

    agent = Agent(
        AgentOptions(
            initial_state={"model": _faux_model(), "tools": [_EchoTool()]},
            finish_turn=finish,
        )
    )
    await agent.prompt("go")

    assert calls == ["toolUse"]
    assistants = [m for m in agent.state.messages if isinstance(m, AssistantMessage)]
    assert len(assistants) == 1


async def test_agent_prepare_request_wired():
    """AgentOptions.prepare_request 透传到 loop，且在第一次请求前运行。"""
    from pi_agent_core import Agent, AgentOptions

    push_script(FauxScript(text="hi"))

    seen: list[int] = []

    def prepare_request(request, cancel):
        seen.append(len(request.context.messages))
        return {"context": AgentContext(system_prompt="patched", messages=[], tools=None)}

    agent = Agent(
        AgentOptions(
            initial_state={"model": _faux_model(), "system_prompt": "orig"},
            prepare_request=prepare_request,
        )
    )
    await agent.prompt("go")

    assert seen == [1]
    assert agent.state.system_prompt == "orig"  # prepare_request 不改 Agent 自身状态


def test_agent_initial_state_can_set_system_prompt():
    """initial_state 里可以给 system_prompt（构造期唯一写入点）。"""
    from pi_agent_core import Agent, AgentOptions

    agent = Agent(AgentOptions(initial_state={"system_prompt": "你是助手"}))
    assert agent.state.system_prompt == "你是助手"


def test_agent_state_system_prompt_is_read_only():
    """v1.3.0：state.system_prompt 只读（对齐上游 readonly systemPrompt）。"""
    import pytest

    from pi_agent_core import Agent, AgentOptions

    agent = Agent(AgentOptions(initial_state={"system_prompt": "orig"}))
    with pytest.raises(AttributeError):
        agent.state.system_prompt = "changed"  # type: ignore[misc]
    assert agent.state.system_prompt == "orig"


def test_agent_reset_does_not_clear_system_prompt():
    """reset() 清空消息但不改 system_prompt。"""
    from pi_agent_core import Agent, AgentOptions

    agent = Agent(AgentOptions(initial_state={"system_prompt": "keep"}))
    agent.reset()
    assert agent.state.system_prompt == "keep"


def test_agent_reset_rejected_during_active_run():
    """Agent 运行中调用 reset() 抛错（对齐 v0.84.1）。"""
    import pytest

    from pi_agent_core import Agent, AgentOptions

    agent = Agent(AgentOptions())
    # 模拟活跃运行（reset 仅检查 _active_run 真值）
    agent._active_run = {"promise": None, "cancel_event": None}
    with pytest.raises(RuntimeError, match="already processing"):
        agent.reset()


def test_agent_reset_ok_when_idle():
    """空闲时 reset() 正常清空状态。"""
    from pi_agent_core import Agent, AgentOptions

    agent = Agent(AgentOptions())
    agent.reset()  # 不应抛错
    assert agent._active_run is None


# ============================================================
# v0.85.1: prepare_next_turn 时机 / 并行工具取消
# ============================================================


async def test_prepare_next_turn_runs_before_next_turn_start():
    """prepare_next_turn 在 turn_end 后、下一轮 turn_start 前执行（仅当循环
    继续时）；可替换 model/thinking level/context。"""
    calls: list[tuple[str, str]] = []

    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})]))
    push_script(FauxScript(text="done"))

    from pi_ai import get_model as _gm

    def prepare_next_turn(ctx):
        calls.append(("prepare", ctx["message"].stop_reason))
        return {
            "model": _gm("faux", "faux"),
            "thinkingLevel": "high",
        }

    config = AgentLoopConfig(
        model=_faux_model(),
        tool_execution="parallel",
        prepare_next_turn=prepare_next_turn,
        get_steering_messages=lambda: [],
    )
    config.tools = None
    ctx = AgentContext(system_prompt="", messages=[], tools=[_EchoTool()])

    es = agent_loop([UserMessage(content="hi")], ctx, config)
    events = await _collect(es)

    types = [e.type for e in events]
    turn_starts = [i for i, t in enumerate(types) if t == "turn_start"]
    assert len(turn_starts) == 2  # 首轮 + 工具轮
    # prepare 恰好执行一次（第二轮开始前），且看到上一轮的 stop_reason
    assert calls == [("prepare", "toolUse")]
    # prepare 在第二个 turn_start 之前发生（通过 calls 顺序与事件数无法直接断言，
    # 但第二轮 turn_start 存在证明循环继续时才执行）
    assert types[-1] == "agent_end"


async def test_prepare_next_turn_not_called_when_loop_ends():
    """循环终止轮（无工具调用）不再执行 prepare_next_turn。"""
    calls: list[str] = []
    push_script(FauxScript(text="done"))

    config = AgentLoopConfig(
        model=_faux_model(),
        tool_execution="parallel",
        prepare_next_turn=lambda ctx: calls.append("prepare") or None,
    )
    ctx = AgentContext(system_prompt="", messages=[], tools=None)

    es = agent_loop([UserMessage(content="hi")], ctx, config)
    await _collect(es)

    assert calls == []


async def test_prepare_next_turn_off_thinking_level():
    """thinkingLevel="off" 清除 reasoning：第二轮 LLM 调用不再带 reasoning。"""
    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})]))
    push_script(FauxScript(text="done"))

    from pi_ai.providers.faux import faux_api_provider

    seen_reasoning: list = []
    inner_stream = faux_api_provider.stream_simple

    def capturing_stream_fn(model, context, options):
        seen_reasoning.append(getattr(options, "reasoning", None) if options else None)
        return inner_stream(model, context, options)

    config = AgentLoopConfig(
        model=_faux_model(),
        tool_execution="parallel",
        reasoning="high",
        prepare_next_turn=lambda ctx: {"thinkingLevel": "off"},
    )
    ctx = AgentContext(system_prompt="", messages=[], tools=[_EchoTool()])
    es = agent_loop([UserMessage(content="hi")], ctx, config, stream_fn=capturing_stream_fn)
    await _collect(es)

    assert seen_reasoning == ["high", None]


async def test_steering_picked_up_after_prepare_next_turn():
    """prepare_next_turn 执行期间排队的 steering 消息在下一轮注入前补拉。"""
    push_script(FauxScript(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})]))
    push_script(FauxScript(text="done"))

    steering_queue: list = []

    def get_steering_messages():
        return [steering_queue.pop()] if steering_queue else []

    def prepare_next_turn(ctx):
        # 准备期间用户插入了 steering 消息
        steering_queue.append(UserMessage(content="steered"))
        return None

    config = AgentLoopConfig(
        model=_faux_model(),
        tool_execution="parallel",
        prepare_next_turn=prepare_next_turn,
        get_steering_messages=get_steering_messages,
    )
    ctx = AgentContext(system_prompt="", messages=[], tools=[_EchoTool()])

    es = agent_loop([UserMessage(content="hi")], ctx, config)
    messages = await _collect_result(es)

    # steering 消息被注入到最终消息列表
    assert any(isinstance(m, UserMessage) and m.content == "steered" for m in messages)


async def _collect_result(es):
    """消费事件流并返回最终消息列表。"""
    async for _ in es:
        pass
    return await es.result()


async def test_parallel_tool_aborted_before_execution():
    """v0.85.1：准备阶段后、执行前取消 → 工具不执行，产出 Operation aborted 错误。"""
    import asyncio

    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(id="c1", name="echo", arguments={"text": "x"}),
            ]
        )
    )
    push_script(FauxScript(text="done"))

    executed: list[str] = []

    class _SlowTool(_EchoTool):
        async def execute(self, tool_call_id, params, cancel_event=None, on_update=None):
            executed.append(tool_call_id)
            return await super().execute(tool_call_id, params, cancel_event, on_update)

    cancel_event = asyncio.Event()

    # before 钩子期间触发取消 → 准备完成后不再执行
    async def before_tool_call(ctx, cancel):
        cancel_event.set()
        return None

    config = AgentLoopConfig(
        model=_faux_model(),
        tool_execution="parallel",
        before_tool_call=before_tool_call,
    )

    es = agent_loop(
        [UserMessage(content="hi")],
        AgentContext(system_prompt="", messages=[], tools=[_SlowTool()]),
        config,
        cancel_event=cancel_event,
    )
    await _collect(es)
    messages = await es.result()

    assert executed == []  # 未执行
    tool_results = [m for m in messages if isinstance(m, ToolResultMessage)]
    assert len(tool_results) == 1
    assert tool_results[0].is_error is True
    assert "Operation aborted" in tool_results[0].content[0].text

    # 取消后循环终止，第二个脚本未被消费；清空避免污染后续 faux 测试
    clear_scripts()
