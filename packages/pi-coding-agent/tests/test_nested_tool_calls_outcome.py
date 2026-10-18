"""`NestedToolCallRunner.execute` 的 outcome 形状与**关联性**。

上游 `execute.ts:488-489` 读 `outcome.toolCall.id` 与 `outcome.isError`；
本仓由 `NestedToolCallRunner.execute` 把宿主返回值**包成** `ToolCallOutcome` 并补上 `tool_call`。

> **光断言形状没断言关联等于没测。** 那个 id 的全部意义是「嵌套调用 ↔ 管线事件」的关联键：
> 只断言它是 `<caller>/<n>` 这样的字符串，无法发现「runner 生成的 id 与发出去的事件不是同一个」。
> 所以这里**跑一次真实调用**，收集 `tool_execution_start` 事件，再断言两者相等。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from pi_coding_agent.nested_tool_calls import (
    NestedToolCallOptions,
    NestedToolCallRunner,
    ToolCallOutcome,
)


@dataclass
class _Result:
    content: list[Any] = field(default_factory=list)
    is_error: bool = False


@dataclass
class _HostOutcome:
    """宿主（会话）返回值的形状：带 ``is_error`` / ``result`` / 可选 ``duration_ms``。"""

    result: Any
    is_error: bool = False
    duration_ms: float | None = None


@dataclass
class _Tool:
    name: str
    execution_mode: str | None = None


class _Host:
    """最小宿主：记录发出的事件，并按给定形状返回 outcome。"""

    def __init__(
        self, *, known: tuple[str, ...] = ("read",), fail: bool = False, omit_result: bool = False
    ) -> None:
        self.events: list[dict[str, Any]] = []
        self._known = known
        self._fail = fail
        self._omit_result = omit_result

    def get_tools(self) -> list[_Tool]:
        return [_Tool(name=name) for name in self._known]

    def is_sequential(self) -> bool:
        return False

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    async def run_tool_call(
        self, tool_call: Any, parent_tool_call_id: str, signal: Any, on_update: Any
    ) -> _HostOutcome:
        # 宿主**不需要**构造 toolCall —— runner 会补。它只回报结果与错误标记。
        if self._omit_result:
            # 工具被拦截/校验失败：宿主标了错，但**没有 result**。
            return _HostOutcome(result=None, is_error=True)
        return _HostOutcome(result=_Result(), is_error=self._fail, duration_ms=12.5)


def _run(host: _Host, caller: str = "call-1", name: str = "read") -> Any:
    runner = NestedToolCallRunner(host=host)
    return asyncio.run(runner.execute(caller, name, {"x": 1}, NestedToolCallOptions()))


# ============================================================
# 形状
# ============================================================


def test_outcome_is_a_dict_with_attribute_access():
    """既支持 ``outcome["tool_call"]``，也保留 ``outcome.is_error`` 的旧读法。"""
    outcome = _run(_Host())
    assert isinstance(outcome, ToolCallOutcome)
    assert isinstance(outcome, dict)
    assert (
        outcome["tool_call"]["id"] == "call-1/1"
    )  # 序号从 1 起（上游 nested-tool-calls.ts:184 同）
    assert outcome.is_error is False
    assert outcome["is_error"] is False
    assert outcome.duration_ms == 12.5


def test_outcome_omits_duration_when_the_host_did_not_report_one():
    """缺席就不写键（与 `pi-mcp` 那轮「在构造处不写键」一致）。"""
    host = _Host(omit_result=True)
    outcome = _run(host)
    assert "duration_ms" not in outcome
    assert hasattr(outcome, "duration_ms") is False


def test_is_error_still_comes_from_the_host_wrapper_not_the_result():
    """**既有语义零变更**：`is_error` 读宿主包装层，不读 ``result.is_error``。

    宿主在「工具被拦截/校验失败」时会置 ``is_error=True`` 而 ``result`` 为 ``None``；
    若改成读 ``result.is_error``，这种情况会**静默**从「错误」变成「正常」。
    """
    outcome = _run(_Host(omit_result=True))
    assert outcome.result is None
    assert outcome.is_error is True


# ============================================================
# 关联性（这条才是重点）
# ============================================================


def test_tool_call_id_matches_the_emitted_pipeline_event():
    """`outcome["tool_call"]["id"]` 必须**就是**发出去的那个 id，且与 parent 对得上。"""
    host = _Host()
    outcome = _run(host, caller="call-7")

    starts = [event for event in host.events if event["type"] == "tool_execution_start"]
    assert len(starts) == 1
    start = starts[0]

    # 1) 形状与上游 `<calling id>/<n>` 一致
    assert outcome["tool_call"]["id"] == "call-7/1"
    # 2) 与管线事件是**同一个** id（关联键的全部意义）
    assert outcome["tool_call"]["id"] == start["toolCallId"]
    # 3) parent 指回调用方
    assert start["parentToolCallId"] == "call-7"
    # 序号从 1 起：上游 `nested-tool-calls.ts:184` 是 `nextId: 1`，本端 `_CallScope.next_id = 1`
    assert start["toolCallId"] == f"{start['parentToolCallId']}/1"


def test_second_nested_call_gets_the_next_ordinal_and_still_matches():
    """序号递增，且每次都与自己的事件对得上（防止 id 只在第一跳正确）。"""
    host = _Host()
    runner = NestedToolCallRunner(host=host)

    async def run_twice() -> list[Any]:
        first = await runner.execute("c", "read", {}, NestedToolCallOptions())
        second = await runner.execute("c", "read", {}, NestedToolCallOptions())
        return [first, second]

    first, second = asyncio.run(run_twice())
    starts = [event for event in host.events if event["type"] == "tool_execution_start"]
    assert [event["toolCallId"] for event in starts] == ["c/1", "c/2"]
    assert first["tool_call"]["id"] == starts[0]["toolCallId"]
    assert second["tool_call"]["id"] == starts[1]["toolCallId"]


def test_end_event_carries_the_same_id_as_the_start_event():
    host = _Host()
    outcome = _run(host, caller="c")
    start = next(event for event in host.events if event["type"] == "tool_execution_start")
    end = next(event for event in host.events if event["type"] == "tool_execution_end")
    assert start["toolCallId"] == end["toolCallId"] == outcome["tool_call"]["id"]
    assert end["isError"] is False
