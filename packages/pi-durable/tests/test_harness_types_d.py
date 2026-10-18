"""批次 D 第 b 步的测试：``harness/types.py`` 的**纯数据契约**部分。

本轮只做「值形状」的那一半（提交草稿、任务、嵌套工具结果、压缩、队列模式），
**接口形状**的那一半（`Harness` / `Conversation` / `RegistryReader` / `PromptInput` 等）
依赖尚未建立的 `DocumentReader` / `TaskRuntime`，留到后续步骤。

⚠️ 这些契约是 **C₂ 的 1,325 行工具**坐的那一层。本轮**只新增**符号，
没有改动 `define_tool` / `define_extension` / `ToolRegistration` / `ToolExecutionApi`
的任何语义——`tools/` 与 `tool_search/` 的测试全绿即是证据。
"""

from __future__ import annotations

from typing import get_args, get_type_hints

import pytest

from pi_durable.harness import (
    AgentChange,
    AgentState,
    AnyTask,
    CompactionPolicy,
    CompactionReason,
    CompactionResult,
    ContextView,
    ConversationAbortOptions,
    ConversationRetryPolicy,
    ConversationStreamOptions,
    DocumentReader,
    HarnessSettings,
    InputEntry,
    InputSubmissionDraft,
    NestedToolExecutionResult,
    ProgressPolicy,
    PromptInput,
    PromptSection,
    QueueMode,
    RegistrySnapshot,
    Settings,
    SettledSubmissionRecord,
    SettledTask,
    SubmissionDraft,
    ToolExecutionMode,
)
from pi_durable.harness.types import ToolExecutionApi, ToolRegistration


def test_tool_execution_mode_and_queue_mode_literals() -> None:
    """这两个是**字面量联合**，值必须与上游逐字一致。"""
    assert set(get_args(ToolExecutionMode)) == {"parallel", "sequential"}
    assert set(get_args(QueueMode)) == {"all", "one-at-a-time"}
    assert set(get_args(CompactionReason)) == {"manual", "threshold", "overflow"}


def test_submission_draft_covers_both_branches() -> None:
    """``SubmissionDraft`` 是 input / write 两支的联合，判别字段是 ``type``。"""
    branches = get_args(SubmissionDraft)
    assert len(branches) == 2
    discriminants = {get_args(get_type_hints(branch)["type"])[0] for branch in branches}
    assert discriminants == {"input", "write"}


def test_input_submission_draft_is_the_input_branch() -> None:
    assert InputSubmissionDraft in get_args(SubmissionDraft)


def test_input_draft_fields() -> None:
    hints = get_type_hints(InputSubmissionDraft)
    assert "content" in hints
    assert "whenBusy" in hints
    assert "entry" in hints
    assert set(get_args(hints["whenBusy"])) == {"steer", "followUp", "reject"}


def test_input_entry_describes_an_application_kind() -> None:
    """``InputEntry``：应用自己的 kind + transcript 展示用的 data。"""
    hints = get_type_hints(InputEntry)
    assert set(hints) == {"kind", "data"}


def test_settled_submission_record_is_done_or_unanswered() -> None:
    assert set(get_args(get_type_hints(SettledSubmissionRecord)["status"])) == {
        "done",
        "unanswered",
    }


def test_nested_tool_execution_result_requires_task_id_and_diagnostics() -> None:
    """``taskId`` / ``isError`` / ``diagnostics`` 必填，其余可选。

    ⚠️ 断言走 ``get_type_hints`` 里有没有 ``Required`` 包装，**不能**用
    ``__required_keys__``——本模块有 ``from __future__ import annotations``，
    那个属性会恒为空（见 PORTING.md **R22**）。
    """
    hints = get_type_hints(NestedToolExecutionResult, include_extras=True)
    assert set(hints) == {
        "taskId",
        "isError",
        "diagnostics",
        "structuredOutput",
        "details",
        "usage",
        "durationMs",
    }
    # 必填项在 hints 里是裸类型，可选项被 NotRequired 包裹
    for field in ("taskId", "isError", "diagnostics"):
        assert "NotRequired" not in str(hints[field]), field
    for field in ("structuredOutput", "details", "usage", "durationMs"):
        assert "NotRequired" in str(hints[field]), field  # 显式 NotRequired 包裹


def test_nested_result_shape_is_constructible() -> None:
    result: NestedToolExecutionResult = {
        "taskId": 7,
        "isError": False,
        "diagnostics": [],
        "structuredOutput": {"ok": True},
        "durationMs": 1.5,
    }
    assert result["taskId"] == 7


def test_compaction_result_carries_either_identity() -> None:
    hints = get_type_hints(CompactionResult, include_extras=True)
    assert set(hints) == {"entryId", "submissionId"}
    # 全表 total=False，两个都可以缺；此时 hints 里是裸类型（没有 Required/NotRequired 包装）
    assert all("Required" not in str(hint) for hint in hints.values())


def test_conversation_abort_options_default_to_ordinary_scope() -> None:
    hints = get_type_hints(ConversationAbortOptions, include_extras=True)
    assert set(hints) == {"background"}
    # total=False → 裸类型，即「可以缺」
    assert "Required" not in str(hints["background"])


def test_settled_task_and_any_task_shapes() -> None:
    assert "state" in get_type_hints(SettledTask)
    assert "definition" in get_type_hints(AnyTask)
    # 这两个字段是**必填**（``total=False`` 表里的 ``Required``）
    assert "Required" in str(get_type_hints(AnyTask, include_extras=True)["definition"])
    assert "Required" in str(get_type_hints(SettledTask, include_extras=True)["state"])


# --------------------------------------------------------------------------------------
# C₂ 的接口未被改动
# --------------------------------------------------------------------------------------


def test_c2_interface_symbols_are_unchanged() -> None:
    """本轮**只加符号**，没动 C₂ 坐的那几个。

    以下是 C₂ 实测使用的成员集合；改动它们就要先报告 Lead。
    """
    api_members = {name for name in dir(ToolExecutionApi) if not name.startswith("_")}
    # D-1 明确定为「C₂ 实测用到的 7 个成员」；本轮一个都没动。
    assert api_members == {
        "agent",
        "diagnostic",
        "env",
        "models",
        "output",
        "output_window",
        "retained_output",
    }

    # ⚠️ 字段名是 **snake_case**（本包约定：Python 自己的结构不用 camelCase）
    hints = get_type_hints(ToolRegistration)
    for field in ("parameters", "description", "execute", "prepare_arguments", "execution_mode"):
        assert field in hints, field


@pytest.mark.parametrize(
    "name",
    [
        "define_tool",
        "define_extension",
        "ToolRegistration",
        "ToolExecutionApi",
        "ToolExecutionResult",
    ],
)
def test_c2_symbols_still_importable_from_the_package_root(name: str) -> None:
    import pi_durable.harness as harness

    assert hasattr(harness, name)


def test_r22_future_annotations_defeat_required_keys() -> None:
    """**R22 的回归测试**：``from __future__ import annotations`` 会让
    ``TypedDict.__required_keys__`` **恒为空**（``Required`` 是字符串，运行期不解析）。

    mypy 不受影响（它会求值字符串注解），但**任何运行期检查必填性的代码都会读到错的答案**。
    所以本模块的必填性断言一律走 ``get_type_hints(..., include_extras=True)``。
    """
    assert NestedToolExecutionResult.__required_keys__ == frozenset()  # ← 这就是不对的地方
    assert "NotRequired" not in str(
        get_type_hints(NestedToolExecutionResult, include_extras=True)["taskId"]
    )


# --------------------------------------------------------------------------------------
# ContextView（D-c 的解锁件）
# --------------------------------------------------------------------------------------


def test_context_view_shape() -> None:
    """``context.py`` 需要的那个**纯数据**契约（``types.ts:628-637``）。

    四个字段都是**派生结果**：``head``（没有就是 ``None``）、原始活动条目、
    与条目一一对应的「应用编辑 / 剔除被排除 stop reason 之后、工具结果排序之前」的消息、
    以及下一次请求要用的模型上下文。
    """
    hints = get_type_hints(ContextView, include_extras=True)
    assert set(hints) == {"head", "entries", "contributions", "messages"}
    # 四个都是必填（total=True 的 TypedDict）
    assert ContextView.__required_keys__ == frozenset(
        {"head", "entries", "contributions", "messages"}
    )


def test_context_view_is_constructible_with_no_head() -> None:
    """没有 head 标记时 ``head`` 是 ``None``——这是最常见的初始状态。"""
    view: ContextView = {"head": None, "entries": [], "contributions": [], "messages": []}
    assert view["head"] is None
    assert view["entries"] == []
    assert view["contributions"] == []
    assert view["messages"] == []


def test_context_view_pairs_entries_with_contributions() -> None:
    """``contributions`` 与 ``entries`` **一一对应**——这是消费方依赖的不变量。"""
    entry = {"id": 1, "conversationId": 1, "kind": "pi.user"}
    view: ContextView = {
        "head": None,
        "entries": [entry],
        "contributions": [[{"role": "user", "content": "hi"}]],
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert len(view["contributions"]) == len(view["entries"])


# --------------------------------------------------------------------------------------
# agent 契约（D-c⁴ 的解锁件）
# --------------------------------------------------------------------------------------


def test_agent_state_fields() -> None:
    """未设的字段**跟随宿主**——所以它整表可选（``types.ts:405-428``）。"""
    hints = get_type_hints(AgentState)
    assert set(hints) == {
        "model",
        "thinkingLevel",
        "extensions",
        "tools",
        "modelTools",
        "instructions",
        "cwd",
    }
    # ⚠️ 整表 total=False，**不要**用 __required_keys__ 断言（见 R22）
    assert "Required" not in str(get_type_hints(AgentState, include_extras=True)["cwd"])


def test_agent_change_is_the_three_state_patch() -> None:
    """**给定字段替换、``None`` 清除、不设则不改**——三种状态，所以整表可选。"""
    hints = get_type_hints(AgentChange)
    assert set(hints) == set(get_type_hints(AgentState))


def test_retry_policy_shape() -> None:
    hints = get_type_hints(ConversationRetryPolicy)
    assert {"enabled", "maxRetries", "baseDelayMs"} <= set(hints)
    # ⚠️ 这里**不能**用 `__required_keys__`：`NotRequired` 让注解变成字符串，
    # Python 不在类创建时求值 → 属性不可靠（**R22**）。改用 include_extras 看包装。
    assert "maxAgentDelayMs" in hints
    extra = get_type_hints(ConversationRetryPolicy, include_extras=True)
    for field in ("enabled", "maxRetries", "baseDelayMs"):
        assert "NotRequired" not in str(extra[field]), field
    assert "NotRequired" in str(extra["maxAgentDelayMs"])


def test_compaction_policy_shape() -> None:
    """四个字段都是必填——它是**已解析**的策略，不是宿主设置。"""
    hints = get_type_hints(CompactionPolicy)
    assert set(hints) == {"enabled", "reserveTokens", "keepRecentTokens", "backgroundTokens"}
    assert CompactionPolicy.__required_keys__ == frozenset(
        {"enabled", "reserveTokens", "keepRecentTokens", "backgroundTokens"}
    )


def test_progress_policy_shape() -> None:
    hints = get_type_hints(ProgressPolicy)
    assert set(hints) == {"partialIntervalMs", "outputIntervalMs"}
    assert ProgressPolicy.__required_keys__ == frozenset({"partialIntervalMs", "outputIntervalMs"})


def test_agent_and_policy_contracts_are_constructible() -> None:
    state: AgentState = {"thinkingLevel": "high", "cwd": "/tmp"}
    change: AgentChange = {"model": None, "instructions": "be terse"}
    retry: ConversationRetryPolicy = {"enabled": True, "maxRetries": 3, "baseDelayMs": 100.0}
    compaction: CompactionPolicy = {
        "enabled": True,
        "reserveTokens": 1000,
        "keepRecentTokens": 500,
        "backgroundTokens": 0,
    }
    progress: ProgressPolicy = {"partialIntervalMs": 100.0, "outputIntervalMs": 250.0}
    assert state["thinkingLevel"] == "high"
    # `None` 是**有意义的**：它表示「清除这个字段」，而不是「没设」
    assert change["model"] is None
    assert retry["maxRetries"] == 3
    assert compaction["backgroundTokens"] == 0
    assert progress["outputIntervalMs"] == 250.0


# --------------------------------------------------------------------------------------
# 注册表与提示词契约（R28 的实例：已知形状就按完整形状写）
# --------------------------------------------------------------------------------------


def test_registry_snapshot_has_all_six_methods() -> None:
    """**R28 的实例**：``agent.ts`` 只用 2 个，但完整形状已知，所以按 6 个定义。

    截断成 2 个会得到一个 ``registry.py`` 必然推翻的形状——那才是窄提取原则想避免的。
    """
    members = {name for name in dir(RegistrySnapshot) if not name.startswith("_")}
    assert members == {"installed", "extension", "tools", "sections", "tasks", "task"}


def test_document_reader_is_the_two_session_reads() -> None:
    """``DocumentReader`` = ``Pick<Session, "snapshot" | "snapshotAsOf">``。

    ⚠️ ``snapshot_as_of`` 在 ``SessionImpl`` 上**尚未落地**——那是**实现缺口**，
    不是形状未知（上游两个方法的形状都是已知的）。
    """
    members = {name for name in dir(DocumentReader) if not name.startswith("_")}
    assert members == {"snapshot", "snapshot_as_of"}


def test_prompt_input_fields() -> None:
    hints = get_type_hints(PromptInput)
    assert set(hints) == {"conversationId", "agent", "env", "shown", "read"}
    assert PromptInput.__required_keys__ == frozenset(
        {"conversationId", "agent", "env", "shown", "read"}
    )


def test_prompt_section_fields() -> None:
    hints = get_type_hints(PromptSection, include_extras=True)
    assert set(hints) == {"key", "render", "tag"}
    # key 与 render 必填（显式的 `Required[...]`）；tag 可选（默认 True = 包成 <key>...</key>）
    #
    # ⚠️ `total=False` 的表里，**只有**显式写的 `Required` 会带上包装；
    # 其余字段是「裸类型即为可选」，**没有** `NotRequired` 标记。别指望它对称。
    assert "Required" in str(hints["key"])
    assert "Required" in str(hints["render"])
    assert "Required" not in str(hints["tag"])


# --------------------------------------------------------------------------------------
# Settings 链（R28 的第二个实例）
# --------------------------------------------------------------------------------------


def test_conversation_stream_options_shape() -> None:
    hints = get_type_hints(ConversationStreamOptions)
    assert set(hints) == {
        "transport",
        "timeoutMs",
        "maxRetries",
        "maxRetryDelayMs",
        "headers",
        "metadata",
        "cacheRetention",
        "deferred",
    }
    # ⚠️ `transport` 是上游的**字面量联合**（`ai/src/types.ts:144`）。
    # pi_ai 还没导出 `Transport` 这个名字，所以这里是内联字面量——形状精确，只是没有名字。
    assert set(get_args(hints["transport"])) == {
        "sse",
        "websocket",
        "websocket-cached",
        "auto",
    }


def test_harness_settings_is_fully_optional() -> None:
    """宿主设置**每一项都可以缺**，缺了就用内建默认。"""
    hints = get_type_hints(HarnessSettings)
    assert set(hints) == {
        "extensions",
        "stream",
        "retry",
        "compaction",
        "progress",
        "toolExecution",
        "steeringMode",
        "followUpMode",
        "contextRetentionMs",
        "documentRetentionMs",
    }
    assert HarnessSettings.__required_keys__ == frozenset()


def test_settings_mirrors_harness_settings_fields() -> None:
    """**已解析**设置与宿主设置字段**一一对应**——这是 ``resolve_settings()`` 的契约。"""
    assert set(get_type_hints(Settings)) == set(get_type_hints(HarnessSettings))


def test_settings_and_harness_settings_are_distinguishable() -> None:
    """``Settings`` 的三个 policy 是**已解析**的（必填四字段对象），不是 ``Partial``。"""
    hints = get_type_hints(Settings)
    assert hints["retry"] is ConversationRetryPolicy
    assert hints["compaction"] is CompactionPolicy
    assert hints["progress"] is ProgressPolicy
