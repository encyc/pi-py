"""``read`` / ``bash`` / ``tool_search`` 工具的测试。

``read`` 的用例对照上游 ``durable/test/tools.test.ts`` 的 ``describe("read")``（202-318 行）
与 ``describe("bash")``。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pi_ai import ImageContent, TextContent
from pi_chord.context import BACKGROUND_CONTEXT
from pi_durable.env import ShellOutputWindow, get_or_throw
from pi_durable.env.node import NodeExecutionEnv, NodeExecutionEnvOptions
from pi_durable.harness import Agent, ModelRef, ToolDiagnostic
from pi_durable.tool_search import create_tool_search_tool
from pi_durable.tool_search.tool_search import ToolSearchToolInput
from pi_durable.tools.bash import BashToolInput, create_bash_tool, create_powershell_tool
from pi_durable.tools.image_processor import DEFAULT_IMAGE_LIMITS
from pi_durable.tools.read import ReadToolInput, create_read_tool

from .test_tools_image import make_png
from .tool_support import FakeToolApi


@pytest.fixture
def env(tmp_path: Path) -> Iterator[NodeExecutionEnv]:
    yield NodeExecutionEnv(NodeExecutionEnvOptions(cwd=str(tmp_path)))


def text_output(result: object) -> str:
    output = getattr(result, "output", None) or []
    return "".join(block.text for block in output if isinstance(block, TextContent))


def diagnostic_codes(result: object) -> list[str | None]:
    return [d.code for d in (getattr(result, "diagnostics", None) or [])]


async def read(env: NodeExecutionEnv, **kwargs: object):
    tool = create_read_tool()
    return await tool.execute(ReadToolInput(**kwargs), FakeToolApi(env=env), BACKGROUND_CONTEXT)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# read（tools.test.ts:202-318）
# --------------------------------------------------------------------------------------


async def test_read_whole_small_file(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("test.txt", "a\nb\nc\n", BACKGROUND_CONTEXT))

    result = await read(env, path="test.txt")

    assert text_output(result) == "a\nb\nc\n"
    assert result.is_error is None


async def test_read_offset_and_limit(env: NodeExecutionEnv) -> None:
    """对照 ``tools.test.ts:216``：从第 41 行起读 20 行。"""
    content = "".join(f"line {i}\n" for i in range(1, 101))
    get_or_throw(await env.write_file("test.txt", content, BACKGROUND_CONTEXT))

    result = await read(env, path="test.txt", offset=41, limit=20)

    shown = text_output(result).split("\n")
    assert shown[0] == "line 41"
    assert shown[19] == "line 60"
    # 还有后续时给一条 info 诊断，告诉模型用哪个 offset 继续。
    assert "truncated" not in diagnostic_codes(result)
    assert any("more lines in file" in d.message for d in result.diagnostics or [])


async def test_read_truncates_large_file_and_says_so(env: NodeExecutionEnv) -> None:
    content = "".join(f"line {i}\n" for i in range(1, 4001))
    get_or_throw(await env.write_file("large.txt", content, BACKGROUND_CONTEXT))

    result = await read(env, path="large.txt")

    assert "truncated" in diagnostic_codes(result)
    assert len(text_output(result).split("\n")) <= 2001
    assert any("Use offset=" in d.message for d in result.diagnostics or [])


async def test_read_exact_fit_is_not_truncated(env: NodeExecutionEnv) -> None:
    """对照 ``tools.test.ts:255``：正好 2000 行不截断。"""
    content = "".join(f"line {i}\n" for i in range(1, 2001))
    get_or_throw(await env.write_file("exact.txt", content, BACKGROUND_CONTEXT))

    result = await read(env, path="exact.txt")

    assert "truncated" not in diagnostic_codes(result)


async def test_read_offset_beyond_end_is_rejected(env: NodeExecutionEnv) -> None:
    """对照 ``tools.test.ts:282``。"""
    get_or_throw(await env.write_file("short.txt", "a\nb\n", BACKGROUND_CONTEXT))

    with pytest.raises(RuntimeError, match="beyond end of file"):
        await read(env, path="short.txt", offset=100)


async def test_read_missing_file_raises(env: NodeExecutionEnv) -> None:
    with pytest.raises(Exception, match="No such file"):
        await read(env, path="nope.txt")


async def test_read_without_env_is_rejected() -> None:
    tool = create_read_tool()
    with pytest.raises(RuntimeError, match="No execution environment"):
        await tool.execute(ReadToolInput(path="x"), FakeToolApi(env=None), BACKGROUND_CONTEXT)


async def test_read_png_returns_image_block(env: NodeExecutionEnv) -> None:
    """PNG 走图像分支，返回一个 ``ImageContent``（``read.ts:138-191``）。

    这条同时回归 ``_ReaderSource.size``：PNG 的 APNG 块遍历要用它，缺了会直接崩。
    """
    png = make_png(4, 4)
    (Path(env.cwd) / "small.png").write_bytes(png)

    result = await read(env, path="small.png")

    assert result.output is not None
    assert isinstance(result.output[0], ImageContent)
    assert result.output[0].mime_type == "image/png"
    assert "image" in diagnostic_codes(result)


async def test_read_no_newline_at_eof(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("n.txt", "no trailing newline", BACKGROUND_CONTEXT))

    result = await read(env, path="n.txt")

    assert text_output(result) == "no trailing newline"


async def test_read_empty_file(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("empty.txt", "", BACKGROUND_CONTEXT))

    result = await read(env, path="empty.txt")

    assert text_output(result) == ""
    assert result.output == []


async def test_read_schema_shape() -> None:
    parameters = create_read_tool().parameters
    assert sorted(parameters["properties"]) == ["limit", "offset", "path"]
    assert parameters["required"] == ["path"]


# --------------------------------------------------------------------------------------
# bash
# --------------------------------------------------------------------------------------


async def test_bash_runs_and_returns_structured_output(env: NodeExecutionEnv) -> None:
    tool = create_bash_tool()
    result = await tool.execute(
        BashToolInput(command="echo hello"), FakeToolApi(env=env), BACKGROUND_CONTEXT
    )

    assert result.is_error is None
    assert result.structured_output["exitCode"] == 0
    assert "hello" in result.structured_output["output"]


async def test_bash_nonzero_exit_is_an_error_result_with_diagnostic(env: NodeExecutionEnv) -> None:
    """1.2.0 起 bash 不再抛异常，而是返回带 ``exit_code`` 诊断的错误结果（``CHANGELOG.md:24``）。"""
    tool = create_bash_tool()
    result = await tool.execute(
        BashToolInput(command="exit 3"), FakeToolApi(env=env), BACKGROUND_CONTEXT
    )

    assert result.is_error is True
    assert result.structured_output["exitCode"] == 3
    codes = [d.code for d in result.diagnostics or []]
    assert "exit_code" in codes


async def test_bash_streams_output_through_api_output(env: NodeExecutionEnv) -> None:
    api = FakeToolApi(env=env, retained_limit=10_000)
    tool = create_bash_tool()

    await tool.execute(BashToolInput(command="echo streamed"), api, BACKGROUND_CONTEXT)

    assert any("streamed" in chunk for chunk in api.chunks)
    assert "streamed" in api.retained_output().text


async def test_bash_passes_output_window_to_the_environment(env: NodeExecutionEnv) -> None:
    window = ShellOutputWindow(
        max_bytes=4096, max_lines=100, min_interval_ms=0, bytes_per_second=1_000_000
    )
    api = FakeToolApi(env=env, output_window=window)

    await create_bash_tool().execute(BashToolInput(command="echo x"), api, BACKGROUND_CONTEXT)

    assert api.output_window is window


# 参数名不叫 timeout：ruff ASYNC109 会把 async 函数的 timeout 参数当成 asyncio 超时。
@pytest.mark.parametrize("seconds", [0, -1, float("inf")])
async def test_bash_rejects_bad_timeout(env: NodeExecutionEnv, seconds: float) -> None:
    with pytest.raises(RuntimeError, match="Invalid timeout"):
        await create_bash_tool().execute(
            BashToolInput(command="echo x", timeout=seconds),
            FakeToolApi(env=env),
            BACKGROUND_CONTEXT,
        )


async def test_bash_rejects_too_large_timeout(env: NodeExecutionEnv) -> None:
    with pytest.raises(RuntimeError, match="Invalid timeout: maximum is"):
        await create_bash_tool().execute(
            BashToolInput(command="echo x", timeout=10**9), FakeToolApi(env=env), BACKGROUND_CONTEXT
        )


async def test_bash_command_prefix_is_prepended(env: NodeExecutionEnv) -> None:
    from pi_durable.tools.bash import BashToolOptions

    tool = create_bash_tool(BashToolOptions(command_prefix="export FOO=bar"))
    result = await tool.execute(
        BashToolInput(command="echo $FOO"), FakeToolApi(env=env), BACKGROUND_CONTEXT
    )

    assert "bar" in result.structured_output["output"]


async def test_bash_prepare_can_rewrite_the_command(env: NodeExecutionEnv) -> None:
    from pi_durable.tools.bash import BashExecution, BashToolOptions

    def prepare(execution: BashExecution, _api: object, _context: object) -> None:
        execution.command = "echo rewritten"

    tool = create_bash_tool(BashToolOptions(prepare=prepare))
    result = await tool.execute(
        BashToolInput(command="echo original"), FakeToolApi(env=env), BACKGROUND_CONTEXT
    )

    assert "rewritten" in result.structured_output["output"]


async def test_bash_without_env_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="No execution environment"):
        await create_bash_tool().execute(
            BashToolInput(command="echo x"), FakeToolApi(env=None), BACKGROUND_CONTEXT
        )


def test_bash_and_powershell_declare_tail_retention() -> None:
    assert create_bash_tool().output_limits is not None
    assert create_bash_tool().output_limits.retain == "tail"  # type: ignore[union-attr]
    assert create_powershell_tool().output_limits is not None
    assert create_powershell_tool().name == "powershell"


# --------------------------------------------------------------------------------------
# tool_search（tool-search.test.ts）
# --------------------------------------------------------------------------------------


def _deferred_tools():
    from pi_durable.harness import define_tool

    async def noop(*_args: object) -> object:
        raise AssertionError("not executed")

    return [
        define_tool(
            name="mcp_github_create_issue",
            description="Create a GitHub issue.",
            parameters={"type": "object"},
            execute=noop,
            deferred=True,
        ),
        define_tool(
            name="mcp_slack_post",
            description="Post a message to Slack.",
            parameters={"type": "object"},
            execute=noop,
            deferred=True,
        ),
    ]


async def test_tool_search_loads_matches_via_control() -> None:
    api = FakeToolApi(agent_value=Agent(deferred=_deferred_tools()))
    tool = create_tool_search_tool()

    result = await tool.execute(ToolSearchToolInput(query="github issue"), api, BACKGROUND_CONTEXT)

    assert result.control is not None
    assert list(result.control.add_tools or []) == ["mcp_github_create_issue"]
    assert "Loaded 1 tool." in text_output(result)


async def test_tool_search_reports_no_match() -> None:
    api = FakeToolApi(agent_value=Agent(deferred=_deferred_tools()))

    result = await create_tool_search_tool().execute(
        ToolSearchToolInput(query="kubernetes"), api, BACKGROUND_CONTEXT
    )

    assert result.control is None
    assert text_output(result) == "No matching tools found."


async def test_tool_search_honours_limit() -> None:
    api = FakeToolApi(agent_value=Agent(deferred=_deferred_tools()))

    result = await create_tool_search_tool().execute(
        ToolSearchToolInput(query="mcp", limit=1), api, BACKGROUND_CONTEXT
    )

    assert len(result.control.add_tools) == 1  # type: ignore[union-attr]


@pytest.mark.parametrize("query", ["", "   "])
async def test_tool_search_rejects_empty_query(query: str) -> None:
    api = FakeToolApi(agent_value=Agent(deferred=_deferred_tools()))

    with pytest.raises(RuntimeError, match="query must not be empty"):
        await create_tool_search_tool().execute(
            ToolSearchToolInput(query=query), api, BACKGROUND_CONTEXT
        )


@pytest.mark.parametrize("limit", [0, -1])
async def test_tool_search_rejects_bad_limit(limit: int) -> None:
    api = FakeToolApi(agent_value=Agent(deferred=_deferred_tools()))

    with pytest.raises(RuntimeError, match="limit must be a positive integer"):
        await create_tool_search_tool().execute(
            ToolSearchToolInput(query="mcp", limit=limit), api, BACKGROUND_CONTEXT
        )


def test_tool_search_is_model_only_and_replay_safe() -> None:
    tool = create_tool_search_tool()
    assert tool.callers == ["model"]
    assert tool.replay == "safe"
    assert tool.name == "tool_search"


def test_tool_search_details_carry_loaded_names() -> None:
    from pi_durable.tool_search import ToolSearchToolDetails

    assert ToolSearchToolDetails(loaded=["a"]).loaded == ["a"]


# --------------------------------------------------------------------------------------
# 编码入口
# --------------------------------------------------------------------------------------


def test_coding_tools_extension_has_the_four_tools() -> None:
    """``tools/index.ts:31-36``。"""
    from pi_durable.tools import CodingTools, create_coding_tools

    assert CodingTools.name == "coding-tools"
    assert [t.name for t in CodingTools.tools] == ["read", "write", "edit", "bash"]
    assert [t.name for t in create_coding_tools().tools] == ["read", "write", "edit", "bash"]


def test_image_limits_default_is_shared() -> None:
    assert DEFAULT_IMAGE_LIMITS.max_width == 2000


def test_read_with_image_processor_advertises_bmp(tmp_path: Path) -> None:
    from pi_durable.tools.read import ReadToolOptions

    class _Noop:
        async def prepare(self, data: bytes, mime_type: str, limits: object):
            return None

    with_images = create_read_tool(ReadToolOptions(images=_Noop()))
    assert "bmp" in with_images.description
    assert "bmp" not in create_read_tool().description


def test_model_ref_and_diagnostic_are_used() -> None:
    """占位：确认本文件引用的 Agent/ModelRef/ToolDiagnostic 都在窄接口里。"""
    assert Agent(model=ModelRef(provider="p", model_id="m")).model is not None
    assert ToolDiagnostic(severity="info", message="m").code is None
