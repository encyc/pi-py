"""JSON-RPC 2.0 消息、错误与判别函数。

对应上游 ``packages/mcp/src/protocol/jsonrpc.ts``（v1.3.0，113 行）。

上游的 ``interface`` 在 Python 侧用 ``TypedDict``（只描述已知键，未知键原样保留），
因为服务端返回的是原样 JSON 对象：这些类型只做校验，不做转换。判别函数用
``TypeGuard`` 声明，以便 mypy 在调用处收窄类型（对齐上游 ``value is X`` 的谓词签名）。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict, TypeGuard

#: JSON-RPC id（字符串或有限数值）。
JsonRpcId = str | int | float


class _JsonRpcRequestRequired(TypedDict):
    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    method: str


class JsonRpcRequest(_JsonRpcRequestRequired, total=False):
    """请求。"""

    params: Any


class _JsonRpcNotificationRequired(TypedDict):
    jsonrpc: Literal["2.0"]
    method: str


class JsonRpcNotification(_JsonRpcNotificationRequired, total=False):
    """通知（无 id）。"""

    params: Any


class _JsonRpcErrorObjectRequired(TypedDict):
    code: int
    message: str


class JsonRpcErrorObject(_JsonRpcErrorObjectRequired, total=False):
    """错误对象。"""

    data: Any


class JsonRpcSuccessResponse(TypedDict):
    """成功响应。"""

    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    result: Any


class JsonRpcErrorResponse(TypedDict):
    """错误响应。"""

    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    error: JsonRpcErrorObject


#: 响应联合。
JsonRpcResponse = JsonRpcSuccessResponse | JsonRpcErrorResponse

#: 任意 JSON-RPC 消息。
JsonRpcMessage = (
    JsonRpcRequest | JsonRpcNotification | JsonRpcSuccessResponse | JsonRpcErrorResponse
)

#: 标准错误码（对应上游 ``JSON_RPC_ERROR_CODES``）。
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

JSON_RPC_ERROR_CODES: dict[str, int] = {
    "parseError": PARSE_ERROR,
    "invalidRequest": INVALID_REQUEST,
    "methodNotFound": METHOD_NOT_FOUND,
    "invalidParams": INVALID_PARAMS,
    "internalError": INTERNAL_ERROR,
}


class McpError(Exception):
    """带 JSON-RPC 错误码的 MCP 错误。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.name = "McpError"
        self.code = code
        self.data = data


class McpConnectionClosedError(Exception):
    """连接已关闭（或不可用）。"""

    def __init__(self, message: str = "MCP connection closed") -> None:
        super().__init__(message)
        self.name = "McpConnectionClosedError"


class McpTimeoutError(Exception):
    """请求超时。"""

    def __init__(self, timeout_ms: int) -> None:
        super().__init__(f"MCP request timed out after {timeout_ms}ms")
        self.name = "McpTimeoutError"
        self.timeout_ms = timeout_ms


class McpAbortError(Exception):
    """请求被调用方取消。

    上游的 ``name`` 是 ``"AbortError"``（沿袭 DOM 命名），保持一致。
    """

    def __init__(self, message: str = "MCP request aborted") -> None:
        super().__init__(message)
        self.name = "AbortError"


def is_object(value: object) -> TypeGuard[dict[str, Any]]:
    """是否是 JSON 对象（排除数组与 None）。"""
    return isinstance(value, dict)


def to_error(value: object) -> Exception:
    """把任意抛出的值归一化成 ``Exception``。"""
    return value if isinstance(value, Exception) else Exception(str(value))


def is_json_rpc_id(value: object) -> TypeGuard[JsonRpcId]:
    """是否是合法 id。数值需有限（``nan``/``inf`` 不算），``bool`` 不算数值。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    return False


def is_json_rpc_request(message: object) -> TypeGuard[JsonRpcRequest]:
    """是否是请求。"""
    return (
        is_object(message)
        and message.get("jsonrpc") == "2.0"
        and is_json_rpc_id(message.get("id"))
        and isinstance(message.get("method"), str)
    )


def is_json_rpc_notification(message: object) -> TypeGuard[JsonRpcNotification]:
    """是否是通知（有 method、无 id）。"""
    return (
        is_object(message)
        and message.get("jsonrpc") == "2.0"
        and "id" not in message
        and isinstance(message.get("method"), str)
    )


def is_json_rpc_response(message: object) -> TypeGuard[JsonRpcResponse]:
    """是否是响应。``result`` 与 ``error`` 互斥。"""
    if (
        not is_object(message)
        or message.get("jsonrpc") != "2.0"
        or not is_json_rpc_id(message.get("id"))
    ):
        return False
    if "result" in message:
        return "error" not in message
    error = message.get("error")
    if "error" not in message or not is_object(error):
        return False
    return isinstance(error.get("code"), int) and isinstance(error.get("message"), str)


def parse_json_rpc_message(value: object) -> JsonRpcMessage:
    """校验并原样返回一条消息；不合法时抛 ``McpError``。"""
    if is_json_rpc_request(value) or is_json_rpc_notification(value) or is_json_rpc_response(value):
        return value
    raise McpError(INVALID_REQUEST, "Invalid JSON-RPC message")


__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JSON_RPC_ERROR_CODES",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "JsonRpcErrorObject",
    "JsonRpcErrorResponse",
    "JsonRpcId",
    "JsonRpcMessage",
    "JsonRpcNotification",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "JsonRpcSuccessResponse",
    "McpAbortError",
    "McpConnectionClosedError",
    "McpError",
    "McpTimeoutError",
    "is_json_rpc_id",
    "is_json_rpc_notification",
    "is_json_rpc_request",
    "is_json_rpc_response",
    "is_object",
    "parse_json_rpc_message",
    "to_error",
]
