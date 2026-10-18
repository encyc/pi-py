"""transcript 归一化与 system message 回放测试。

用例逐条对应上游 ``test/system-message-replay.test.ts``（v1.3.0），
外加本端 v1.3.0 同步的骨架断言（M-1 / M-2）。
"""

from __future__ import annotations

from pi_ai.text import get_system_message_text, render_system_message_update
from pi_ai.transcript import (
    collapse_system_messages,
    declarations_equal,
    get_current_system_message,
    get_current_system_prompt,
    get_current_tools,
    get_initial_system_message,
    get_tool_state_changes,
    has_non_additive_tool_changes,
    has_tool_redefinitions,
    normalize_context,
    resolve_transcript,
    resolve_transcript_tools,
    without_initial_system_message,
)
from pi_ai.types import (
    AssistantMessage,
    Context,
    SystemMessage,
    TextContent,
    Tool,
    ToolReference,
    TranscriptContext,
    UserMessage,
)


def tool(name: str, description: str | None = None) -> Tool:
    return Tool(name=name, description=description or f"{name} tool", parameters={})


def _transcript() -> TranscriptContext:
    """上游测试用的同一条 transcript（含中途 system 消息与工具增删）。"""
    return normalize_context(
        Context(
            messages=[
                SystemMessage(
                    content="base",
                    sections={"a": "<a>1</a>", "b": "<b>1</b>"},
                    tools_added=[tool("first")],
                    timestamp=10,
                ),
                UserMessage(content="hello", timestamp=11),
                SystemMessage(content="also do this", timestamp=12),
                AssistantMessage(content=[TextContent(text="ok")], timestamp=13),
                SystemMessage(
                    content="",
                    sections={"a": "<a>2</a>", "b": None, "c": "<c>1</c>"},
                    tools_removed=[ToolReference(name="first")],
                    tools_added=[tool("second")],
                    timestamp=14,
                ),
            ]
        )
    )


# ============================================================
# 回放
# ============================================================


def test_replays_content_sections_and_tools_into_one_leading_message():
    transcript = _transcript()
    current = get_current_system_message(transcript.messages)
    assert current is not None
    assert current.role == "system"
    assert current.content == "base\n\nalso do this"
    assert current.sections == {"a": "<a>2</a>", "c": "<c>1</c>"}
    assert [t.name for t in current.tools_added or []] == ["second"]
    assert current.timestamp == 10
    assert (
        get_current_system_prompt(transcript.messages)
        == "base\n\nalso do this\n\n<a>2</a>\n\n<c>1</c>"
    )


def test_collapse_keeps_only_non_system_messages_after_the_replayed_head():
    transcript = _transcript()
    collapsed = collapse_system_messages(transcript)
    assert [m.role for m in collapsed.messages] == ["system", "user", "assistant"]
    # 幂等
    assert collapse_system_messages(collapsed).messages == collapsed.messages


def test_replay_of_transcript_without_system_messages_is_empty():
    context = normalize_context(Context(messages=[UserMessage(content="hi", timestamp=1)]))
    assert get_current_system_message(context.messages) is None
    assert get_current_system_prompt(context.messages) == ""
    assert collapse_system_messages(context).messages == context.messages


def test_late_full_patch_without_leading_message_replays_as_prompt():
    context = normalize_context(
        Context(
            messages=[
                UserMessage(content="old session", timestamp=1),
                SystemMessage(
                    content="",
                    sections={"preamble": "You are pi."},
                    tools_added=[tool("x")],
                    timestamp=2,
                ),
            ]
        )
    )
    assert get_current_system_prompt(context.messages) == "You are pi."
    head = collapse_system_messages(context).messages[0]
    assert isinstance(head, SystemMessage)
    assert [t.name for t in head.tools_added or []] == ["x"]


def test_renders_complete_prompts_and_framed_updates():
    transcript = _transcript()
    leading = transcript.messages[0]
    update = transcript.messages[4]
    assert isinstance(leading, SystemMessage)
    assert isinstance(update, SystemMessage)
    assert get_system_message_text(leading) == "base\n\n<a>1</a>\n\n<b>1</b>"
    assert render_system_message_update(update) == "\n\n".join(
        [
            'Updated system prompt section "a":\n\n<a>2</a>',
            'Removed system prompt section "b".',
            'Updated system prompt section "c":\n\n<c>1</c>',
        ]
    )


# ============================================================
# 归一化（M-1）
# ============================================================


def test_normalizes_legacy_prompt_and_tool_fields_into_leading_system_message():
    messages = [UserMessage(content="hi", timestamp=1)]
    # 两者皆空 -> 不插入 system 消息
    assert normalize_context(Context(messages=list(messages))).messages == messages
    assert (
        normalize_context(Context(system_prompt="", tools=[], messages=list(messages))).messages
        == messages
    )
    # 有 prompt 或 tools -> 折进首条 system message（timestamp 固定 0）
    normalized = normalize_context(
        Context(system_prompt="be brief", tools=[tool("a")], messages=list(messages))
    ).messages
    assert len(normalized) == 2
    head = normalized[0]
    assert isinstance(head, SystemMessage)
    assert head.content == "be brief"
    assert head.timestamp == 0
    assert [t.name for t in head.tools_added or []] == ["a"]


def test_normalized_context_is_a_transcript_context():
    ctx = normalize_context(Context(system_prompt="x", messages=[]))
    assert isinstance(ctx, TranscriptContext)


def test_initial_system_message_helpers():
    with_system = normalize_context(
        Context(system_prompt="p", messages=[UserMessage(content="u")])
    ).messages
    assert get_initial_system_message(with_system) is not None
    assert len(without_initial_system_message(with_system)) == 1
    without = normalize_context(Context(messages=[UserMessage(content="u")])).messages
    assert get_initial_system_message(without) is None
    # 无首条 system 消息时原样返回
    assert without_initial_system_message(without) == without


# ============================================================
# 工具集解析
# ============================================================


def test_get_current_tools_applies_removals_then_additions():
    transcript = _transcript()
    assert [t.name for t in get_current_tools(transcript.messages)] == ["second"]


def test_resolve_transcript_tools_anchors_only_additive_history():
    transcript = _transcript()
    # 历史含 toolsRemoved -> 不能原位锚定，发送当前完整工具集
    resolved = resolve_transcript_tools(transcript.messages, True)
    assert resolved.anchors_additions is False
    assert [t.name for t in resolved.request_tools] == ["second"]
    # 不支持原位追加时同样发送完整集
    assert resolve_transcript_tools(transcript.messages, False).anchors_additions is False

    additive = normalize_context(
        Context(
            messages=[
                SystemMessage(content="", tools_added=[tool("a")], timestamp=1),
                SystemMessage(content="", tools_added=[tool("b")], timestamp=2),
            ]
        )
    )
    anchored = resolve_transcript_tools(additive.messages, True)
    assert anchored.anchors_additions is True
    assert [t.name for t in anchored.request_tools] == ["a"]


def test_resolve_transcript_collapses_for_non_supporting_models():
    transcript = _transcript()
    kept = resolve_transcript(transcript, True)
    assert kept is transcript
    collapsed = resolve_transcript(transcript, False)
    assert [m.role for m in collapsed.messages] == ["system", "user", "assistant"]


# ============================================================
# 声明比较 / 工具状态增量
# ============================================================


def test_compares_tool_declarations():
    assert declarations_equal(tool("a"), tool("a")) is True
    assert declarations_equal(tool("a"), tool("a", "changed")) is False
    # constrained_sampling 参与比较
    a = tool("a")
    a_cs = tool("a")
    a_cs.constrained_sampling = False
    assert declarations_equal(a, a_cs) is False


def test_tool_state_changes_treat_changed_definitions_as_removal_plus_addition():
    changes = get_tool_state_changes([tool("a"), tool("b")], [tool("b", "changed"), tool("c")])
    assert [t.name for t in changes.tools_added] == ["b", "c"]
    assert [(t.name, t.description) for t in changes.tools_added] == [
        ("b", "changed"),
        ("c", "c tool"),
    ]
    assert [t.name for t in changes.tools_removed] == ["a", "b"]
    assert get_tool_state_changes([tool("a")], [tool("a")]).tools_added == []
    assert get_tool_state_changes([tool("a")], [tool("a")]).tools_removed == []


def test_detects_non_additive_tool_history_and_redefinitions():
    transcript = _transcript()
    assert has_non_additive_tool_changes(transcript.messages) is True
    assert has_tool_redefinitions(transcript.messages) is False

    additive = normalize_context(
        Context(
            messages=[
                SystemMessage(content="", tools_added=[tool("a")], timestamp=1),
                SystemMessage(content="", tools_added=[tool("b")], timestamp=2),
            ]
        )
    )
    assert has_non_additive_tool_changes(additive.messages) is False

    redeclared = normalize_context(
        Context(
            messages=[
                SystemMessage(content="", tools_added=[tool("a")], timestamp=1),
                SystemMessage(content="", tools_added=[tool("a", "changed")], timestamp=2),
            ]
        )
    )
    assert has_non_additive_tool_changes(redeclared.messages) is True
    assert has_tool_redefinitions(redeclared.messages) is True
