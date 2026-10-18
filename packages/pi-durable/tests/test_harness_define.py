"""``pi_durable.harness``（批次 D-1）的单元测试。

上游：``durable/src/harness/define.ts:6-17`` 与 ``harness/types.ts`` 的窄提取。

两组重点：

1. **符号覆盖**：把 ``PORTING.md`` 里批次 C₂ 需要的符号逐个 import 一遍——
   本段的验收基准就是「能满足 C₂ 的符号需求」，所以把它写成可执行的断言。
2. **形状可用**：按 ``tools/write.ts`` 与 ``tool-search.ts`` 的真实用法各写一个
   最小工具，用一个假 ``ToolExecutionApi`` 跑通，证明 C₂ 拿这个接口是能落地的。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import FrozenInstanceError, dataclass
from typing import Any

import pytest

from pi_ai import ImageContent, TextContent, Usage
from pi_chord.context import BACKGROUND_CONTEXT, Context
from pi_durable.env import ShellOutputSkip, ShellOutputWindow
from pi_durable.harness import (
    Agent,
    Extension,
    ModelRef,
    RetainedOutput,
    ToolControl,
    ToolDiagnostic,
    ToolExecutionApi,
    ToolExecutionResult,
    ToolOutputLimits,
    ToolRegistration,
    define_extension,
    define_tool,
)

# --------------------------------------------------------------------------------------
# C₂ 符号覆盖（验收基准的可执行版本）
# --------------------------------------------------------------------------------------


def test_c2_required_symbols_exist() -> None:
    """批次 C₂ 的 9 个文件需要的 harness 符号，逐个断言存在。

    对照表在 ``PORTING.md``〈批次 C₂ 的符号需求〉一节。
    """
    import pi_durable.harness as harness

    for name in (
        "define_tool",
        "define_extension",
        "ToolRegistration",
        "ToolExecutionApi",
        "ToolExecutionResult",
        "ToolDiagnostic",
    ):
        assert hasattr(harness, name), f"C₂ 需要 {name}"

    assert callable(harness.define_tool)
    assert callable(harness.define_extension)


def test_tool_execution_api_has_exactly_the_seven_members_c2_uses() -> None:
    """C₂ 实测用到 7 个成员（12 处、无解构）。

    这里把**上游有、本端省略**的成员也列出来，防止有人误以为已经做完。
    """
    present = {
        "env",
        "models",
        "output_window",
        "output",
        "retained_output",
        "diagnostic",
        "agent",
    }
    protocol_members = {name for name in vars(ToolExecutionApi) if not name.startswith("_")}
    assert protocol_members == present

    # 上游 harness/types.ts:205-292 有、本端**故意没有**的成员：
    omitted = {
        "taskId",
        "conversationId",
        "callId",
        "registry",
        "details",
        "commit",
        "memo",
        "createTask",
        "getTask",
        "waitForTask",
        "conversation",
        "executeTool",
    }
    for name in omitted:
        assert name not in protocol_members


# --------------------------------------------------------------------------------------
# 假 api：按 7 个成员实现，供下面的形状测试使用
# --------------------------------------------------------------------------------------


@dataclass
class FakeApi:
    env: Any = None
    models: Any = None
    output_window: ShellOutputWindow | None = None
    agent_value: Agent[Any] | None = None

    def __post_init__(self) -> None:
        self.chunks: list[str] = []
        self.diagnostics: list[ToolDiagnostic] = []

    def output(self, chunk: str | bytes, skipped: ShellOutputSkip | None = None) -> None:
        del skipped
        self.chunks.append(chunk if isinstance(chunk, str) else chunk.decode())

    def retained_output(self) -> RetainedOutput:
        return RetainedOutput(text="".join(self.chunks), truncated=False)

    def diagnostic(self, diagnostic: ToolDiagnostic) -> None:
        self.diagnostics.append(diagnostic)

    async def agent(self, context: Context) -> Agent[Any]:
        del context
        assert self.agent_value is not None
        return self.agent_value


# --------------------------------------------------------------------------------------
# define_tool（define.ts:13-17）
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteInput:
    path: str
    content: str


WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
    "required": ["path", "content"],
}


async def _write_execute(
    args: WriteInput,
    api: ToolExecutionApi,
    context: Context,
) -> ToolExecutionResult[Any]:
    del api, context
    return ToolExecutionResult(output=[TextContent(text=f"Successfully wrote to {args.path}")])


def test_define_tool_fills_every_field() -> None:
    tool = define_tool(
        name="write",
        description="Write content to a file.",
        parameters=WRITE_SCHEMA,
        execute=_write_execute,
        replay="safe",
        structured_output_schema={"type": "object"},
        callers=["model"],
        deferred=True,
        output_limits=ToolOutputLimits(retain="tail"),
    )

    assert isinstance(tool, ToolRegistration)
    assert tool.name == "write"
    assert tool.description == "Write content to a file."
    assert tool.parameters == WRITE_SCHEMA
    assert tool.execute is _write_execute
    assert tool.replay == "safe"
    assert tool.structured_output_schema == {"type": "object"}
    assert tool.callers == ["model"]
    assert tool.deferred is True
    assert tool.output_limits == ToolOutputLimits(retain="tail")


def test_define_tool_defaults_are_none() -> None:
    tool = define_tool(name="t", description="d", parameters={}, execute=_write_execute)

    assert tool.replay is None
    assert tool.execution_mode is None
    assert tool.structured_output_schema is None
    assert tool.callers is None
    assert tool.deferred is None
    assert tool.output_limits is None


def test_tool_registration_is_frozen() -> None:
    tool = define_tool(name="t", description="d", parameters={}, execute=_write_execute)

    with pytest.raises(FrozenInstanceError):
        tool.name = "other"  # type: ignore[misc]


async def test_execute_is_awaitable_and_keeps_args_type() -> None:
    tool = define_tool(
        name="write", description="d", parameters=WRITE_SCHEMA, execute=_write_execute
    )

    result = await tool.execute(
        WriteInput(path="a.txt", content="hi"), FakeApi(), BACKGROUND_CONTEXT
    )

    assert result.output is not None
    text = result.output[0]
    assert isinstance(text, TextContent)
    assert text.text == "Successfully wrote to a.txt"


def test_tool_output_limits_defaults() -> None:
    limits = ToolOutputLimits()
    assert limits.max_bytes is None
    assert limits.max_lines is None
    assert limits.retain is None


def test_tool_execution_result_defaults() -> None:
    result: ToolExecutionResult[Any] = ToolExecutionResult()
    assert result.output is None
    assert result.structured_output is None
    assert result.is_error is None
    assert result.details is None
    assert result.diagnostics is None
    assert result.usage is None
    assert result.control is None


def test_tool_execution_result_carries_pi_ai_usage_and_image() -> None:
    """``usage`` 与 ``output`` 的元素就是 pi-ai 的 ``Usage`` / ``ImageContent``。"""
    result = ToolExecutionResult[Any](
        output=[ImageContent(data="AAAA", mimeType="image/png")],
        usage=Usage(input=10, output=5),
        is_error=False,
    )

    assert result.usage is not None and result.usage.input == 10
    assert result.output is not None and isinstance(result.output[0], ImageContent)


# --------------------------------------------------------------------------------------
# define_extension（define.ts:6-10）
# --------------------------------------------------------------------------------------


def test_define_extension_wraps_tools() -> None:
    tool = define_tool(name="read", description="d", parameters={}, execute=_write_execute)

    extension = define_extension(name="coding-tools", tools=[tool])

    assert isinstance(extension, Extension)
    assert extension.name == "coding-tools"
    assert list(extension.tools) == [tool]


def test_define_extension_defaults_to_no_tools() -> None:
    assert define_extension(name="empty").tools == ()


def test_extension_is_frozen() -> None:
    extension = define_extension(name="x")
    with pytest.raises(FrozenInstanceError):
        extension.name = "y"  # type: ignore[misc]


def test_coding_tools_shape_matches_tools_index() -> None:
    """``tools/index.ts:31-36`` 的形状：四个工具打包成一个 ``coding-tools`` 扩展。"""
    tools = [
        define_tool(name=name, description=f"{name} desc", parameters={}, execute=_write_execute)
        for name in ("read", "write", "edit", "bash")
    ]

    extension = define_extension(name="coding-tools", tools=tools)

    assert [t.name for t in extension.tools] == ["read", "write", "edit", "bash"]


# --------------------------------------------------------------------------------------
# 形状：按 tool-search.ts 的真实用法走一遍
# --------------------------------------------------------------------------------------


async def test_tool_search_shape_reads_agent_deferred_and_returns_control() -> None:
    """``tool-search.ts:63-84`` 的形状：读 ``api.agent().deferred``，返回 ``control.addTools``。"""

    async def _search_execute(
        args: dict[str, Any],
        api: ToolExecutionApi,
        context: Context,
    ) -> ToolExecutionResult[Any]:
        agent = await api.agent(context)
        loaded = [t.name for t in agent.deferred][: int(args.get("limit") or 8)]
        text = "No matching tools found." if not loaded else f"Loaded {len(loaded)} tool(s)."
        return ToolExecutionResult(
            output=[TextContent(text=text)],
            details={"loaded": loaded},
            control=None if not loaded else ToolControl(add_tools=loaded),
        )

    deferred = [
        define_tool(
            name=name, description="d", parameters={}, execute=_write_execute, deferred=True
        )
        for name in ("mcp_alpha", "mcp_beta")
    ]
    api = FakeApi(agent_value=Agent(deferred=deferred))
    tool = define_tool(
        name="tool_search",
        description="Search deferred tools.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        execute=_search_execute,
        callers=["model"],
        replay="safe",
    )

    result = await tool.execute({"query": "alpha"}, api, BACKGROUND_CONTEXT)

    assert result.control is not None
    assert list(result.control.add_tools or []) == ["mcp_alpha", "mcp_beta"]
    assert result.details == {"loaded": ["mcp_alpha", "mcp_beta"]}


async def test_bash_shape_streams_output_and_records_diagnostic() -> None:
    """``bash.ts:104-117`` 的形状：``api.output(text, info.skipped)`` + ``api.diagnostic(...)``
    + ``api.output_window`` 透传给 ``ShellExecOptions.window``。"""

    window = ShellOutputWindow(
        max_bytes=1024, max_lines=10, min_interval_ms=100, bytes_per_second=1024
    )

    async def _bash_execute(
        args: dict[str, Any],
        api: ToolExecutionApi,
        context: Context,
    ) -> ToolExecutionResult[Any]:
        del args, context
        api.output("hello\n")
        api.output("world\n")
        api.diagnostic(
            ToolDiagnostic(severity="info", code="full_output", message="Full output: /tmp/x")
        )
        retained = api.retained_output()
        return ToolExecutionResult(
            output=[TextContent(text=retained.text)],
            structured_output={"output": retained.text, "truncated": retained.truncated},
        )

    api = FakeApi(output_window=window)
    tool = define_tool(
        name="bash",
        description="Run a command.",
        parameters={"type": "object"},
        execute=_bash_execute,
        output_limits=ToolOutputLimits(retain="tail"),
    )

    result = await tool.execute({}, api, BACKGROUND_CONTEXT)

    assert isinstance(api, FakeApi)
    assert api.chunks == ["hello\n", "world\n"]
    assert api.output_window is window
    assert [d.code for d in api.diagnostics] == ["full_output"]
    assert result.structured_output == {"output": "hello\nworld\n", "truncated": False}


async def test_read_shape_resolves_model_ref() -> None:
    """``read.ts:201-202`` 的形状：``(await api.agent(context)).model`` → ``api.models.get_model(...)``。"""
    seen: list[tuple[str, str]] = []

    class FakeModels:
        def get_model(self, provider: str, model_id: str) -> Any | None:
            seen.append((provider, model_id))
            return object()

    async def _read_execute(
        args: dict[str, Any],
        api: ToolExecutionApi,
        context: Context,
    ) -> ToolExecutionResult[Any]:
        del args
        ref = (await api.agent(context)).model
        api.models.get_model(ref.provider, ref.model_id) if ref is not None else None
        return ToolExecutionResult(output=[TextContent(text="ok")])

    api = FakeApi(
        agent_value=Agent(model=ModelRef(provider="anthropic", model_id="haiku")),
        models=FakeModels(),
    )
    tool = define_tool(name="read", description="d", parameters={}, execute=_read_execute)

    await tool.execute({}, api, BACKGROUND_CONTEXT)

    assert seen == [("anthropic", "haiku")]


def test_require_env_shape() -> None:
    """``tools/env.ts:5-8`` 的形状：``api.env`` 为 ``None`` 时抛出可读错误。"""

    def require_env(api: ToolExecutionApi) -> Any:
        if api.env is None:
            raise RuntimeError("No execution environment is configured")
        return api.env

    with pytest.raises(RuntimeError, match="No execution environment"):
        require_env(FakeApi(env=None))

    sentinel = object()
    assert require_env(FakeApi(env=sentinel)) is sentinel


def test_tool_registration_is_generic_in_args_type() -> None:
    """``ToolRegistration`` 是 ``Generic[TArgs]``，标注能通过（可读性断言）。"""
    tool: ToolRegistration[WriteInput] = define_tool(
        name="write", description="d", parameters=WRITE_SCHEMA, execute=_write_execute
    )

    assert tool.name == "write"


def test_extension_preserves_sequence_order() -> None:
    names: Sequence[str] = ["a", "b", "c"]
    tools = [
        define_tool(name=n, description="d", parameters={}, execute=_write_execute) for n in names
    ]
    extension = define_extension(name="e", tools=tools)
    assert [t.name for t in extension.tools] == list(names)
