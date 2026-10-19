"""骨架冒烟测试：验证包可被 import 且元数据正确。"""

from __future__ import annotations

import importlib
from importlib.metadata import version as dist_version

import pytest


def test_import() -> None:
    import pi_agent_core

    # 与**包元数据**比对，而不是硬编码字面量。硬编码会在版本升级时漂移，而这条
    # 断言真正要保证的不变量是「``__version__`` 与 ``pyproject`` 声明一致」——
    # pyproject 与 __version__ 曾经一个是 0.85.1、一个是 1.3.0，而硬编码的断言
    # 两边都"通过"，正是这个不变量失守却没被发现的原因。
    assert pi_agent_core.__version__ == dist_version("pi-py-agent-core")
    assert pi_agent_core.__upstream_ref__ == "earendil-works/pi@v1.3.0"


def test_sessions_subpackage_replaces_harness() -> None:
    """v1.3.0：``harness/`` 更名为 ``sessions/``，旧导入路径不再存在。"""
    import pi_agent_core.sessions as sessions

    assert sessions.__name__ == "pi_agent_core.sessions"
    assert importlib.import_module("pi_agent_core.sessions.compaction") is not None
    assert importlib.import_module("pi_agent_core.sessions.session") is not None
    assert importlib.import_module("pi_agent_core.sessions.skills") is not None

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("pi_agent_core.harness")


def test_root_reexports_survive_the_rename() -> None:
    """根包导出不随子包改名而变（SDK 用户的主要入口）。"""
    import pi_agent_core

    for name in (
        "Agent",
        "AgentOptions",
        "AgentTool",
        "AgentToolResult",
        "agent_loop",
        "agent_loop_continue",
        "Session",
        "SessionEntry",
        "SessionStorage",
        "InMemorySessionStorage",
        "JsonlSessionStorage",
        "load_skills",
        "compact",
        "calculate_context_tokens",
    ):
        assert hasattr(pi_agent_core, name), f"根包缺少导出: {name}"


def test_new_agent_api_is_exported() -> None:
    """v1.3.0 新增类型可从根包导入。"""
    from pi_agent_core import (  # noqa: F401
        AgentRequestUpdate,
        AgentTurnContext,
        AgentTurnDecision,
        FinishTurn,
        PrepareRequestContext,
        RequestThinkingLevel,
    )
    from pi_agent_core.types import AgentTurnContext as _AgentTurnContext

    assert _AgentTurnContext is AgentTurnContext
    assert AgentTurnDecision is not None and FinishTurn is not None
    assert RequestThinkingLevel is not None and AgentRequestUpdate is not None
    assert PrepareRequestContext is not None
