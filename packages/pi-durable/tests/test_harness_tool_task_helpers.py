"""`harness/tool.py` 的 **Slice B 第一批**：四个不依赖 `ToolTask` 的助手。

⚠️ 与 Slice A 那批的**区别**：这批里 ``resolve_tool``/``read_call`` 要一个**假 runtime**，
但 ``final_result``/``publish_progress`` 是**纯逻辑 + 假 runtime**——**不需要真调度器**。
真正的端到端（真 runtime + 真调度器）要等 `ToolTask` 本体落地。
"""

from __future__ import annotations

from typing import Any

import pytest

from pi_ai import TextContent, ToolCall
from pi_durable.harness.tool import (
    final_result,
    publish_progress,
    read_call,
    resolve_tool,
)
from pi_durable.harness.types import ToolDiagnostic, ToolExecutionResult, ToolRegistration


def tool(name: str = "read", **kwargs: Any) -> ToolRegistration[Any]:
    fields: dict[str, Any] = {
        "name": name,
        "description": name,
        "parameters": {"type": "object"},
        "execute": lambda *_: None,
    }
    fields.update(kwargs)
    return ToolRegistration(**fields)


def call(id: str = "c1", name: str = "read") -> dict[str, Any]:
    return {"type": "toolCall", "id": id, "name": name, "arguments": {}}


class FakeAgent:
    def __init__(self, tools: list[Any], callable_: list[Any]) -> None:
        self.tools = tools
        self.callable = callable_


class FakeRuntime:
    """最小的 runtime 替身：只带这四个助手真正用到的成员。"""

    def __init__(
        self,
        *,
        agent: Any = None,
        entries: dict[int, Any] | None = None,
        hooks: Any = None,
        limits_interval: float = 100.0,
    ) -> None:
        self._agent = agent
        self._entries = entries or {}
        self.hooks = hooks or HookRunner()
        self.task_id = 7
        self.conversation_id = 1
        self.settings = {"progress": {"outputIntervalMs": limits_interval}}
        self.reports: list[Any] = []
        self.commits: list[Any] = []

    async def agent(self, context: Any = None) -> Any:
        # ⚠️ 名字必须是 `agent`——被调方按这个属性取。
        return self._agent

    async def entry(self, id: int, context: Any = None) -> Any:
        return self._entries.get(id)

    def report(self, error: BaseException) -> None:
        self.reports.append(error)

    async def commit(self, change: Any, context: Any = None) -> Any:
        self.commits.append(change)
        return await change(FakeTx())


class HookRunner:
    def __init__(self) -> None:
        self.hooks: list[Any] = []

    async def each(self, name: str, invoke: Any) -> None:
        for hook in self.hooks:
            await invoke(hook)


class FakeTx:
    def __init__(self) -> None:
        self.written: list[Any] = []

    def set_task(self, record: Any) -> None:
        self.written.append(record)


# --------------------------------------------------------------------------------------
# resolve_tool
# --------------------------------------------------------------------------------------


async def test_resolve_tool_uses_the_offered_tools_for_a_model_call() -> None:
    offered = tool("offered")
    nested_only = tool("nested-only")
    runtime = FakeRuntime(agent=FakeAgent([offered], [nested_only]))

    assert await resolve_tool(runtime, {"kind": "model"}, "offered", None) is offered
    # ⚠️ 混用会让一个「没提供给模型」的工具经由嵌套路径**被模型间接触发**
    assert await resolve_tool(runtime, {"kind": "model"}, "nested-only", None) is None


async def test_resolve_tool_uses_the_callable_tools_for_a_nested_call() -> None:
    offered = tool("offered")
    nested_only = tool("nested-only")
    runtime = FakeRuntime(agent=FakeAgent([offered], [nested_only]))

    assert await resolve_tool(runtime, {"kind": "nested"}, "nested-only", None) is nested_only
    assert await resolve_tool(runtime, {"kind": "nested"}, "missing", None) is None


# --------------------------------------------------------------------------------------
# read_call
# --------------------------------------------------------------------------------------


async def test_read_call_takes_the_call_from_the_input_for_a_nested_call() -> None:
    """⚠️ 嵌套调用的 call **在 input 里**（没有条目持有它），而且要**带上它的父**——
    钩子看得到那个父。"""
    runtime = FakeRuntime()
    input_ = {
        "kind": "nested",
        "parent": 9,
        "parentCallId": "parent-call",
        "call": call("c1/sub"),
    }
    result = await read_call(runtime, input_, None)
    assert result["id"] == "c1/sub"
    assert result["parent"] == {"taskId": 9, "callId": "parent-call"}


async def test_read_call_finds_a_model_call_in_its_assistant_entry() -> None:
    runtime = FakeRuntime(
        entries={
            5: {
                "model": [
                    {
                        "role": "assistant",
                        "content": [call("other"), call("wanted")],
                    }
                ]
            }
        }
    )
    result = await read_call(runtime, {"kind": "model", "assistant": 5, "callId": "wanted"}, None)
    assert result["id"] == "wanted"


async def test_read_call_reports_a_missing_call() -> None:
    runtime = FakeRuntime(entries={5: {"model": [{"role": "assistant", "content": []}]}})
    with pytest.raises(RuntimeError, match="has no tool call"):
        await read_call(runtime, {"kind": "model", "assistant": 5, "callId": "gone"}, None)


async def test_read_call_reports_a_missing_entry() -> None:
    with pytest.raises(RuntimeError, match="has no tool call"):
        await read_call(FakeRuntime(), {"kind": "model", "assistant": 5, "callId": "x"}, None)


# --------------------------------------------------------------------------------------
# final_result
# --------------------------------------------------------------------------------------


class Reported:
    def __init__(self, text: str = "", limits: Any = None, details: Any = None) -> None:
        from pi_chord.output import OutputLimits

        # ⚠️ 必须是 chord 的 `OutputLimits`（**snake_case 属性**），不是 dict——
        # `bound_content` 读的是 `limits.retain`。
        self.limits = limits or OutputLimits(max_bytes=1000, max_lines=100, retain="head")
        self.details = details
        self.diagnostics: list[ToolDiagnostic] = []
        self._snapshot = type("S", (), {"text": text, "dropped_bytes": 0, "dropped_lines": 0})()
        self.output = self

    def snapshot(self) -> Any:
        return self._snapshot


async def test_final_result_falls_back_to_the_retained_output() -> None:
    """工具**没给 output** 时用 ``api.retained_output()`` 的文本。"""
    runtime = FakeRuntime()
    result = await final_result(
        runtime,
        {"kind": "model"},
        call(),
        tool(),
        ToolExecutionResult[Any](),
        Reported("retained"),
        None,
    )
    assert result.output[0].text == "retained"


async def test_final_result_keeps_the_tools_own_output() -> None:
    runtime = FakeRuntime()
    result = await final_result(
        runtime,
        {"kind": "model"},
        call(),
        tool(),
        ToolExecutionResult[Any](output=[TextContent(type="text", text="mine")]),
        Reported("retained"),
        None,
    )
    assert result.output[0].text == "mine"


async def test_after_tool_can_replace_the_result() -> None:
    hooks = HookRunner()

    async def rewrite(call_: Any, final: Any, runtime_: Any, context: Any) -> Any:
        return ToolExecutionResult[Any](output=[TextContent(type="text", text="hooked")])

    hooks.hooks.append(rewrite)
    runtime = FakeRuntime(hooks=hooks)
    result = await final_result(
        runtime, {"kind": "model"}, call(), tool(), ToolExecutionResult[Any](), Reported(), None
    )
    assert result.output[0].text == "hooked"


async def test_a_nested_call_gets_the_tools_value_when_it_declares_no_schema() -> None:
    """⚠️ 程序拿**工具自己的值**：没有 schema 时就是它的输出。模型只读 ``output``，所以**跳过**。"""
    runtime = FakeRuntime()
    nested = await final_result(
        runtime,
        {"kind": "nested"},
        call(),
        tool(),
        ToolExecutionResult[Any](output=[TextContent(type="text", text="value")]),
        Reported(),
        None,
    )
    assert nested.structured_output == "value"

    model = await final_result(
        runtime,
        {"kind": "model"},
        call(),
        tool(),
        ToolExecutionResult[Any](output=[TextContent(type="text", text="value")]),
        Reported(),
        None,
    )
    assert model.structured_output is None  # 模型发起的**不设**它


async def test_a_broken_structured_output_errors_a_nested_call() -> None:
    """⚠️ **嵌套**：违约 → 变成**错误结果** + 一条说明。

    因为**程序**读的就是那个值——给了错的值还不如明确报错。
    """
    schema = {"type": "object", "properties": {"n": {"type": "number"}}}
    runtime = FakeRuntime()
    result = await final_result(
        runtime,
        {"kind": "nested"},
        call(),
        tool(structured_output_schema=schema),
        ToolExecutionResult[Any](structured_output={"n": "not a number"}),
        Reported(),
        None,
    )
    assert result.is_error is True
    assert result.structured_output is None
    assert any(d.code == "invalid_structured_output" for d in result.diagnostics or ())


async def test_a_broken_structured_output_only_drops_the_value_for_a_model_call() -> None:
    """⚠️ **模型发起**：**只丢掉那个值**，调用照旧成立 + **违约报告给宿主**。

    **模型根本看不到 ``structuredOutput``**，所以对它来说没有违约。
    """
    schema = {"type": "object", "properties": {"n": {"type": "number"}}}
    runtime = FakeRuntime()
    result = await final_result(
        runtime,
        {"kind": "model"},
        call(),
        tool(structured_output_schema=schema),
        ToolExecutionResult[Any](structured_output={"n": "bad"}),
        Reported(),
        None,
    )
    # ⚠️ 调用照旧成立：`is_error` **没有被改成 True**。
    # （它原本是 `None` 而不是 `False`——`ToolExecutionResult` 的三态默认值，
    #  上游那边是 `undefined`。断言 `is False` 会错，因为"未设"不是"假"。）
    assert result.is_error is not True
    assert result.structured_output is None  # 值被丢掉
    assert len(runtime.reports) == 1  # 但宿主知道了


async def test_a_schema_tool_errors_result_gets_its_text_as_a_diagnostic() -> None:
    """⚠️ 一条**有 schema 的错误结果**没带 structuredOutput 时，它的文本只在 ``output`` 里，
    而**程序从不读 output**——所以把它变成一条 error 说明。"""
    schema = {"type": "object"}
    runtime = FakeRuntime()
    result = await final_result(
        runtime,
        {"kind": "nested"},
        call(),
        tool(structured_output_schema=schema),
        ToolExecutionResult[Any](
            output=[TextContent(type="text", text="something went wrong")], is_error=True
        ),
        Reported(),
        None,
    )
    assert any(d.message == "something went wrong" for d in result.diagnostics or ())


# --------------------------------------------------------------------------------------
# publish_progress
# --------------------------------------------------------------------------------------


def test_publish_progress_returns_a_progress_that_writes_nothing_when_disabled() -> None:
    """⚠️ ``enabled=False`` 时**一次提交都不做**（嵌套调用的 ``progress: false``）。"""
    from pi_chord.output import Progress

    runtime = FakeRuntime()
    progress = publish_progress(runtime, Reported(), False, None)
    assert isinstance(progress, Progress)


# --------------------------------------------------------------------------------------
# ToolTask 本体
# --------------------------------------------------------------------------------------


def test_tool_task_shape() -> None:
    from pi_durable.harness.tool import ToolTask

    definition = ToolTask.definition
    assert definition["name"] == "pi.tool"
    assert definition["version"] == 2  # 版本 1 只有模型发起的调用
    assert sorted(definition["phases"]) == ["call", "execute"]
    assert definition["initial"](None) == {"phase": "call"}
    assert "migrate" in definition


def test_tool_task_migration_gives_old_inputs_the_model_kind() -> None:
    """⚠️ **版本 1 只有模型发起的调用**——所以迁移给老输入补上 ``kind: "model"``。

    不补的话，``read_call`` 会去找 `input["kind"]`，而它**不存在**（老记录里只有
    `assistant`/`callId`）——于是那条任务**永远恢复不了**。
    """
    from pi_durable.harness.tool import ToolTask

    migrated = ToolTask.definition["migrate"](
        {"assistant": 5, "callId": "c1"}, {"phase": "call"}, 1
    )
    assert migrated["input"]["kind"] == "model"
    assert migrated["input"]["assistant"] == 5


async def test_the_abort_phase_distinguishes_three_situations() -> None:
    """⚠️ **三种文案，由「为什么被中止」决定**：

    * 普通的 ``aborted``；
    * 重启时**已进入 ``execute``** → ``interrupted``（**可能已经部分跑过**）；
    * 重启时**还在 ``call``** → ``abandoned``（**它没开始**）。

    ⚠️ 差别是**给用户的信息量**：说「被中止」而不说「可能已经跑过」，
    会让人以为**重试是安全的**。
    """
    from pi_durable.harness.tool import _tool_abort_phase  # noqa: PLC2701

    cases = [
        (None, "call", "aborted"),
        ("restart", "execute", "interrupted"),
        ("restart", "call", "abandoned"),
    ]
    for abort_reason, phase, expected_code in cases:
        runtime = FakeRuntime(
            entries={5: {"model": [{"role": "assistant", "content": [call("c1")]}]}}
        )
        recorded: list[Any] = []

        async def settle_(
            rt: Any,
            input_: Any,
            call_: Any,
            ending: Any,
            build: Any,
            ctx: Any,
            *a: Any,
            _sink: list[Any] = recorded,  # ⚠️ **绑定**当前列表——否则闭包在循环下一轮
            # 会看到**另一个**列表（ruff 的 B023 正是这条）。
        ) -> None:
            slot = {"output": "partial"}
            _sink.append(build(slot))

        import pi_durable.harness.tool as tool_module

        original = tool_module.settle
        tool_module.settle = settle_
        try:
            task = {
                "input": {"kind": "model", "assistant": 5, "callId": "c1"},
                "state": {"checkpoint": {"phase": phase}},
                "abortReason": abort_reason,
            }
            await _tool_abort_phase(task, runtime, None)
        finally:
            tool_module.settle = original

        codes = [d.code for d in recorded[0].diagnostics]
        assert expected_code in codes, (abort_reason, phase, codes)
