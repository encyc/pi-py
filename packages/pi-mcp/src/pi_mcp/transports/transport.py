"""传输抽象与监听器簿记。

对应上游 ``packages/mcp/src/transports/transport.ts``（v1.3.0，64 行）。

上游用 ``AbortSignal`` 表示「调用方已不再等待答复」；Python 侧沿用本仓既有约定，
用 ``asyncio.Event``（``is_set()`` 即上游的 ``signal.aborted``）。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..protocol.jsonrpc import JsonRpcMessage, to_error

#: 单条消息的默认上限（16 MiB）。
DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024

#: 收到一条 JSON-RPC 消息。
McpTransportMessageListener = Callable[[JsonRpcMessage], None]
#: 传输层错误（只报告，不使在途请求失败）。
McpTransportErrorListener = Callable[[Exception], None]
#: 传输关闭。
McpTransportCloseListener = Callable[[], None]


@dataclass(frozen=True)
class McpTransportSendOptions:
    """``send()`` 的选项。

    ``signal`` 在发送方不再等待答复时（请求被取消、超时、已答复，或客户端关闭）被置位。
    传输在该信号之后**不得**再投递该消息（例如在授权刷新后重试时）；它不会撤销已经投递的消息。
    """

    signal: asyncio.Event | None = None


class McpTransport(Protocol):
    """传输契约。

    上游把 ``setProtocolVersion`` 声明为可选（``setProtocolVersion?``）；Python 侧在
    :class:`TransportEvents` 里给了一个空实现，于是协议里可以声明为必需 —— 具体传输要么
    覆写它，要么什么都不做。
    """

    async def start(self) -> None: ...

    async def send(
        self, message: JsonRpcMessage, options: McpTransportSendOptions | None = None
    ) -> None: ...

    async def close(self) -> None: ...

    def on_message(self, listener: McpTransportMessageListener) -> Callable[[], None]: ...

    def on_error(self, listener: McpTransportErrorListener) -> Callable[[], None]: ...

    def on_close(self, listener: McpTransportCloseListener) -> Callable[[], None]: ...

    def set_protocol_version(self, version: str) -> None: ...


class TransportEvents(ABC):
    """各传输共享的监听器簿记。``emit_close`` 每个传输最多触发一次。"""

    def __init__(self) -> None:
        self._message_listeners: list[McpTransportMessageListener] = []
        self._error_listeners: list[McpTransportErrorListener] = []
        self._close_listeners: list[McpTransportCloseListener] = []
        self._close_emitted = False

    def on_message(self, listener: McpTransportMessageListener) -> Callable[[], None]:
        """注册消息监听器，返回取消订阅函数。"""
        self._message_listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._message_listeners:
                self._message_listeners.remove(listener)

        return _unsubscribe

    def on_error(self, listener: McpTransportErrorListener) -> Callable[[], None]:
        """注册错误监听器，返回取消订阅函数。"""
        self._error_listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._error_listeners:
                self._error_listeners.remove(listener)

        return _unsubscribe

    def on_close(self, listener: McpTransportCloseListener) -> Callable[[], None]:
        """注册关闭监听器，返回取消订阅函数。"""
        self._close_listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._close_listeners:
                self._close_listeners.remove(listener)

        return _unsubscribe

    def set_protocol_version(self, version: str) -> None:  # noqa: B027 - 有意的空默认实现
        """记录协商出的协议版本。默认什么也不做（对齐上游的可选 ``setProtocolVersion``）。"""

    def emit_error(self, error: object) -> None:
        """上报一个错误（归一化成 ``Exception``）。

        上游把 ``emitError`` 声明为 ``protected``，但 ``InMemoryTransport`` 覆写为 public
        以便测试模拟传输层故障；Python 侧统一为 public（下划线前缀只用于「不对外」的约定）。
        """
        normalized = to_error(error)
        for listener in list(self._error_listeners):
            listener(normalized)

    def _emit_message(self, message: JsonRpcMessage) -> None:
        for listener in list(self._message_listeners):
            listener(message)

    def _emit_close(self) -> None:
        if self._close_emitted:
            return
        self._close_emitted = True
        for listener in list(self._close_listeners):
            listener()

    @abstractmethod
    async def start(self) -> None:
        """启动传输。"""

    @abstractmethod
    async def send(
        self, message: JsonRpcMessage, options: McpTransportSendOptions | None = None
    ) -> None:
        """发送一条消息。"""

    @abstractmethod
    async def close(self) -> None:
        """关闭传输。"""


def is_send_aborted(options: McpTransportSendOptions | None) -> bool:
    """``send()`` 的 signal 是否已置位。"""
    return options is not None and options.signal is not None and options.signal.is_set()


__all__ = [
    "DEFAULT_MAX_MESSAGE_BYTES",
    "McpTransport",
    "McpTransportCloseListener",
    "McpTransportErrorListener",
    "McpTransportMessageListener",
    "McpTransportSendOptions",
    "TransportEvents",
    "is_send_aborted",
]
