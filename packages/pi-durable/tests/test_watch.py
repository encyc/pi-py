"""``NodeFileWatcher``：快照差分语义的边界（env-conformance 之外的部分）。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from pi_chord.context import BACKGROUND_CONTEXT
from pi_durable.env import Err, Ok, WatchChange, WatchExclude, WatchTarget, get_or_throw
from pi_durable.env.node import NodeExecutionEnv, NodeExecutionEnvOptions
from pi_durable.env.watch import NodeWatchOptions

FAST = NodeWatchOptions(poll_interval_ms=20)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[NodeExecutionEnv]:
    yield NodeExecutionEnv(NodeExecutionEnvOptions(cwd=str(tmp_path), watch=FAST))


async def _wait_for(predicate: Callable[[], bool], seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_watcher_reports_polling_mode(env: NodeExecutionEnv) -> None:
    changes: list[WatchChange] = []
    watcher = get_or_throw(
        await env.watch([WatchTarget(path="file.txt")], changes.append, BACKGROUND_CONTEXT)
    )
    try:
        assert watcher.mode == "polling"
    finally:
        await watcher.close(BACKGROUND_CONTEXT)


async def test_native_mode_is_rejected_loudly(tmp_path: Path) -> None:
    env = NodeExecutionEnv(
        NodeExecutionEnvOptions(cwd=str(tmp_path), watch=NodeWatchOptions(mode="native"))
    )
    result = await env.watch(
        [WatchTarget(path="file.txt")], lambda change: None, BACKGROUND_CONTEXT
    )
    assert isinstance(result, Err)
    assert "native" in str(result.error)


async def test_directory_budget_is_enforced(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    env = NodeExecutionEnv(
        NodeExecutionEnvOptions(
            cwd=str(tmp_path), watch=NodeWatchOptions(poll_interval_ms=20, max_directories=1)
        )
    )
    result = await env.watch(
        [WatchTarget(path="a", recursive=True)], lambda change: None, BACKGROUND_CONTEXT
    )
    assert isinstance(result, Err) and result.error.code == "invalid"
    assert "exceed" in str(result.error)


async def test_throwing_callbacks_do_not_stop_watching(env: NodeExecutionEnv) -> None:
    received: list[WatchChange] = []

    def on_change(change: WatchChange) -> None:
        received.append(change)
        raise RuntimeError("callback boom")

    watcher = get_or_throw(
        await env.watch([WatchTarget(path="f.txt")], on_change, BACKGROUND_CONTEXT)
    )
    try:
        get_or_throw(await env.write_file("f.txt", "one", BACKGROUND_CONTEXT))
        assert await _wait_for(lambda: len(received) >= 1)
        get_or_throw(await env.write_file("f.txt", "two!", BACKGROUND_CONTEXT))
        assert await _wait_for(lambda: len(received) >= 2)
    finally:
        await watcher.close(BACKGROUND_CONTEXT)


async def test_close_is_idempotent_and_stops_reporting(env: NodeExecutionEnv) -> None:
    changes: list[WatchChange] = []
    watcher = get_or_throw(
        await env.watch([WatchTarget(path="f.txt")], changes.append, BACKGROUND_CONTEXT)
    )
    await watcher.close(BACKGROUND_CONTEXT)
    await watcher.close(BACKGROUND_CONTEXT)
    get_or_throw(await env.write_file("f.txt", "x", BACKGROUND_CONTEXT))
    await asyncio.sleep(0.2)
    assert changes == []


async def test_excluded_entries_never_appear_in_reported_paths(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    env = NodeExecutionEnv(NodeExecutionEnvOptions(cwd=str(tmp_path), watch=FAST))
    changes: list[WatchChange] = []
    watcher = get_or_throw(
        await env.watch(
            [WatchTarget(path="skills", recursive=True, exclude=WatchExclude(hidden=True))],
            changes.append,
            BACKGROUND_CONTEXT,
        )
    )
    try:
        get_or_throw(await env.write_file("skills/.hidden", "x", BACKGROUND_CONTEXT))
        get_or_throw(await env.write_file("skills/visible.txt", "x", BACKGROUND_CONTEXT))
        assert await _wait_for(lambda: len(changes) >= 1)
        reported = [
            path
            for change in changes
            if hasattr(change, "paths")
            for path in change.paths  # type: ignore[union-attr]
        ]
        assert not any(path.endswith(".hidden") for path in reported)
    finally:
        await watcher.close(BACKGROUND_CONTEXT)


async def test_watch_result_is_ok_for_an_existing_target(env: NodeExecutionEnv) -> None:
    get_or_throw(await env.write_file("present.txt", "x", BACKGROUND_CONTEXT))
    result = await env.watch(
        [WatchTarget(path="present.txt")], lambda change: None, BACKGROUND_CONTEXT
    )
    assert isinstance(result, Ok)
    await result.value.close(BACKGROUND_CONTEXT)
