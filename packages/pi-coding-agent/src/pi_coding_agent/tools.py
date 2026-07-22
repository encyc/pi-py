"""编码工具集：bash / read / edit / write / grep / find / ls。

对应上游 ``packages/coding-agent/src/core/tools/``。

每个工具实现 ``AgentTool`` 协议（来自 pi-agent-core）。
``create_coding_tools`` 返回默认工具集 [read, bash, edit, write]。
``create_read_only_tools`` 返回 [read, grep, find, ls]。
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from pi_agent_core.types import AgentTool, AgentToolResult, ToolExecutionMode
from pi_ai import ImageContent, TextContent

from .truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    format_size,
    truncate_head,
    truncate_line,
    truncate_tail,
)

# ============================================================
# 路径解析
# ============================================================


def resolve_to_cwd(path: str, cwd: str) -> str:
    """相对路径解析成绝对路径（相对 cwd）。~ 展开。"""
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(cwd, expanded))


# ============================================================
# bash 工具
# ============================================================

_BASH_DESC = (
    "Execute a bash command in the current working directory. "
    "Returns stdout and stderr. Output is truncated to last "
    f"{DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB "
    "(whichever is hit first). If truncated, full output is saved to a temp file. "
    "Optionally provide a timeout in seconds."
)


class BashTool:
    """执行 bash 命令。"""

    name = "bash"
    label = "bash"
    execution_mode: ToolExecutionMode | None = "sequential"
    description = _BASH_DESC
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Bash command to execute"},
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds (optional, no default timeout)",
            },
        },
        "required": ["command"],
    }

    def __init__(self, cwd: str = ".", command_prefix: str = "") -> None:
        self._cwd = cwd
        self._command_prefix = command_prefix

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        cancel_event: asyncio.Event | None = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        command = params["command"]
        timeout = params.get("timeout")
        if self._command_prefix:
            command = self._command_prefix + "\n" + command

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self._cwd,
                start_new_session=True,
            )
        except Exception as e:  # noqa: BLE001
            return AgentToolResult(
                content=[TextContent(text=f"Failed to start command: {e}")],
                details={"error": str(e)},
            )

        # 等待完成（带超时和取消）
        try:
            stdout_data, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout if timeout else None,
            )
        except TimeoutError:
            # 杀进程树
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                pass
            return AgentToolResult(
                content=[TextContent(text=f"Command timed out after {timeout} seconds")],
                details={"timeout": timeout},
            )
        except asyncio.CancelledError:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                pass
            raise

        output = stdout_data.decode("utf-8", errors="replace") if stdout_data else ""
        exit_code = proc.returncode

        # 截断（尾部保留）
        trunc = truncate_tail(output)
        result_text = trunc.content
        if trunc.truncated:
            # 保存全文到临时文件
            tmp = (
                Path(tempfile.gettempdir())
                / f"pi-bash-{os.getpid()}-{int(time.time()) % 100000}.log"
            )
            tmp.write_text(output, encoding="utf-8")
            if trunc.truncated_by == "lines":
                result_text += (
                    f"\n\n[Showing lines {trunc.total_lines - trunc.output_lines + 1}-"
                    f"{trunc.total_lines} of {trunc.total_lines}. Full output: {tmp}]"
                )
            else:
                result_text += (
                    f"\n\n[Output truncated to {format_size(trunc.output_bytes)} "
                    f"of {format_size(trunc.total_bytes)}. Full output: {tmp}]"
                )

        # 非零退出码
        if exit_code is not None and exit_code != 0:
            result_text += f"\n\nCommand exited with code {exit_code}"

        return AgentToolResult(
            content=[TextContent(text=result_text)],
            details={"exit_code": exit_code, "truncated": trunc.truncated},
        )


# ============================================================
# read 工具
# ============================================================

_READ_DESC = (
    "Read the contents of a file. Supports text files and images "
    "(jpg, png, gif, webp, bmp). Images are sent as attachments. "
    "For text files, output is truncated to "
    f"{DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB "
    "(whichever is hit first). Use offset/limit for large files."
)

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


class ReadTool:
    """读取文件内容。"""

    name = "read"
    label = "read"
    execution_mode: ToolExecutionMode | None = None
    description = _READ_DESC
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read"},
            "offset": {
                "type": "number",
                "description": "Line number to start reading from (1-indexed)",
            },
            "limit": {"type": "number", "description": "Maximum number of lines to read"},
        },
        "required": ["path"],
    }

    def __init__(self, cwd: str = ".") -> None:
        self._cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        cancel_event: asyncio.Event | None = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        path = resolve_to_cwd(params["path"], self._cwd)
        offset = params.get("offset")
        limit = params.get("limit")

        if not os.path.exists(path):
            return AgentToolResult(
                content=[TextContent(text=f"File not found: {params['path']}")],
                details={"error": "not_found"},
            )

        ext = os.path.splitext(path)[1].lower()

        # 图像分支
        if ext in _IMAGE_EXTENSIONS:
            with open(path, "rb") as f:
                data = f.read()
            import base64

            return AgentToolResult(
                content=[
                    TextContent(text=f"Image: {params['path']}"),
                    ImageContent(
                        data=base64.b64encode(data).decode("ascii"),
                        mime_type=f"image/{ext[1:]}" if ext != ".jpg" else "image/jpeg",
                    ),
                ],
                details={"path": path, "image": True},
            )

        # 文本分支
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()

        all_lines = text.split("\n")
        total_lines = len(all_lines)
        start = max(0, (offset - 1) if offset else 0)

        if offset and offset >= total_lines:
            return AgentToolResult(
                content=[
                    TextContent(
                        text=f"Offset {offset} is beyond end of file ({total_lines} lines total)"
                    )
                ],
                details={"error": "offset_exceeds"},
            )

        if limit is not None:
            end = min(start + int(limit), total_lines)
            selected = "\n".join(all_lines[start:end])
        else:
            selected = "\n".join(all_lines[start:])

        trunc = truncate_head(selected)
        result_text = trunc.content

        if trunc.first_line_exceeds_limit:
            start_display = start + 1
            result_text = (
                f"Line {start_display} is {format_size(trunc.total_bytes)}, exceeds "
                f"{DEFAULT_MAX_BYTES // 1024}KB limit. "
                f"Use bash: sed -n '{start_display}p' {params['path']} | head -c {DEFAULT_MAX_BYTES}"
            )
        elif trunc.truncated:
            next_offset = start + trunc.output_lines + 1
            result_text += f"\n\n[Showing lines {start + 1}-{start + trunc.output_lines} of {total_lines}. Use offset={next_offset} to continue.]"
        elif limit is not None and end < total_lines:
            next_offset = end + 1
            result_text += f"\n\n[{total_lines - end} more lines in file. Use offset={next_offset} to continue.]"

        return AgentToolResult(
            content=[TextContent(text=result_text)],
            details={"path": path, "truncated": trunc.truncated},
        )


# ============================================================
# edit 工具
# ============================================================

_EDIT_DESC = (
    "Edit a single file using exact text replacement. Every edits[].oldText "
    "must match a unique, non-overlapping region of the original file. "
    "If two changes affect the same block or nearby lines, merge them into one edit."
)

# per-path 文件写入锁
_file_locks: dict[str, asyncio.Lock] = {}


def _get_file_lock(path: str) -> asyncio.Lock:
    if path not in _file_locks:
        _file_locks[path] = asyncio.Lock()
    return _file_locks[path]


class EditTool:
    """精确文本替换编辑文件。"""

    name = "edit"
    label = "edit"
    execution_mode: ToolExecutionMode | None = "sequential"
    description = _EDIT_DESC
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit"},
            "edits": {
                "type": "array",
                "description": "One or more targeted replacements.",
                "items": {
                    "type": "object",
                    "properties": {
                        "oldText": {"type": "string", "description": "Exact text to match"},
                        "newText": {"type": "string", "description": "Replacement text"},
                    },
                    "required": ["oldText", "newText"],
                },
            },
        },
        "required": ["path", "edits"],
    }

    def __init__(self, cwd: str = ".") -> None:
        self._cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        cancel_event: asyncio.Event | None = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        path = resolve_to_cwd(params["path"], self._cwd)
        edits = params["edits"]

        async with _get_file_lock(path):
            if not os.path.exists(path):
                return AgentToolResult(
                    content=[
                        TextContent(text=f"Could not edit file: {params['path']}. File not found.")
                    ],
                    details={"error": "not_found"},
                )

            with open(path, encoding="utf-8") as f:
                content = f.read()

            # BOM 处理
            has_bom = content.startswith("\ufeff")
            if has_bom:
                content = content[1:]

            new_content = content
            for edit in edits:
                old_text = edit["oldText"]
                new_text = edit["newText"]
                count = new_content.count(old_text)
                if count == 0:
                    return AgentToolResult(
                        content=[
                            TextContent(
                                text=f"Could not edit file: oldText not found in {params['path']}.\n\n"
                                f"Expected:\n{old_text[:200]}"
                            )
                        ],
                        details={"error": "not_found", "old_text": old_text[:200]},
                    )
                if count > 1:
                    return AgentToolResult(
                        content=[
                            TextContent(
                                text=f"Could not edit file: oldText matches {count} times in {params['path']}. "
                                "It must be unique."
                            )
                        ],
                        details={"error": "not_unique", "count": count},
                    )
                new_content = new_content.replace(old_text, new_text, 1)

            # 写回（恢复 BOM）
            with open(path, "w", encoding="utf-8") as f:
                if has_bom:
                    f.write("\ufeff")
                f.write(new_content)

        return AgentToolResult(
            content=[
                TextContent(
                    text=f"Successfully replaced {len(edits)} block(s) in {params['path']}."
                )
            ],
            details={"edits": len(edits)},
        )


# ============================================================
# write 工具
# ============================================================

_WRITE_DESC = (
    "Write content to a file. Creates the file if it doesn't exist, "
    "overwrites if it does. Automatically creates parent directories."
)


class WriteTool:
    """写入文件。"""

    name = "write"
    label = "write"
    execution_mode: ToolExecutionMode | None = "sequential"
    description = _WRITE_DESC
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write"},
            "content": {"type": "string", "description": "Content to write to the file"},
        },
        "required": ["path", "content"],
    }

    def __init__(self, cwd: str = ".") -> None:
        self._cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        cancel_event: asyncio.Event | None = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        path = resolve_to_cwd(params["path"], self._cwd)
        content = params["content"]

        async with _get_file_lock(path):
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

        return AgentToolResult(
            content=[
                TextContent(text=f"Successfully wrote {len(content)} bytes to {params['path']}.")
            ],
            details={},
        )


# ============================================================
# grep 工具（纯 Python 实现，不依赖 ripgrep）
# ============================================================

_GREP_DEFAULT_LIMIT = 100

_GREP_DESC = (
    "Search file contents for a pattern. Returns matching lines with file paths "
    "and line numbers. Respects .gitignore. Output is truncated to "
    f"{_GREP_DEFAULT_LIMIT} matches or {DEFAULT_MAX_BYTES // 1024}KB."
)

_GITIGNORE_CACHE: dict[str, list[str]] = {}


def _load_gitignore(dir_path: str) -> list[str]:
    """加载 .gitignore 模式（简单版，不递归父目录）。"""
    gi = os.path.join(dir_path, ".gitignore")
    if os.path.isfile(gi):
        with open(gi, encoding="utf-8", errors="replace") as f:
            return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    return []


def _is_ignored(rel_path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(os.path.basename(rel_path), pat):
            return True
    return False


class GrepTool:
    """搜索文件内容。"""

    name = "grep"
    label = "grep"
    execution_mode: ToolExecutionMode | None = None
    description = _GREP_DESC
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search pattern (regex or literal)"},
            "path": {"type": "string", "description": "Directory or file to search (default: cwd)"},
            "glob": {"type": "string", "description": "Filter files by glob pattern"},
            "ignoreCase": {
                "type": "boolean",
                "description": "Case-insensitive search (default: false)",
            },
            "literal": {
                "type": "boolean",
                "description": "Treat pattern as literal (default: false)",
            },
            "context": {
                "type": "number",
                "description": "Lines of context around each match (default: 0)",
            },
            "limit": {"type": "number", "description": "Max matches (default: 100)"},
        },
        "required": ["pattern"],
    }

    def __init__(self, cwd: str = ".") -> None:
        self._cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        cancel_event: asyncio.Event | None = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        pattern = params["pattern"]
        search_dir = resolve_to_cwd(params.get("path", "."), self._cwd)
        glob_pat = params.get("glob")
        ignore_case = params.get("ignoreCase", False)
        literal = params.get("literal", False)
        context = int(params.get("context", 0) or 0)
        limit = int(params.get("limit", _GREP_DEFAULT_LIMIT) or _GREP_DEFAULT_LIMIT)

        flags = re.IGNORECASE if ignore_case else 0
        if literal:
            pat = re.escape(pattern)
        else:
            pat = pattern
        try:
            regex = re.compile(pat, flags)
        except re.error as e:
            return AgentToolResult(
                content=[TextContent(text=f"Invalid regex pattern: {e}")],
                details={"error": str(e)},
            )

        # 收集文件
        if os.path.isfile(search_dir):
            files = [search_dir]
            base = os.path.dirname(search_dir)
        else:
            base = search_dir
            gi_patterns = _load_gitignore(search_dir)
            files = []
            for root, dirs, filenames in os.walk(search_dir):
                # 跳过 .git / node_modules / __pycache__
                dirs[:] = [
                    d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".venv")
                ]
                for fn in filenames:
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, search_dir)
                    if _is_ignored(rel, gi_patterns):
                        continue
                    if glob_pat and not fnmatch.fnmatch(rel, glob_pat):
                        continue
                    files.append(full)

        matches: list[str] = []
        match_count = 0
        limit_reached = False

        for filepath in sorted(files):
            if limit_reached:
                break
            try:
                with open(filepath, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError:
                continue

            rel = (
                os.path.relpath(filepath, base)
                if os.path.isdir(search_dir)
                else os.path.basename(filepath)
            )

            for i, line in enumerate(lines):
                if regex.search(line):
                    if match_count >= limit:
                        limit_reached = True
                        break
                    match_count += 1
                    line_text, _ = truncate_line(line.rstrip("\n"))
                    if context > 0:
                        start_ctx = max(0, i - context)
                        end_ctx = min(len(lines), i + context + 1)
                        for j in range(start_ctx, end_ctx):
                            sep = ":" if j == i else "-"
                            lt, _ = truncate_line(lines[j].rstrip("\n"))
                            matches.append(f"{rel}-{j + 1}{sep} {lt}")
                    else:
                        matches.append(f"{rel}:{i + 1}: {line_text}")

        if not matches:
            return AgentToolResult(
                content=[TextContent(text="No matches found.")],
                details={"count": 0},
            )

        raw = "\n".join(matches)
        trunc = truncate_head(raw, max_lines=10**9)
        result_text = trunc.content
        if limit_reached:
            result_text += f"\n\n[... matches truncated at {limit}]"
        if trunc.truncated:
            result_text += f"\n\n[... output truncated to {format_size(trunc.output_bytes)}]"

        return AgentToolResult(
            content=[TextContent(text=result_text)],
            details={"count": match_count, "limit_reached": limit_reached},
        )


# ============================================================
# find 工具
# ============================================================

_FIND_DEFAULT_LIMIT = 1000

_FIND_DESC = (
    "Search for files by glob pattern. Returns matching file paths relative to "
    "the search directory. Respects .gitignore. Output is truncated to "
    f"{_FIND_DEFAULT_LIMIT} results."
)


class FindTool:
    """按 glob 模式查找文件。"""

    name = "find"
    label = "find"
    execution_mode: ToolExecutionMode | None = None
    description = _FIND_DESC
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. '*.py' or '**/*.json'",
            },
            "path": {"type": "string", "description": "Directory to search (default: cwd)"},
            "limit": {"type": "number", "description": "Max results (default: 1000)"},
        },
        "required": ["pattern"],
    }

    def __init__(self, cwd: str = ".") -> None:
        self._cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        cancel_event: asyncio.Event | None = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        pattern = params["pattern"]
        search_dir = resolve_to_cwd(params.get("path", "."), self._cwd)
        limit = int(params.get("limit", _FIND_DEFAULT_LIMIT) or _FIND_DEFAULT_LIMIT)

        gi_patterns = _load_gitignore(search_dir) if os.path.isdir(search_dir) else []
        results: list[str] = []
        limit_reached = False

        for root, dirs, filenames in os.walk(search_dir):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".venv")]
            entries = dirs + filenames
            for entry in entries:
                rel = os.path.relpath(os.path.join(root, entry), search_dir)
                if _is_ignored(rel, gi_patterns):
                    continue
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(
                    os.path.basename(entry), pattern
                ):
                    if len(results) >= limit:
                        limit_reached = True
                        break
                    # 目录加 trailing slash
                    if os.path.isdir(os.path.join(root, entry)):
                        rel += "/"
                    results.append(rel)
            if limit_reached:
                break

        if not results:
            return AgentToolResult(
                content=[TextContent(text="No files found matching pattern.")],
                details={"count": 0},
            )

        results.sort()
        raw = "\n".join(results)
        result_text = raw
        if limit_reached:
            result_text += f"\n\n[... results truncated at {limit}]"

        return AgentToolResult(
            content=[TextContent(text=result_text)],
            details={"count": len(results), "limit_reached": limit_reached},
        )


# ============================================================
# ls 工具
# ============================================================

_LS_DEFAULT_LIMIT = 500

_LS_DESC = (
    "List directory contents. Returns entries sorted alphabetically, with '/' "
    "suffix for directories. Includes dotfiles. Output is truncated to "
    f"{_LS_DEFAULT_LIMIT} entries."
)


class LsTool:
    """列出目录内容。"""

    name = "ls"
    label = "ls"
    execution_mode: ToolExecutionMode | None = None
    description = _LS_DESC
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list (default: cwd)"},
            "limit": {"type": "number", "description": "Max entries (default: 500)"},
        },
        "required": [],
    }

    def __init__(self, cwd: str = ".") -> None:
        self._cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        cancel_event: asyncio.Event | None = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        dir_path = resolve_to_cwd(params.get("path", "."), self._cwd)
        limit = int(params.get("limit", _LS_DEFAULT_LIMIT) or _LS_DEFAULT_LIMIT)

        if not os.path.exists(dir_path):
            return AgentToolResult(
                content=[TextContent(text=f"Path not found: {params.get('path', '.')}")],
                details={"error": "not_found"},
            )
        if not os.path.isdir(dir_path):
            return AgentToolResult(
                content=[TextContent(text=f"Not a directory: {params.get('path', '.')}")],
                details={"error": "not_dir"},
            )

        entries = sorted(os.listdir(dir_path), key=str.lower)
        result_lines: list[str] = []
        limit_reached = False

        for entry in entries[:limit]:
            full = os.path.join(dir_path, entry)
            if os.path.isdir(full):
                result_lines.append(entry + "/")
            else:
                result_lines.append(entry)

        if len(entries) > limit:
            limit_reached = True

        if not result_lines:
            return AgentToolResult(
                content=[TextContent(text="(empty directory)")],
                details={"count": 0},
            )

        raw = "\n".join(result_lines)
        result_text = raw
        if limit_reached:
            result_text += (
                f"\n\n[{len(entries) - limit} more entries. Use limit={limit * 2} for more.]"
            )

        return AgentToolResult(
            content=[TextContent(text=result_text)],
            details={"count": len(entries), "limit_reached": limit_reached},
        )


# ============================================================
# 工具集工厂
# ============================================================


def create_coding_tools(cwd: str = ".") -> list[AgentTool]:
    """默认编码工具集：read, bash, edit, write。"""
    return [ReadTool(cwd), BashTool(cwd), EditTool(cwd), WriteTool(cwd)]


def create_read_only_tools(cwd: str = ".") -> list[AgentTool]:
    """只读工具集：read, grep, find, ls。"""
    return [ReadTool(cwd), GrepTool(cwd), FindTool(cwd), LsTool(cwd)]


def create_all_tools(cwd: str = ".") -> list[AgentTool]:
    """全部工具。"""
    return [
        ReadTool(cwd),
        BashTool(cwd),
        EditTool(cwd),
        WriteTool(cwd),
        GrepTool(cwd),
        FindTool(cwd),
        LsTool(cwd),
    ]


__all__ = [
    "BashTool",
    "ReadTool",
    "EditTool",
    "WriteTool",
    "GrepTool",
    "FindTool",
    "LsTool",
    "create_coding_tools",
    "create_read_only_tools",
    "create_all_tools",
]
