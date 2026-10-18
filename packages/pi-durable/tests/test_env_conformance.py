"""上游 env-conformance 可执行规格接入 pytest —— 本批验收的主依据。

套件本身与上游 ``testing/env-conformance.ts`` 逐条对应（名字、顺序、断言、超时），
这里只提供 ``with_env``（每个用例一个全新的空目录）与 pytest 参数化。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Awaitable, Callable

import pytest

from pi_chord.context import BACKGROUND_CONTEXT
from pi_durable.env import ExecutionEnv
from pi_durable.env.node import NodeExecutionEnv, NodeExecutionEnvOptions, NodeWatchOptions
from pi_durable.testing import EnvConformanceCase, conformance_ids, env_conformance_cases

#: 上游默认轮询间隔 2000 ms。用例里每步最多等 3 s，取 25 ms 让用例既快又稳；
#: 语义不变（仍然是快照差分 + 轮询），只是采样更密。
WATCH_OPTIONS = NodeWatchOptions(poll_interval_ms=25)


async def _with_env(use: Callable[[ExecutionEnv], Awaitable[None]]) -> None:
    directory = tempfile.mkdtemp(prefix="pi-durable-conformance-")
    try:
        env = NodeExecutionEnv(NodeExecutionEnvOptions(cwd=directory, watch=WATCH_OPTIONS))
        await use(env)
        await env.cleanup(BACKGROUND_CONTEXT)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


CASES = env_conformance_cases(_with_env)


@pytest.mark.parametrize("case", CASES, ids=conformance_ids(CASES))
async def test_env_conformance(case: EnvConformanceCase) -> None:
    """一个上游环境契约用例。"""
    if case.timeout_ms is None:
        await case.run()
    else:
        await asyncio.wait_for(case.run(), timeout=case.timeout_ms / 1000)


def test_the_suite_covers_the_upstream_cases() -> None:
    """用例集合与上游 env-conformance.ts 一致（symlinks=True、listFiles=False）。"""
    assert len(CASES) == 24
    names = [case.name for case in CASES]
    assert names[0] == "binary reader reads byte ranges of the opened file"
    # 上游把符号链接用例追加在末尾（`if (symlinks)`），所以最后一条是 watch 用例。
    assert names[-1] == "watch reports changes to the file a watched symbolic link points to"
    assert names[21] == "argv exec distinguishes timeout from abort"
    # watch 用例需要更长超时（上游 30 s），其余用例用 runner 默认超时。
    assert [case.timeout_ms for case in CASES if case.timeout_ms is not None] == [30_000] * 9


def test_optional_list_files_cases_are_ported() -> None:
    """可选的 listFiles 能力对应上游 5 个用例；本端环境与上游 node.ts 一样不实现它。

    套件仍然生成这些用例（供实现了该能力的远程/容器环境使用），只是默认不启用。
    """
    cases = env_conformance_cases(_with_env, list_files=True)
    names = [case.name for case in cases]
    assert len(cases) == 29
    assert "listFiles lists files and directories depth first in name order" in names
    assert "listFiles lists symbolic links without following them" in names


def test_watch_defaults_match_upstream() -> None:
    """出厂默认值来自上游 DEFAULT_POLL_MS / DEFAULT_MAX_DIRECTORIES。

    测试用 25 ms 让用例更快；出厂默认 2000 ms 下的 9 个 watch 用例已单独验证通过。
    """
    assert NodeWatchOptions().poll_interval_ms == 2000.0
    assert NodeWatchOptions().max_directories == 10_000
    assert WATCH_OPTIONS.poll_interval_ms == 25.0
