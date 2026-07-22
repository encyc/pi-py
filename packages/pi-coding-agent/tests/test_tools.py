"""编码工具集测试：每个工具用真实文件系统验证。"""

from __future__ import annotations

from pi_coding_agent import (
    BashTool,
    EditTool,
    FindTool,
    GrepTool,
    LsTool,
    ReadTool,
    WriteTool,
    create_all_tools,
    create_coding_tools,
    create_read_only_tools,
)
from pi_coding_agent.truncate import truncate_head, truncate_line, truncate_tail

# ============================================================
# 工具集工厂
# ============================================================


def test_create_coding_tools():
    tools = create_coding_tools("/tmp")
    names = {t.name for t in tools}
    assert names == {"read", "bash", "edit", "write"}


def test_create_read_only_tools():
    tools = create_read_only_tools("/tmp")
    names = {t.name for t in tools}
    assert names == {"read", "grep", "find", "ls"}


def test_create_all_tools():
    tools = create_all_tools("/tmp")
    names = {t.name for t in tools}
    assert names == {"read", "bash", "edit", "write", "grep", "find", "ls"}


# ============================================================
# bash
# ============================================================


async def test_bash_echo():
    tool = BashTool(cwd="/tmp")
    result = await tool.execute("id", {"command": "echo hello world"})
    assert len(result.content) == 1
    assert "hello world" in result.content[0].text
    assert result.details["exit_code"] == 0


async def test_bash_failure():
    tool = BashTool(cwd="/tmp")
    result = await tool.execute("id", {"command": "exit 42"})
    assert "exited with code 42" in result.content[0].text


async def test_bash_cwd():
    """bash 在指定 cwd 执行。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tool = BashTool(cwd=d)
        result = await tool.execute("id", {"command": "pwd"})
        assert d in result.content[0].text


# ============================================================
# read
# ============================================================


async def test_read_text_file(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("line1\nline2\nline3\n")
    tool = ReadTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"path": "test.txt"})
    assert "line1" in result.content[0].text
    assert "line2" in result.content[0].text


async def test_read_with_offset_limit(tmp_path):
    f = tmp_path / "nums.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 21)))
    tool = ReadTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"path": "nums.txt", "offset": 5, "limit": 3})
    text = result.content[0].text
    assert "line5" in text
    assert "line7" in text
    assert "line4" not in text
    assert "line8" not in text


async def test_read_not_found(tmp_path):
    tool = ReadTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"path": "nonexistent.txt"})
    assert "not found" in result.content[0].text.lower()


async def test_read_offset_beyond_end(tmp_path):
    f = tmp_path / "short.txt"
    f.write_text("only one line")
    tool = ReadTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"path": "short.txt", "offset": 100})
    assert "beyond end" in result.content[0].text


# ============================================================
# edit
# ============================================================


async def test_edit_replace(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("def hello():\n    print('hello')\n")
    tool = EditTool(cwd=str(tmp_path))
    result = await tool.execute(
        "id",
        {
            "path": "code.py",
            "edits": [{"oldText": "print('hello')", "newText": "print('world')"}],
        },
    )
    assert "Successfully replaced" in result.content[0].text
    assert "print('world')" in f.read_text()


async def test_edit_multiple(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("a = 1\nb = 2\nc = 3\n")
    tool = EditTool(cwd=str(tmp_path))
    result = await tool.execute(
        "id",
        {
            "path": "code.py",
            "edits": [
                {"oldText": "a = 1", "newText": "a = 10"},
                {"oldText": "c = 3", "newText": "c = 30"},
            ],
        },
    )
    assert "2 block" in result.content[0].text
    content = f.read_text()
    assert "a = 10" in content
    assert "c = 30" in content


async def test_edit_not_found(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("hello")
    tool = EditTool(cwd=str(tmp_path))
    result = await tool.execute(
        "id",
        {"path": "code.py", "edits": [{"oldText": "nonexistent", "newText": "x"}]},
    )
    assert "not found" in result.content[0].text.lower()


async def test_edit_not_unique(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("x\nx\n")
    tool = EditTool(cwd=str(tmp_path))
    result = await tool.execute(
        "id",
        {"path": "code.py", "edits": [{"oldText": "x", "newText": "y"}]},
    )
    assert "matches 2 times" in result.content[0].text or "unique" in result.content[0].text.lower()


# ============================================================
# write
# ============================================================


async def test_write_new_file(tmp_path):
    tool = WriteTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"path": "new.txt", "content": "hello\n"})
    assert "Successfully wrote" in result.content[0].text
    assert (tmp_path / "new.txt").read_text() == "hello\n"


async def test_write_creates_parent_dirs(tmp_path):
    tool = WriteTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"path": "sub/dir/file.txt", "content": "nested"})
    assert "Successfully wrote" in result.content[0].text
    assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "nested"


async def test_write_overwrite(tmp_path):
    f = tmp_path / "exists.txt"
    f.write_text("old")
    tool = WriteTool(cwd=str(tmp_path))
    await tool.execute("id", {"path": "exists.txt", "content": "new"})
    assert f.read_text() == "new"


# ============================================================
# grep
# ============================================================


async def test_grep_basic(tmp_path):
    (tmp_path / "a.py").write_text("import os\nimport sys\n")
    (tmp_path / "b.py").write_text("print('hello')\n")
    tool = GrepTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"pattern": "import"})
    text = result.content[0].text
    assert "a.py" in text
    assert "import os" in text
    assert "import sys" in text
    assert "hello" not in text  # b.py 不匹配


async def test_grep_no_matches(tmp_path):
    (tmp_path / "a.py").write_text("hello")
    tool = GrepTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"pattern": "nonexistent_pattern_xyz"})
    assert "No matches" in result.content[0].text


async def test_grep_case_insensitive(tmp_path):
    (tmp_path / "a.txt").write_text("Hello World\n")
    tool = GrepTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"pattern": "hello", "ignoreCase": True})
    assert "Hello World" in result.content[0].text


async def test_grep_glob_filter(tmp_path):
    (tmp_path / "a.py").write_text("target line\n")
    (tmp_path / "b.txt").write_text("target line\n")
    tool = GrepTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"pattern": "target", "glob": "*.py"})
    text = result.content[0].text
    assert "a.py" in text
    assert "b.txt" not in text


# ============================================================
# find
# ============================================================


async def test_find_basic(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.txt").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("")
    tool = FindTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"pattern": "*.py"})
    text = result.content[0].text
    assert "a.py" in text
    assert "c.py" in text
    assert "b.txt" not in text


async def test_find_no_matches(tmp_path):
    tool = FindTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"pattern": "*.nonexistent"})
    assert "No files" in result.content[0].text


async def test_find_recursive(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.json").write_text("{}")
    tool = FindTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"pattern": "*.json"})
    assert "deep.json" in result.content[0].text


# ============================================================
# ls
# ============================================================


async def test_ls_basic(tmp_path):
    (tmp_path / "file1.txt").write_text("")
    (tmp_path / "file2.py").write_text("")
    (tmp_path / "subdir").mkdir()
    tool = LsTool(cwd=str(tmp_path))
    result = await tool.execute("id", {})
    text = result.content[0].text
    assert "file1.txt" in text
    assert "file2.py" in text
    assert "subdir/" in text  # 目录加 trailing slash


async def test_ls_empty_dir(tmp_path):
    tool = LsTool(cwd=str(tmp_path))
    result = await tool.execute("id", {})
    assert "empty" in result.content[0].text.lower()


async def test_ls_not_dir(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    tool = LsTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"path": "file.txt"})
    assert "Not a directory" in result.content[0].text


# ============================================================
# truncate
# ============================================================


def test_truncate_head_no_truncation():
    result = truncate_head("short content")
    assert not result.truncated
    assert result.content == "short content"


def test_truncate_head_by_lines():
    content = "\n".join(f"line{i}" for i in range(100))
    result = truncate_head(content, max_lines=10)
    assert result.truncated
    assert result.output_lines <= 10
    assert "line0" in result.content


def test_truncate_tail_keeps_end():
    content = "\n".join(f"line{i}" for i in range(100))
    result = truncate_tail(content, max_lines=10)
    assert result.truncated
    # 尾部保留 → line99 应在
    assert "line99" in result.content
    assert "line0" not in result.content


def test_truncate_line():
    short, trunc = truncate_line("short", 500)
    assert not trunc
    long_str = "x" * 600
    result, trunc = truncate_line(long_str, 500)
    assert trunc
    assert "[truncated]" in result
