"""有状态 Agent 封装。

对应上游 ``packages/agent/src/agent.ts``。维护 transcript、消息队列、生命周期，
内部委托给 ``agent_loop`` / ``agent_loop_continue``。

关键机制：
- 每次 prompt/continue 给 loop 一个 context 快照（messages/tools 都 copy）。
- steering（工作中注入）vs follow-up（完成后追加）两个队列，QueueMode 控制。
- 事件经 process_events 归约到 _state，并串行广播给订阅者。
- prompt 期间不能再次 prompt（用 steer/follow_up 排队）。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from pi_ai import (
    AssistantMessage,
    TextContent,
    UserMessage,
    stream_simple,
)

from .agent_loop import _run_agent_loop, _run_agent_loop_continue
from .types import (
    AgentContext,
    AgentEndEvent,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    AgentState,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    QueueMode,
    StreamFn,
    ToolExecutionEndEvent,
    ToolExecutionMode,
    ToolExecutionStartEvent,
    TurnEndEvent,
)


class _PendingMessageQueue:
    """消息队列（all 或 one-at-a-time 模式）。"""

    def __init__(self, mode: QueueMode = "one-at-a-time") -> None:
        self.mode = mode
        self._messages: list[AgentMessage] = []

    def enqueue(self, message: AgentMessage) -> None:
        self._messages.append(message)

    def has_items(self) -> bool:
        return len(self._messages) > 0

    def drain(self) -> list[AgentMessage]:
        if self.mode == "all":
            drained = list(self._messages)
            self._messages.clear()
            return drained
        if not self._messages:
            return []
        first = self._messages[0]
        self._messages = self._messages[1:]
        return [first]

    def clear(self) -> None:
        self._messages.clear()


class AgentOptions:
    """Agent 配置。对应上游 ``AgentOptions``。

    所有字段可选；initial_state 含 system_prompt/model/tools 等。
    """

    def __init__(
        self,
        initial_state: dict[str, Any] | AgentState | None = None,
        *,
        convert_to_llm: Any = None,
        transform_context: Any = None,
        stream_fn: StreamFn | None = None,
        get_api_key: Any = None,
        before_tool_call: Any = None,
        after_tool_call: Any = None,
        prepare_next_turn: Any = None,
        steering_mode: QueueMode = "one-at-a-time",
        follow_up_mode: QueueMode = "one-at-a-time",
        tool_execution: ToolExecutionMode = "parallel",
        **extra: Any,
    ) -> None:
        # 初始状态
        if isinstance(initial_state, AgentState):
            self.initial_state = initial_state
        else:
            self.initial_state = AgentState()
            if initial_state:
                for k, v in initial_state.items():
                    setattr(self.initial_state, k, v)

        self.convert_to_llm = convert_to_llm
        self.transform_context = transform_context
        self.stream_fn = stream_fn or stream_simple
        self.get_api_key = get_api_key
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.prepare_next_turn = prepare_next_turn
        self.steering_mode = steering_mode
        self.follow_up_mode = follow_up_mode
        self.tool_execution = tool_execution
        self.extra = extra


class Agent:
    """有状态 Agent。"""

    def __init__(self, options: AgentOptions | None = None) -> None:
        opts = options or AgentOptions()
        self._state = opts.initial_state
        self._convert_to_llm = opts.convert_to_llm
        self._transform_context = opts.transform_context
        self._stream_fn = opts.stream_fn
        self._get_api_key = opts.get_api_key
        self._before_tool_call = opts.before_tool_call
        self._after_tool_call = opts.after_tool_call
        self._prepare_next_turn = opts.prepare_next_turn
        self._tool_execution = opts.tool_execution

        self.steering_queue = _PendingMessageQueue(opts.steering_mode)
        self.follow_up_queue = _PendingMessageQueue(opts.follow_up_mode)

        self._listeners: list[Any] = []  # Callable[[AgentEvent, asyncio.Event|None], Any]
        self._active_run: dict[str, Any] | None = None

    # ---- 状态 ----

    @property
    def state(self) -> AgentState:
        return self._state

    # ---- 订阅 ----

    def subscribe(self, listener: Any) -> Any:
        """订阅事件。返回取消订阅函数。"""
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsubscribe

    # ---- 队列 ----

    def steer(self, message: AgentMessage) -> None:
        """入 steering 队列（工作中注入）。"""
        self.steering_queue.enqueue(message)

    def follow_up(self, message: AgentMessage) -> None:
        """入 follow-up 队列（完成后追加）。"""
        self.follow_up_queue.enqueue(message)

    def clear_steering_queue(self) -> None:
        self.steering_queue.clear()

    def clear_follow_up_queue(self) -> None:
        self.follow_up_queue.clear()

    def clear_all_queues(self) -> None:
        self.steering_queue.clear()
        self.follow_up_queue.clear()

    def has_queued_messages(self) -> bool:
        return self.steering_queue.has_items() or self.follow_up_queue.has_items()

    # ---- 生命周期 ----

    @property
    def is_running(self) -> bool:
        return self._active_run is not None

    @property
    def cancel_event(self) -> asyncio.Event | None:
        return self._active_run["cancel_event"] if self._active_run else None

    def abort(self) -> None:
        if self._active_run and self._active_run["cancel_event"]:
            self._active_run["cancel_event"].set()

    async def wait_for_idle(self) -> None:
        if self._active_run:
            await self._active_run["promise"]

    def reset(self) -> None:
        self._state.messages = []
        self._state.streaming_message = None
        self._state.error_message = None
        self._state.pending_tool_calls = set()
        self.clear_all_queues()

    # ---- prompt / continue ----

    async def prompt(self, message: AgentMessage | list[AgentMessage] | str) -> None:
        """发送用户输入，启动循环。运行中调用会报错（用 steer/follow_up 排队）。"""
        if self._active_run:
            raise RuntimeError(
                "Agent is already processing. Use steer() or follow_up() to queue messages."
            )
        messages = self._normalize_input(message)
        await self._run_prompt_messages(messages)

    async def continue_(self) -> None:
        """从已有 context 继续。末尾须是 user/toolResult，或有排队消息。"""
        if self._active_run:
            raise RuntimeError("Agent is already processing.")
        last = self._state.messages[-1] if self._state.messages else None
        if not last:
            raise RuntimeError("No messages to continue from")
        if isinstance(last, AssistantMessage):
            # 末尾是 assistant：尝试消费排队消息作为 prompt
            queued = self.steering_queue.drain()
            if queued:
                await self._run_prompt_messages(queued)
                return
            queued = self.follow_up_queue.drain()
            if queued:
                await self._run_prompt_messages(queued)
                return
            raise RuntimeError("Cannot continue from message role: assistant")
        await self._run_continuation()

    # ---- 内部 ----

    def _normalize_input(self, message: Any) -> list[AgentMessage]:
        if isinstance(message, str):
            return [
                UserMessage(
                    content=[TextContent(text=message)],
                    timestamp=int(asyncio.get_event_loop().time() * 1000),
                )
            ]
        if isinstance(message, list):
            return message
        return [message]

    def _create_context_snapshot(self) -> AgentContext:
        return AgentContext(
            system_prompt=self._state.system_prompt,
            messages=list(self._state.messages),
            tools=list(self._state.tools) if self._state.tools else None,
        )

    def _create_loop_config(self, skip_initial_steering: bool = False) -> AgentLoopConfig:
        cfg = AgentLoopConfig(
            model=self._state.model,
            tool_execution=self._tool_execution,
            convert_to_llm=self._convert_to_llm,
            transform_context=self._transform_context,
            get_api_key=self._get_api_key,
            before_tool_call=self._before_tool_call,
            after_tool_call=self._after_tool_call,
        )
        cfg.reasoning = self._state.thinking_level or None
        # 用闭包接 steering/follow-up 队列
        _skip = [skip_initial_steering]

        async def _get_steering() -> list[AgentMessage]:
            if _skip[0]:
                _skip[0] = False
                return []
            return self.steering_queue.drain()

        async def _get_follow_up() -> list[AgentMessage]:
            return self.follow_up_queue.drain()

        cfg.get_steering_messages = _get_steering
        cfg.get_follow_up_messages = _get_follow_up
        return cfg

    async def _run_prompt_messages(
        self, messages: list[AgentMessage], skip_initial_steering: bool = False
    ) -> None:
        await self._run_with_lifetime(
            lambda cancel_event: _run_agent_loop(
                messages,
                self._create_context_snapshot(),
                self._create_loop_config(skip_initial_steering),
                self._process_events,
                cancel_event,
                self._stream_fn,
            )
        )

    async def _run_continuation(self) -> None:
        await self._run_with_lifetime(
            lambda cancel_event: _run_agent_loop_continue(
                self._create_context_snapshot(),
                self._create_loop_config(),
                self._process_events,
                cancel_event,
                self._stream_fn,
            )
        )

    async def _run_with_lifetime(self, executor: Any) -> None:
        if self._active_run:
            raise RuntimeError("Agent is already processing.")
        cancel_event = asyncio.Event()
        future: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        self._active_run = {"promise": future, "cancel_event": cancel_event}
        self._state.is_streaming = True
        self._state.streaming_message = None
        self._state.error_message = None
        try:
            await executor(cancel_event)
        except Exception as exc:  # noqa: BLE001
            # 补发完整生命周期的事件（保证监听器看到一致序列）
            err_msg = AssistantMessage(
                stop_reason="error",
                error_message=str(exc),
                api="",
                provider="",
                model="",
            )
            await self._process_events(MessageStartEvent(message=err_msg))
            await self._process_events(MessageEndEvent(message=err_msg))
            await self._process_events(TurnEndEvent(message=err_msg, tool_results=[]))
            await self._process_events(AgentEndEvent(messages=[err_msg]))
        finally:
            self._state.is_streaming = False
            self._state.streaming_message = None
            self._active_run = None
            if not future.done():
                future.set_result(None)

    async def _process_events(self, event: AgentEvent) -> None:
        """状态归约 + 串行广播。"""
        if isinstance(event, (MessageStartEvent, MessageUpdateEvent)):
            self._state.streaming_message = event.message
        elif isinstance(event, MessageEndEvent):
            self._state.streaming_message = None
            self._state.messages.append(event.message)
        elif isinstance(event, ToolExecutionStartEvent):
            self._state.pending_tool_calls = self._state.pending_tool_calls | {event.tool_call_id}
        elif isinstance(event, ToolExecutionEndEvent):
            self._state.pending_tool_calls = self._state.pending_tool_calls - {event.tool_call_id}
        elif isinstance(event, TurnEndEvent):
            if isinstance(event.message, AssistantMessage) and event.message.error_message:
                self._state.error_message = event.message.error_message
        elif isinstance(event, AgentEndEvent):
            self._state.streaming_message = None

        # 串行广播
        for listener in self._listeners:
            result = listener(event, self.cancel_event)
            if inspect.isawaitable(result):
                await result


__all__ = ["Agent", "AgentOptions"]
