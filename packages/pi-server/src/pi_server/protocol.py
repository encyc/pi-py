"""RPC 协议定义。

对应上游 ``packages/server/src/ipc/protocol.ts`` + ``types.ts``。

通信格式：JSONL over Unix socket（每行一个 JSON 对象，``\\n`` 分隔）。

两层协议：
- **上层**（server 控制）：spawn / list / stop / status / rpc / rpc_stream
- **下层**（agent RPC）：prompt / get_state 等，透传给 CodingAgent

与上游的关键偏离：上游 spawn 子进程（pi --mode rpc），Python 版
supervisor 直接在进程内管理 ``CodingAgent`` 实例（无子进程）。
"""

from __future__ import annotations

import json
from typing import Any, Literal

# ============================================================
# 实例状态
# ============================================================

InstanceStatus = Literal["starting", "online", "stopping", "stopped", "error"]


# ============================================================
# 实例记录
# ============================================================


class InstanceSummary:
    """实例摘要（所有 list/status/spawn 返回的视图）。"""

    def __init__(
        self,
        id: str,
        status: InstanceStatus,
        cwd: str,
        label: str | None = None,
        created_at: str = "",
    ) -> None:
        self.id = id
        self.status = status
        self.cwd = cwd
        self.label = label
        self.created_at = created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "cwd": self.cwd,
            "label": self.label,
            "created_at": self.created_at,
        }


# ============================================================
# 上层请求 / 响应
# ============================================================

#: 上层请求类型。
ServerRequest = dict[str, Any]


def encode_message(msg: Any) -> bytes:
    """编码为 JSONL 行（带 \\n）。"""
    return (json.dumps(msg, ensure_ascii=False, default=_default_encoder) + "\n").encode("utf-8")


def _default_encoder(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True, mode="json")
    return str(obj)


def parse_line(line: str | bytes) -> dict[str, Any]:
    """解析一行 JSON。"""
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    result: dict[str, Any] = json.loads(line.strip())
    return result


# ============================================================
# 响应构造
# ============================================================


def ok_response(resp_type: str, **extra: Any) -> dict[str, Any]:
    return {"type": resp_type, "ok": True, **extra}


def error_response(error: str) -> dict[str, Any]:
    return {"type": "error", "ok": False, "error": error}


__all__ = [
    "InstanceStatus",
    "InstanceSummary",
    "ServerRequest",
    "encode_message",
    "parse_line",
    "ok_response",
    "error_response",
]
