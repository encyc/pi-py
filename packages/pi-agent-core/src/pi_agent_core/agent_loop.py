"""无状态 agent 循环引擎。

对应上游 ``packages/agent/src/agent-loop.ts``。核心是双层循环：
- 外层：agent 本应停止时检查 follow-up 消息
- 内层：处理 tool calls + steering

设计要点（与上游一致）：
- 全程使用 AgentMessage，仅在 LLM 调用边界通过 convert_to_llm 转 Message[]。
- 错误编码为消息（stop_reason="error"/"aborted"），不抛异常。
- 流式 partial assistant 消息用占位-替换模式。
- length stop_reason 时跳过工具执行（参数可能被截断）。
- steering 在内层末尾轮询；follow-up 在外层（agent 本应停止时）轮询。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import replace
from typing import Any, cast

from pi_ai import (
    AssistantMessage,
    Context,
    EventStream,
    SimpleStreamOptions,
    TextContent,
    ToolResultMessage,
    UserMessage,
    stream_simple,
)

from .event_stream import create_agent_stream
from .types import (
    AgentContext,
    AgentEndEvent,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    AgentStartEvent,
    AgentTool,
    AgentToolCall,
    AgentToolResult,
    AgentTurnContext,
    AgentTurnDecision,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    PrepareRequestContext,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)

#: 事件接收器。

#: 事件接收器。
AgentEventSink = Any  # Callable[[AgentEvent], Awaitable[None] | None]


# ============================================================
# 公共入口
# ============================================================


def agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    cancel_event: asyncio.Event | None = None,
    stream_fn: Any = None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    """新建会话入口。返回事件流，结束时 result() 给出最终消息列表。"""
    es = create_agent_stream()

    async def _run() -> None:
        try:
            messages = await _run_agent_loop(
                prompts, context, config, es.push, cancel_event, stream_fn
            )
            es.end(messages)
        except BaseException as exc:  # noqa: BLE001
            es.error(exc)

    asyncio.ensure_future(_run())
    return es


def agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    cancel_event: asyncio.Event | None = None,
    stream_fn: Any = None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    """续会话入口。context 最后一条消息必须是 user 或 toolResult。"""
    if not context.messages:
        raise ValueError("Cannot continue: no messages in context")
    last = context.messages[-1]
    if isinstance(last, AssistantMessage):
        raise ValueError("Cannot continue from message role: assistant")

    es = create_agent_stream()

    async def _run() -> None:
        try:
            messages = await _run_agent_loop_continue(
                context, config, es.push, cancel_event, stream_fn
            )
            es.end(messages)
        except BaseException as exc:  # noqa: BLE001
            es.error(exc)

    asyncio.ensure_future(_run())
    return es


async def _run_agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    cancel_event: asyncio.Event | None,
    stream_fn: Any,
) -> list[AgentMessage]:
    new_messages: list[AgentMessage] = list(prompts)
    current_context = AgentContext(
        system_prompt=context.system_prompt,
        messages=[*context.messages, *prompts],
        tools=context.tools,
    )
    await _safe_emit(emit, AgentStartEvent())
    await _safe_emit(emit, TurnStartEvent())
    for prompt in prompts:
        await _safe_emit(emit, MessageStartEvent(message=prompt))
        await _safe_emit(emit, MessageEndEvent(message=prompt))
    await _run_loop(current_context, new_messages, config, cancel_event, emit, stream_fn)
    return new_messages


async def _run_agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    cancel_event: asyncio.Event | None,
    stream_fn: Any,
) -> list[AgentMessage]:
    new_messages: list[AgentMessage] = []
    await _safe_emit(emit, AgentStartEvent())
    await _safe_emit(emit, TurnStartEvent())
    await _run_loop(context, new_messages, config, cancel_event, emit, stream_fn)
    return new_messages


# ============================================================
# 双层循环核心
# ============================================================


async def _run_loop(
    initial_context: AgentContext,
    new_messages: list[AgentMessage],
    initial_config: AgentLoopConfig,
    cancel_event: asyncio.Event | None,
    emit: AgentEventSink,
    stream_fn: Any,
) -> None:
    current_context = initial_context
    config = initial_config
    last_completed_turn: AgentTurnContext | None = None
    #: finish_turn 返回 {"action": "continue"} 时置位：保证再发起一次「仅上下文」请求。
    explicit_continuation = False
    pending_messages: list[AgentMessage] = await _safe_drain(config.get_steering_messages)

    # 外层：agent 本应停止时检查 follow-up 消息
    while True:
        # 注意：每轮外层循环都重置为 True（与上游一致），这样 explicit_continuation
        # 走 continue 复活外层时，内层仍会执行「仅上下文」的那一次请求。
        has_more_tool_calls = True

        # 内层：tool calls + steering
        while has_more_tool_calls or pending_messages:
            if last_completed_turn is not None:
                # v0.85.1：prepare_next_turn 移到下一轮开始前（仅当循环继续时执行），
                # 可替换 context/model/thinking level。准备可能是长操作（如压缩），
                # 期间排队的 steering 在此补拉（仅当此前未拉到消息，避免单轮注入两条）。
                if config.prepare_next_turn:
                    snapshot = await _maybe_await(config.prepare_next_turn(last_completed_turn))
                    if snapshot:
                        current_context, config = _apply_turn_update(
                            current_context, config, snapshot
                        )
                if not pending_messages:
                    pending_messages = await _safe_drain(config.get_steering_messages)
                await _safe_emit(emit, TurnStartEvent())

            # 注入待处理消息（steering，在下一个 assistant 响应前）
            if pending_messages:
                for message in pending_messages:
                    await _safe_emit(emit, MessageStartEvent(message=message))
                    await _safe_emit(emit, MessageEndEvent(message=message))
                    current_context.messages.append(message)
                    new_messages.append(message)
                pending_messages = []

            # v0.87.0：prepare_request —— 待处理消息已追加并发出之后、provider 请求之前
            # 运行（含第一次请求），可替换本次请求的 context/model/thinking level。
            if config.prepare_request:
                request_update = await _maybe_await(
                    config.prepare_request(
                        PrepareRequestContext(
                            context=current_context,
                            model=config.model,
                            thinking_level=config.reasoning or "off",
                        ),
                        cancel_event,
                    )
                )
                if request_update:
                    current_context, config = _apply_turn_update(
                        current_context, config, request_update
                    )

            # 流式拉取 assistant 响应
            message = await _stream_assistant_response(
                current_context, config, cancel_event, emit, stream_fn
            )
            new_messages.append(message)

            # 错误/中止：硬退出。finish_turn 仍会被调用（上游语义），但其决策被忽略。
            if message.stop_reason in ("error", "aborted"):
                last_completed_turn = AgentTurnContext(
                    message=message,
                    tool_results=[],
                    context=current_context,
                    new_messages=new_messages,
                )
                await _invoke_finish_turn(config, last_completed_turn, cancel_event)
                await _safe_emit(emit, TurnEndEvent(message=message, tool_results=[]))
                await _safe_emit(emit, AgentEndEvent(messages=new_messages))
                return

            # 抽取工具调用
            tool_calls = [c for c in message.content if isinstance(c, AgentToolCall)]
            tool_results: list[ToolResultMessage] = []
            has_more_tool_calls = False

            if tool_calls:
                if message.stop_reason == "length":
                    # 输出被截断，工具参数可能不完整，全部当失败
                    tool_results = _fail_tool_calls_from_truncated(tool_calls)
                else:
                    batch = await _execute_tool_calls(
                        current_context, message, config, cancel_event, emit
                    )
                    tool_results = batch["messages"]
                    has_more_tool_calls = not batch["terminate"]

                for result in tool_results:
                    current_context.messages.append(result)
                    new_messages.append(result)

            # v0.87.0：finish_turn 在 assistant + 全部工具结果定稿之后、turn_end 之前运行；
            # 决策在 turn_end 之后生效。error/aborted 是硬退出，其决策被忽略。
            last_completed_turn = AgentTurnContext(
                message=message,
                tool_results=tool_results,
                context=current_context,
                new_messages=new_messages,
            )
            decision = await _invoke_finish_turn(config, last_completed_turn, cancel_event)
            await _safe_emit(emit, TurnEndEvent(message=message, tool_results=tool_results))

            if decision == "end":
                await _safe_emit(emit, AgentEndEvent(messages=new_messages))
                return

            # 检查取消（本端在 v0.85.1 加的保险；上游由 aborted 消息承载）
            if cancel_event is not None and cancel_event.is_set():
                await _safe_emit(emit, AgentEndEvent(messages=new_messages))
                return

            # {"action": "continue"} 只保证「有一次下一次请求」：工具结果 / steering 若已
            # 自然选中下一轮，就由它们满足，不额外追加。
            explicit_continuation = decision == "continue"
            pending_messages = await _safe_drain(config.get_steering_messages)
            if has_more_tool_calls or pending_messages:
                explicit_continuation = False

        # Agent 本应停止。检查 follow-up
        follow_ups = await _safe_drain(config.get_follow_up_messages)
        if follow_ups:
            explicit_continuation = False
            pending_messages = follow_ups
            continue

        # 没有自然选中的请求，用一次「仅上下文」回合兑现 continue 决策
        if explicit_continuation:
            explicit_continuation = False
            continue

        break

    await _safe_emit(emit, AgentEndEvent(messages=new_messages))


# ============================================================
# 回合钩子辅助
# ============================================================


def _read_field(source: Any, *names: str) -> Any:
    """从 dict / dataclass / 对象里按候选名读字段（兼容 snake_case 与上游 camelCase）。"""
    for name in names:
        if isinstance(source, dict):
            if name in source:
                return source[name]
        elif hasattr(source, name):
            return getattr(source, name)
    return None


def _apply_turn_update(
    context: AgentContext,
    config: AgentLoopConfig,
    update: Any,
) -> tuple[AgentContext, AgentLoopConfig]:
    """把 ``prepare_next_turn`` / ``prepare_request`` 的返回值套用到运行时状态。

    - ``context``：替换下一次请求的上下文。
    - ``model``：替换模型。
    - ``thinkingLevel`` / ``thinking_level``：``"off"`` 清除 reasoning，其余替换。
      未提供（None）时保持原值。
    """
    next_context = _read_field(update, "context")
    next_model = _read_field(update, "model")
    thinking_level = _read_field(update, "thinkingLevel", "thinking_level")

    if next_context is not None:
        context = next_context

    if next_model is None and thinking_level is None:
        return context, config

    return context, replace(
        config,
        model=next_model if next_model is not None else config.model,
        reasoning=(
            config.reasoning
            if thinking_level is None
            else (None if thinking_level == "off" else thinking_level)
        ),
    )


async def _invoke_finish_turn(
    config: AgentLoopConfig,
    turn: AgentTurnContext,
    cancel_event: asyncio.Event | None,
) -> AgentTurnDecision | None:
    """调用 ``finish_turn`` 并归一化其决策。

    接受三种写法（对应上游 ``{ action: "end" | "continue" }``）：

    - ``{"action": "end"}`` / ``{"action": "continue"}``（上游原样）
    - ``"end"`` / ``"continue"``（Python 简写）
    - ``True`` / ``False``（便于从已移除的 ``should_stop_after_turn`` 直接迁移）
    """
    if config.finish_turn is None:
        return None
    raw = await _maybe_await(config.finish_turn(turn, cancel_event))
    if raw is None or raw is False:
        return None
    if raw is True:
        return "end"
    if isinstance(raw, str):
        return cast("AgentTurnDecision", raw) if raw in ("end", "continue") else None
    action = _read_field(raw, "action")
    if action in ("end", "continue"):
        return cast("AgentTurnDecision", action)
    return None


# ============================================================
# LLM 调用边界
# ============================================================


async def _stream_assistant_response(
    context: AgentContext,
    config: AgentLoopConfig,
    cancel_event: asyncio.Event | None,
    emit: AgentEventSink,
    stream_fn: Any,
) -> AssistantMessage:
    """调用 LLM，桥接 pi-ai 流式事件 → agent 事件。"""
    # 1. 可选的上下文变换
    messages = context.messages
    if config.transform_context:
        messages = await _maybe_await(config.transform_context(messages, cancel_event))

    # 2. 转成 LLM Message[]
    if config.convert_to_llm:
        llm_messages = await _maybe_await(config.convert_to_llm(messages))
    else:
        llm_messages = [
            m for m in messages if isinstance(m, (UserMessage, AssistantMessage, ToolResultMessage))
        ]

    # 3. 构造 LLM Context
    llm_context = Context(
        system_prompt=context.system_prompt or None,
        messages=llm_messages,
        tools=_convert_tools(context.tools) if context.tools else None,
    )

    # 4. 选 stream 函数，解析 api key
    fn = stream_fn or stream_simple
    api_key = None
    if config.get_api_key:
        api_key = await _maybe_await(config.get_api_key(config.model.provider))
    if not api_key:
        api_key = getattr(config, "api_key", None)

    # 5. 构造 options
    opts_kwargs: dict[str, Any] = {}
    if api_key:
        opts_kwargs["api_key"] = api_key
    if config.reasoning:
        opts_kwargs["reasoning"] = config.reasoning
    if config.temperature is not None:
        opts_kwargs["temperature"] = config.temperature
    if config.max_tokens is not None:
        opts_kwargs["max_tokens"] = config.max_tokens
    options = SimpleStreamOptions(**opts_kwargs) if opts_kwargs else None

    # 6. 调用 + 消费流
    response = await _maybe_await(fn(config.model, llm_context, options))
    partial_message: AssistantMessage | None = None
    added_partial = False

    async for event in response:
        etype = event.type
        if etype == "start":
            partial_message = event.partial
            context.messages.append(partial_message)
            added_partial = True
            await _safe_emit(emit, MessageStartEvent(message=partial_message.model_copy()))
        elif etype in (
            "text_start",
            "text_delta",
            "text_end",
            "thinking_start",
            "thinking_delta",
            "thinking_end",
            "toolcall_start",
            "toolcall_delta",
            "toolcall_end",
        ):
            if partial_message is not None:
                partial_message = event.partial
                context.messages[-1] = partial_message
                await _safe_emit(
                    emit,
                    MessageUpdateEvent(
                        message=partial_message.model_copy(), assistant_message_event=event
                    ),
                )
        elif etype in ("done", "error"):
            final_message = await response.result()
            if added_partial:
                context.messages[-1] = final_message
            else:
                context.messages.append(final_message)
            if not added_partial:
                await _safe_emit(emit, MessageStartEvent(message=final_message.model_copy()))
            await _safe_emit(emit, MessageEndEvent(message=final_message))
            return final_message  # type: ignore[no-any-return]

    # 兜底：流自然结束但没发 done/error
    final: AssistantMessage = await response.result()
    return final


def _convert_tools(tools: list[AgentTool]) -> list[Any]:
    """AgentTool 列表 → pi-ai Tool 列表（供 LLM 调用）。"""
    from pi_ai import Tool

    out = []
    for t in tools:
        params = t.parameters
        out.append(
            Tool(
                name=t.name,
                description=t.description,
                parameters=params,
                constrained_sampling=getattr(t, "constrained_sampling", None),
            )
        )
    return out


# ============================================================
# 工具执行
# ============================================================


async def _execute_tool_calls(
    context: AgentContext,
    assistant_message: AssistantMessage,
    config: AgentLoopConfig,
    cancel_event: asyncio.Event | None,
    emit: AgentEventSink,
) -> dict[str, Any]:
    """执行一批工具调用。返回 {messages, terminate}。"""
    tool_calls = [c for c in assistant_message.content if isinstance(c, AgentToolCall)]

    # 检查是否有 sequential 工具
    has_sequential = False
    if context.tools:
        for tc in tool_calls:
            tool = _find_tool(context.tools, tc.name)
            if tool and getattr(tool, "execution_mode", None) == "sequential":
                has_sequential = True
                break

    if config.tool_execution == "sequential" or has_sequential:
        return await _execute_sequential(
            context, assistant_message, tool_calls, config, cancel_event, emit
        )
    return await _execute_parallel(
        context, assistant_message, tool_calls, config, cancel_event, emit
    )


async def _execute_sequential(
    context: AgentContext,
    assistant_message: AssistantMessage,
    tool_calls: list[AgentToolCall],
    config: AgentLoopConfig,
    cancel_event: asyncio.Event | None,
    emit: AgentEventSink,
) -> dict[str, Any]:
    results: list[ToolResultMessage] = []
    all_terminate = True
    for tc in tool_calls:
        if cancel_event is not None and cancel_event.is_set():
            break
        result_msg, terminate = await _execute_single(
            context, assistant_message, tc, config, cancel_event, emit
        )
        results.append(result_msg)
        await _safe_emit(emit, MessageStartEvent(message=result_msg))
        await _safe_emit(emit, MessageEndEvent(message=result_msg))
        if not terminate:
            all_terminate = False
    return {"messages": results, "terminate": len(results) > 0 and all_terminate}


async def _execute_parallel(
    context: AgentContext,
    assistant_message: AssistantMessage,
    tool_calls: list[AgentToolCall],
    config: AgentLoopConfig,
    cancel_event: asyncio.Event | None,
    emit: AgentEventSink,
) -> dict[str, Any]:
    # 并发执行，按原始顺序收集结果
    tasks = [
        _execute_single(context, assistant_message, tc, config, cancel_event, emit)
        for tc in tool_calls
    ]
    outcomes = await asyncio.gather(*tasks)
    results: list[ToolResultMessage] = []
    all_terminate = True
    for result_msg, terminate in outcomes:
        results.append(result_msg)
        await _safe_emit(emit, MessageStartEvent(message=result_msg))
        await _safe_emit(emit, MessageEndEvent(message=result_msg))
        if not terminate:
            all_terminate = False
    return {"messages": results, "terminate": len(results) > 0 and all_terminate}


async def _execute_single(
    context: AgentContext,
    assistant_message: AssistantMessage,
    tool_call: AgentToolCall,
    config: AgentLoopConfig,
    cancel_event: asyncio.Event | None,
    emit: AgentEventSink,
) -> tuple[ToolResultMessage, bool]:
    """执行单个工具调用，返回 (ToolResultMessage, terminate)。"""
    tool = _find_tool(context.tools, tool_call.name) if context.tools else None
    await _safe_emit(
        emit,
        ToolExecutionStartEvent(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            args=tool_call.arguments,
        ),
    )

    is_error = False
    result: AgentToolResult
    terminate = False

    if tool is None:
        result = _error_result(f"Tool '{tool_call.name}' not found")
        is_error = True
    else:
        try:
            # 校验参数
            validated = _validate_args(tool, tool_call.arguments)
            # before_tool_call 钩子
            if config.before_tool_call:
                before = await _maybe_await(
                    config.before_tool_call(
                        {
                            "assistant_message": assistant_message,
                            "tool_call": tool_call,
                            "args": validated,
                            "context": context,
                        },
                        cancel_event,
                    )
                )
                if before and before.get("block"):
                    result = _error_result(before.get("reason", "Tool execution was blocked"))
                    is_error = True
                    await _safe_emit(
                        emit,
                        ToolExecutionEndEvent(
                            tool_call_id=tool_call.id,
                            tool_name=tool_call.name,
                            result=result,
                            is_error=is_error,
                        ),
                    )
                    # 对齐 v0.84.1：被 block 的工具调用可声明 terminate 以终止后续轮次
                    return _make_result_msg(tool_call, result, is_error), bool(
                        before.get("terminate")
                    )

            # v0.85.1：准备阶段（校验 + before 钩子）完成后若已被取消，不再执行
            # 工具，产出 "Operation aborted" 错误结果（含并行批次内尚未轮到的调用）。
            if cancel_event is not None and cancel_event.is_set():
                result = _error_result("Operation aborted")
                is_error = True
                await _safe_emit(
                    emit,
                    ToolExecutionEndEvent(
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        result=result,
                        is_error=is_error,
                    ),
                )
                return _make_result_msg(tool_call, result, is_error), False

            # 执行
            def on_update(partial: AgentToolResult) -> None:
                # on_update 是同步回调；_safe_emit 若返回 coroutine 则调度执行
                coro = _safe_emit(
                    emit,
                    ToolExecutionUpdateEvent(
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        args=tool_call.arguments,
                        partial_result=partial,
                    ),
                )
                if inspect.isawaitable(coro):
                    asyncio.ensure_future(coro)

            execute_fn = tool.execute
            maybe_coro = execute_fn(tool_call.id, validated, cancel_event, on_update)
            result = await _maybe_await(maybe_coro)

            # after_tool_call 钩子
            if config.after_tool_call:
                after = await _maybe_await(
                    config.after_tool_call(
                        {
                            "assistant_message": assistant_message,
                            "tool_call": tool_call,
                            "args": validated,
                            "result": result,
                            "is_error": is_error,
                            "context": context,
                        },
                        cancel_event,
                    )
                )
                if after:
                    if "content" in after:
                        result.content = after["content"]
                    if "details" in after:
                        result.details = after["details"]
                    structured = _read_field(after, "structured_content", "structuredContent")
                    if structured is not None:
                        result.structured_content = structured
                    if "is_error" in after:
                        is_error = after["is_error"]
                    if "terminate" in after:
                        terminate = after["terminate"]

        except Exception as exc:  # noqa: BLE001
            result = _error_result(str(exc))
            is_error = True

    # 返回值自报失败（不抛异常）：模型看到错误结果，details / structured_content 仍留给调用方。
    if result.is_error:
        is_error = True

    await _safe_emit(
        emit,
        ToolExecutionEndEvent(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            result=result,
            is_error=is_error,
        ),
    )
    return _make_result_msg(tool_call, result, is_error), terminate or result.terminate


def _find_tool(tools: list[AgentTool], name: str) -> AgentTool | None:
    for t in tools:
        if t.name == name:
            return t
    return None


def _validate_args(tool: AgentTool, args: dict[str, Any]) -> dict[str, Any]:
    """参数校验（简单版：用 Pydantic 校验 tool.parameters schema）。"""
    # 上游用 validateToolArguments（基于 typebox schema）。
    # Python 版：如果 tool 有 prepare_arguments 方法先调用，否则直接返回。
    prepare = getattr(tool, "prepare_arguments", None)
    if prepare:
        prepared: dict[str, Any] = prepare(args)
        return prepared
    return args


def _error_result(msg: str) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=msg)], details={"error": msg}, is_error=True)


def _make_result_msg(
    tool_call: AgentToolCall, result: AgentToolResult, is_error: bool
) -> ToolResultMessage:
    """AgentToolResult → transcript 的 ToolResultMessage。

    注意：``structured_content`` **不写入 transcript**（对齐上游「Not sent to the model」），
    它只通过 ``tool_execution_end`` 事件与 ``after_tool_call`` 钩子暴露给程序调用方。
    """
    return ToolResultMessage(
        tool_call_id=tool_call.id,
        tool_name=tool_call.name,
        content=result.content,
        details=result.details,
        usage=result.usage,
        added_tool_names=result.added_tool_names,
        is_error=is_error,
        timestamp=int(time.time() * 1000),
    )


def _fail_tool_calls_from_truncated(tool_calls: list[AgentToolCall]) -> list[ToolResultMessage]:
    """stop_reason=length 时，所有工具调用参数可能被截断，全部返回错误。"""
    return [
        ToolResultMessage(
            tool_call_id=tc.id,
            tool_name=tc.name,
            content=[TextContent(text="Tool call was truncated (output length limit reached)")],
            is_error=True,
            timestamp=int(time.time() * 1000),
        )
        for tc in tool_calls
    ]


# ============================================================
# 辅助
# ============================================================


async def _maybe_await(value: Any) -> Any:
    """同步/异步统一处理。"""
    if inspect.isawaitable(value):
        return await value
    return value


async def _safe_emit(emit: AgentEventSink, event: AgentEvent) -> None:
    """发送事件，兼容 sync/async emit。"""
    result = emit(event)
    if inspect.isawaitable(result):
        await result


async def _safe_drain(getter: Any) -> list[AgentMessage]:
    """安全排空消息队列。"""
    if getter is None:
        return []
    result = getter()
    if inspect.isawaitable(result):
        drained: list[AgentMessage] = await result
        return drained
    return result or []


__all__ = ["agent_loop", "agent_loop_continue"]
