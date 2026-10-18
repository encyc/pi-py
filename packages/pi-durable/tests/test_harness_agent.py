"""``harness/agent.py`` 的测试 —— 移植 ``harness/agent.ts``（314 行）。

三块：
1. **纯函数**：``resolve_settings``（含**展开 vs 逐字段 ``??`` 的不对称**）、``resolve_agent``
   的四个助手（选择扩展 / 过滤工具 / 提供工具 / 包装器）；
2. **三态写入**：``apply_change``、``configure``、``add_tools``（**R29**）；
3. **``AgentDoc`` 的分叉语义**：``fork: "asOf"``——**与同族三个文档相反**，单独钉住。

接存储的部分内存与真实 SQLite 各跑一遍。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.harness.agent import (
    DEFAULT_COMPACTION_POLICY,
    DEFAULT_CONTEXT_RETENTION_MS,
    DEFAULT_PROGRESS_POLICY,
    DEFAULT_RETRY_POLICY,
    INSTRUCTIONS_KEY,
    AgentDoc,
    add_tools,
    agent_hooks,
    apply_change,
    configure,
    create_agent,
    resolve_agent,
    resolve_settings,
)
from pi_durable.harness.types import Agent, Extension, ToolRegistration
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage


def make_tool(name: str, **kwargs: Any) -> ToolRegistration[Any]:
    fields: dict[str, Any] = {
        "name": name,
        "description": f"{name} tool",
        "parameters": {"type": "object"},
        "execute": lambda *_: None,
    }
    fields.update(kwargs)  # 调用方可以覆盖任意字段
    return ToolRegistration(**fields)


class FakeSnapshot:
    """``RegistrySnapshot`` 的最小实现（6 个方法全在，因为协议是完整形状）。"""

    def __init__(self, extensions: list[Extension[Any]]) -> None:
        self._extensions = extensions

    def installed(self) -> list[Extension[Any]]:
        return list(self._extensions)

    def extension(self, name: str) -> Extension[Any] | None:
        for extension in self._extensions:
            if extension.name == name:
                return extension
        return None

    def tools(self) -> list[Any]:
        return [
            {"extension": extension, "tool": tool}
            for extension in self._extensions
            for tool in extension.tools
        ]

    def sections(self) -> list[Any]:
        return [
            {"extension": extension, "section": section}
            for extension in self._extensions
            for section in extension.sections
        ]

    def tasks(self) -> list[Any]:
        return [task for extension in self._extensions for task in extension.tasks]

    def task(self, name: str) -> Any:
        for task in self.tasks():
            if task["definition"]["name"] == name:
                return task
        return None


async def make_session(backend: str, tmp_path: Path) -> SessionImpl:
    storage = (
        MemoryStorage()
        if backend == "memory"
        else await open_node_sqlite_storage(str(tmp_path / "durable.db"))
    )
    session = SessionImpl(storage)
    await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    return session


# --------------------------------------------------------------------------------------
# 1a. resolve_settings
# --------------------------------------------------------------------------------------


def test_resolve_settings_defaults() -> None:
    settings = resolve_settings(None)
    assert settings["retry"] == DEFAULT_RETRY_POLICY
    assert settings["compaction"] == DEFAULT_COMPACTION_POLICY
    assert settings["progress"] == DEFAULT_PROGRESS_POLICY
    assert settings["toolExecution"] == "parallel"
    assert settings["steeringMode"] == "one-at-a-time"
    assert settings["followUpMode"] == "one-at-a-time"
    assert settings["contextRetentionMs"] == DEFAULT_CONTEXT_RETENTION_MS
    # 宿主没给 extensions 时，**不写这个键**（而不是写一个 None）——见 R29
    assert "extensions" not in settings


def test_resolve_settings_spread_vs_nullish_asymmetry() -> None:
    """⚠️ **上游有意的不对称**（``agent.ts:63-71``）：

    * ``retry`` / ``compaction`` 用**展开**——显式给出的 ``None`` **会覆盖**默认；
    * ``progress`` 用**逐字段 ``??``**——显式给出的 ``None`` **保留**默认。
    """
    settings = resolve_settings(
        {"retry": {"maxRetries": None}, "progress": {"partialIntervalMs": None}}
    )
    # 展开：None 覆盖了默认值
    assert settings["retry"]["maxRetries"] is None
    # ?? ：None 保留默认值
    assert settings["progress"]["partialIntervalMs"] == DEFAULT_PROGRESS_POLICY["partialIntervalMs"]


def test_resolve_settings_merges_partial_policies() -> None:
    settings = resolve_settings({"retry": {"maxRetries": 9}, "compaction": {"enabled": False}})
    assert settings["retry"]["maxRetries"] == 9
    assert settings["retry"]["baseDelayMs"] == DEFAULT_RETRY_POLICY["baseDelayMs"]  # 其余保留
    assert settings["compaction"]["enabled"] is False
    assert settings["compaction"]["reserveTokens"] == DEFAULT_COMPACTION_POLICY["reserveTokens"]


def test_resolve_settings_uses_document_retention_rules() -> None:
    """``documentRetentionMs`` 走**同一个** ``document_retention()`` 合法化：

    ``0`` / ``inf`` 有效，负数与非数字退回默认。
    """
    assert resolve_settings({"documentRetentionMs": 0})["documentRetentionMs"] == 0
    assert resolve_settings({"documentRetentionMs": 5})["documentRetentionMs"] == 5
    assert resolve_settings({"documentRetentionMs": -1})["documentRetentionMs"] == 600_000
    assert resolve_settings({"documentRetentionMs": "x"})["documentRetentionMs"] == 600_000  # type: ignore[typeddict-item]


def test_resolve_settings_keeps_the_host_extensions() -> None:
    extension = Extension(name="demo")
    settings = resolve_settings({"extensions": [extension]})
    assert settings["extensions"] == [extension]


# --------------------------------------------------------------------------------------
# 1b. AgentDoc 的分叉语义（Lead 点名要单测）
# --------------------------------------------------------------------------------------


def test_agent_doc_forks_from_the_fork_point_not_from_empty() -> None:
    """⚠️ **``fork: "asOf"``——分叉从「分叉点的 agent 状态」开始**。

    这与**同族另外三个文档恰好相反**：

    ==================  =========================  ==========================
    文档                 语义                        分叉时的行为
    ==================  =========================  ==========================
    ``pi.provider``      ``fork: "initial"``        **全新**身份
    ``pi.usage``         ``fork: "initial"``        账本**从零**
    ``pi.inbox``         ``fork: "initial"``        队列**从空**
    **``pi.agent``**     **``fork: "asOf"``**       **沿用**分叉点的 agent
    ==================  =========================  ==========================

    **不要为了「统一」把它改成 ``"initial"``**：子会话本该继承父会话的模型与工具选择，
    否则每次分叉都要重新选一遍。
    """
    definition = AgentDoc.definition
    assert definition["kind"] == "pi.agent"
    assert definition["scope"] == "conversation"
    assert definition["history"] == "rewindable"
    assert definition["fork"] == "asOf"  # ← 不是 "initial"
    assert definition["initial"](None) == {}
    assert definition["checkpointWhen"]({}, [], {"deltasSinceBase": 0}) is True


def test_the_sibling_documents_are_initial_not_asof() -> None:
    """反向对照：同族另外三个都是 ``"initial"``——**这四处不一致是有意的**。"""
    from pi_durable.harness.inbox import InboxDoc
    from pi_durable.harness.provider import ProviderDoc
    from pi_durable.harness.usage import UsageDoc

    for token in (ProviderDoc, UsageDoc, InboxDoc):
        assert token.definition["fork"] == "initial", token.definition["kind"]
    assert AgentDoc.definition["fork"] == "asOf"


# --------------------------------------------------------------------------------------
# 2. 三态写入（R29）
# --------------------------------------------------------------------------------------


def test_apply_change_three_states() -> None:
    """**不设 → 不动；``None`` → 清除；有值 → 替换**（``agent.ts:109-152``）。"""
    state: dict[str, Any] = {"model": {"provider": "p", "modelId": "m"}, "cwd": "/old"}

    # 不设：什么都不改
    apply_change(state, {})
    assert state == {"model": {"provider": "p", "modelId": "m"}, "cwd": "/old"}

    # 有值：替换
    apply_change(state, {"cwd": "/new"})
    assert state["cwd"] == "/new"

    # None：清除
    apply_change(state, {"cwd": None, "model": None})
    assert "cwd" not in state
    assert "model" not in state


def test_apply_change_converts_extensions_and_tools_to_names() -> None:
    """状态里存的是**名字，不是对象**——所以换一个注册表快照重新解析就得到新结果。"""
    state: dict[str, Any] = {}
    extension = Extension(name="ext-a")
    tool = make_tool("read")

    apply_change(state, {"extensions": [extension], "tools": [tool]})
    assert state["extensions"] == ["ext-a"]
    assert state["tools"] == ["read"]

    apply_change(state, {"extensions": {"add": [extension]}, "tools": {"remove": [tool]}})
    assert state["extensions"] == {"add": ["ext-a"]}
    assert state["tools"] == {"remove": ["read"]}


def test_apply_change_model_is_copied() -> None:
    state: dict[str, Any] = {}
    model = {"provider": "p", "modelId": "m"}
    apply_change(state, {"model": model})
    model["modelId"] = "mutated"
    assert state["model"]["modelId"] == "m"  # 上游是 `{...change.model}`


# --------------------------------------------------------------------------------------
# 2b. configure / add_tools（接存储）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_configure_writes_and_clears(backend: str, tmp_path: Path) -> None:
    session = await make_session(backend, tmp_path)
    try:

        async def change(tx: Any) -> None:
            await configure(tx, 1, {"instructions": "be terse", "cwd": "/tmp"})

        await session.commit(change, CTX)
        state = await session.snapshot(AgentDoc, 1, CTX)
        assert state == {"instructions": "be terse", "cwd": "/tmp"}
        del state

        async def clear(tx: Any) -> None:
            await configure(tx, 1, {"cwd": None})

        await session.commit(clear, CTX)
        after = await session.snapshot(AgentDoc, 1, CTX)
        assert after == {"instructions": "be terse"}
    finally:
        await session.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_add_tools_appends_to_an_array(backend: str, tmp_path: Path) -> None:
    session = await make_session(backend, tmp_path)
    try:

        async def change(tx: Any) -> None:
            await configure(
                tx, 1, {"tools": [make_tool("read")], "modelTools": [make_tool("read")]}
            )
            await add_tools(tx, 1, ["grep", "read"])

        await session.commit(change, CTX)
        state = await session.snapshot(AgentDoc, 1, CTX)
        assert state["tools"] == ["read", "grep"]  # 已存在的不重复加
        assert state["modelTools"] == ["read", "grep"]
    finally:
        await session.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_add_tools_removes_from_a_remove_filter(backend: str, tmp_path: Path) -> None:
    session = await make_session(backend, tmp_path)
    try:

        async def change(tx: Any) -> None:
            await configure(tx, 1, {"tools": {"remove": [make_tool("a"), make_tool("b")]}})
            await add_tools(tx, 1, ["a"])

        await session.commit(change, CTX)
        state = await session.snapshot(AgentDoc, 1, CTX)
        assert state["tools"] == {"remove": ["b"]}
    finally:
        await session.close(CTX)


async def test_add_tools_leaves_an_unset_tools_alone() -> None:
    """未设的 ``tools`` 已经启用了每个工具，所以**不写**它。"""
    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]

    async def change(tx: Any) -> None:
        await add_tools(tx, 1, ["read"])

    await session.commit(change, CTX)
    state = await session.snapshot(AgentDoc, 1, CTX)
    assert "tools" not in state
    # modelTools 未设 → 追加到它的 add
    assert state["modelTools"] == {"add": ["read"]}


async def test_add_tools_appends_to_a_model_tools_object() -> None:
    session = await make_session("memory", tmp_path=None)  # type: ignore[arg-type]

    async def change(tx: Any) -> None:
        await configure(
            tx,
            1,
            {"modelTools": {"remove": [make_tool("x")], "add": [make_tool("keep")]}},
        )
        await add_tools(tx, 1, ["x", "loaded"])

    await session.commit(change, CTX)
    state = await session.snapshot(AgentDoc, 1, CTX)
    # ⚠️ 上游的语义：`remove` 里被 addTools 点名的工具会先从 remove 去掉，
    # **然后**因为「不在 add 里」而被追加进 add——也就是**被加载**。
    # 所以 "x" 出现在 add 里是对的，不是 bug。
    assert state["modelTools"] == {"add": ["keep", "x", "loaded"]}


# --------------------------------------------------------------------------------------
# 3. create_agent（三种情形）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_create_agent_skips_a_fork(backend: str, tmp_path: Path) -> None:
    """**分叉什么都不做**——``AgentDoc`` 的 ``fork: "asOf"`` 已经在存储层复制过来了。"""
    session = await make_session(backend, tmp_path)
    try:

        async def change(tx: Any) -> None:
            await create_agent(tx, {"id": 1, "parent": {"conversationId": 9, "at": 1}})

        await session.commit(change, CTX)
        assert await session.snapshot(AgentDoc, 1, CTX) is None  # 一个文档都没建
    finally:
        await session.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_create_agent_starts_an_ownerless_conversation_empty(
    backend: str, tmp_path: Path
) -> None:
    session = await make_session(backend, tmp_path)
    try:

        async def change(tx: Any) -> None:
            await create_agent(tx, {"id": 1})

        await session.commit(change, CTX)
        assert await session.snapshot(AgentDoc, 1, CTX) == {}
    finally:
        await session.close(CTX)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_create_agent_copies_the_owner_conversation(backend: str, tmp_path: Path) -> None:
    """新的**任务拥有**的会话复制它拥有者任务那个会话已存的 agent。"""
    session = await make_session(backend, tmp_path)
    try:
        owner_id = await session.commit(
            lambda tx: tx.create_conversation({"kind": "ownerless"}), CTX
        )

        async def seed(tx: Any) -> None:
            await configure(tx, owner_id["id"], {"instructions": "from owner", "cwd": "/owner"})

        await session.commit(seed, CTX)

        child_id = await session.commit(
            lambda tx: tx.create_conversation({"kind": "ownerless"}), CTX
        )

        async def copy(tx: Any) -> None:
            await create_agent(
                tx,
                {"id": child_id["id"], "owner": {"conversationId": owner_id["id"], "taskId": 1}},
            )

        await session.commit(copy, CTX)
        assert await session.snapshot(AgentDoc, child_id["id"], CTX) == {
            "instructions": "from owner",
            "cwd": "/owner",
        }
    finally:
        await session.close(CTX)


# --------------------------------------------------------------------------------------
# 4. resolve_agent
# --------------------------------------------------------------------------------------


def test_agent_hooks_matches_by_task_name_in_extension_order() -> None:
    first = Extension(name="a", hooks=[{"task": "t1", "handlers": {"h": 1}}])
    second = Extension(
        name="b", hooks=[{"task": "t1", "handlers": {"h": 2}}, {"task": "t2", "handlers": {}}]
    )
    agent = Agent[Any](extensions=[first, second])

    assert agent_hooks(agent, "t1") == [{"h": 1}, {"h": 2}]
    assert agent_hooks(agent, "t2") == [{}]
    assert agent_hooks(agent, "missing") == []


def test_resolve_agent_uses_defaults_from_an_absent_state() -> None:
    settings = resolve_settings(None)
    agent = resolve_agent(None, FakeSnapshot([]), settings, lambda error: None)

    assert agent.model is None
    assert agent.thinking_level == "off"
    assert agent.extensions == []
    assert agent.tools == []
    assert agent.sections == []
    assert agent.instructions is None
    assert agent.cwd is None


def test_resolve_agent_selects_stored_extensions_in_order() -> None:
    settings = resolve_settings(None)
    snapshot = FakeSnapshot([Extension(name="a"), Extension(name="b"), Extension(name="c")])

    agent = resolve_agent({"extensions": ["c", "a"]}, snapshot, settings, lambda e: None)
    assert [extension.name for extension in agent.extensions] == ["c", "a"]


def test_resolve_agent_edits_the_host_default_selection() -> None:
    """对象形状：在**宿主默认选择**（这里是 ``settings.extensions``）上编辑。"""
    a, b, c = Extension(name="a"), Extension(name="b"), Extension(name="c")
    settings = resolve_settings({"extensions": [a, b]})
    snapshot = FakeSnapshot([a, b, c])

    agent = resolve_agent(
        {"extensions": {"add": ["c"], "remove": ["a"]}}, snapshot, settings, lambda e: None
    )
    assert [extension.name for extension in agent.extensions] == ["b", "c"]


def test_resolve_agent_falls_back_to_every_installed_extension() -> None:
    settings = resolve_settings(None)
    snapshot = FakeSnapshot([Extension(name="a"), Extension(name="b")])
    agent = resolve_agent({}, snapshot, settings, lambda e: None)
    assert [extension.name for extension in agent.extensions] == ["a", "b"]


def test_resolve_agent_appends_instructions_as_the_last_section() -> None:
    settings = resolve_settings(None)
    section = {"key": "rules", "render": lambda *_: "r"}
    snapshot = FakeSnapshot([Extension(name="a", sections=[section])])

    agent = resolve_agent({"instructions": "be nice"}, snapshot, settings, lambda e: None)
    assert [s["key"] for s in agent.sections] == ["rules", INSTRUCTIONS_KEY]
    assert agent.instructions == "be nice"


def test_resolve_agent_filters_tools_by_an_array_in_its_order() -> None:
    settings = resolve_settings(None)
    snapshot = FakeSnapshot(
        [Extension(name="a", tools=[make_tool("x"), make_tool("y"), make_tool("z")])]
    )

    agent = resolve_agent({"tools": ["z", "x"]}, snapshot, settings, lambda e: None)
    assert [tool.name for tool in agent.tools] == ["z", "x"]


def test_resolve_agent_filters_tools_by_remove() -> None:
    settings = resolve_settings(None)
    snapshot = FakeSnapshot([Extension(name="a", tools=[make_tool("x"), make_tool("y")])])

    agent = resolve_agent({"tools": {"remove": ["x"]}}, snapshot, settings, lambda e: None)
    assert [tool.name for tool in agent.tools] == ["y"]


def test_resolve_agent_offers_no_deferred_tool_by_default() -> None:
    settings = resolve_settings(None)
    snapshot = FakeSnapshot(
        [Extension(name="a", tools=[make_tool("plain"), make_tool("hidden", deferred=True)])]
    )

    agent = resolve_agent({}, snapshot, settings, lambda e: None)
    assert [tool.name for tool in agent.tools] == ["plain"]
    assert [tool.name for tool in agent.deferred] == ["hidden"]  # 可加载，但还没提供


def test_resolve_agent_offers_a_loaded_deferred_tool_after_the_declared_ones() -> None:
    """已加载的延迟工具**按加载序跟在后面**——所以加载一个就是往声明的工具后面追加。"""
    settings = resolve_settings(None)
    snapshot = FakeSnapshot(
        [
            Extension(
                name="a",
                tools=[
                    make_tool("plain"),
                    make_tool("d1", deferred=True),
                    make_tool("d2", deferred=True),
                ],
            )
        ]
    )

    agent = resolve_agent({"modelTools": {"add": ["d2", "d1"]}}, snapshot, settings, lambda e: None)
    assert [tool.name for tool in agent.tools] == ["plain", "d2", "d1"]
    assert agent.deferred == []


def test_resolve_agent_never_widens_with_model_tools() -> None:
    """``modelTools`` **从不加宽**：它只过滤，别的工具仍然可被嵌套调用。"""
    settings = resolve_settings(None)
    snapshot = FakeSnapshot([Extension(name="a", tools=[make_tool("x"), make_tool("y")])])

    agent = resolve_agent(
        {"tools": ["x"], "modelTools": ["x", "y"]}, snapshot, settings, lambda e: None
    )
    assert [tool.name for tool in agent.tools] == ["x"]
    assert [tool.name for tool in agent.callable] == ["x"]  # y 被 tools 过滤掉了


def test_resolve_agent_splits_callers() -> None:
    settings = resolve_settings(None)
    snapshot = FakeSnapshot(
        [
            Extension(
                name="a",
                tools=[
                    make_tool("both"),
                    make_tool("model-only", callers=["model"]),
                    make_tool("tools-only", callers=["tools"]),
                ],
            )
        ]
    )

    agent = resolve_agent({}, snapshot, settings, lambda e: None)
    assert [tool.name for tool in agent.tools] == ["both", "model-only"]
    assert [tool.name for tool in agent.callable] == ["both", "tools-only"]


def test_resolve_agent_applies_a_tool_wrapper() -> None:
    settings = resolve_settings(None)
    wrapped = make_tool("x", description="wrapped")
    snapshot = FakeSnapshot(
        [
            Extension(
                name="a",
                tools=[make_tool("x")],
                wraps=[{"tool": "x", "wrap": lambda _tool: wrapped}],
            )
        ]
    )

    agent = resolve_agent({}, snapshot, settings, lambda e: None)
    assert agent.tools[0].description == "wrapped"


def test_resolve_agent_drops_a_wrapper_that_renames_its_target() -> None:
    """改名的包装器 → **丢掉目标**并报告。"""
    settings = resolve_settings(None)
    reported: list[Any] = []
    renamed = make_tool("other")
    snapshot = FakeSnapshot(
        [
            Extension(
                name="a", tools=[make_tool("x")], wraps=[{"tool": "x", "wrap": lambda _t: renamed}]
            )
        ]
    )

    agent = resolve_agent({}, snapshot, settings, reported.append)
    assert agent.tools == []  # 目标被丢掉
    assert len(reported) == 1
    assert "renamed" in str(reported[0])


def test_resolve_agent_reports_a_wrapper_that_throws() -> None:
    settings = resolve_settings(None)
    reported: list[Any] = []

    def boom(_tool: Any) -> Any:
        raise RuntimeError("wrap exploded")

    snapshot = FakeSnapshot(
        [Extension(name="a", tools=[make_tool("x")], wraps=[{"tool": "x", "wrap": boom}])]
    )
    agent = resolve_agent({}, snapshot, settings, reported.append)
    assert agent.tools == []
    assert len(reported) == 1
    assert "wrap exploded" in str(reported[0])


def test_resolve_agent_ignores_a_wrapper_without_a_target() -> None:
    """没有目标的包装器**什么都不做**（不报告）。"""
    settings = resolve_settings(None)
    reported: list[Any] = []
    snapshot = FakeSnapshot(
        [
            Extension(
                name="a", tools=[make_tool("x")], wraps=[{"tool": "missing", "wrap": lambda t: t}]
            )
        ]
    )

    agent = resolve_agent({}, snapshot, settings, reported.append)
    assert [tool.name for tool in agent.tools] == ["x"]
    assert reported == []


def test_resolve_agent_wraps_sections_too() -> None:
    settings = resolve_settings(None)
    original = {"key": "rules", "render": lambda *_: "r"}
    replaced = {"key": "rules", "render": lambda *_: "wrapped"}
    snapshot = FakeSnapshot(
        [
            Extension(
                name="a",
                sections=[original],
                wraps=[{"section": "rules", "wrap": lambda _s: replaced}],
            )
        ]
    )

    agent = resolve_agent({}, snapshot, settings, lambda e: None)
    assert agent.sections[0]["render"]() == "wrapped"
