"""MCP server 通过 ``notifications/message`` 发来的日志。

对应上游 ``extensions/mcp/log.ts``（69 行，**完整移植**）。

日志追加到 agent 目录的 ``mcp.log``。多个 pi 进程可能写同一个文件，因此每条消息都是**一次同步
append**。文件超过 :data:`MAX_LOG_BYTES` 后轮转到 ``mcp.log.1``。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

__all__ = ["MAX_LOG_BYTES", "McpServerLog", "format_mcp_log_message"]

#: 轮转阈值（对齐上游 5 MiB）。
MAX_LOG_BYTES = 5 * 1024 * 1024


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _format_data(data: Any) -> str:
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(data)


def format_mcp_log_message(server: str, params: Any, now: datetime | None = None) -> str:
    """把一条来自 ``server`` 的 ``notifications/message`` 格式化成日志行；续行带缩进。"""
    message = params if _is_record(params) else {"data": params}
    level = message.get("level") if isinstance(message.get("level"), str) else "info"
    logger_raw = message.get("logger")
    logger = f" {logger_raw}:" if isinstance(logger_raw, str) and logger_raw else ""
    timestamp = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    text = _format_data(message.get("data")).replace("\r\n", "\n").replace("\n", "\n    ")
    return f"{timestamp} [{server}] {level}{logger} {text}\n"


class McpServerLog:
    """把各 server 的日志消息追加到同一个文件。写失败被忽略：日志不能拖垮工具。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._size: int | None = None

    def write(self, server: str, params: Any) -> None:
        line = format_mcp_log_message(server, params)
        try:
            if self._size is None:
                parent = os.path.dirname(self.path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                self._size = self._current_size()
            if self._size > MAX_LOG_BYTES:
                # 别的进程可能已经轮转过了；重命名前再确认一次。
                if self._current_size() > MAX_LOG_BYTES:
                    os.replace(self.path, f"{self.path}.1")
                self._size = self._current_size()
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
            self._size += len(line.encode("utf-8"))
        except OSError:
            # 忽略：日志是尽力而为。
            pass

    def _current_size(self) -> int:
        try:
            return os.stat(self.path).st_size
        except OSError:
            return 0
