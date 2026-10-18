"""``harness/compaction.py`` 的 **Slice A**（叶子层）测试 —— 移植 ``harness/compaction.ts``
的纯函数部分（约 210 行）。

⚠️ 按**可测性**切：``CompactionTask`` 的三个 phase 都收 ``TaskRuntime``，
而它的实现在 `scheduler.py`。本片不碰它。
"""

from __future__ import annotations

from typing import Any

import pytest

from pi_ai import AssistantMessage, TextContent, ToolCall, ToolResultMessage, Usage, UserMessage
from pi_durable.harness.compaction import (
    SUMMARIZATION_SYSTEM_PROMPT,
    SUMMARY_PREFIX,
    SUMMARY_SUFFIX,
    TOOL_RESULT_MAX_CHARS,
    content_text,
    estimate_context,
    is_candidate,
    select_cut,
    serialize_conversation,
    summarized_messages,
    summary_failure,
    summary_prompt,
    summary_text,
    truncate,
)


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def assistant(
    text: str = "",
    *,
    calls: list[dict[str, Any]] | None = None,
    stop: str = "stop",
    usage: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend(calls or [])
    message: dict[str, Any] = {"role": "assistant", "content": content, "stop_reason": stop}
    if usage is not None:
        message["usage"] = usage
    if error is not None:
        message["error_message"] = error
    return message


def tool_call(id: str, name: str = "read") -> dict[str, Any]:
    return {"type": "toolCall", "id": id, "name": name, "arguments": {}}


def tool_result(call_id: str, text: str = "ok") -> dict[str, Any]:
    return {
        "role": "toolResult",
        "tool_call_id": call_id,
        "tool_name": "read",
        "content": [{"type": "text", "text": text}],
    }


def view(
    contributions: list[list[dict[str, Any]]],
    *,
    messages: list[dict[str, Any]] | None = None,
    head: dict[str, Any] | None = None,
    entries: list[dict[str, Any]] | None = None,
) -> Any:
    return {
        "head": head,
        "entries": entries
        if entries is not None
        else [{"id": i + 1} for i in range(len(contributions))],
        "contributions": contributions,
        "messages": messages if messages is not None else [m for b in contributions for m in b],
    }


# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------


def test_summary_wrapper_constants() -> None:
    assert SUMMARY_PREFIX.endswith("<summary>\n")
    assert SUMMARY_SUFFIX == "\n</summary>"
    assert "Do NOT continue the conversation" in SUMMARIZATION_SYSTEM_PROMPT
    assert TOOL_RESULT_MAX_CHARS == 2000


# --------------------------------------------------------------------------------------
# truncate / content_text
# --------------------------------------------------------------------------------------


def test_truncate_is_a_noop_below_the_limit() -> None:
    assert truncate("abc", 10) == "abc"
    assert truncate("abc", 3) == "abc"


def test_truncate_reports_how_much_was_dropped() -> None:
    assert truncate("abcdef", 3) == "abc\n\n[... 3 more characters truncated]"


def test_content_text_accepts_a_string_or_blocks() -> None:
    assert content_text("plain") == "plain"
    assert content_text([{"type": "text", "text": "a"}, {"type": "image"}]) == "a"
    assert content_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"


# --------------------------------------------------------------------------------------
# serialize_conversation
# --------------------------------------------------------------------------------------


def test_serialize_omits_system_messages_and_thinking() -> None:
    """⚠️ **系统消息省略**；**思考也省略**——提供方返回的是真实推理的摘要，
    而重放的思考常常不属于活上下文，带上它会让请求比它要压缩的上下文还大。"""
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "sys"}]},
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "thinking", "text": "hmm"}]},
    ]
    assert serialize_conversation(messages) == "[User]: hi"


def test_serialize_renders_tool_calls_with_json_arguments() -> None:
    """⚠️ 参数用 ``JSON.stringify`` 的语义渲染——**不是** Python 的 ``str()``：
    ``True`` 要写成 ``true``、``None`` 写成 ``null``。"""
    messages = [
        assistant(
            calls=[
                {
                    "type": "toolCall",
                    "id": "c",
                    "name": "read",
                    "arguments": {"a": True, "b": None, "c": "x"},
                }
            ]
        )
    ]
    assert serialize_conversation(messages) == '[Assistant tool calls]: read(a=true, b=null, c="x")'


def test_serialize_joins_parts_with_blank_lines() -> None:
    messages = [user("hi"), assistant("hello"), tool_result("c", "out")]
    assert (
        serialize_conversation(messages) == "[User]: hi\n\n[Assistant]: hello\n\n[Tool result]: out"
    )


def test_serialize_truncates_tool_results() -> None:
    long = "x" * (TOOL_RESULT_MAX_CHARS + 50)
    rendered = serialize_conversation([tool_result("c", long)])
    assert "more characters truncated" in rendered


def test_serialize_skips_empty_messages() -> None:
    assert serialize_conversation([user(""), assistant("")]) == ""


# --------------------------------------------------------------------------------------
# summary_text / summary_failure
# --------------------------------------------------------------------------------------


def test_summary_text_requires_a_clean_stop_with_text() -> None:
    assert summary_text(assistant("the summary")) == "the summary"
    assert summary_text(assistant("  padded  ")) == "padded"


def test_summary_text_rejects_a_tool_call() -> None:
    assert summary_text(assistant("text", calls=[tool_call("c")])) is None


def test_summary_text_rejects_an_empty_summary() -> None:
    assert summary_text(assistant("   ")) is None


def test_summary_text_rejects_a_non_stop_reason() -> None:
    assert summary_text(assistant("text", stop="length")) is None


@pytest.mark.parametrize(
    ("message", "phrase"),
    [
        (assistant(stop="error", error="boom"), "Summarization failed: boom"),
        (assistant(stop="aborted"), "Summarization failed: aborted"),
        (assistant(stop="length"), "hit the token limit"),
        (assistant(calls=[tool_call("c")]), "attempted to call a tool"),
        (assistant(""), "produced no text"),
    ],
)
def test_summary_failure_messages(message: dict[str, Any], phrase: str) -> None:
    assert phrase in summary_failure(message)


# --------------------------------------------------------------------------------------
# summary_prompt
# --------------------------------------------------------------------------------------


def test_summary_prompt_wraps_the_conversation_and_appends_focus() -> None:
    prompt = summary_prompt([user("hi")], "be brief")
    assert prompt.startswith("<conversation>\n[User]: hi\n</conversation>")
    assert prompt.endswith("Additional focus: be brief")


def test_summary_prompt_without_instructions_has_no_focus_line() -> None:
    assert "Additional focus" not in summary_prompt([user("hi")], None)


# --------------------------------------------------------------------------------------
# is_candidate / select_cut
# --------------------------------------------------------------------------------------


def test_a_block_starting_with_an_assistant_is_a_candidate() -> None:
    contributions = [[user("a")], [assistant("b")]]
    assert is_candidate(contributions, 1) is True


def test_a_block_starting_with_something_else_is_not_a_candidate() -> None:
    contributions = [[tool_result("c")]]
    assert is_candidate(contributions, 0) is False


def test_a_user_block_is_a_candidate_when_the_previous_assistant_made_no_calls() -> None:
    contributions = [[assistant("a")], [user("b")]]
    assert is_candidate(contributions, 1) is True


def test_a_user_block_is_not_a_candidate_when_its_result_would_be_orphaned() -> None:
    """⚠️ 前面那条助手的调用的**结果**在这个条目**之后、下一条助手之前**——
    那些结果属于**切点之前**，所以这个条目**不能**切（否则工具结果会被留下但它的调用被切掉）。"""
    contributions = [
        [assistant(calls=[tool_call("c1")])],
        [user("mid")],
        [tool_result("c1")],  # 下一条助手之前的结果
    ]
    assert is_candidate(contributions, 1) is False


def test_select_cut_returns_none_without_enough_recent_tokens() -> None:
    contributions = [[user("short")], [assistant("short")]]
    assert select_cut(view(contributions), 10_000_000) is None


def test_select_cut_needs_something_before_the_cut() -> None:
    """切点**之前**至少要有一个非空条目，否则没有东西可压。

    构造：唯一能当切点的是下标 0（下标 1 以 ``toolResult`` 开头，**不是**候选），
    于是 ``cut`` 落在 0，而「之前」是空区间 → 返回 ``None``。
    """
    contributions = [[assistant(calls=[tool_call("c1")])], [tool_result("c1")]]
    assert is_candidate(contributions, 0) is True
    assert is_candidate(contributions, 1) is False
    assert select_cut(view(contributions), 0) is None


def test_select_cut_picks_a_candidate_at_or_after_the_keep_boundary() -> None:
    contributions = [
        [user("x" * 400)],
        [assistant("y" * 400)],
        [user("z" * 400)],
        [assistant("w" * 400)],
    ]
    cut = select_cut(view(contributions), 1)
    assert cut is not None
    assert 0 < cut < len(contributions)
    assert is_candidate(contributions, cut)


def test_select_cut_starts_after_the_head_marker() -> None:
    head = {"id": 99}
    contributions = [[user("x" * 400)], [assistant("y" * 400)], [assistant("z" * 400)]]
    # head 在时 start=1，所以下标 0 永远不是切点
    cut = select_cut(view(contributions, head=head), 0)
    assert cut != 0


# --------------------------------------------------------------------------------------
# summarized_messages
# --------------------------------------------------------------------------------------


def test_summarized_messages_orders_tool_results() -> None:
    """切点之前的消息按**模型上下文的顺序**排（工具结果紧跟它的调用）。"""
    contributions = [
        [assistant(calls=[tool_call("c1")])],
        [tool_result("c1")],
        [user("kept")],
    ]
    messages = summarized_messages(view(contributions), 2)
    assert [m["role"] for m in messages] == ["assistant", "toolResult"]


# --------------------------------------------------------------------------------------
# estimate_context
# --------------------------------------------------------------------------------------


def test_estimate_context_uses_the_newest_measured_assistant_usage() -> None:
    """⚠️ 提供方只对**已经发出过**的请求报用量，所以要用**最新那条有 usages 的助手**做基准。"""
    measured = assistant("a", usage={"input": 100, "output": 0, "total_tokens": 100})
    contributions = [[user("u" * 50)], [measured]]
    counted = estimate_context(
        view(contributions, messages=[user("u"), measured, user("tail")]), []
    )
    assert counted > 0


def test_estimate_context_includes_the_extra_messages() -> None:
    contributions = [[assistant("a")]]
    base = estimate_context(view(contributions, messages=[assistant("a")]), [])
    extra = [user("x" * 400)]
    assert estimate_context(view(contributions, messages=[assistant("a")]), extra) > base


def test_estimate_context_ignores_entries_at_or_before_the_head_marker() -> None:
    old = assistant("old", usage={"input": 999, "output": 0, "total_tokens": 999})
    head = {"id": 2}
    contributions = [[user("a")], [old], [assistant("new")]]
    entries = [{"id": 1}, {"id": 2}, {"id": 3}]
    counted = estimate_context(view(contributions, head=head, entries=entries), [])
    # 旧那条在 head 标记处或之前，所以它的 999 不该被当基准
    assert counted < 999
