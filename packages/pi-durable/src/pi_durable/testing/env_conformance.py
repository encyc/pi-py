"""The executable environment specification — port of ``testing/env-conformance.ts``.

This suite *is* the acceptance criterion for an :class:`~pi_durable.env.ExecutionEnv`:
upstream writes the contract as runnable cases, and every case here keeps the
upstream name, order, body and timeout, so the two files can be diffed by eye.
Adaptations are mechanical:

============================  ==========================================
``getOrThrow(await …)``        ``get_or_throw(await …)``
``decoder.decode(bytes)``      ``bytes.decode("utf-8", "replace")``
``encoder.encode(text).length``  ``len(text.encode("utf-8", "replace"))``
``setTimeout(resolve, 20)``    ``await asyncio.sleep(0.02)``
``Date.now()``                 ``time.monotonic() * 1000``
``{entries, done}``            :class:`~pi_durable.env.DirPage`
============================  ==========================================
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import final

from pi_chord.context import (
    BACKGROUND_CONTEXT,
    AbortController,
    Context,
    with_abort_signal,
)
from pi_durable.env import (
    DirPage,
    ExecutionEnv,
    ExecutionError,
    FileError,
    FileInfo,
    LineRangeOptions,
    ListFilesOptions,
    Ok,
    OpenBinaryReaderOptions,
    Result,
    ShellExecOptions,
    ShellExecResult,
    ShellOutputInfo,
    ShellOutputWindow,
    WatchChange,
    WatchExclude,
    WatchOverflow,
    WatchPaths,
    WatchTarget,
    get_or_throw,
)

from .types import EnvConformanceCase, EnvConformanceOptions

__all__ = ["create_env_conformance"]

_CONTEXT: Context = BACKGROUND_CONTEXT


def _aborted_context() -> Context:
    """Upstream ``abortedContext()``: a context cancelled before the call."""
    controller = AbortController()
    controller.abort()
    return with_abort_signal(controller.signal, BACKGROUND_CONTEXT)


def _error_code(result: Result[object, BaseException]) -> str | None:
    if isinstance(result, Ok):
        return None
    error = result.error
    return error.code if isinstance(error, (FileError, ExecutionError)) else None


def _decode(data: bytes, *, strip_bom: bool) -> str:
    """``new TextDecoder("utf-8", {ignoreBOM: not strip_bom})`` over ``data``."""
    text = data.decode("utf-8", "replace")
    return text[1:] if strip_bom and text.startswith("\ufeff") else text


def _decoded(data: bytes) -> str:
    """``new TextDecoder().decode(data)``: a leading byte-order mark is dropped."""
    return _decode(data, strip_bom=True)


def _encoded_length(text: str) -> int:
    return len(text.encode("utf-8", "replace"))


def _newlines(text: str) -> int:
    return text.count("\n")


async def _read_all(
    env: ExecutionEnv, path: str, max_entries: int
) -> tuple[list[Sequence[FileInfo]], bool]:
    """Upstream ``readAll``: page a directory to its end."""
    reader = get_or_throw(await env.open_dir_reader(path, _CONTEXT))
    pages: list[Sequence[FileInfo]] = []
    try:
        for _page in range(1000):
            page = get_or_throw(await reader.next(max_entries, _CONTEXT))
            pages.append(page.entries)
            if page.done:
                return pages, True
        return pages, False
    finally:
        await reader.close(_CONTEXT)


async def _listed(
    env: ExecutionEnv, path: str, options: ListFilesOptions | None = None
) -> tuple[list[str], bool]:
    """A listing as ``<kind letter> <path>`` lines (``f``, ``d``, ``l``), with its truncation."""
    if env.list_files is None:
        raise RuntimeError("The environment does not implement listFiles")
    listing = get_or_throw(await env.list_files(path, options, _CONTEXT))
    letter = {"file": "f", "directory": "d", "symlink": "l"}
    return [f"{letter[entry.kind]} {entry.path}" for entry in listing.entries], listing.truncated


def _covers(change: WatchChange, path: str) -> bool:
    """Whether a change reports ``path``: an overflow, or a reported path at or above it."""
    if isinstance(change, WatchOverflow):
        return True
    if not isinstance(change, WatchPaths):
        return False
    return any(
        path == reported or path.startswith(f"{reported}/") or path.startswith(f"{reported}\\")
        for reported in change.paths
    )


@final
@dataclass
class _WindowState:
    """Mutable accumulator for the windowed-exec case."""

    byte_count: int = 0
    newline_count: int = 0
    tail: str = ""


@final
@dataclass(frozen=True, slots=True)
class _WatchHelpers:
    """The helper object upstream passes into a watching body."""

    changes: list[WatchChange]
    expect_change: Callable[[str, Callable[[], Awaitable[None]]], Awaitable[None]]
    absolute: Callable[[str], Awaitable[str]]


async def _watching(
    env: ExecutionEnv,
    targets: Sequence[WatchTarget],
    run: Callable[[_WatchHelpers], Awaitable[None]],
) -> None:
    """Watch ``targets`` while ``run`` changes files, with a helper that waits for a change."""
    changes: list[WatchChange] = []
    watcher = get_or_throw(await env.watch(targets, changes.append, _CONTEXT))
    try:

        async def absolute(path: str) -> str:
            return get_or_throw(await env.absolute_path(path, _CONTEXT))

        async def expect_change(path: str, change: Callable[[], Awaitable[None]]) -> None:
            target = await absolute(path)
            start = len(changes)
            await change()
            deadline = time.monotonic() * 1000 + 3000
            while not any(_covers(entry, target) for entry in changes[start:]):
                if time.monotonic() * 1000 > deadline:
                    raise AssertionError(f"No change reported {target}; got {changes[start:]!r}")
                await asyncio.sleep(0.02)

        await run(_WatchHelpers(changes=changes, expect_change=expect_change, absolute=absolute))
    finally:
        await watcher.close(_CONTEXT)


async def _exec_collect(
    env: ExecutionEnv, command: str | Sequence[str], cwd: str | None = None
) -> tuple[Result[ShellExecResult, ExecutionError], str, str]:
    """Upstream ``execCollect``: run a command and collect both streams."""
    output = {"stdout": "", "stderr": ""}

    def on_output(text: str, _context: Context, info: ShellOutputInfo) -> None:
        output[info.stream] += text

    result = await env.exec(command, ShellExecOptions(cwd=cwd, on_output=on_output), _CONTEXT)
    return result, output["stdout"], output["stderr"]


def _create_env_conformance(  # noqa: C901 - one factory per case, exactly like upstream
    options: EnvConformanceOptions,
) -> tuple[EnvConformanceCase, ...]:
    assert_ = options.assertions
    shell = options.shell
    symlinks = options.symlinks

    def create_case(
        name: str,
        test: Callable[[ExecutionEnv], Awaitable[None]],
        timeout_ms: int | None = None,
    ) -> EnvConformanceCase:
        return EnvConformanceCase(
            name=name, timeout_ms=timeout_ms, run=lambda: options.with_env(test)
        )

    def watch_case(
        name: str, test: Callable[[ExecutionEnv], Awaitable[None]]
    ) -> EnvConformanceCase:
        # Watch cases wait up to three seconds per step, longer than runners allow by default.
        return create_case(name, test, 30_000)

    async def binary_reader_reads_byte_ranges(env: ExecutionEnv) -> None:
        get_or_throw(await env.write_file("data.txt", "hello world", _CONTEXT))
        reader = get_or_throw(await env.open_binary_reader("data.txt", None, _CONTEXT))
        info = get_or_throw(await reader.info(_CONTEXT))
        assert_.partial_deep_equal(info, {"name": "data.txt", "kind": "file", "size": 11})
        assert_.strict_equal(_decoded(get_or_throw(await reader.read(0, 5, _CONTEXT))), "hello")
        assert_.strict_equal(_decoded(get_or_throw(await reader.read(6, 100, _CONTEXT))), "world")
        assert_.strict_equal(len(get_or_throw(await reader.read(11, 4, _CONTEXT))), 0)
        assert_.strict_equal(len(get_or_throw(await reader.read(50, 1, _CONTEXT))), 0)
        assert_.strict_equal(len(get_or_throw(await reader.read(3, 0, _CONTEXT))), 0)
        assert_.strict_equal(_error_code(await reader.read(-1, 1, _CONTEXT)), "invalid")
        assert_.strict_equal(_error_code(await reader.read(0, 1.5, _CONTEXT)), "invalid")  # type: ignore[arg-type]
        assert_.strict_equal(_error_code(await reader.read(0, 1, _aborted_context())), "aborted")
        await reader.close(_CONTEXT)
        await reader.close(_CONTEXT)
        assert_.strict_equal(_error_code(await reader.read(0, 1, _CONTEXT)), "invalid")
        assert_.strict_equal(_error_code(await reader.info(_CONTEXT)), "invalid")

    async def binary_reader_scans_lines(env: ExecutionEnv) -> None:
        # A byte-order mark, an invalid sequence before a newline, an empty line, a later U+FEFF,
        # and no final newline.
        data = bytes(
            [
                0xEF,
                0xBB,
                0xBF,
                0x61,
                0x0A,
                0xE2,
                0x82,
                0x0A,
                0x0A,
                0xEF,
                0xBB,
                0xBF,
                0x62,
                0x0A,
                0xC3,
                0xA9,
            ]
        )
        get_or_throw(await env.write_file("lines.txt", data, _CONTEXT))
        lines = _decoded(data).split("\n")
        reader = get_or_throw(await env.open_binary_reader("lines.txt", None, _CONTEXT))

        def range_text(start: int, end: int) -> str:
            # Upstream decodes with BOM handling off unless the range starts at the file start.
            return _decode(data[start:end], strip_bom=start == 0)

        try:
            ranges: list[tuple[int, int | None]] = [
                (0, None),
                (0, 1),
                (1, 3),
                (2, 3),
                (3, None),
                (4, 9),
            ]
            for start_line, end_line in ranges:
                scan = get_or_throw(
                    await reader.scan_lines(LineRangeOptions(start_line, end_line), _CONTEXT)
                )
                selected = lines[start_line:end_line]
                joined = "\n".join(selected)
                assert_.strict_equal(scan.newlines, len(lines) - 1)
                assert_.strict_equal(range_text(scan.start, scan.end), joined)
                assert_.strict_equal(scan.selected_bytes, _encoded_length(joined))
                assert_.strict_equal(range_text(scan.start, scan.first_line_end), lines[start_line])
                assert_.strict_equal(scan.first_line_bytes, _encoded_length(lines[start_line]))
            assert_.partial_deep_equal(
                get_or_throw(await reader.scan_lines(LineRangeOptions(9), _CONTEXT)),
                {"start": len(data), "end": len(data), "selected_bytes": 0},
            )
            assert_.strict_equal(
                _error_code(await reader.scan_lines(LineRangeOptions(2, 2), _CONTEXT)), "invalid"
            )
        finally:
            await reader.close(_CONTEXT)

    async def binary_reader_survives_rename(env: ExecutionEnv) -> None:
        get_or_throw(await env.write_file("a.txt", "one", _CONTEXT))
        reader = get_or_throw(await env.open_binary_reader("a.txt", None, _CONTEXT))
        try:
            get_or_throw(await env.rename_file("a.txt", "b.txt", _CONTEXT))
            get_or_throw(await env.write_file("a.txt", "two", _CONTEXT))
            assert_.strict_equal(_decoded(get_or_throw(await reader.read(0, 10, _CONTEXT))), "one")
        finally:
            await reader.close(_CONTEXT)

    async def binary_reader_refuses_directories(env: ExecutionEnv) -> None:
        get_or_throw(await env.create_dir("dir", None, _CONTEXT))
        get_or_throw(await env.write_file("file.txt", "x", _CONTEXT))
        assert_.strict_equal(
            _error_code(await env.open_binary_reader("dir", None, _CONTEXT)), "is_directory"
        )
        assert_.strict_equal(
            _error_code(await env.open_binary_reader("missing.txt", None, _CONTEXT)), "not_found"
        )
        assert_.strict_equal(
            _error_code(await env.open_binary_reader("file.txt", None, _aborted_context())),
            "aborted",
        )

    async def directory_reader_pages_every_entry(env: ExecutionEnv) -> None:
        names = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]
        for name in names:
            get_or_throw(await env.write_file(name, name, _CONTEXT))
        get_or_throw(await env.create_dir("sub", None, _CONTEXT))
        pages, done = await _read_all(env, ".", 2)
        assert_.ok(done, "directory reader reached the end")
        for page in pages:
            assert_.ok(len(page) <= 2, "page within maxEntries")
        entries = [entry for page in pages for entry in page]
        assert_.deep_equal(sorted(entry.name for entry in entries), sorted([*names, "sub"]))
        by_name = {entry.name: entry for entry in entries}
        assert_.strict_equal(by_name["sub"].kind, "directory")
        assert_.partial_deep_equal(by_name["a.txt"], {"kind": "file", "size": 5})

    async def directory_reader_reports_the_end(env: ExecutionEnv) -> None:
        get_or_throw(await env.create_dir("empty", None, _CONTEXT))
        reader = get_or_throw(await env.open_dir_reader("empty", _CONTEXT))
        assert_.deep_equal(get_or_throw(await reader.next(10, _CONTEXT)), DirPage((), True))
        assert_.deep_equal(get_or_throw(await reader.next(10, _CONTEXT)), DirPage((), True))
        assert_.strict_equal(_error_code(await reader.next(0, _CONTEXT)), "invalid")
        assert_.strict_equal(_error_code(await reader.next(1, _aborted_context())), "aborted")
        await reader.close(_CONTEXT)
        await reader.close(_CONTEXT)
        assert_.strict_equal(_error_code(await reader.next(1, _CONTEXT)), "invalid")

    async def directory_reader_refuses_missing(env: ExecutionEnv) -> None:
        get_or_throw(await env.write_file("file.txt", "x", _CONTEXT))
        assert_.strict_equal(
            _error_code(await env.open_dir_reader("missing", _CONTEXT)), "not_found"
        )
        file = await env.open_dir_reader("file.txt", _CONTEXT)
        assert_.strict_equal(_error_code(file), "not_directory")
        assert_.strict_equal(
            _error_code(await env.open_dir_reader(".", _aborted_context())), "aborted"
        )

    async def directory_reader_skips_removed(env: ExecutionEnv) -> None:
        get_or_throw(await env.create_dir("dir", None, _CONTEXT))
        for name in ["x", "y", "z"]:
            get_or_throw(await env.write_file(f"dir/{name}", name, _CONTEXT))
        reader = get_or_throw(await env.open_dir_reader("dir", _CONTEXT))
        try:
            for name in ["x", "y", "z"]:
                get_or_throw(await env.remove(f"dir/{name}", None, _CONTEXT))
            entries: list[FileInfo] = []
            for _page in range(10):
                page = get_or_throw(await reader.next(10, _CONTEXT))
                entries.extend(page.entries)
                if page.done:
                    break
            assert_.deep_equal(entries, [])
        finally:
            await reader.close(_CONTEXT)

    async def watch_reports_creation_and_removal(env: ExecutionEnv) -> None:
        async def body(helpers: _WatchHelpers) -> None:
            async def create() -> None:
                get_or_throw(await env.write_file("AGENTS.md", "one", _CONTEXT))

            await helpers.expect_change("AGENTS.md", create)

            async def change() -> None:
                get_or_throw(await env.write_file("AGENTS.md", "two!", _CONTEXT))

            await helpers.expect_change("AGENTS.md", change)

            # Editors replace a file by renaming a new one over it.
            async def replace() -> None:
                get_or_throw(await env.write_file("AGENTS.md.tmp", "three", _CONTEXT))
                get_or_throw(await env.rename_file("AGENTS.md.tmp", "AGENTS.md", _CONTEXT))

            await helpers.expect_change("AGENTS.md", replace)

            async def rewrite() -> None:
                get_or_throw(await env.write_file("AGENTS.md", "four", _CONTEXT))

            await helpers.expect_change("AGENTS.md", rewrite)

            async def destroy() -> None:
                get_or_throw(await env.remove("AGENTS.md", None, _CONTEXT))

            await helpers.expect_change("AGENTS.md", destroy)

        await _watching(env, [WatchTarget(path="AGENTS.md")], body)

    async def watch_reports_created_ancestors(env: ExecutionEnv) -> None:
        async def body(helpers: _WatchHelpers) -> None:
            async def create() -> None:
                get_or_throw(await env.write_file("a/b/c/AGENTS.md", "x", _CONTEXT))

            await helpers.expect_change("a/b/c/AGENTS.md", create)

        await _watching(env, [WatchTarget(path="a/b/c/AGENTS.md")], body)

    async def watch_follows_new_directories(env: ExecutionEnv) -> None:
        get_or_throw(await env.create_dir("skills", None, _CONTEXT))
        targets = [WatchTarget(path="skills", recursive=True)]

        async def body(helpers: _WatchHelpers) -> None:
            # Written before any watcher on the new directories can exist.
            async def first() -> None:
                get_or_throw(await env.write_file("skills/a/b/SKILL.md", "one", _CONTEXT))

            await helpers.expect_change("skills/a/b/SKILL.md", first)

            async def second() -> None:
                get_or_throw(await env.write_file("skills/a/b/SKILL.md", "two!", _CONTEXT))

            await helpers.expect_change("skills/a/b/SKILL.md", second)

            async def deeper() -> None:
                get_or_throw(await env.write_file("skills/a/b/c/SKILL.md", "deeper", _CONTEXT))

            await helpers.expect_change("skills/a/b/c/SKILL.md", deeper)

        await _watching(env, targets, body)

    async def watch_survives_renamed_parent(env: ExecutionEnv) -> None:
        get_or_throw(await env.write_file("proj/.pi/skills/x.md", "x", _CONTEXT))
        targets = [WatchTarget(path="proj/.pi/skills", recursive=True)]

        async def body(helpers: _WatchHelpers) -> None:
            async def rename() -> None:
                get_or_throw(await env.rename_file("proj/.pi", "proj/old", _CONTEXT))

            await helpers.expect_change("proj/.pi/skills", rename)

            async def write() -> None:
                get_or_throw(await env.write_file("proj/.pi/skills/y.md", "y", _CONTEXT))

            await helpers.expect_change("proj/.pi/skills/y.md", write)

            async def rewrite() -> None:
                get_or_throw(await env.write_file("proj/.pi/skills/y.md", "yy", _CONTEXT))

            await helpers.expect_change("proj/.pi/skills/y.md", rewrite)

        await _watching(env, targets, body)

    async def watch_skips_excluded(env: ExecutionEnv) -> None:
        get_or_throw(await env.create_dir("skills", None, _CONTEXT))
        targets = [
            WatchTarget(
                path="skills",
                recursive=True,
                exclude=WatchExclude(hidden=True, names=("node_modules",)),
            )
        ]

        async def body(helpers: _WatchHelpers) -> None:
            get_or_throw(await env.write_file("skills/node_modules/dep/SKILL.md", "dep", _CONTEXT))
            get_or_throw(await env.write_file("skills/.SKILL.md.tmp", "draft", _CONTEXT))

            async def rename() -> None:
                get_or_throw(
                    await env.rename_file("skills/.SKILL.md.tmp", "skills/SKILL.md", _CONTEXT)
                )

            await helpers.expect_change("skills/SKILL.md", rename)
            hidden = [
                await helpers.absolute("skills/node_modules"),
                await helpers.absolute("skills/.SKILL.md.tmp"),
            ]
            for change in helpers.changes:
                if not isinstance(change, WatchPaths):
                    continue
                for path in change.paths:
                    assert_.ok(
                        not any(
                            path == excluded or path.startswith(excluded) for excluded in hidden
                        ),
                        f"excluded {path}",
                    )

        await _watching(env, targets, body)

    async def watch_keeps_recursive_coverage(env: ExecutionEnv) -> None:
        get_or_throw(await env.write_file("skills/a/one.md", "one", _CONTEXT))
        targets = [WatchTarget(path="skills"), WatchTarget(path="skills", recursive=True)]

        async def body(helpers: _WatchHelpers) -> None:
            async def write() -> None:
                get_or_throw(await env.write_file("skills/a/two.md", "two", _CONTEXT))

            await helpers.expect_change("skills/a/two.md", write)

        await _watching(env, targets, body)

    async def watch_follows_replaced_directory(env: ExecutionEnv) -> None:
        get_or_throw(await env.write_file("skills/a/x.md", "x", _CONTEXT))
        targets = [WatchTarget(path="skills", recursive=True)]

        async def body(helpers: _WatchHelpers) -> None:
            async def replace() -> None:
                get_or_throw(await env.rename_file("skills/a", "skills-old", _CONTEXT))
                get_or_throw(await env.create_dir("skills/a", None, _CONTEXT))

            await helpers.expect_change("skills/a", replace)

            async def write() -> None:
                get_or_throw(await env.write_file("skills/a/y.md", "y", _CONTEXT))

            await helpers.expect_change("skills/a/y.md", write)

            async def rewrite() -> None:
                get_or_throw(await env.write_file("skills/a/y.md", "yy", _CONTEXT))

            await helpers.expect_change("skills/a/y.md", rewrite)

        await _watching(env, targets, body)

    async def watch_stops_once_closed(env: ExecutionEnv) -> None:
        changes: list[WatchChange] = []
        watcher = get_or_throw(
            await env.watch([WatchTarget(path="file.txt")], changes.append, _CONTEXT)
        )
        assert_.ok(watcher.mode in ("native", "polling"), "watcher reports its mode")
        await watcher.close(_CONTEXT)
        await watcher.close(_CONTEXT)
        get_or_throw(await env.write_file("file.txt", "x", _CONTEXT))
        await asyncio.sleep(0.3)
        assert_.deep_equal(changes, [])

    async def argv_exec_without_shell_parsing(env: ExecutionEnv) -> None:
        hostile = "it's $(touch pwned) `touch pwned` *; touch pwned"
        result, stdout, _stderr = await _exec_collect(
            env,
            (*shell, 'printf "%s|%s" "$1" "$2"', "argv0", hostile, "a b"),
        )
        assert_.strict_equal(get_or_throw(result).exit_code, 0)
        assert_.strict_equal(stdout, f"{hostile}|a b")
        assert_.strict_equal(get_or_throw(await env.exists("pwned", _CONTEXT)), False)

    async def exec_reports_every_chunk(env: ExecutionEnv) -> None:
        script = "printf out; printf err >&2; printf more"
        argv_result, argv_stdout, argv_stderr = await _exec_collect(env, (*shell, script))
        assert_.strict_equal(get_or_throw(argv_result).exit_code, 0)
        assert_.strict_equal(argv_stdout, "outmore")
        assert_.strict_equal(argv_stderr, "err")
        string_result, string_stdout, string_stderr = await _exec_collect(env, script)
        assert_.strict_equal(get_or_throw(string_result).exit_code, 0)
        assert_.strict_equal(string_stdout, "outmore")
        assert_.strict_equal(string_stderr, "err")

    async def argv_exec_honors_cwd(env: ExecutionEnv) -> None:
        get_or_throw(await env.create_dir("sub", None, _CONTEXT))
        made, _stdout, _stderr = await _exec_collect(
            env, (*shell, "printf x > made.txt; exit 3"), "sub"
        )
        assert_.strict_equal(get_or_throw(made).exit_code, 3)
        assert_.strict_equal(get_or_throw(await env.read_text_file("sub/made.txt", _CONTEXT)), "x")

    async def argv_exec_reports_spawn_errors(env: ExecutionEnv) -> None:
        assert_.strict_equal(
            _error_code(await env.exec(["pi-durable-conformance-missing-program"], None, _CONTEXT)),
            "spawn_error",
        )
        assert_.strict_equal(_error_code(await env.exec([], None, _CONTEXT)), "spawn_error")

    async def windowed_exec_keeps_the_tail(env: ExecutionEnv) -> None:
        lines = 2000
        window = ShellOutputWindow(
            max_bytes=200, max_lines=5, min_interval_ms=0, bytes_per_second=1_000_000_000
        )
        state = _WindowState()

        def on_output(text: str, _context: Context, info: ShellOutputInfo) -> None:
            if info.skipped is not None:
                state.byte_count += info.skipped.bytes
                state.newline_count += info.skipped.newlines
                after = _encoded_length(text)
                after_newlines = _newlines(text)
                assert_.ok(
                    after > window.max_bytes or after_newlines > window.max_lines,
                    "a skip is followed by more than the window",
                )
                state.tail = ""
            state.byte_count += _encoded_length(text)
            state.newline_count += _newlines(text)
            state.tail += text

        result = await env.exec(
            (*shell, f"i=0; while [ $i -lt {lines} ]; do echo line-$i; i=$((i+1)); done"),
            ShellExecOptions(window=window, on_output=on_output),
            _CONTEXT,
        )
        assert_.strict_equal(get_or_throw(result).exit_code, 0)
        expected = "".join(f"line-{index}\n" for index in range(lines))
        assert_.strict_equal(state.byte_count, len(expected))
        assert_.strict_equal(state.newline_count, lines)
        assert_.ok(
            state.tail.endswith("".join(f"line-{index}\n" for index in range(lines - 5, lines))),
            "the delivered output ends with the tail",
        )

    async def exec_distinguishes_timeout_from_abort(env: ExecutionEnv) -> None:
        timed_out = await env.exec((*shell, "sleep 2"), ShellExecOptions(timeout=0.1), _CONTEXT)
        assert_.strict_equal(_error_code(timed_out), "timeout")
        controller = AbortController()
        running = asyncio.ensure_future(
            env.exec((*shell, "sleep 2"), None, with_abort_signal(controller.signal, _CONTEXT))
        )
        handle = asyncio.get_running_loop().call_later(0.1, controller.abort)
        try:
            assert_.strict_equal(_error_code(await running), "aborted")
        finally:
            handle.cancel()

    cases: list[EnvConformanceCase] = [
        create_case(
            "binary reader reads byte ranges of the opened file", binary_reader_reads_byte_ranges
        ),
        create_case(
            "binary reader scans lines like decoding the whole file", binary_reader_scans_lines
        ),
        create_case(
            "binary reader keeps reading the file it opened after a rename",
            binary_reader_survives_rename,
        ),
        create_case(
            "binary reader refuses directories, missing files and aborted opens",
            binary_reader_refuses_directories,
        ),
        create_case(
            "directory reader pages every entry exactly once", directory_reader_pages_every_entry
        ),
        create_case(
            "directory reader reports the end and refuses use after close",
            directory_reader_reports_the_end,
        ),
        create_case(
            "directory reader refuses missing paths and files", directory_reader_refuses_missing
        ),
        create_case(
            "directory reader skips entries removed during enumeration",
            directory_reader_skips_removed,
        ),
        watch_case(
            "watch reports a missing file's creation, changes, replacement and removal",
            watch_reports_creation_and_removal,
        ),
        watch_case(
            "watch reports a missing target whose ancestors are created",
            watch_reports_created_ancestors,
        ),
        watch_case(
            "watch follows directories created together with their contents",
            watch_follows_new_directories,
        ),
        watch_case(
            "watch keeps watching a path whose parent is renamed and recreated",
            watch_survives_renamed_parent,
        ),
        watch_case(
            "watch skips excluded entries and reports a rename out of them", watch_skips_excluded
        ),
        watch_case(
            "watch keeps recursive coverage where a non-recursive target overlaps",
            watch_keeps_recursive_coverage,
        ),
        watch_case(
            "watch follows a directory replaced at the same path", watch_follows_replaced_directory
        ),
        watch_case("watch stops reporting once closed", watch_stops_once_closed),
        create_case(
            "argv exec passes arguments to the program without shell parsing",
            argv_exec_without_shell_parsing,
        ),
        create_case(
            "exec reports the stream of every chunk in both forms", exec_reports_every_chunk
        ),
        create_case("argv exec honors cwd and exit codes", argv_exec_honors_cwd),
        create_case(
            "argv exec reports missing programs and empty argv as spawn errors",
            argv_exec_reports_spawn_errors,
        ),
        create_case(
            "windowed exec keeps the exact tail and counts what it skips",
            windowed_exec_keeps_the_tail,
        ),
        create_case(
            "argv exec distinguishes timeout from abort", exec_distinguishes_timeout_from_abort
        ),
    ]

    if options.list_files:

        async def list_files_depth_first(env: ExecutionEnv) -> None:
            for file in ["b.txt", "a/z.txt", "a/b/c.txt", ".hidden/x", "A.txt"]:
                get_or_throw(await env.write_file(file, "", _CONTEXT))
            get_or_throw(await env.create_dir("empty", None, _CONTEXT))
            assert_.deep_equal(
                await _listed(env, "."),
                (
                    [
                        "d .hidden",
                        "f .hidden/x",
                        "f A.txt",
                        "d a",
                        "d a/b",
                        "f a/b/c.txt",
                        "f a/z.txt",
                        "f b.txt",
                        "d empty",
                    ],
                    False,
                ),
            )
            hidden_off, _ = await _listed(env, ".", ListFilesOptions(hidden=False))
            assert_.deep_equal(
                hidden_off,
                ["f A.txt", "d a", "d a/b", "f a/b/c.txt", "f a/z.txt", "f b.txt", "d empty"],
            )
            expected = (["d b", "f b/c.txt", "f z.txt"], False)
            assert_.deep_equal(await _listed(env, "a"), expected)
            assert_.deep_equal(
                await _listed(env, get_or_throw(await env.absolute_path("a", _CONTEXT))), expected
            )
            assert_.deep_equal(await _listed(env, "empty"), ([], False))

        async def list_files_skips_ignored(env: ExecutionEnv) -> None:
            for file in [
                "keep.txt",
                "out/x.js",
                "sub/keep.txt",
                "sub/drop.log",
                "node_modules/m/i.js",
            ]:
                get_or_throw(await env.write_file(file, "", _CONTEXT))
            get_or_throw(await env.write_file(".gitignore", "out/\n*.log\n", _CONTEXT))
            get_or_throw(await env.write_file("sub/.gitignore", "!keep.txt\n", _CONTEXT))
            # Not a work tree yet: `.gitignore` files have no effect.
            all_entries, _ = await _listed(env, ".")
            assert_.ok("f out/x.js" in all_entries)
            get_or_throw(await env.write_file(".git/HEAD", "ref: refs/heads/main\n", _CONTEXT))
            get_or_throw(await env.write_file(".git/info/exclude", "keep.txt\n", _CONTEXT))
            excluded, _ = await _listed(env, ".", ListFilesOptions(exclude=("node_modules",)))
            assert_.deep_equal(
                excluded, ["f .gitignore", "d sub", "f sub/.gitignore", "f sub/keep.txt"]
            )
            # A directory inside the work tree applies its parents' rules.
            sub_entries, _ = await _listed(env, "sub")
            assert_.deep_equal(sub_entries, ["f .gitignore", "f keep.txt"])
            all_no_ignore, _ = await _listed(env, ".", ListFilesOptions(gitignore=False))
            assert_.ok(
                "f out/x.js" in all_no_ignore
                and "f node_modules/m/i.js" in all_no_ignore
                and "f keep.txt" in all_no_ignore
            )
            assert_.ok(not any(".git/" in entry or entry == "d .git" for entry in all_no_ignore))

        async def list_files_stops_at_bounds(env: ExecutionEnv) -> None:
            list_files = env.list_files
            assert list_files is not None
            for file in ["a", "b", "c"]:
                get_or_throw(await env.write_file(file, "", _CONTEXT))
            assert_.deep_equal(
                await _listed(env, ".", ListFilesOptions(max_entries=2)), (["f a", "f b"], True)
            )
            assert_.deep_equal(
                await _listed(env, ".", ListFilesOptions(max_entries=3)),
                (["f a", "f b", "f c"], False),
            )
            # Each entry counts its path plus two bytes.
            assert_.deep_equal(
                await _listed(env, ".", ListFilesOptions(max_bytes=6)), (["f a", "f b"], True)
            )
            assert_.deep_equal(await _listed(env, ".", ListFilesOptions(max_entries=0)), ([], True))
            assert_.strict_equal(
                _error_code(await list_files(".", ListFilesOptions(max_entries=-1), _CONTEXT)),
                "invalid",
            )
            assert_.strict_equal(
                _error_code(await list_files(".", ListFilesOptions(timeout_ms=1.5), _CONTEXT)),
                "invalid",
            )

        async def list_files_fails_on_missing(env: ExecutionEnv) -> None:
            list_files = env.list_files
            assert list_files is not None
            get_or_throw(await env.write_file("file.txt", "", _CONTEXT))
            assert_.strict_equal(
                _error_code(await list_files("missing", None, _CONTEXT)),
                "not_found",
            )
            assert_.strict_equal(
                _error_code(await list_files("file.txt", None, _CONTEXT)),
                "not_directory",
            )
            assert_.strict_equal(
                _error_code(await list_files(".", None, _aborted_context())),
                "aborted",
            )

        cases.extend(
            [
                create_case(
                    "listFiles lists files and directories depth first in name order",
                    list_files_depth_first,
                ),
                create_case(
                    "listFiles skips what git ignores in work trees, .git, and excluded names",
                    list_files_skips_ignored,
                ),
                create_case(
                    "listFiles stops at its bounds and reports truncated",
                    list_files_stops_at_bounds,
                ),
                create_case(
                    "listFiles fails on missing directories, files, and aborts",
                    list_files_fails_on_missing,
                ),
            ]
        )

    if symlinks and options.list_files:

        async def list_files_lists_symlinks(env: ExecutionEnv) -> None:
            get_or_throw(await env.write_file("dir/inner.txt", "", _CONTEXT))
            linked = await env.exec(
                (*shell, "ln -s dir link && ln -s missing dangling && ln -s .. dir/loop"),
                None,
                _CONTEXT,
            )
            assert_.strict_equal(get_or_throw(linked).exit_code, 0)
            entries, _ = await _listed(env, ".")
            assert_.deep_equal(
                entries, ["l dangling", "d dir", "f dir/inner.txt", "l dir/loop", "l link"]
            )
            # The listed directory itself may be a link.
            link_entries, _ = await _listed(env, "link")
            assert_.deep_equal(link_entries, ["f inner.txt", "l loop"])

        cases.append(
            create_case(
                "listFiles lists symbolic links without following them", list_files_lists_symlinks
            )
        )

    if symlinks:

        async def binary_reader_follows_symlinks(env: ExecutionEnv) -> None:
            get_or_throw(await env.write_file("target.txt", "target", _CONTEXT))
            get_or_throw(await env.create_dir("sub", None, _CONTEXT))
            get_or_throw(await env.write_file("sub/inner.txt", "inner", _CONTEXT))
            linked = await env.exec(
                (*shell, "ln -s target.txt link.txt && ln -s sub dirlink"), None, _CONTEXT
            )
            assert_.strict_equal(get_or_throw(linked).exit_code, 0)

            followed = get_or_throw(await env.open_binary_reader("link.txt", None, _CONTEXT))
            assert_.strict_equal(
                _decoded(get_or_throw(await followed.read(0, 10, _CONTEXT))), "target"
            )
            await followed.close(_CONTEXT)

            assert_.strict_equal(
                _error_code(
                    await env.open_binary_reader(
                        "link.txt", OpenBinaryReaderOptions(no_follow=True), _CONTEXT
                    )
                ),
                "invalid",
            )

            # Only the final component is refused; earlier symlinked directories still resolve.
            inner = get_or_throw(
                await env.open_binary_reader(
                    "dirlink/inner.txt", OpenBinaryReaderOptions(no_follow=True), _CONTEXT
                )
            )
            assert_.strict_equal(_decoded(get_or_throw(await inner.read(0, 10, _CONTEXT))), "inner")
            await inner.close(_CONTEXT)

        async def watch_reports_linked_file_changes(env: ExecutionEnv) -> None:
            get_or_throw(await env.write_file("data/real.md", "one", _CONTEXT))
            get_or_throw(await env.create_dir("config", None, _CONTEXT))
            linked = await env.exec(
                (*shell, "ln -s ../data/real.md config/AGENTS.md"), None, _CONTEXT
            )
            assert_.strict_equal(get_or_throw(linked).exit_code, 0)

            async def body(helpers: _WatchHelpers) -> None:
                async def write() -> None:
                    get_or_throw(await env.write_file("data/real.md", "two!", _CONTEXT))

                await helpers.expect_change("config/AGENTS.md", write)

            await _watching(env, [WatchTarget(path="config/AGENTS.md")], body)

        cases.append(
            create_case(
                "binary reader follows symlinks unless noFollow refuses the final one",
                binary_reader_follows_symlinks,
            )
        )
        cases.append(
            watch_case(
                "watch reports changes to the file a watched symbolic link points to",
                watch_reports_linked_file_changes,
            )
        )

    return tuple(cases)


def create_env_conformance(options: EnvConformanceOptions) -> tuple[EnvConformanceCase, ...]:
    """Create the runner-independent cases for an ``ExecutionEnv``.

    ``with_env`` must call and await its callback exactly once per case, with an
    environment whose ``cwd`` is a fresh, empty, writable directory.
    """
    return _create_env_conformance(options)
