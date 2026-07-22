"""SQLite 会话存储测试：repo CRUD + entry 追加 + Session 集成。"""

from __future__ import annotations

import pytest

from pi_agent_core import Session, SessionEntry
from pi_ai import AssistantMessage, TextContent, UserMessage
from pi_storage_sqlite import SqliteSessionRepo, open_database


def _user(text: str) -> UserMessage:
    return UserMessage(content=text, timestamp=0)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)], api="faux", provider="faux", model="faux"
    )


# ============================================================
# database / migration
# ============================================================


def test_database_creates_tables(tmp_path):
    """open_database 执行 migration，所有表存在。"""
    db = open_database(str(tmp_path / "test.db"))
    tables = {r["name"] for r in db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {
        "sessions",
        "session_entries",
        "session_sequences",
        "branch_entries",
        "session_materialized",
        "entry_materialized",
        "migrations",
    }
    assert expected.issubset(tables), f"缺失表: {expected - tables}"
    db.close()


def test_migration_idempotent(tmp_path):
    """重复 open 不报错（migration 已应用）。"""
    path = str(tmp_path / "test.db")
    db1 = open_database(path)
    db1.close()
    db2 = open_database(path)
    db2.close()


def test_pragma_configured(tmp_path):
    """PRAGMA 正确设置。"""
    db = open_database(str(tmp_path / "test.db"))
    assert db.query_one("PRAGMA journal_mode")["journal_mode"] == "wal"
    db.close()


# ============================================================
# Repo CRUD
# ============================================================


def test_repo_create_and_open(tmp_path):
    """创建 session → open → 可用。"""
    repo = SqliteSessionRepo(str(tmp_path / "test.db"))
    sid, storage = repo.create(cwd="/project")
    assert sid

    # open 同一个
    storage2 = repo.open(sid)
    assert storage2.get_metadata() == {}


def test_repo_list(tmp_path):
    """创建多个 session → list。"""
    repo = SqliteSessionRepo(str(tmp_path / "test.db"))
    sid1, _ = repo.create(cwd="/a")
    sid2, _ = repo.create(cwd="/b")
    sid3, _ = repo.create(cwd="/a")

    all_sessions = repo.list()
    assert len(all_sessions) == 3

    a_sessions = repo.list(cwd="/a")
    assert len(a_sessions) == 2
    assert all(s["cwd"] == "/a" for s in a_sessions)


def test_repo_delete(tmp_path):
    """删除 session → list 为空。"""
    repo = SqliteSessionRepo(str(tmp_path / "test.db"))
    sid, _ = repo.create(cwd="/x")
    assert len(repo.list()) == 1

    repo.delete(sid)
    assert len(repo.list()) == 0


def test_repo_delete_not_found(tmp_path):
    """删除不存在的 session 报错。"""
    repo = SqliteSessionRepo(str(tmp_path / "test.db"))
    with pytest.raises(KeyError):
        repo.delete("nonexistent")


def test_repo_open_not_found(tmp_path):
    """open 不存在的 session 报错。"""
    repo = SqliteSessionRepo(str(tmp_path / "test.db"))
    repo.create(cwd="/x")  # 建一个，确保 db 有数据
    with pytest.raises(KeyError):
        repo.open("nonexistent")


# ============================================================
# Entry 追加与查询
# ============================================================


def test_append_and_get_entries(tmp_path):
    """追加消息 → get_entries 返回。"""
    repo = SqliteSessionRepo(str(tmp_path / "test.db"))
    sid, storage = repo.create(cwd="/x")

    storage.append_entry(
        SessionEntry(
            id=storage.create_entry_id(),
            parent_id=None,
            timestamp=storage.create_timestamp(),
            type="message",
            data=_user("hello"),
        )
    )
    storage.append_entry(
        SessionEntry(
            id=storage.create_entry_id(),
            parent_id=storage.get_leaf_id(),
            timestamp=storage.create_timestamp(),
            type="message",
            data=_assistant("world"),
        )
    )

    entries = storage.get_entries()
    assert len(entries) == 2
    assert entries[0].type == "message"
    assert entries[1].type == "message"


def test_entry_seq_monotonic(tmp_path):
    """entry_seq 单调递增。"""
    repo = SqliteSessionRepo(str(tmp_path / "test.db"))
    sid, storage = repo.create(cwd="/x")

    for i in range(5):
        storage.append_entry(
            SessionEntry(
                id=storage.create_entry_id(),
                parent_id=storage.get_leaf_id(),
                timestamp=storage.create_timestamp(),
                type="message",
                data=_user(f"msg {i}"),
            )
        )

    entries = storage.get_entries()
    # 全部能查到
    assert len(entries) == 5


def test_leaf_id_updates(tmp_path):
    """追加后 leaf_id 更新为最新 entry。"""
    repo = SqliteSessionRepo(str(tmp_path / "test.db"))
    sid, storage = repo.create(cwd="/x")
    assert storage.get_leaf_id() is None

    eid = storage.create_entry_id()
    storage.append_entry(
        SessionEntry(
            id=eid,
            parent_id=None,
            timestamp=storage.create_timestamp(),
            type="message",
            data=_user("test"),
        )
    )
    assert storage.get_leaf_id() == eid


def test_persistence_across_reopen(tmp_path):
    """关闭后重新 open，数据保持。"""
    path = str(tmp_path / "test.db")
    repo = SqliteSessionRepo(path)
    sid, storage = repo.create(cwd="/x")
    storage.append_entry(
        SessionEntry(
            id="e1",
            parent_id=None,
            timestamp=storage.create_timestamp(),
            type="message",
            data=_user("persisted"),
        )
    )
    repo.close()

    # 重新打开
    repo2 = SqliteSessionRepo(path)
    storage2 = repo2.open(sid)
    entries = storage2.get_entries()
    assert len(entries) == 1
    assert isinstance(entries[0].data, UserMessage)
    assert "persisted" in str(entries[0].data.content)
    repo2.close()


# ============================================================
# Session 集成（pi-agent-core 的 Session 使用 SQLite 后端）
# ============================================================


def test_session_with_sqlite_backend(tmp_path):
    """Session + SqliteSessionStorage：完整对话流程。"""
    repo = SqliteSessionRepo(str(tmp_path / "test.db"))
    sid, storage = repo.create(cwd="/x")
    session = Session(storage)

    session.append_message(_user("hello"))
    session.append_message(_assistant("hi there"))
    session.append_message(_user("bye"))

    ctx = session.build_context()
    assert len(ctx) == 3
    assert isinstance(ctx[0], UserMessage)
    assert isinstance(ctx[1], AssistantMessage)


def test_session_sqlite_persistence(tmp_path):
    """Session 对话保存后，重新打开能恢复 build_context。"""
    path = str(tmp_path / "test.db")
    repo = SqliteSessionRepo(path)
    sid, storage = repo.create(cwd="/x")
    session = Session(storage)
    session.append_message(_user("saved question"))
    session.append_message(_assistant("saved answer"))
    repo.close()

    # 重新打开
    repo2 = SqliteSessionRepo(path)
    storage2 = repo2.open(sid)
    session2 = Session(storage2)
    ctx = session2.build_context()
    assert len(ctx) == 2
    assert "saved question" in str(ctx[0].content)
    assert "saved answer" in str(ctx[1].content[0].text)
    repo2.close()


def test_session_sqlite_compaction(tmp_path):
    """compaction 条目在 SQLite 中持久化。"""
    repo = SqliteSessionRepo(str(tmp_path / "test.db"))
    sid, storage = repo.create(cwd="/x")
    session = Session(storage)
    session.append_message(_user("old1"))
    session.append_message(_assistant("old2"))
    session.append_compaction(summary="之前的讨论", retained_tail=[_user("recent")])
    session.append_message(_assistant("new"))

    ctx = session.build_context()
    # summary + retained + new = 3
    assert len(ctx) == 3
    assert "之前的讨论" in str(ctx[0].content)


def test_metadata_persistence(tmp_path):
    """metadata 持久化。"""
    repo = SqliteSessionRepo(str(tmp_path / "test.db"))
    sid, storage = repo.create(cwd="/x", metadata={"name": "my-session", "custom": 42})

    repo2 = SqliteSessionRepo(str(tmp_path / "test.db"))
    storage2 = repo2.open(sid)
    meta = storage2.get_metadata()
    assert meta["name"] == "my-session"
    assert meta["custom"] == 42


def test_multiple_sessions_in_one_db(tmp_path):
    """一个 SQLite 文件存多个 session。"""
    repo = SqliteSessionRepo(str(tmp_path / "test.db"))
    sid1, st1 = repo.create(cwd="/a")
    sid2, st2 = repo.create(cwd="/b")

    Session(st1).append_message(_user("session 1 msg"))
    Session(st2).append_message(_user("session 2 msg"))

    # 各自独立
    e1 = repo.open(sid1).get_entries()
    e2 = repo.open(sid2).get_entries()
    assert len(e1) == 1
    assert len(e2) == 1
    assert "session 1" in str(e1[0].data.content)
    assert "session 2" in str(e2[0].data.content)
