"""JSON-RPC 判别与解析的单元规格（本端补充，上游无独立单测）。

上游 ``protocol/jsonrpc.ts`` 的判别函数只被 ``client.test.ts`` / ``streamable-http.test.ts``
间接覆盖；这里把边界（bad id、result/error 互斥、非对象）单独钉住。
"""

from __future__ import annotations

import pytest

from pi_mcp import (
    JSON_RPC_ERROR_CODES,
    McpError,
    is_json_rpc_notification,
    is_json_rpc_request,
    is_json_rpc_response,
    parse_json_rpc_message,
)


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": "abc", "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
    ],
)
def test_recognizes_requests(message: dict[str, object]) -> None:
    assert is_json_rpc_request(message)


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "ping"},  # 无 id → 通知而非请求
        {"jsonrpc": "2.0", "id": None, "method": "ping"},
        {"jsonrpc": "2.0", "id": float("inf"), "method": "ping"},
        {"jsonrpc": "2.0", "id": float("nan"), "method": "ping"},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},  # bool 不是合法 id
        {"jsonrpc": "2.0", "id": 1},
        "not a message",
        [1, 2],
    ],
)
def test_rejects_non_requests(message: object) -> None:
    assert not is_json_rpc_request(message)


def test_recognizes_notifications() -> None:
    assert is_json_rpc_notification({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert not is_json_rpc_notification({"jsonrpc": "2.0", "id": 1, "method": "ping"})


def test_result_and_error_are_mutually_exclusive() -> None:
    """``result`` 与 ``error`` 同时出现时不认为是响应。"""
    assert is_json_rpc_response({"jsonrpc": "2.0", "id": 1, "result": {}})
    assert is_json_rpc_response({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}})
    assert not is_json_rpc_response(
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": -1, "message": "x"}}
    )
    # error 的形状也要合法
    assert not is_json_rpc_response({"jsonrpc": "2.0", "id": 1, "error": {"code": "-1"}})
    assert not is_json_rpc_response({"jsonrpc": "2.0", "id": 1, "error": "boom"})


def test_parse_json_rpc_message_returns_input_unchanged() -> None:
    """解析只做校验，原样返回（未知键保留）。"""
    message = {"jsonrpc": "2.0", "id": 7, "method": "ping", "extra": {"kept": True}}
    assert parse_json_rpc_message(message) is message


def test_parse_json_rpc_message_raises_mcp_error() -> None:
    with pytest.raises(McpError) as excinfo:
        parse_json_rpc_message({"jsonrpc": "2.0"})
    assert excinfo.value.code == JSON_RPC_ERROR_CODES["invalidRequest"]
    assert str(excinfo.value) == "Invalid JSON-RPC message"
