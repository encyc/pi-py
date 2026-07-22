"""session 会话存储测试：内存/JSONL、分支回溯、上下文构建、compaction、fork。"""

from __future__ import annotations

from pi_agent_core import InMemorySessionStorage, JsonlSessionStorage, Session
from pi_ai import AssistantMessage, TextContent, ToolResultMessage, UserMessage


def _user(text: str) -> UserMessage:
    return UserMessage(content=text, timestamp=0)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="faux",
        provider="faux",
        model="faux",
        timestamp=0,
    )


# ============================================================
# 内存存储
# ============================================================


def test_memory_append_and_branch():
    """追加消息 → 分支回溯。"""
    storage = InMemorySessionStorage()
    session = Session(storage)
    session.append_message(_user("a"))
    session.append_message(_assistant("b"))
    session.append_message(_user("c"))

    branch = session.get_branch()
    assert len(branch) == 3
    assert branch[0].data.content == "a" or branch[0].data.content[0].text == "a"


def test_memory_build_context():
    """build_context 提取消息序列。"""
    session = Session(InMemorySessionStorage())
    session.append_message(_user("hello"))
    session.append_message(_assistant("hi"))
    session.append_message(_user("bye"))

    ctx = session.build_context()
    assert len(ctx) == 3
    assert isinstance(ctx[0], UserMessage)
    assert isinstance(ctx[1], AssistantMessage)


def test_memory_label():
    storage = InMemorySessionStorage()
    session = Session(storage)
    session.append_message(_user("x"))
    entry = session.append_label("important")
    assert entry.type == "label"
    # label 不进 build_context
    ctx = session.build_context()
    assert len(ctx) == 1


def test_memory_thinking_level_change():
    """thinking_level_change 不进消息序列。"""
    session = Session(InMemorySessionStorage())
    session.append_message(_user("x"))
    session.append_thinking_level_change("high")
    session.append_message(_assistant("y"))

    ctx = session.build_context()
    assert len(ctx) == 2  # 只有两条消息


# ============================================================
# compaction
# ============================================================


def test_compaction_in_context():
    """compaction：summary + retained_tail 进上下文。"""
    session = Session(InMemorySessionStorage())
    session.append_message(_user("old1"))
    session.append_message(_assistant("old2"))
    # 在此处压缩
    session.append_compaction(
        summary="之前讨论了 X",
        retained_tail=[_user("recent1"), _assistant("recent2")],
    )
    session.append_message(_user("new"))

    ctx = session.build_context()
    # summary(1) + retained_tail(2) + new(1) = 4
    assert len(ctx) == 4
    # 第一条应是 summary
    assert "讨论了 X" in str(ctx[0].content)


def test_branch_stops_at_compaction():
    """get_branch 在 compaction 处停止回溯。"""
    session = Session(InMemorySessionStorage())
    session.append_message(_user("old1"))
    session.append_message(_assistant("old2"))
    session.append_compaction(summary="sum", retained_tail=[])
    session.append_message(_user("new"))

    branch = session.get_branch()
    # compaction + new = 2（不含 compaction 之前的）
    assert len(branch) == 2
    assert branch[0].type == "compaction"
    assert branch[1].type == "message"


# ============================================================
# fork
# ============================================================


def test_fork_independent():
    """fork 后两个会话独立演进。"""
    session = Session(InMemorySessionStorage())
    session.append_message(_user("shared"))
    session.append_message(_assistant("resp"))

    forked = session.fork()
    # 各自追加不同消息
    session.append_message(_user("original path"))
    forked.append_message(_user("forked path"))

    orig_ctx = session.build_context()
    fork_ctx = forked.build_context()
    assert "original path" in str(orig_ctx[-1].content)
    assert "forked path" in str(fork_ctx[-1].content)
    assert "original path" not in str(fork_ctx[-1].content)


# ============================================================
# JSONL 持久化
# ============================================================


def test_jsonl_persist_and_reload(tmp_path):
    """JSONL：写入后重新加载，数据一致。"""
    path = tmp_path / "session.jsonl"
    storage = JsonlSessionStorage(path, metadata={"name": "test"})
    session = Session(storage)
    session.append_message(_user("hello"))
    session.append_message(_assistant("world"))
    session.append_label("milestone")

    # 重新加载
    storage2 = JsonlSessionStorage(path)
    session2 = Session(storage2)
    assert storage2.get_metadata()["name"] == "test"
    assert storage2.get_label() == "milestone"

    ctx = session2.build_context()
    assert len(ctx) == 2
    assert isinstance(ctx[0], UserMessage)
    assert isinstance(ctx[1], AssistantMessage)
    # 消息内容保持
    assert "hello" in str(ctx[0].content)
    assert "world" in str(ctx[1].content[0].text)


def test_jsonl_compaction_persist(tmp_path):
    """JSONL：compaction 持久化与恢复。"""
    path = tmp_path / "session.jsonl"
    storage = JsonlSessionStorage(path)
    session = Session(storage)
    session.append_message(_user("old"))
    session.append_compaction(summary="压缩了", retained_tail=[_assistant("kept")])

    storage2 = JsonlSessionStorage(path)
    session2 = Session(storage2)
    ctx = session2.build_context()
    # summary + retained_tail = 2
    assert len(ctx) == 2
    assert "压缩了" in str(ctx[0].content)


def test_jsonl_move_to_after_reload(tmp_path):
    """JSONL：重载后 move_to 切换分支。"""
    path = tmp_path / "session.jsonl"
    storage = JsonlSessionStorage(path)
    session = Session(storage)
    session.append_message(_user("a"))
    branch_point = session.leaf_id
    session.append_message(_assistant("b"))

    # 重载
    storage2 = JsonlSessionStorage(path)
    session2 = Session(storage2)
    # move 回分支点
    session2.move_to(branch_point)
    ctx = session2.build_context()
    assert len(ctx) == 1  # 只有 a，不含 b


def test_jsonl_roundtrip_tool_result(tmp_path):
    """JSONL：ToolResultMessage 往返。"""
    path = tmp_path / "session.jsonl"
    storage = JsonlSessionStorage(path)
    session = Session(storage)
    session.append_message(_user("calc"))
    session.append_message(
        AssistantMessage(
            content=[
                TextContent(text="calling"),
                __import__("pi_ai").ToolCall(id="tc1", name="calc", arguments={}),
            ],
            api="faux",
            provider="faux",
            model="faux",
        )
    )
    session.append_message(
        ToolResultMessage(tool_call_id="tc1", tool_name="calc", content=[TextContent(text="42")])
    )

    storage2 = JsonlSessionStorage(path)
    session2 = Session(storage2)
    ctx = session2.build_context()
    assert len(ctx) == 3
    assert isinstance(ctx[2], ToolResultMessage)
    assert ctx[2].content[0].text == "42"
