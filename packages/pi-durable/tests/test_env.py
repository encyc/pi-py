"""``NodeExecutionEnv`` 的补充单元测试（env-conformance 未覆盖的部分）。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from pi_chord.context import BACKGROUND_CONTEXT, AbortController, Context, with_abort_signal
from pi_durable.env import (
    CreateTempFileOptions,
    Err,
    FileError,
    Ok,
    OpenBinaryReaderOptions,
    RemoveOptions,
    Result,
    TextLinesOptions,
    err,
    get_or_throw,
    get_or_undefined,
    ok,
    starts_with_bom,
    to_error,
)
from pi_durable.env.node import NodeExecutionEnv, NodeExecutionEnvOptions


# 路径断言放在模块级同步函数里：测试是 async 的，但计算期望值不需要在事件循环上做阻塞 I/O。
def _join(*parts: str) -> str:
    return os.path.join(*parts)


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def _basename(path: str) -> str:
    return os.path.basename(path)


def _is_dir(path: str) -> bool:
    return os.path.isdir(path)


def _is_file(path: str) -> bool:
    return os.path.isfile(path)


def _symlink(source: str, destination: str) -> None:
    os.symlink(source, destination)


def _aborted() -> Context:
    controller = AbortController()
    controller.abort()
    return with_abort_signal(controller.signal, BACKGROUND_CONTEXT)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[NodeExecutionEnv]:
    yield NodeExecutionEnv(NodeExecutionEnvOptions(cwd=str(tmp_path)))


def test_result_helpers() -> None:
    success: Result[int, FileError] = ok(3)
    failure: Result[int, FileError] = err(FileError("invalid", "nope"))
    assert isinstance(success, Ok) and success.ok is True
    assert isinstance(failure, Err) and failure.ok is False
    assert get_or_throw(success) == 3
    assert get_or_undefined(failure) is None
    with pytest.raises(FileError, match="nope"):
        get_or_throw(failure)
    assert to_error("boom").args == ("boom",)
    original = ValueError("x")
    assert to_error(original) is original
    assert to_error({"a": 1}).args == ('{"a": 1}',)
    assert isinstance(to_error(object()), RuntimeError)


async def test_read_text_file_keeps_a_leading_mark(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("bom.txt", "\ufeffhello", BACKGROUND_CONTEXT))
    assert get_or_throw(await env.read_text_file("bom.txt", BACKGROUND_CONTEXT)) == "\ufeffhello"
    raw = get_or_throw(await env.read_binary_file("bom.txt", BACKGROUND_CONTEXT))
    assert starts_with_bom(raw)


async def test_text_line_reader_reports_termination(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("lines.txt", "one\ntwo\nthree", BACKGROUND_CONTEXT))
    reader = get_or_throw(await env.open_text_line_reader("lines.txt", BACKGROUND_CONTEXT))
    first = get_or_throw(await reader.read_line(BACKGROUND_CONTEXT))
    second = get_or_throw(await reader.read_line(BACKGROUND_CONTEXT))
    third = get_or_throw(await reader.read_line(BACKGROUND_CONTEXT))
    assert (first.text, first.terminated) == ("one", True)
    assert (second.text, second.terminated) == ("two", True)
    assert (third.text, third.terminated) == ("three", False)
    assert get_or_throw(await reader.read_line(BACKGROUND_CONTEXT)) is None
    await reader.close(BACKGROUND_CONTEXT)
    await reader.close(BACKGROUND_CONTEXT)
    closed = await reader.read_line(BACKGROUND_CONTEXT)
    assert isinstance(closed, Err) and closed.error.code == "invalid"


async def test_text_line_reader_drops_a_leading_mark(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("bom.txt", "\ufeffone\ntwo", BACKGROUND_CONTEXT))
    reader = get_or_throw(await env.open_text_line_reader("bom.txt", BACKGROUND_CONTEXT))
    try:
        assert get_or_throw(await reader.read_line(BACKGROUND_CONTEXT)).text == "one"
    finally:
        await reader.close(BACKGROUND_CONTEXT)


async def test_read_text_lines_honours_max_lines(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("many.txt", "a\nb\nc\nd", BACKGROUND_CONTEXT))
    assert get_or_throw(await env.read_text_lines("many.txt", None, BACKGROUND_CONTEXT)) == (
        "a",
        "b",
        "c",
        "d",
    )
    limited = get_or_throw(
        await env.read_text_lines("many.txt", TextLinesOptions(max_lines=2), BACKGROUND_CONTEXT)
    )
    assert limited == ("a", "b")
    assert (
        get_or_throw(
            await env.read_text_lines("many.txt", TextLinesOptions(max_lines=0), BACKGROUND_CONTEXT)
        )
        == ()
    )


async def test_append_truncate_and_flush(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("f.txt", "abc", BACKGROUND_CONTEXT))
    get_or_throw(await env.append_file("f.txt", "de", BACKGROUND_CONTEXT))
    assert get_or_throw(await env.read_text_file("f.txt", BACKGROUND_CONTEXT)) == "abcde"
    get_or_throw(await env.flush_file("f.txt", BACKGROUND_CONTEXT))
    get_or_throw(await env.truncate_file("f.txt", 2, BACKGROUND_CONTEXT))
    assert get_or_throw(await env.read_text_file("f.txt", BACKGROUND_CONTEXT)) == "ab"


async def test_truncate_file_rejects_negative_sizes(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("f.txt", "abc", BACKGROUND_CONTEXT))
    rejected = await env.truncate_file("f.txt", -1, BACKGROUND_CONTEXT)
    assert isinstance(rejected, Err) and rejected.error.code == "invalid"


async def test_flush_file_refuses_directories(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.create_dir("dir", None, BACKGROUND_CONTEXT))
    refused = await env.flush_file("dir", BACKGROUND_CONTEXT)
    assert isinstance(refused, Err) and refused.error.code == "is_directory"


async def test_file_info_exists_and_canonical_path(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("f.txt", "abc", BACKGROUND_CONTEXT))
    info = get_or_throw(await env.file_info("f.txt", BACKGROUND_CONTEXT))
    assert (info.name, info.kind, info.size) == ("f.txt", "file", 3)
    assert get_or_throw(await env.exists("f.txt", BACKGROUND_CONTEXT)) is True
    assert get_or_throw(await env.exists("missing.txt", BACKGROUND_CONTEXT)) is False
    canonical = get_or_throw(await env.canonical_path("f.txt", BACKGROUND_CONTEXT))
    assert canonical.endswith("f.txt")
    missing = await env.canonical_path("missing.txt", BACKGROUND_CONTEXT)
    assert isinstance(missing, Err) and missing.error.code == "not_found"


async def test_path_helpers(env: NodeExecutionEnv) -> None:
    absolute = get_or_throw(await env.absolute_path("sub/f.txt", BACKGROUND_CONTEXT))
    assert absolute == _join(str(env.cwd), "sub", "f.txt")
    assert get_or_throw(await env.join_path(["a", "b"], BACKGROUND_CONTEXT)) == _join("a", "b")
    assert get_or_throw(await env.join_path([], BACKGROUND_CONTEXT)) == "."
    assert get_or_throw(await env.absolute_path("~", BACKGROUND_CONTEXT)) == _expand("~")
    as_url = get_or_throw(await env.absolute_path("file:///tmp/x", BACKGROUND_CONTEXT))
    assert as_url == "/tmp/x"
    # 畸形 URL 当作普通路径处理，文件系统方法保持不抛异常的契约
    assert get_or_throw(await env.absolute_path("file://", BACKGROUND_CONTEXT)).startswith(
        str(env.cwd)
    )


async def test_directory_and_metadata_error_codes(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("f.txt", "abc", BACKGROUND_CONTEXT))
    missing = await env.file_info("missing", BACKGROUND_CONTEXT)
    assert isinstance(missing, Err) and missing.error.code == "not_found"
    not_directory = await env.list_dir("f.txt", BACKGROUND_CONTEXT)
    assert isinstance(not_directory, Err) and not_directory.error.code == "not_directory"
    absent = await env.read_binary_file("dir-missing", BACKGROUND_CONTEXT)
    assert isinstance(absent, Err) and absent.error.code == "not_found"


async def test_open_binary_reader_refuses_symlink_without_following(
    env: NodeExecutionEnv,
) -> None:
    get_or_throw(await env.write_file("target.txt", "target", BACKGROUND_CONTEXT))
    _symlink(_join(str(env.cwd), "target.txt"), _join(str(env.cwd), "link.txt"))
    refused = await env.open_binary_reader(
        "link.txt", OpenBinaryReaderOptions(no_follow=True), BACKGROUND_CONTEXT
    )
    assert isinstance(refused, Err) and refused.error.code == "invalid"
    followed = get_or_throw(await env.open_binary_reader("link.txt", None, BACKGROUND_CONTEXT))
    assert get_or_throw(await followed.read(0, 10, BACKGROUND_CONTEXT)) == b"target"
    await followed.close(BACKGROUND_CONTEXT)


async def test_remove_recursive_and_force(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("tree/a/b.txt", "x", BACKGROUND_CONTEXT))
    get_or_throw(await env.remove("tree", RemoveOptions(recursive=True), BACKGROUND_CONTEXT))
    assert get_or_throw(await env.exists("tree", BACKGROUND_CONTEXT)) is False
    missing = await env.remove("tree", None, BACKGROUND_CONTEXT)
    assert isinstance(missing, Err) and missing.error.code == "not_found"
    forced = await env.remove("tree", RemoveOptions(force=True), BACKGROUND_CONTEXT)
    assert isinstance(forced, Ok)
    # 目录不带 recursive 时拒绝（POSIX 上为 EISDIR）。
    get_or_throw(await env.create_dir("tree2", None, BACKGROUND_CONTEXT))
    refused = await env.remove("tree2", None, BACKGROUND_CONTEXT)
    assert isinstance(refused, Err) and refused.error.code == "is_directory"


async def test_create_temp_dir_and_file(env: NodeExecutionEnv) -> None:
    directory = get_or_throw(await env.create_temp_dir("pi-test-", BACKGROUND_CONTEXT))
    assert _is_dir(directory)
    created = get_or_throw(
        await env.create_temp_file(
            CreateTempFileOptions(prefix="pre-", suffix=".log"), BACKGROUND_CONTEXT
        )
    )
    assert _basename(created).startswith("pre-")
    assert created.endswith(".log")
    assert _is_file(created)


async def test_list_files_capability_is_absent(env: NodeExecutionEnv) -> None:
    # 与上游 node.ts 一致：本地环境不实现可选的 listFiles。
    assert env.list_files is None
    assert env.id == "node:local"


async def test_file_operations_honour_an_aborted_context(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("f.txt", "abc", BACKGROUND_CONTEXT))
    context = _aborted()
    for name, operation in [
        ("read_text_file", env.read_text_file("f.txt", context)),
        ("read_binary_file", env.read_binary_file("f.txt", context)),
        ("write_file", env.write_file("g.txt", "x", context)),
        ("append_file", env.append_file("f.txt", "x", context)),
        ("truncate_file", env.truncate_file("f.txt", 0, context)),
        ("flush_file", env.flush_file("f.txt", context)),
        ("rename_file", env.rename_file("f.txt", "g.txt", context)),
        ("file_info", env.file_info("f.txt", context)),
        ("list_dir", env.list_dir(".", context)),
        ("open_dir_reader", env.open_dir_reader(".", context)),
        ("open_text_line_reader", env.open_text_line_reader("f.txt", context)),
        ("open_binary_reader", env.open_binary_reader("f.txt", None, context)),
        ("canonical_path", env.canonical_path("f.txt", context)),
        ("create_dir", env.create_dir("d", None, context)),
        ("remove", env.remove("f.txt", None, context)),
        ("create_temp_dir", env.create_temp_dir("x-", context)),
        ("create_temp_file", env.create_temp_file(None, context)),
        ("exec", env.exec("true", None, context)),
    ]:
        result = await operation
        assert isinstance(result, Err), name
        assert result.error.code == "aborted", name


async def test_watch_honours_an_aborted_context(env: NodeExecutionEnv) -> None:
    set_up = await env.watch([], lambda change: None, _aborted())
    assert isinstance(set_up, Err) and set_up.error.code == "aborted"
