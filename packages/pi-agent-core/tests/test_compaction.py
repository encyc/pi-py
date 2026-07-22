"""compaction 上下文压缩测试：token 估算、should_compact、find_cut_point。"""

from __future__ import annotations

from pi_agent_core import (
    CompactionSettings,
    calculate_context_tokens,
    estimate_context_tokens,
    estimate_tokens,
    find_cut_point,
    should_compact,
)
from pi_ai import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)


def _user(text: str) -> UserMessage:
    return UserMessage(content=text, timestamp=0)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], api="x", provider="x", model="x")


# ============================================================
# token 估算
# ============================================================


def test_estimate_tokens_string_content():
    msg = _user("a" * 40)  # 40 chars ≈ 10 tokens
    tokens = estimate_tokens(msg)
    assert tokens >= 10  # 含元数据开销
    assert tokens < 30


def test_estimate_tokens_list_content():
    msg = _assistant("b" * 80)
    tokens = estimate_tokens(msg)
    assert tokens > 0


def test_estimate_tokens_tool_call():
    msg = AssistantMessage(
        content=[ToolCall(id="c1", name="search", arguments={"q": "test query"})],
        api="x",
        provider="x",
        model="x",
    )
    tokens = estimate_tokens(msg)
    assert tokens > 0


def test_estimate_context_tokens():
    msgs = [_user("hello world"), _assistant("hi there")]
    total = estimate_context_tokens(msgs)
    assert total == estimate_tokens(msgs[0]) + estimate_tokens(msgs[1])


def test_calculate_context_tokens():
    """从 usage 提取 context tokens。"""

    class FakeUsage:
        input = 100
        cache_read = 50
        cache_write = 0

    assert calculate_context_tokens(FakeUsage()) == 150
    assert calculate_context_tokens(None) == 0


# ============================================================
# should_compact
# ============================================================


def test_should_compact_below_threshold():
    settings = CompactionSettings(enabled=True)
    assert should_compact(50000, 128000, settings) is False


def test_should_compact_above_threshold():
    settings = CompactionSettings(enabled=True)
    # 80% of 128000 = 102400
    assert should_compact(110000, 128000, settings) is True


def test_should_compact_disabled():
    settings = CompactionSettings(enabled=False)
    assert should_compact(999999, 128000, settings) is False


def test_should_compact_exact_threshold():
    settings = CompactionSettings(enabled=True)
    assert should_compact(102400, 128000, settings) is True


# ============================================================
# find_cut_point
# ============================================================


def test_find_cut_point_basic():
    """keep_recent 容纳最后几条，前面的被切掉。"""
    msgs = [_user(f"message number {i} " * 10) for i in range(10)]
    cut = find_cut_point(msgs, keep_recent_tokens=estimate_tokens(msgs[-1]))
    assert cut < len(msgs)
    assert cut > 0
    # 保留部分
    retained = msgs[cut:]
    assert len(retained) >= 1


def test_find_cut_point_all_recent():
    """keep_recent 足够大：全部保留。"""
    msgs = [_user("short"), _assistant("reply")]
    cut = find_cut_point(msgs, keep_recent_tokens=10000)
    assert cut == 0  # 全部保留


def test_find_cut_point_avoids_tool_result():
    """切割点不落在 toolResult（需配对 assistant）。"""
    msgs = [
        _user("q"),
        AssistantMessage(
            content=[ToolCall(id="c1", name="f", arguments={})],
            api="x",
            provider="x",
            model="x",
        ),
        ToolResultMessage(tool_call_id="c1", tool_name="f", content=[TextContent(text="r")]),
        _assistant("final answer that is long enough " * 5),
    ]
    # 设 keep_recent 让切割点落在 toolResult 附近
    cut = find_cut_point(msgs, keep_recent_tokens=5)
    # 切割点不应在 toolResult 索引（2）上
    assert not (cut < len(msgs) and isinstance(msgs[cut], ToolResultMessage))


def test_find_cut_point_empty():
    assert find_cut_point([], keep_recent_tokens=100) == 0
