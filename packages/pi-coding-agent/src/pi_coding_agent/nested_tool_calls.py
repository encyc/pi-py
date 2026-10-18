"""工具在执行期间发起的嵌套工具调用（``ctx.executeTool()``）。

对应上游 ``core/nested-tool-calls.ts``（263 行，**完整移植**）。

agent 循环并不知道这些调用：会话把每一个都送进 agent 的工具管线（``runToolCall``）并带上自己的
hook，发出带 ``parentToolCallId`` 的 ``tool_execution_*`` 事件，并把调用及其用量记录在
**模型发起的那个工具调用**的 tool result 消息上。

**在某个工具调用 ``ctx.executeTool()`` 之前，这里什么都不跑。**
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

__all__ = [
    "NESTED_CALL_LIMITS",
    "NestedCallRecorder",
    "NestedCallSummary",
    "NestedToolCallHost",
    "NestedToolCallOptions",
    "NestedToolCallRecord",
    "NestedToolCallRunner",
    "NestedToolCalls",
    "ToolCallOutcome",
]

#: tool result 上嵌套调用记录的限额：参数超过单次/总量上限就省略，调用数超限就丢弃，
#: 任一情况发生都会把记录标为不完整。
NESTED_CALL_LIMITS: dict[str, int] = {
    "max_calls": 256,
    "max_argument_bytes_per_call": 8 * 1024,
    "max_argument_bytes_total": 32 * 1024,
    "max_error_chars": 500,
}

_encoder = json.JSONEncoder(ensure_ascii=False)

NestedCallStatus = Literal["unfinished", "ok", "error"]


def _argument_bytes(arguments: Any) -> int:
    return len(_encoder.encode(arguments if arguments is not None else {}).encode("utf-8"))


@dataclass
class NestedToolCallRecord:
    """一个嵌套调用留在调用方 tool result 上的记录。"""

    id: str
    name: str
    status: NestedCallStatus = "unfinished"
    arguments: dict[str, Any] | None = None
    #: 参数过大被省略时，记录其字节数。
    arguments_bytes: int | None = None
    duration_ms: int | None = None
    error: str | None = None


class ToolCallOutcome(dict):  # type: ignore[type-arg]
    """一次嵌套调用的结果（对应上游 ``AgentToolCallOutcome``）。

    既是 **dict**（``outcome["tool_call"]["id"]``、``outcome["is_error"]``），
    也保留**属性访问**（``outcome.is_error`` / ``outcome.result`` / ``outcome.duration_ms``）——
    后者是 :meth:`NestedToolCalls.execute` 既有的读法，也是宿主返回值的形状，
    改成纯 dict 会让旧读法全部 ``AttributeError``。

    ``tool_call`` 由 **runner 自己生成**（``<caller id>/<n>``，与上游约定一致）：
    它是这条调用 id 的**唯一来源**，因此由 runner 负责补进结果里，
    而不是要求宿主再造一个 ``toolCall``。

    **下游可见变更**：宿主 ``run_tool_call`` 的返回值现在会被包成 ``ToolCallOutcome``。
    属性访问不变，但 ``返回对象 is 宿主原对象`` **不再成立**（见 ``PORTING.md``）。
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        # ``__getattr__`` 只在常规查找失败时被调用，因此 ``keys``/``get`` 等仍是 dict 的方法。
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            ) from None


@dataclass
class NestedToolCalls:
    """一个模型发起的工具调用的全部嵌套调用快照。"""

    calls: list[NestedToolCallRecord]
    complete: bool


@dataclass
class NestedCallSummary:
    """一个模型发起的工具调用的嵌套调用留下什么。"""

    #: 成为 tool result 消息的 ``nestedCalls``；没有嵌套调用时为 None。
    calls: NestedToolCalls | None = None
    #: 嵌套结果 ``usage`` 之和，加到消息的 ``usage`` 上。
    usage: Any = None


def _combine_usage(first: Any, second: Any) -> Any:
    """把两个 usage 相加。

    对齐上游 ``usage-totals.ts: combineUsage`` —— 该模块本包未移植（无 usage 汇总消费者），
    因此这里内联它需要的形状：两侧都是对象/映射时逐键相加，否则退回后者。
    """
    if first is None:
        return second
    if second is None:
        return first
    if isinstance(first, dict) and isinstance(second, dict):
        combined = dict(first)
        for key, value in second.items():
            existing = combined.get(key)
            combined[key] = (
                existing + value
                if isinstance(existing, (int, float)) and isinstance(value, (int, float))
                else value
            )
        return combined
    return second


class NestedCallRecorder:
    """收集一个模型发起的工具调用的嵌套调用（含嵌套工具自己发起的调用）。

    快照会成为 tool result 消息上的 ``nestedCalls``。
    """

    def __init__(self) -> None:
        self._calls: list[NestedToolCallRecord] = []
        self._started_at: dict[int, float] = {}
        self._complete = True
        self._argument_bytes = 0
        self._usage: Any = None

    def start(self, tool_call_id: str, name: str, arguments: Any) -> NestedToolCallRecord | None:
        """调用开始时记录。被丢弃时返回 None。"""
        if len(self._calls) >= NESTED_CALL_LIMITS["max_calls"]:
            self._complete = False
            return None
        record = NestedToolCallRecord(id=tool_call_id, name=name, status="unfinished")
        payload = arguments if arguments is not None else {}
        size = _argument_bytes(payload)
        if (
            size > NESTED_CALL_LIMITS["max_argument_bytes_per_call"]
            or self._argument_bytes + size > NESTED_CALL_LIMITS["max_argument_bytes_total"]
        ):
            record.arguments_bytes = size
            self._complete = False
        else:
            record.arguments = json.loads(json.dumps(payload))
            self._argument_bytes += size
        self._calls.append(record)
        self._started_at[id(record)] = time.perf_counter()
        return record

    def finish(self, record: NestedToolCallRecord | None, is_error: bool, error_text: str) -> None:
        if record is None:
            return
        record.status = "error" if is_error else "ok"
        started = self._started_at.pop(id(record), None)
        now = time.perf_counter()
        record.duration_ms = round((now - (started if started is not None else now)) * 1000)
        if is_error and error_text:
            record.error = error_text[: NESTED_CALL_LIMITS["max_error_chars"]]

    def add_usage(self, usage: Any) -> None:
        self._usage = _combine_usage(self._usage, usage) if self._usage is not None else usage

    @property
    def total_usage(self) -> Any:
        return self._usage

    def snapshot(self) -> NestedToolCalls | None:
        """当前记录的副本；没有任何嵌套调用时返回 None。"""
        if not self._calls and self._complete:
            return None
        calls = [NestedToolCallRecord(**vars(call)) for call in self._calls]
        return NestedToolCalls(
            calls=calls,
            complete=self._complete and all(call.status != "unfinished" for call in calls),
        )


@dataclass
class NestedToolCallOptions:
    """嵌套调用的选项。"""

    #: 默认沿用调用方工具的信号。
    signal: Any = None
    #: 除 ``tool_execution_update`` 事件外，额外接收嵌套工具的部分结果。
    on_update: Callable[[Any], None] | None = None


class NestedToolCallHost(Protocol):
    """嵌套调用的宿主（由会话实现）。"""

    def get_tools(self) -> Sequence[Any]:
        """嵌套调用可解析到的工具。"""
        ...

    def is_sequential(self) -> bool:
        """是否所有嵌套调用都独占执行（例如 agent 顺序执行工具调用时）。"""
        ...

    async def run_tool_call(
        self,
        tool_call: Any,
        parent_tool_call_id: str,
        signal: Any,
        on_update: Callable[[Any], Awaitable[None]],
    ) -> Any:
        """把调用送进工具管线，hook 会带上 ``parentToolCallId``。"""
        ...

    async def emit(self, event: dict[str, Any]) -> None:
        """发出嵌套调用的 ``tool_execution_*`` 事件。"""
        ...


@dataclass
class _CallScope:
    recorder: NestedCallRecorder
    next_id: int = 1
    holds_queue: bool = False


def _text_of(result: Any) -> str:
    content = getattr(result, "content", None) or []
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts)


@dataclass
class NestedToolCallRunner:
    """执行嵌套工具调用并记录它们。"""

    host: NestedToolCallHost
    _scopes: dict[str, _CallScope] = field(default_factory=dict)
    #: 串行化必须独占的嵌套调用；首次 ``execute`` 时在运行中的事件循环上初始化。
    _queue_tail: Any = None

    async def execute(
        self,
        caller_id: str,
        name: str,
        args: Any,
        options: NestedToolCallOptions | None = None,
    ) -> Any:
        """代表调用 ``caller_id`` 执行 ``name``；嵌套调用的 id 是 ``<callerId>/<n>``。

        工具失败不会抛出，而是以 ``is_error=True`` 返回。
        """
        import asyncio

        options = options or NestedToolCallOptions()
        if self._queue_tail is None:
            self._queue_tail = asyncio.get_running_loop().create_future()
            self._queue_tail.set_result(None)
        scope = self._scopes.get(caller_id)
        if scope is None:
            scope = _CallScope(recorder=NestedCallRecorder())
            self._scopes[caller_id] = scope
        tool_call_id = f"{caller_id}/{scope.next_id}"
        scope.next_id += 1
        tool_call = {"type": "toolCall", "id": tool_call_id, "name": name, "arguments": args or {}}

        record = scope.recorder.start(tool_call_id, name, tool_call["arguments"])
        await self.host.emit(
            {
                "type": "tool_execution_start",
                "toolCallId": tool_call_id,
                "toolName": name,
                "args": tool_call["arguments"],
                "parentToolCallId": caller_id,
            }
        )

        tool = next(
            (item for item in self.host.get_tools() if getattr(item, "name", None) == name), None
        )
        exclusive = not scope.holds_queue and (
            self.host.is_sequential() or getattr(tool, "execution_mode", None) == "sequential"
        )
        release = None
        if exclusive:
            previous = self._queue_tail
            release = asyncio.get_running_loop().create_future()
            self._queue_tail = release
            await previous

        self._scopes[tool_call_id] = _CallScope(
            recorder=scope.recorder, holds_queue=scope.holds_queue or exclusive
        )

        async def _on_update(partial_result: Any) -> None:
            if options.on_update is not None:
                options.on_update(partial_result)
            await self.host.emit(
                {
                    "type": "tool_execution_update",
                    "toolCallId": tool_call_id,
                    "toolName": name,
                    "args": tool_call["arguments"],
                    "partialResult": partial_result,
                    "parentToolCallId": caller_id,
                }
            )

        try:
            outcome = await self.host.run_tool_call(
                tool_call, caller_id, options.signal, _on_update
            )
        finally:
            self._scopes.pop(tool_call_id, None)
            if release is not None and not release.done():
                release.set_result(None)

        is_error = bool(getattr(outcome, "is_error", False))
        result = getattr(outcome, "result", None)
        scope.recorder.finish(record, is_error, _text_of(result))
        # 嵌套结果不落盘，其用量只经 recorder 计入。
        usage = getattr(result, "usage", None)
        if usage is not None:
            scope.recorder.add_usage(usage)

        duration_ms = getattr(outcome, "duration_ms", None)
        await self.host.emit(
            {
                "type": "tool_execution_end",
                "toolCallId": tool_call_id,
                "toolName": name,
                "result": result,
                "isError": is_error,
                **({} if duration_ms is None else {"durationMs": duration_ms}),
                "parentToolCallId": caller_id,
            }
        )
        # 补上 runner 自己知道的 ``tool_call``（id 的唯一来源）；``duration_ms`` 缺席时不写键。
        wrapped = ToolCallOutcome(tool_call=tool_call, is_error=is_error, result=result)
        if duration_ms is not None:
            wrapped["duration_ms"] = duration_ms
        return wrapped

    def take_record(self, tool_call_id: str) -> NestedCallSummary | None:
        """取出并移除某个模型发起的工具调用的嵌套调用记录。"""
        scope = self._scopes.pop(tool_call_id, None)
        if scope is None:
            return None
        return NestedCallSummary(calls=scope.recorder.snapshot(), usage=scope.recorder.total_usage)

    def clear(self) -> None:
        self._scopes.clear()
