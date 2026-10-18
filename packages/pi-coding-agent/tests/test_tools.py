"""编码工具集测试：每个工具用真实文件系统验证。"""

from __future__ import annotations

import re

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
    format_read_line_range,
)
from pi_coding_agent.truncate import (
    truncate_head,
    truncate_line,
    truncate_middle,
    truncate_tail,
)

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


# ============================================================
# v0.85.1: 信号杀死的进程映射为 128+signal 退出码
# ============================================================


async def test_bash_signal_exit_code_mapped():
    """SIGKILL 杀死的命令映射为 137（shell 惯例 128+信号号），非零退出。"""
    tools = create_coding_tools()
    bash = next(t for t in tools if t.name == "bash")
    result = await bash.execute("t1", {"command": "kill -9 $$"})
    assert result.details["exit_code"] == 137
    assert "exited with code 137" in result.content[0].text


# ============================================================
# Tier-A 对齐上游 v1.3.0
# ============================================================


# ---- A1: 内置工具默认开启 strict-prefer 受约束采样 ----


def test_a1_constrained_sampling_default_on():
    """read/bash/edit/write 默认开启；上游对 grep/find/ls 未声明该属性。"""
    for cls in (ReadTool, BashTool, EditTool, WriteTool):
        assert cls.constrained_sampling == {"type": "json_schema", "strict": "prefer"}, cls.__name__


def test_a1_constrained_sampling_not_set_for_other_tools():
    for cls in (GrepTool, FindTool, LsTool):
        assert not hasattr(cls, "constrained_sampling"), cls.__name__


def test_a1_each_tool_gets_its_own_config_dict():
    """共享同一个 dict 会被 provider 侧的改动串味。"""
    assert ReadTool.constrained_sampling is not BashTool.constrained_sampling


# ---- A3: truncate_middle ----


def test_a3_truncate_middle_no_truncation():
    result = truncate_middle("short", 100)
    assert not result.truncated
    assert result.content == "short"
    assert result.removed_chars == 0
    assert result.total_bytes == 5
    assert result.total_lines == 1


def test_a3_truncate_middle_keeps_both_ends():
    content = "".join(f"{i:03d}" for i in range(100))  # 300 字节
    result = truncate_middle(content, 60)
    assert result.truncated
    assert result.total_bytes == 300
    assert result.content.startswith(content[:30])
    assert result.content.endswith(content[-30:])
    assert "chars truncated" in result.content
    assert result.removed_chars == 240


def test_a3_truncate_middle_respects_utf8_boundaries():
    """多字节字符不能被切成半个，否则解码即损坏。"""
    content = "中文" * 100  # 200 字符 / 600 字节
    result = truncate_middle(content, 61)
    assert result.truncated
    marker = re.search(r"…\d+ chars truncated…", result.content)
    assert marker, result.content
    kept = result.content.replace(marker.group(0), "")
    # 去掉标记后剩下的必须是完整的「中」「文」序列，且没有替换字符
    assert set(kept) == set("中文")
    assert "\ufffd" not in result.content
    assert result.removed_chars == 200 - len(kept)


def test_a3_truncate_middle_counts_lines_like_upstream():
    """对齐上游 splitLinesForCounting：末尾换行不算新行，空串 0 行。"""
    assert truncate_middle("", 10).total_lines == 0
    assert truncate_middle("a\nb\n", 10).total_lines == 2
    assert truncate_middle("a\nb", 10).total_lines == 2


# ---- A4: bash 退出码语义 + 结构化输出 ----


async def test_a4_nonzero_exit_is_error_result():
    """v1.3.0：非零退出码是 is_error 结果（不再靠抛异常），details 仍保留。"""
    tool = BashTool(cwd="/tmp")
    result = await tool.execute("id", {"command": "exit 42"})
    assert result.is_error is True
    assert result.details["exit_code"] == 42
    assert "exited with code 42" in result.content[0].text


async def test_a4_success_is_not_error():
    tool = BashTool(cwd="/tmp")
    result = await tool.execute("id", {"command": "echo ok"})
    assert result.is_error is False
    assert result.details["exit_code"] == 0
    assert "ok" in result.content[0].text


async def test_a4_structured_content_shape():
    tool = BashTool(cwd="/tmp")
    result = await tool.execute("id", {"command": "echo structured"})
    structured = result.structured_content
    assert structured["output"] == "structured\n"
    assert structured["truncated"] is False
    assert structured["exit_code"] == 0
    assert isinstance(structured["wall_time_seconds"], float)
    # 未截断时不带 full_output_path（对齐上游 `...(truncated && path ? {...} : {})`）
    assert "full_output_path" not in structured


async def test_a4_empty_output_failure_has_no_leading_blank_lines():
    """对齐上游 appendStatus：输出为空时状态行不带前导空行。"""
    tool = BashTool(cwd="/tmp")
    result = await tool.execute("id", {"command": "exit 3"})
    assert result.content[0].text == "Command exited with code 3"


async def test_a4_structured_output_is_bounded():
    """结构化输出受 1MiB 上限约束（而不是模型侧的 2000 行 / 50KB）。"""
    from pi_coding_agent import STRUCTURED_OUTPUT_MAX_BYTES
    from pi_coding_agent.tools import _bounded_output

    assert STRUCTURED_OUTPUT_MAX_BYTES == 1024 * 1024

    content = "a" * 100 + "b" * 100
    bounded, truncated = _bounded_output(content, 40)
    assert truncated
    assert bounded.startswith("a" * 20)
    assert bounded.endswith("b" * 20)
    assert "[... 160 bytes omitted ...]" in bounded

    # 未超限时原样返回
    same, flag = _bounded_output("小", 100)
    assert same == "小" and flag is False


def test_a4_bounded_output_keeps_utf8_intact():
    from pi_coding_agent.tools import _bounded_output

    bounded, truncated = _bounded_output("中" * 100, 31)
    assert truncated
    assert "�" not in bounded
    assert "bytes omitted" in bounded


# ---- A5': read 结构化输出（resize 档案因 pi-ai 无 inputLimits 而跳过） ----


async def test_a5_read_text_structured_content(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\nworld\n", encoding="utf-8")
    tool = ReadTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"path": "a.txt"})
    # 无图片块时结构化结果就是模型看到的文本
    assert result.structured_content == result.content[0].text
    assert tool.output_schema is not None


async def test_a5_read_image_structured_content(tmp_path):
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    (tmp_path / "px.png").write_bytes(png)
    tool = ReadTool(cwd=str(tmp_path))
    result = await tool.execute("id", {"path": "px.png"})
    structured = result.structured_content
    assert structured["type"] == "image"
    assert structured["mimeType"] == "image/png"
    assert structured["data"] == base64.b64encode(png).decode("ascii")
    assert structured["note"] == "Image: px.png"


def test_a5_output_schema_none_for_tools_upstream_does_not_declare():
    for cls in (EditTool, WriteTool, GrepTool, FindTool, LsTool):
        assert cls.output_schema is None, cls.__name__


# ---- A6: format_read_line_range ----


def test_a6_format_read_line_range():
    assert format_read_line_range(None, None) == ""
    assert format_read_line_range(10, None) == ":10"
    assert format_read_line_range(10, 5) == ":10-14"
    assert format_read_line_range(None, 5) == ":1-5"


def test_a6_accepts_numeric_strings():
    """上游 #9887：模型把数字参数发成字符串时标签仍要正确。"""
    assert format_read_line_range("10", "5") == ":10-14"
    assert format_read_line_range("10", None) == ":10"


def test_a6_rejects_malformed_values():
    assert format_read_line_range("abc", 5) == ""
    assert format_read_line_range(object(), 5) == ""
    assert format_read_line_range(10, "abc") == ""
    assert format_read_line_range(True, 5) == ""
    assert format_read_line_range(float("nan"), 5) == ""
    assert format_read_line_range("  ", 5) == ""


def test_a6_omits_zero_end_like_upstream():
    """对齐上游 `end ? ... : ...`：end 为 0 时省略区间（limit=1 得到 `:1-1`）。"""
    assert format_read_line_range(1, 0) == ":1"
    assert format_read_line_range(1, 1) == ":1-1"
    assert format_read_line_range(1, 2) == ":1-2"
