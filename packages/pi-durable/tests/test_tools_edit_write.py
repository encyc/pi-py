"""``write`` / ``edit`` 工具的测试。

用例逐条对照上游 ``durable/test/tools.test.ts`` 的 ``describe("write")``（320-349 行）与
``describe("edit")``（350-560+ 行）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pi_ai import TextContent
from pi_chord.context import BACKGROUND_CONTEXT
from pi_durable.env import get_or_throw
from pi_durable.env.node import NodeExecutionEnv, NodeExecutionEnvOptions
from pi_durable.tools.edit import EditToolDetails, create_edit_tool, prepare_edit_arguments
from pi_durable.tools.write import create_write_tool

from .tool_support import FakeToolApi


@pytest.fixture
def env(tmp_path: Path) -> Iterator[NodeExecutionEnv]:
    yield NodeExecutionEnv(NodeExecutionEnvOptions(cwd=str(tmp_path)))


def text_output(result: object) -> str:
    output = getattr(result, "output", None) or []
    return "".join(block.text for block in output if isinstance(block, TextContent))


async def run(tool: object, args: object, env: NodeExecutionEnv | None):
    api = FakeToolApi(env=env)
    execute = tool.execute
    return await execute(args, api, BACKGROUND_CONTEXT)


# --------------------------------------------------------------------------------------
# write（tools.test.ts:320-349）
# --------------------------------------------------------------------------------------


async def test_write_creates_parent_directories(env: NodeExecutionEnv) -> None:
    # 上游 `write.ts:20` 的 description 承诺自动建父目录。
    result = await run(create_write_tool(), _write("nested/dir/file.txt", "hello"), env)

    assert text_output(result).startswith("Successfully wrote to nested/dir/file.txt")
    assert (
        get_or_throw(await env.read_text_file("nested/dir/file.txt", BACKGROUND_CONTEXT)) == "hello"
    )


async def test_write_overwrites(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("f.txt", "old", BACKGROUND_CONTEXT))

    await run(create_write_tool(), _write("f.txt", "new"), env)

    assert get_or_throw(await env.read_text_file("f.txt", BACKGROUND_CONTEXT)) == "new"


async def test_write_without_env_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="No execution environment"):
        await run(create_write_tool(), _write("x.txt", "hi"), None)


async def test_write_schema_matches_typebox_shape() -> None:
    parameters = create_write_tool().parameters

    assert parameters["type"] == "object"
    assert sorted(parameters["properties"]) == ["content", "path"]
    assert parameters["required"] == ["path", "content"]
    # 不出现 pydantic 自动加的 title
    assert "title" not in parameters
    assert "title" not in parameters["properties"]["path"]


def _write(path: str, content: str) -> object:
    from pi_durable.tools.write import WriteToolInput

    return WriteToolInput(path=path, content=content)


# --------------------------------------------------------------------------------------
# edit（tools.test.ts:350-410）
# --------------------------------------------------------------------------------------


def _edit(path: str, edits: list[tuple[str, str]]) -> object:
    from pi_durable.tools.edit import EditToolInput, ReplaceEditInput

    return EditToolInput(
        path=path, edits=[ReplaceEditInput(oldText=o, newText=n) for o, n in edits]
    )


async def test_edit_applies_disjoint_edits_and_returns_both_diff_formats(
    env: NodeExecutionEnv,
) -> None:
    """对照 ``tools.test.ts:351-371``。"""
    original = "alpha\nbeta\ngamma\ndelta\n"
    get_or_throw(await env.write_file("edit.txt", original, BACKGROUND_CONTEXT))

    result = await run(
        create_edit_tool(),
        _edit("edit.txt", [("alpha\n", "ALPHA\n"), ("gamma\n", "GAMMA\n")]),
        env,
    )

    assert text_output(result) == "Successfully replaced 2 block(s) in edit.txt."
    details = result.details
    assert isinstance(details, EditToolDetails)
    assert "ALPHA" in details.diff
    assert "GAMMA" in details.diff
    assert get_or_throw(await env.read_text_file("edit.txt", BACKGROUND_CONTEXT)) == (
        "ALPHA\nbeta\nGAMMA\ndelta\n"
    )


async def test_edit_patch_round_trips(env: NodeExecutionEnv) -> None:
    """上游用 ``applyPatch(original, details.patch)`` 断言（``tools.test.ts:369``）。

    这里用同一套语义的最小应用器验证：patch 应用回原内容必须得到改后的内容。
    """
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_edit_diff_parity import apply_unified_patch

    original = "alpha\nbeta\ngamma\ndelta\n"
    get_or_throw(await env.write_file("edit.txt", original, BACKGROUND_CONTEXT))

    result = await run(
        create_edit_tool(),
        _edit("edit.txt", [("alpha\n", "ALPHA\n"), ("gamma\n", "GAMMA\n")]),
        env,
    )

    assert apply_unified_patch(original, result.details.patch) == "ALPHA\nbeta\nGAMMA\ndelta\n"


async def test_edit_rejects_overlapping_edits(env: NodeExecutionEnv) -> None:
    """对照 ``tools.test.ts:377-399``：重叠要被拒绝，且**文件不变**。"""
    get_or_throw(await env.write_file("edit.txt", "one\ntwo\nthree\n", BACKGROUND_CONTEXT))

    with pytest.raises(ValueError, match="overlap"):
        await run(
            create_edit_tool(),
            _edit("edit.txt", [("one\ntwo\n", "ONE\nTWO\n"), ("two\nthree\n", "TWO\nTHREE\n")]),
            env,
        )

    assert get_or_throw(await env.read_text_file("edit.txt", BACKGROUND_CONTEXT)) == (
        "one\ntwo\nthree\n"
    )


async def test_edit_rejects_missing_text(env: NodeExecutionEnv) -> None:
    """对照 ``tools.test.ts:401-410``。"""
    get_or_throw(await env.write_file("edit.txt", "foo foo foo", BACKGROUND_CONTEXT))

    with pytest.raises(ValueError, match="Could not find the exact text"):
        await run(create_edit_tool(), _edit("edit.txt", [("bar", "baz")]), env)


async def test_edit_rejects_duplicate_text(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("edit.txt", "foo foo foo", BACKGROUND_CONTEXT))

    with pytest.raises(ValueError, match="Found 3 occurrences"):
        await run(create_edit_tool(), _edit("edit.txt", [("foo", "bar")]), env)


async def test_edit_rejects_empty_edits(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("edit.txt", "abc", BACKGROUND_CONTEXT))

    with pytest.raises(RuntimeError, match="edits must contain at least one replacement"):
        await run(create_edit_tool(), _edit("edit.txt", []), env)


async def test_edit_rejects_no_change(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("edit.txt", "abc", BACKGROUND_CONTEXT))

    with pytest.raises(ValueError, match="No changes made"):
        await run(create_edit_tool(), _edit("edit.txt", [("abc", "abc")]), env)


async def test_edit_preserves_crlf(env: NodeExecutionEnv) -> None:
    """行尾由文件决定：CRLF 文件改完仍是 CRLF（``edit.ts:117-122``）。"""
    get_or_throw(await env.write_file("crlf.txt", "a\r\nb\r\n", BACKGROUND_CONTEXT))

    await run(create_edit_tool(), _edit("crlf.txt", [("b", "B")]), env)

    assert get_or_throw(await env.read_text_file("crlf.txt", BACKGROUND_CONTEXT)) == "a\r\nB\r\n"


async def test_edit_preserves_bom(env: NodeExecutionEnv) -> None:
    """``strip_bom`` / BOM 回写（``edit.ts:116,122``）。"""
    get_or_throw(await env.write_file("bom.txt", "\ufeffa\nb\n", BACKGROUND_CONTEXT))

    await run(create_edit_tool(), _edit("bom.txt", [("b", "B")]), env)

    assert get_or_throw(await env.read_text_file("bom.txt", BACKGROUND_CONTEXT)) == "\ufeffa\nB\n"


async def test_edit_uses_fuzzy_match_for_smart_quotes(env: NodeExecutionEnv) -> None:
    """模糊匹配：文件里是弯引号，模型给直引号（``edit-diff.ts:27-51``）。"""
    get_or_throw(await env.write_file("q.txt", "say \u201chello\u201d\n", BACKGROUND_CONTEXT))

    result = await run(create_edit_tool(), _edit("q.txt", [('say "hello"', 'say "bye"')]), env)

    assert result.is_error is None
    content = get_or_throw(await env.read_text_file("q.txt", BACKGROUND_CONTEXT))
    assert "bye" in content
    # 未改动的行保持原字节——归一化只用于定位。
    assert content.endswith("\n")


async def test_edit_rejects_non_file(env: NodeExecutionEnv, tmp_path: Path) -> None:
    (tmp_path / "adir").mkdir()

    with pytest.raises(RuntimeError, match="Path is not a file"):
        await run(create_edit_tool(), _edit("adir", [("a", "b")]), env)


async def test_edit_missing_file_reports_access_error(env: NodeExecutionEnv) -> None:
    with pytest.raises(RuntimeError, match="Could not edit file"):
        await run(create_edit_tool(), _edit("missing.txt", [("a", "b")]), env)


# --------------------------------------------------------------------------------------
# prepareArguments（tools.test.ts:373-390）
# --------------------------------------------------------------------------------------


def test_prepare_arguments_repairs_shapes_without_mutating() -> None:
    """逐条对照 ``tools.test.ts:375-390``。"""
    edit = {"oldText": "a", "newText": "b"}

    as_string = {"path": "f", "edits": '[{"oldText": "a", "newText": "b"}]'}
    assert prepare_edit_arguments(as_string) == {"path": "f", "edits": [edit]}
    assert as_string["edits"] == '[{"oldText": "a", "newText": "b"}]'  # 未被改动

    assert prepare_edit_arguments({"path": "f", "edits": '{"oldText": "a", "newText": "b"}'}) == {
        "path": "f",
        "edits": [edit],
    }
    assert prepare_edit_arguments({"path": "f", "edits": edit}) == {"path": "f", "edits": [edit]}
    assert prepare_edit_arguments(
        {"path": "f", "edits": [edit], "oldText": "c", "newText": "d"}
    ) == {"path": "f", "edits": [edit, {"oldText": "c", "newText": "d"}]}
    # 不是 JSON 就原样留着，交给后面的验证报错
    assert prepare_edit_arguments({"path": "f", "edits": "not json"}) == {
        "path": "f",
        "edits": "not json",
    }


def test_edit_tool_declares_prepare_arguments() -> None:
    assert create_edit_tool().prepare_arguments is prepare_edit_arguments


def test_edit_schema_shape() -> None:
    parameters = create_edit_tool().parameters

    assert sorted(parameters["properties"]) == ["edits", "path"]
    edit_props = parameters["$defs"]["ReplaceEditInput"]["properties"]
    assert sorted(edit_props) == ["newText", "oldText"]


# --------------------------------------------------------------------------------------
# 序列化（tools.test.ts:411-470 的并发用例简化版）
# --------------------------------------------------------------------------------------


async def test_concurrent_edits_of_one_file_are_serialized(env: NodeExecutionEnv) -> None:
    """两次并发 edit 都作用于同一文件，结果必须都落上（靠改动队列）。"""
    import asyncio

    get_or_throw(await env.write_file("f.txt", "alpha\nbeta\n", BACKGROUND_CONTEXT))
    tool = create_edit_tool()
    api = FakeToolApi(env=env)

    await asyncio.gather(
        tool.execute(_edit("f.txt", [("alpha", "ALPHA")]), api, BACKGROUND_CONTEXT),
        tool.execute(_edit("f.txt", [("beta", "BETA")]), api, BACKGROUND_CONTEXT),
    )

    assert get_or_throw(await env.read_text_file("f.txt", BACKGROUND_CONTEXT)) == "ALPHA\nBETA\n"
